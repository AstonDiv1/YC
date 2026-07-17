# -*- coding: utf-8 -*-
from __future__ import annotations
"""
app.py
------
Serveur Flask. Toute la logique métier est déléguée à logic.py.

Nouveautés :
  - Rate-limiting IP sur les endpoints publics (/api/submit, /api/contact) :
    10 requêtes / IP / 24h (fenêtre glissante).
  - Anti brute-force sur la connexion admin :
    3 tentatives échouées => blocage de 10 minutes pour cette IP.
  - Nouvelle page publique /conditions-utilisation (CGU).

Lancement local :
    pip install -r requirements.txt
    python app.py
Puis ouvrir http://127.0.0.1:5000
"""

import json
import mimetypes
import os
import re
import secrets
from functools import wraps
from typing import Optional
from urllib.parse import urlsplit

from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, session, send_from_directory, abort,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

import logic

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_CONTENT_LENGTH_BYTES", str(64 * 1024 * 1024))),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"},
)

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# ---------------------------------------------------------------------------
# CONFIGURATION ADMIN
# ---------------------------------------------------------------------------
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

_ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH")
if not _ADMIN_PASSWORD_HASH:
    _plain = os.environ.get("ADMIN_PASSWORD", "change-moi")
    _ADMIN_PASSWORD_HASH = generate_password_hash(_plain)
    if _plain == "change-moi":
        print("[admin] ATTENTION : mot de passe admin par défaut ('change-moi') "
              "utilisé. Définis la variable d'environnement ADMIN_PASSWORD "
              "avant de mettre le site en ligne.")

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

_DEFAULT_ADMIN_SLUG = "gestion-yc-4c9e2a8f7b"


def _normalize_admin_slug(raw) -> str:
    """Accepte un slug, un chemin ou une URL complète, et garde un chemin sûr."""
    value = (raw or _DEFAULT_ADMIN_SLUG).strip()
    if "://" in value:
        value = urlsplit(value).path
    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    if value.endswith("/connexion"):
        value = value[:-len("/connexion")].strip("/")
    if value.endswith("/login"):
        value = value[:-len("/login")].strip("/")
    value = re.sub(r"[^A-Za-z0-9._~/-]+", "-", value)
    value = re.sub(r"/+", "/", value).strip("/")
    return value or _DEFAULT_ADMIN_SLUG


# Slug de l'espace admin — non deviné. Configurable via l'environnement.
ADMIN_URL_SLUG = _normalize_admin_slug(os.environ.get("ADMIN_URL_SLUG"))
print(f"[admin] Espace admin accessible sur /{ADMIN_URL_SLUG}/connexion")


# ---------------------------------------------------------------------------
# RATE LIMITING & ANTI BRUTE-FORCE
# ---------------------------------------------------------------------------
# Compteurs persistants en SQLite, donc actifs même après redémarrage
# et visibles sur les déploiements simples mono-serveur.

# --- Public : demandes de devis + messages de contact ---
PUBLIC_RATE_LIMIT = 10
PUBLIC_RATE_WINDOW = 24 * 3600  # secondes

# --- Admin : tentatives de connexion ---
ADMIN_MAX_ATTEMPTS = 3
ADMIN_BLOCK_SECONDS = 10 * 60


def _client_ip() -> str:
    """IP réelle du client, y compris derrière un reverse proxy."""
    return request.remote_addr or "0.0.0.0"


def _check_public_rate_limit() -> tuple[bool, int]:
    """Retourne (autorisé, secondes_avant_reset)."""
    ok, retry_after = logic.check_public_rate_limit(
        _client_ip(),
        "public_forms",
        PUBLIC_RATE_LIMIT,
        PUBLIC_RATE_WINDOW,
    )
    if not ok:
        print(f"[rate-limit] IP {_client_ip()} bloquée sur {request.path}")
    return ok, retry_after


def _json_error(message: str, status: int = 400, *, retry_after: Optional[int] = None):
    payload = {"erreur": message}
    if retry_after is not None:
        payload["retry_after_seconds"] = retry_after
    response = jsonify(payload)
    response.status_code = status
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    return response


def _clean_text(value, max_len: int) -> str:
    return str(value or "").replace("\x00", "").strip()[:max_len]


def _sanitize_contact(raw: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "nom": _clean_text(raw.get("nom"), 100),
        "email": _clean_text(raw.get("email"), 255).lower(),
        "telephone": _clean_text(raw.get("telephone"), 40),
        "ville": _clean_text(raw.get("ville"), 120),
        "message": _clean_text(raw.get("message"), 2000),
    }


