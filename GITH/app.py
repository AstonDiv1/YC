# -*- coding: utf-8 -*-
"""
app.py
------
Serveur Flask. Toute la logique est déléguée à logic.py : ce fichier ne
fait que router les requêtes HTTP et gérer la sécurité transversale
(sessions, rate limiting, CSRF, gestion d'erreurs, logging).

Variables d'environnement importantes :
    SECRET_KEY               clé de signature des sessions (obligatoire en prod)
    ADMIN_PASSWORD           mot de passe admin (ou ADMIN_PASSWORD_HASH)
    ADMIN_URL_SLUG           chemin secret de l'espace admin
    ADMIN_TOKEN              (optionnel) token d'API admin, ?token=…
    RGPD_RETENTION_DAYS      rétention en jours (défaut 365)
    RATE_LIMIT_LOGIN         "5/15m" — max tentatives login/IP/fenêtre
    RATE_LIMIT_SUBMIT        "20/1h" — max soumissions publiques/IP/fenêtre
    FORCE_HTTPS_COOKIES      "1" pour forcer SESSION_COOKIE_SECURE (prod)
    RESEND_API_KEY / ADMIN_EMAIL / RESEND_FROM   notifications e-mail
"""

import json
import logging
import mimetypes
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify, redirect, url_for,
    session, send_from_directory, abort, make_response,
)
from werkzeug.security import generate_password_hash, check_password_hash

import logic

# ---------------------------------------------------------------------------
# LOGGING (module standard, remplace les print())
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ycdigital.app")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 110 * 1024 * 1024

# ---------------------------------------------------------------------------
# SESSIONS ET COOKIES DURCIS
# ---------------------------------------------------------------------------
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
if not os.environ.get("SECRET_KEY"):
    logger.warning(
        "SECRET_KEY non définie — une clé aléatoire volatile est utilisée. "
        "Les sessions seront invalidées à chaque redémarrage. "
        "Définis SECRET_KEY en production."
    )

# En prod (HTTPS), passer FORCE_HTTPS_COOKIES=1 pour interdire tout envoi
# du cookie de session en clair.
_force_secure = os.environ.get("FORCE_HTTPS_COOKIES", "0") == "1"
app.config.update(
    SESSION_COOKIE_SECURE=_force_secure,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

# ---------------------------------------------------------------------------
# AUTHENTIFICATION ADMIN
# ---------------------------------------------------------------------------
_ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH")
if not _ADMIN_PASSWORD_HASH:
    _plain = os.environ.get("ADMIN_PASSWORD", "change-moi")
    _ADMIN_PASSWORD_HASH = generate_password_hash(_plain)
    if _plain == "change-moi":
        logger.warning(
            "Mot de passe admin par défaut ('change-moi') utilisé. "
            "Définis ADMIN_PASSWORD avant la mise en ligne."
        )

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

ADMIN_URL_SLUG = os.environ.get("ADMIN_URL_SLUG", "gestion-yc-4c9e2a8f7b").strip("/")
if not ADMIN_URL_SLUG:
    ADMIN_URL_SLUG = "gestion-yc-4c9e2a8f7b"
logger.info("Espace admin accessible sur /%s/connexion", ADMIN_URL_SLUG)


def _is_authorized() -> bool:
    if session.get("admin_logged_in"):
        return True
    token = request.args.get("token")
    return bool(ADMIN_TOKEN) and token == ADMIN_TOKEN


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _is_authorized():
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def api_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _is_authorized():
            return jsonify({"erreur": "Non autorisé."}), 401
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# RATE LIMITING — implémentation minimale en mémoire (par process)
#
# Suffisant pour un déploiement single-worker ou un petit multi-worker
# derrière un LB "sticky". Pour un vrai cluster, brancher Flask-Limiter
# avec Redis. On garde une API simple : rate_limit(key, max, per_seconds).
# ---------------------------------------------------------------------------

_rate_lock = threading.Lock()
_rate_hits: "dict[str, deque]" = defaultdict(deque)


def _parse_rate(spec: str, default_max: int, default_per: int) -> "tuple[int,int]":
    """Parse '5/15m' -> (5, 900). Unités : s, m, h."""
    try:
        left, right = spec.split("/", 1)
        n = int(left)
        right = right.strip().lower()
        unit = right[-1]
        val = int(right[:-1] or "1")
        mult = {"s": 1, "m": 60, "h": 3600}.get(unit, 60)
        return n, val * mult
    except Exception:
        return default_max, default_per


LOGIN_MAX, LOGIN_PER = _parse_rate(os.environ.get("RATE_LIMIT_LOGIN", "5/15m"), 5, 900)
SUBMIT_MAX, SUBMIT_PER = _parse_rate(os.environ.get("RATE_LIMIT_SUBMIT", "20/1h"), 20, 3600)
CONTACT_MAX, CONTACT_PER = _parse_rate(os.environ.get("RATE_LIMIT_CONTACT", "10/1h"), 10, 3600)


def _client_ip() -> str:
    # Derrière un reverse proxy (nginx, Caddy, Cloudflare), le vrai IP est
    # dans X-Forwarded-For. On prend le premier segment.
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limit_hit(bucket: str, max_hits: int, per_seconds: int) -> bool:
    """Retourne True si la requête est autorisée, False si dépassement."""
    key = f"{bucket}:{_client_ip()}"
    now = time.time()
    cutoff = now - per_seconds
    with _rate_lock:
        dq = _rate_hits[key]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= max_hits:
            return False
        dq.append(now)
        return True


# ---------------------------------------------------------------------------
# CSRF — jeton dans la session + double-submit via header X-CSRF-Token
# ou champ caché _csrf pour les formulaires POST classiques.
# Les endpoints publics /api/submit et /api/contact sont exemptés
# (ils sont appelés depuis le site lui-même sans session).
# ---------------------------------------------------------------------------

CSRF_EXEMPT_PATHS = {"/api/submit", "/api/contact"}


def _get_or_create_csrf_token() -> str:
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


def _csrf_valid(submitted: str) -> bool:
    expected = session.get("_csrf")
    if not expected or not submitted:
        return False
    # comparaison à temps constant contre les attaques par timing
    return secrets.compare_digest(str(expected), str(submitted))


@app.before_request
def _csrf_and_bootstrap():
    # 1. init DB paresseuse
    logic.init_db()

    # 2. CSRF sur les méthodes qui mutent
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if request.path in CSRF_EXEMPT_PATHS:
        return None

    submitted = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("_csrf")
        or ((request.get_json(silent=True) or {}).get("_csrf") if request.is_json else None)
    )

    if not _csrf_valid(submitted):
        logger.warning("CSRF rejeté path=%s ip=%s", request.path, _client_ip())
        if request.path.startswith("/api/"):
            return jsonify({"erreur": "Jeton CSRF invalide."}), 403
        abort(403)
    return None


