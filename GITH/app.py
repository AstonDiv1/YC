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
import logging
import mimetypes
import os
import re
import secrets
from functools import wraps
from typing import Optional
from urllib.parse import urlsplit

# --------------------------------------------------------------------------
# LOGS : sortie stdout structurée, consultable dans les logs Render.
# LOG_LEVEL configurable via variable d'environnement (INFO par défaut).
# --------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("yc_digital.app")

from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, session, send_from_directory, abort, Response,
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
CONTACT_EMAIL_ONLY = os.environ.get("CONTACT_EMAIL_ONLY", "0").lower() in {"1", "true", "yes"}
REQUIRE_EMAIL_DELIVERY = os.environ.get("REQUIRE_EMAIL_DELIVERY", "0").lower() in {"1", "true", "yes"}
MAX_EMAIL_ATTACHMENT_BYTES = 16 * 1024 * 1024

# Use the configured public origin, never the incoming Host header, for indexing.
PUBLIC_SITE_URL = os.environ.get("PUBLIC_SITE_URL", "https://yc-digital.onrender.com").rstrip("/")
CANONICAL_REDIRECT_ENABLED = os.environ.get("CANONICAL_REDIRECT_ENABLED", "0").lower() in {"1", "true", "yes"}
_public_origin = urlsplit(PUBLIC_SITE_URL)
if (_public_origin.scheme != "https" or not _public_origin.hostname
        or _public_origin.username or _public_origin.password
        or _public_origin.path or _public_origin.query or _public_origin.fragment):
    raise ValueError("PUBLIC_SITE_URL doit être une origine HTTPS sans chemin ni identifiants.")
# Public proof supplied by Search Console for the Numéryl account (not a secret).
GOOGLE_SITE_VERIFICATION = os.environ.get("GOOGLE_SITE_VERIFICATION", "gxXhVr42bB7PUHCqtEVOZbIkh2FBbsm6MEVsSFQdhzI")

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PUBLIC_CONTACT_EMAIL = os.environ.get("PUBLIC_CONTACT_EMAIL", "yc.digital33@gmail.com").strip()
if not EMAIL_RE.fullmatch(PUBLIC_CONTACT_EMAIL):
    raise ValueError("PUBLIC_CONTACT_EMAIL doit être une adresse e-mail valide.")

# ---------------------------------------------------------------------------
# CONFIGURATION ADMIN
# ---------------------------------------------------------------------------
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

_ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH")
if not _ADMIN_PASSWORD_HASH:
    _plain = os.environ.get("ADMIN_PASSWORD", "")
    if _plain and _plain != "change-moi":
        _ADMIN_PASSWORD_HASH = generate_password_hash(_plain)
    else:
        logger.warning("Administration désactivée : configurez un mot de passe administrateur.")

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
logger.info("Espace admin accessible sur /%s/connexion", ADMIN_URL_SLUG)


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
        logger.warning("Rate-limit : IP %s bloquée sur %s", _client_ip(), request.path)
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
        "disponibilite": _clean_text(raw.get("disponibilite"), 200),
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
    return {
        "csrf_token": _csrf_token,
        "public_site_url": PUBLIC_SITE_URL,
        "public_contact_email": PUBLIC_CONTACT_EMAIL,
        "google_site_verification": GOOGLE_SITE_VERIFICATION,
        "website_schema": {
            "@context": "https://schema.org", "@type": "WebSite",
            "name": "Numéryl", "alternateName": "NUMÉRYL",
            "url": PUBLIC_SITE_URL + "/", "inLanguage": "fr-FR",
        },
    }


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
        logger.warning("Admin : IP %s bloquée %d min.", ip, ADMIN_BLOCK_SECONDS//60)


def _admin_register_success() -> None:
    logic.admin_register_success(_client_ip())


def _admin_remaining_attempts() -> int:
    return logic.admin_remaining_attempts(_client_ip(), ADMIN_MAX_ATTEMPTS, ADMIN_BLOCK_SECONDS)


# ---------------------------------------------------------------------------
# AUTH HELPERS
# ---------------------------------------------------------------------------

def _is_authorized() -> bool:
    if not _ADMIN_PASSWORD_HASH or CONTACT_EMAIL_ONLY:
        return False
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
    if (request.path.startswith(("/api/", "/" + ADMIN_URL_SLUG, "/static/demos/"))
            or request.path == "/healthz" or response.status_code >= 400):
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    return response


@app.errorhandler(RequestEntityTooLarge)
def _handle_request_too_large(_exc):
    message = "Fichier ou requête trop volumineux. Merci de réduire la taille des pièces jointes."
    if request.path.startswith("/api/"):
        return _json_error(message, 413)
    return message, 413


@app.errorhandler(404)
def _handle_not_found(_exc):
    """404 : sans détails techniques. JSON pour /api/*, page simple sinon."""
    if request.path.startswith("/api/"):
        return _json_error("Ressource introuvable.", 404)
    html = (
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
        "<title>Page introuvable — Numéryl</title>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#FBF9F4;color:#0B1229;display:flex;align-items:center;justify-content:center;"
        "min-height:100vh;margin:0;padding:24px;text-align:center;}"
        "h1{font-size:2rem;margin:0 0 8px;}p{color:#5B6478;margin:0 0 24px;}"
        "a{color:#0F52BA;text-decoration:none;font-weight:600;}</style></head>"
        "<body><div><h1>Page introuvable</h1>"
        "<p>La page que vous cherchez n'existe pas ou a été déplacée.</p>"
        "<a href='/'>← Retour à l'accueil</a></div></body></html>"
    )
    return html, 404


@app.errorhandler(500)
def _handle_internal_error(exc):
    """500 : jamais de détails techniques exposés à l'utilisateur.
    L'erreur est loguée côté serveur pour investigation.
    """
    logger.exception("Erreur serveur non gérée sur %s : %s", request.path, exc)
    if request.path.startswith("/api/"):
        return _json_error(
            "Une erreur inattendue est survenue. Merci de réessayer dans quelques instants.",
            500,
        )
    html = (
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
        "<title>Erreur — Numéryl</title>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#FBF9F4;color:#0B1229;display:flex;align-items:center;justify-content:center;"
        "min-height:100vh;margin:0;padding:24px;text-align:center;}"
        "h1{font-size:2rem;margin:0 0 8px;}p{color:#5B6478;margin:0 0 24px;}"
        "a{color:#0F52BA;text-decoration:none;font-weight:600;}</style></head>"
        "<body><div><h1>Une erreur est survenue</h1>"
        "<p>Nous en avons été notifiés. Merci de réessayer dans quelques instants.</p>"
        "<a href='/'>← Retour à l'accueil</a></div></body></html>"
    )
    return html, 500


@app.before_request
def _redirect_public_pages_to_canonical_domain():
    # Enable only after the custom domain's DNS and HTTPS have been verified.
    # POST requests and API clients must never lose a submitted form through a redirect.
    if (CANONICAL_REDIRECT_ENABLED and request.method in {"GET", "HEAD"}
            and request.path != "/healthz" and not request.path.startswith("/api/")
            and request.host.lower() != urlsplit(PUBLIC_SITE_URL).netloc.lower()):
        path = request.full_path if request.query_string else request.path
        return redirect(PUBLIC_SITE_URL + path, code=301)


@app.before_request
def _ensure_db():
    logic.init_db()


# ---------------------------------------------------------------------------
# Pages publiques
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", contact_email_only=CONTACT_EMAIL_ONLY)


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/conditions-utilisation")
def conditions_utilisation():
    return render_template("conditions.html", contact_email_only=CONTACT_EMAIL_ONLY)


@app.get("/robots.txt")
def robots_txt():
    return Response("User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: "
                    + PUBLIC_SITE_URL + "/sitemap.xml\n", mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    # Only the real studio homepage is indexed; the fictive demo stays excluded.
    from xml.etree.ElementTree import Element, SubElement, tostring
    root = Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    entry = SubElement(root, "url")
    SubElement(entry, "loc").text = PUBLIC_SITE_URL + "/"
    return Response(tostring(root, encoding="utf-8", xml_declaration=True), mimetype="application/xml")


# ---------------------------------------------------------------------------
# API - questionnaire
# ---------------------------------------------------------------------------

@app.route("/api/config")
def api_config():
    return jsonify(logic.get_public_config())


@app.route("/api/submit", methods=["POST"])
def api_submit():
    if CONTACT_EMAIL_ONLY:
        return _json_error(f"Contactez-nous par e-mail : {PUBLIC_CONTACT_EMAIL}.", 503)
    if REQUIRE_EMAIL_DELIVERY and not logic.email_notifications_configured():
        return _json_error(f"L'envoi est momentanément indisponible. Écrivez-nous à {PUBLIC_CONTACT_EMAIL}.", 503)
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
            logger.warning("Submit : JSON invalide dans multipart: %s", exc)
            return _json_error("Format des réponses invalide.", 400)
        uploaded = request.files.getlist("fichiers")
    else:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return _json_error("Format de la demande invalide.", 400)
        service = data.get("service") or ""
        if not isinstance(service, str):
            return _json_error("Service invalide.", 400)
        service = service.strip()
        reponses = data.get("reponses", {}) or {}
        contact = data.get("contact", {}) or {}
        uploaded = []

    if not isinstance(reponses, dict):
        return _json_error("Format des réponses invalide.", 400)
    contact = _sanitize_contact(contact)

    uploads_ok, upload_error = logic.validate_uploaded_files(uploaded)
    if not uploads_ok:
        return _json_error(upload_error, 400)

    if REQUIRE_EMAIL_DELIVERY:
        attachment_bytes = 0
        for file_storage in uploaded:
            attachment_bytes += len(file_storage.read())
            file_storage.stream.seek(0)
        if attachment_bytes > MAX_EMAIL_ATTACHMENT_BYTES:
            return _json_error("Les pièces jointes ne doivent pas dépasser 16 Mo au total pour l'envoi. Réduisez leur taille ou envoyez-nous un lien par e-mail.", 400)

    logger.info(
        "Submit : content_type=%r service=%r nb_fichiers=%d nom=%r",
        ctype, service, len(uploaded), contact.get("nom"),
    )

    if service not in logic.SERVICES:
        logger.warning(
            "Submit : REJET service inconnu — reçu=%r attendus=%s",
            service, list(logic.SERVICES.keys()),
        )
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

    notified = logic.notify_new_booking(booking_id, service, contact, recommandation, fichiers_sauves, reponses)
    if REQUIRE_EMAIL_DELIVERY and not notified:
        return _json_error(f"Votre demande n'a pas pu être transmise par e-mail. Merci de réessayer ou de nous écrire à {PUBLIC_CONTACT_EMAIL}.", 503)

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
    if CONTACT_EMAIL_ONLY:
        return _json_error(f"Contactez-nous par e-mail : {PUBLIC_CONTACT_EMAIL}.", 503)
    if REQUIRE_EMAIL_DELIVERY and not logic.email_notifications_configured():
        return _json_error(f"L'envoi est momentanément indisponible. Écrivez-nous à {PUBLIC_CONTACT_EMAIL}.", 503)
    allowed, retry_after = _check_public_rate_limit()
    if not allowed:
        return _json_error(
            "Trop de messages envoyés depuis votre connexion. Merci de patienter avant de réessayer.",
            429,
            retry_after=retry_after,
        )

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return _json_error("Format du message invalide.", 400)
    nom = _clean_text(data.get("nom"), 100)
    email = _clean_text(data.get("email"), 255).lower()
    message = _clean_text(data.get("message"), 2000)

    if not nom or not email or not message:
        return _json_error("Le nom, l'e-mail et le message sont obligatoires.", 400)
    if not EMAIL_RE.match(email):
        return _json_error("Adresse e-mail invalide.", 400)

    message_id = logic.save_contact_message(nom, email, message)
    notified = logic.notify_new_message(message_id, nom, email, message)
    if REQUIRE_EMAIL_DELIVERY and not notified:
        return _json_error(f"Votre message n'a pas pu être transmis par e-mail. Merci de réessayer ou de nous écrire à {PUBLIC_CONTACT_EMAIL}.", 503)

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
    if not _ADMIN_PASSWORD_HASH or CONTACT_EMAIL_ONLY:
        abort(404)
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
            logger.error("Admin : erreur de vérification du mot de passe : %s", exc)
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