def _safe_next_url(value: Optional[str]) -> str:
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//"):
        return url_for("admin_dashboard")
    return value


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def _inject_template_helpers():
    return {"csrf_token": _csrf_token}


def _csrf_is_valid() -> bool:
    expected = session.get("_csrf_token", "")
    provided = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token") or ""
    return bool(expected and provided and secrets.compare_digest(expected, provided))


def _admin_is_blocked() -> int:
    """Retourne le nb de secondes de blocage restant, ou 0 si non bloqué."""
    return logic.admin_block_remaining(_client_ip())


def _admin_register_failure() -> None:
    ip = _client_ip()
    logic.admin_register_failure(ip, ADMIN_MAX_ATTEMPTS, ADMIN_BLOCK_SECONDS)
    if logic.admin_block_remaining(ip) > 0:
        print(f"[admin] IP {ip} bloquée {ADMIN_BLOCK_SECONDS//60} min.")


def _admin_register_success() -> None:
    logic.admin_register_success(_client_ip())


def _admin_remaining_attempts() -> int:
    return logic.admin_remaining_attempts(_client_ip(), ADMIN_MAX_ATTEMPTS, ADMIN_BLOCK_SECONDS)


# ---------------------------------------------------------------------------
# AUTH HELPERS
# ---------------------------------------------------------------------------

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


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


@app.errorhandler(RequestEntityTooLarge)
def _handle_request_too_large(_exc):
    message = "Fichier ou requête trop volumineux. Merci de réduire la taille des pièces jointes."
    if request.path.startswith("/api/"):
        return _json_error(message, 413)
    return message, 413


@app.before_request
def _ensure_db():
    logic.init_db()


# ---------------------------------------------------------------------------
# Pages publiques
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/conditions-utilisation")
def conditions_utilisation():
    return render_template("conditions.html")


# ---------------------------------------------------------------------------
# API - questionnaire
# ---------------------------------------------------------------------------

@app.route("/api/config")
def api_config():
    return jsonify(logic.get_public_config())


@app.route("/api/submit", methods=["POST"])
def api_submit():
    allowed, retry_after = _check_public_rate_limit()
    if not allowed:
        return _json_error(
            "Trop de demandes depuis votre connexion. Merci de patienter avant de réessayer.",
            429,
            retry_after=retry_after,
        )

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
            print(f"[submit] JSON invalide dans multipart: {exc}")
            return _json_error("Format des réponses invalide.", 400)
        uploaded = request.files.getlist("fichiers")
    else:
        data = request.get_json(silent=True) or {}
        service = (data.get("service") or "").strip()
        reponses = data.get("reponses", {}) or {}
        contact = data.get("contact", {}) or {}
        uploaded = []

    if not isinstance(reponses, dict):
        return _json_error("Format des réponses invalide.", 400)
    contact = _sanitize_contact(contact)

    uploads_ok, upload_error = logic.validate_uploaded_files(uploaded)
    if not uploads_ok:
        return _json_error(upload_error, 400)

    print(f"[submit] content_type={ctype!r} service={service!r} "
          f"nb_fichiers={len(uploaded)} nom={contact.get('nom')!r}")

    if service not in logic.SERVICES:
        print(f"[submit] REJET service inconnu : reçu={service!r} "
              f"attendus={list(logic.SERVICES.keys())}")
        return _json_error(
            f"Service inconnu ({service!r}). Attendus : {', '.join(logic.SERVICES.keys())}.",
            400,
        )

    if not contact.get("nom") or not contact.get("email"):
        return _json_error("Nom et e-mail sont obligatoires.", 400)
    if not EMAIL_RE.match(contact["email"]):
        return _json_error("Adresse e-mail invalide.", 400)

    recommandation = logic.compute_recommendation(service, reponses)
    booking_id = logic.save_booking(service, reponses, contact, recommandation)

    fichiers_sauves = []
    for fs in uploaded:
        try:
            info = logic.save_booking_file(booking_id, fs)
            if info:
                fichiers_sauves.append(info)
        except logic.UploadValidationError as exc:
            return _json_error(str(exc), 400)

    logic.notify_new_booking(booking_id, service, contact, recommandation, fichiers_sauves)

    return jsonify({
        "booking_id": booking_id,
        "recommandation": recommandation,
        "fichiers": fichiers_sauves,
    })


# ---------------------------------------------------------------------------
# API - formulaire de contact direct
# ---------------------------------------------------------------------------