@app.context_processor
def _inject_csrf():
    # Rend {{ csrf_token() }} disponible dans les templates Jinja.
    return {"csrf_token": _get_or_create_csrf_token}


# ---------------------------------------------------------------------------
# GESTION D'ERREURS GLOBALE
# ---------------------------------------------------------------------------

def _wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept


@app.errorhandler(400)
def _err_400(e):
    if _wants_json():
        return jsonify({"erreur": "Requête invalide."}), 400
    return "Requête invalide.", 400


@app.errorhandler(401)
def _err_401(e):
    if _wants_json():
        return jsonify({"erreur": "Non autorisé."}), 401
    return "Non autorisé.", 401


@app.errorhandler(403)
def _err_403(e):
    if _wants_json():
        return jsonify({"erreur": "Interdit."}), 403
    return "Accès interdit.", 403


@app.errorhandler(404)
def _err_404(e):
    if _wants_json():
        return jsonify({"erreur": "Introuvable."}), 404
    return "Page introuvable.", 404


@app.errorhandler(413)
def _err_413(e):
    return jsonify({"erreur": "Fichier trop volumineux (limite 25 Mo par fichier)."}), 413


@app.errorhandler(429)
def _err_429(e):
    if _wants_json():
        return jsonify({"erreur": "Trop de tentatives. Réessaie plus tard."}), 429
    return "Trop de tentatives. Réessaie plus tard.", 429


@app.errorhandler(500)
def _err_500(e):
    # Ne PAS exposer la stack au client. La stack est déjà loggée par Flask
    # via logger.exception ci-dessous.
    logger.exception("Erreur interne non gérée sur %s : %s", request.path, e)
    if _wants_json():
        return jsonify({"erreur": "Une erreur interne est survenue."}), 500
    return "Une erreur interne est survenue.", 500


@app.errorhandler(Exception)
def _err_unhandled(e):
    # Filet ultime : toute exception non prévue arrive ici avec un log complet
    # et une réponse générique.
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    logger.exception("Exception non gérée sur %s : %s", request.path, e)
    if _wants_json():
        return jsonify({"erreur": "Une erreur interne est survenue."}), 500
    return "Une erreur interne est survenue.", 500


# ---------------------------------------------------------------------------
# PAGES PUBLIQUES
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/politique-de-confidentialite")
def politique_confidentialite():
    """Politique de confidentialité RGPD — accessible librement."""
    return render_template(
        "politique_confidentialite.html",
        retention_jours=logic.RETENTION_DAYS,
        contact_email=os.environ.get("ADMIN_EMAIL", "contact@ycdigital.fr"),
    )


# L'ancien /admin ne doit plus donner d'indice sur l'existence d'un espace
# admin : on renvoie un 404 générique.
@app.route("/admin")
@app.route("/admin/login")
@app.route("/admin/logout")
def _legacy_admin_disabled():
    abort(404)


# ---------------------------------------------------------------------------
# API — questionnaire
# ---------------------------------------------------------------------------

@app.route("/api/config")
def api_config():
    return jsonify(logic.get_public_config())


@app.route("/api/submit", methods=["POST"])
def api_submit():
    # Anti-spam / anti-flood
    if not rate_limit_hit("submit", SUBMIT_MAX, SUBMIT_PER):
        logger.warning("Rate limit /api/submit atteint ip=%s", _client_ip())
        abort(429)

    ctype = (request.content_type or "").lower()
    is_multipart = (
        ctype.startswith("multipart/")
        or bool(request.files)
        or bool(request.form)
    )

    if is_multipart:
        service = (request.form.get("service") or "").strip()
        try:
            reponses = json.loads(request.form.get("reponses") or "{}")
            contact = json.loads(request.form.get("contact") or "{}")
        except (TypeError, ValueError) as exc:
            logger.info("JSON invalide dans multipart: %s", exc)
            return jsonify({"erreur": "Format des réponses invalide."}), 400
        uploaded = request.files.getlist("fichiers")
    else:
        data = request.get_json(silent=True) or {}
        service = (data.get("service") or "").strip()
        reponses = data.get("reponses", {}) or {}
        contact = data.get("contact", {}) or {}
        uploaded = []

    # Nettoyage / bornage systématique
    contact = logic.sanitize_contact(contact)
    reponses = logic.sanitize_reponses(reponses)

    logger.info(
        "submit content_type=%r service=%r nb_fichiers=%d nom=%r",
        ctype, service, len(uploaded), contact.get("nom"),
    )

    if service not in logic.SERVICES:
        logger.info("submit REJET service inconnu : reçu=%r", service)
        return jsonify({
            "erreur": f"Service inconnu ({service!r}). "
                      f"Attendus : {', '.join(logic.SERVICES.keys())}."
        }), 400

    if not contact.get("nom") or not contact.get("email"):
        return jsonify({"erreur": "Nom et e-mail sont obligatoires."}), 400

    if not logic.is_valid_email(contact["email"]):
        return jsonify({"erreur": "Adresse e-mail invalide."}), 400

    recommandation = logic.compute_recommendation(service, reponses)
    booking_id = logic.save_booking(service, reponses, contact, recommandation)

    fichiers_sauves = []
    fichiers_refuses = []
    for fs in uploaded:
        info = logic.save_booking_file(booking_id, fs)
        if info:
            fichiers_sauves.append(info)
        elif fs and fs.filename:
            fichiers_refuses.append(fs.filename)

    logic.notify_new_booking(booking_id, service, contact, recommandation, fichiers_sauves)

    payload = {
        "booking_id": booking_id,
        "recommandation": recommandation,
        "fichiers": fichiers_sauves,
    }
    if fichiers_refuses:
        payload["fichiers_refuses"] = fichiers_refuses
    return jsonify(payload)


# ---------------------------------------------------------------------------
# API — formulaire de contact direct
# ---------------------------------------------------------------------------

@app.route("/api/contact", methods=["POST"])
def api_contact():
    if not rate_limit_hit("contact", CONTACT_MAX, CONTACT_PER):
        logger.warning("Rate limit /api/contact atteint ip=%s", _client_ip())
        abort(429)

    data = request.get_json(silent=True) or {}
    nom = (data.get("nom") or "").strip()[:logic.MAX_LEN_NAME]
    email = (data.get("email") or "").strip()[:logic.MAX_LEN_EMAIL]
    message = (data.get("message") or "").strip()[:logic.MAX_LEN_LONG_TEXT]

    if not nom or not email or not message:
        return jsonify({"erreur": "Le nom, l'e-mail et le message sont obligatoires."}), 400

    if not logic.is_valid_email(email):
        return jsonify({"erreur": "Adresse e-mail invalide."}), 400

    message_id = logic.save_contact_message(nom, email, message)
    logic.notify_new_message(message_id, nom, email, message)

    return jsonify({"message_id": message_id})


# ---------------------------------------------------------------------------
# API — RGPD : demande de suppression (droit à l'effacement)
#
# Publique et exemptée de CSRF (elle est déclenchée par l'utilisateur lui-même
# depuis un autre onglet / un email de confirmation). Elle est rate-limitée
# et exige que le demandeur fournisse son e-mail : la suppression n'est
# effectuée que pour les données associées à cet e-mail.
#
# En pratique on recommande d'envoyer ensuite un e-mail de confirmation au
# demandeur (workflow à brancher côté opérationnel).
# ---------------------------------------------------------------------------