@app.route("/api/contact", methods=["POST"])
def api_contact():
    allowed, retry_after = _check_public_rate_limit()
    if not allowed:
        return _json_error(
            "Trop de messages envoyés depuis votre connexion. Merci de patienter avant de réessayer.",
            429,
            retry_after=retry_after,
        )

    data = request.get_json(silent=True) or {}
    nom = _clean_text(data.get("nom"), 100)
    email = _clean_text(data.get("email"), 255).lower()
    message = _clean_text(data.get("message"), 2000)

    if not nom or not email or not message:
        return _json_error("Le nom, l'e-mail et le message sont obligatoires.", 400)
    if not EMAIL_RE.match(email):
        return _json_error("Adresse e-mail invalide.", 400)

    message_id = logic.save_contact_message(nom, email, message)
    logic.notify_new_message(message_id, nom, email, message)

    return jsonify({"message_id": message_id})


# ---------------------------------------------------------------------------
# API - données admin
# ---------------------------------------------------------------------------

@app.route("/api/bookings")
@api_admin_required
def api_bookings():
    statut = request.args.get("statut")
    return jsonify(logic.list_bookings(statut))


@app.route("/api/bookings/<booking_id>/statut", methods=["POST"])
@api_admin_required
def api_update_statut(booking_id):
    if not _csrf_is_valid():
        return _json_error("Session expirée. Rechargez la page puis réessayez.", 400)
    data = request.get_json(silent=True) or {}
    nouveau_statut = _clean_text(data.get("statut", "nouveau"), 40)
    if nouveau_statut not in logic.VALID_STATUSES:
        return _json_error("Statut invalide.", 400)
    ok = logic.update_booking_status(booking_id, nouveau_statut)
    if not ok:
        return _json_error("Demande introuvable.", 404)
    return jsonify({"ok": True})


@app.route("/api/bookings/<booking_id>/fichiers/<path:filename>")
@api_admin_required
def api_download_file(booking_id, filename):
    """Sert un fichier joint à une demande (admin uniquement).

    ?dl=1 force le téléchargement. Par défaut, inline pour afficher les
    images directement dans le dashboard.
    """
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
# Espace admin (URL non devinée via ADMIN_URL_SLUG)
# ---------------------------------------------------------------------------

@app.route(f"/{ADMIN_URL_SLUG}")
@admin_required
def admin_dashboard():
    return render_template("admin_dashboard.html")


@app.route(f"/{ADMIN_URL_SLUG}/connexion", methods=["GET", "POST"], endpoint="admin_login")
@app.route(f"/{ADMIN_URL_SLUG}/login", methods=["GET", "POST"])
def admin_login():
    erreur = None
    status = 200

    # 1) Blocage actif : on refuse même les GET.
    blocage_restant = _admin_is_blocked()
    if blocage_restant > 0:
        minutes = (blocage_restant + 59) // 60
        erreur = (f"Trop de tentatives échouées. "
                  f"Réessayez dans {minutes} minute(s).")
        return render_template(
            "admin_login.html",
            erreur=erreur,
            blocage_restant=blocage_restant,
            restant_essais=0,
        ), 429

    # 2) POST : on vérifie le mot de passe.
    if request.method == "POST":
        if not _csrf_is_valid():
            return render_template(
                "admin_login.html",
                erreur="Session expirée. Rechargez la page puis réessayez.",
                blocage_restant=0,
                restant_essais=_admin_remaining_attempts(),
            ), 400
        mot_de_passe = request.form.get("mot_de_passe", "")
        try:
            ok = check_password_hash(_ADMIN_PASSWORD_HASH, mot_de_passe)
        except Exception as exc:  # hash mal formé dans l'env, etc.
            print(f"[admin] Erreur de vérification du mot de passe : {exc}")
            ok = False

        if ok:
            _admin_register_success()
            session.clear()
            session["admin_logged_in"] = True
            session["_csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            dest = _safe_next_url(request.args.get("next"))
            return redirect(dest)

        # échec
        _admin_register_failure()
        blocage_restant = _admin_is_blocked()
        if blocage_restant > 0:
            minutes = (blocage_restant + 59) // 60
            erreur = f"Trop de tentatives. Accès bloqué {minutes} minute(s)."
            status = 429
        else:
            restant_essais = _admin_remaining_attempts()
            erreur = (f"Mot de passe incorrect. "
                      f"Il vous reste {restant_essais} tentative(s) "
                      f"avant blocage.")
            status = 401

    return render_template(
        "admin_login.html",
        erreur=erreur,
        blocage_restant=blocage_restant,
        restant_essais=_admin_remaining_attempts(),
    ), status


@app.route(f"/{ADMIN_URL_SLUG}/deconnexion", endpoint="admin_logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


if __name__ == "__main__":
    logic.init_db()
    app.run(debug=True, port=5000)