@app.route("/api/rgpd/suppression", methods=["POST"])
def api_rgpd_suppression():
    if not rate_limit_hit("rgpd", 5, 3600):
        abort(429)
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    if not logic.is_valid_email(email):
        return jsonify({"erreur": "Adresse e-mail invalide."}), 400
    resultat = logic.delete_by_email(email)
    return jsonify({"ok": True, "resume": resultat})


# On exempte cette route du contrôle CSRF, comme /api/submit et /api/contact.
CSRF_EXEMPT_PATHS.add("/api/rgpd/suppression")


# ---------------------------------------------------------------------------
# API — données admin
# ---------------------------------------------------------------------------

@app.route("/api/bookings")
@api_admin_required
def api_bookings():
    statut = request.args.get("statut")
    return jsonify(logic.list_bookings(statut))


@app.route("/api/bookings/<booking_id>/statut", methods=["POST"])
@api_admin_required
def api_update_statut(booking_id):
    data = request.get_json(silent=True) or {}
    nouveau_statut = data.get("statut", "nouveau")
    ok = logic.update_booking_status(booking_id, nouveau_statut)
    if not ok:
        return jsonify({"erreur": "Demande introuvable ou statut invalide."}), 404
    return jsonify({"ok": True})


@app.route("/api/bookings/<booking_id>", methods=["DELETE"])
@api_admin_required
def api_delete_booking(booking_id):
    """Suppression manuelle depuis l'admin (droit à l'effacement)."""
    ok = logic.delete_booking(booking_id)
    if not ok:
        return jsonify({"erreur": "Demande introuvable."}), 404
    return jsonify({"ok": True})


@app.route("/api/bookings/<booking_id>/fichiers/<path:filename>")
@api_admin_required
def api_download_file(booking_id, filename):
    path = logic.get_booking_file_path(booking_id, filename)
    if not path:
        abort(404)
    as_attachment = request.args.get("dl") == "1"
    guessed_mime, _ = mimetypes.guess_type(path.name)
    return send_from_directory(
        path.parent, path.name,
        as_attachment=as_attachment,
        mimetype=guessed_mime or "application/octet-stream",
    )


@app.route("/api/messages")
@api_admin_required
def api_messages():
    return jsonify(logic.list_contact_messages())


# ---------------------------------------------------------------------------
# ESPACE ADMIN (URL non devinée via ADMIN_URL_SLUG)
# ---------------------------------------------------------------------------

@app.route(f"/{ADMIN_URL_SLUG}")
@admin_required
def admin_dashboard():
    return render_template("admin_dashboard.html")


@app.route(f"/{ADMIN_URL_SLUG}/connexion", methods=["GET", "POST"], endpoint="admin_login")
def admin_login():
    erreur = None
    if request.method == "POST":
        # Rate limiting anti brute-force
        if not rate_limit_hit("login", LOGIN_MAX, LOGIN_PER):
            logger.warning("Rate limit login atteint ip=%s", _client_ip())
            return render_template(
                "admin_login.html",
                erreur=f"Trop de tentatives. Réessaie dans {LOGIN_PER // 60} minutes.",
            ), 429

        mot_de_passe = request.form.get("mot_de_passe", "")
        if check_password_hash(_ADMIN_PASSWORD_HASH, mot_de_passe):
            # Session neuve => nouvel identifiant de session (empêche session fixation)
            session.clear()
            session["admin_logged_in"] = True
            session.permanent = True
            # Régénère un nouveau jeton CSRF pour la nouvelle session
            session["_csrf"] = secrets.token_urlsafe(32)
            logger.info("Connexion admin réussie ip=%s", _client_ip())

            dest = request.args.get("next") or url_for("admin_dashboard")
            return redirect(dest)

        logger.warning("Connexion admin ÉCHEC ip=%s", _client_ip())
        erreur = "Mot de passe incorrect."
    return render_template("admin_login.html", erreur=erreur)


@app.route(f"/{ADMIN_URL_SLUG}/deconnexion", endpoint="admin_logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


# ---------------------------------------------------------------------------
# Purge RGPD au démarrage (best-effort, non bloquant)
# ---------------------------------------------------------------------------

def _startup_purge():
    try:
        logic.init_db()
        logic.purge_old_data()
    except Exception as exc:
        logger.exception("Purge de démarrage échouée : %s", exc)


threading.Thread(target=_startup_purge, daemon=True).start()


if __name__ == "__main__":
    logic.init_db()
    app.run(debug=True, port=5000)
