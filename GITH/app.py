# -*- coding: utf-8 -*-
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
import secrets
import time
from collections import defaultdict, deque
from functools import wraps
from threading import Lock

from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, session, send_from_directory, abort,
)
from werkzeug.security import generate_password_hash, check_password_hash

import logic

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 110 * 1024 * 1024

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

# Slug de l'espace admin — non deviné. Configurable via l'environnement,
# avec un défaut long et opaque pour éviter que /admin fonctionne.
ADMIN_URL_SLUG = os.environ.get("ADMIN_URL_SLUG", "gestion-yc-4c9e2a8f7b").strip("/")
if not ADMIN_URL_SLUG:
    ADMIN_URL_SLUG = "gestion-yc-4c9e2a8f7b"
print(f"[admin] Espace admin accessible sur /{ADMIN_URL_SLUG}/connexion")


# ---------------------------------------------------------------------------
# RATE LIMITING & ANTI BRUTE-FORCE
# ---------------------------------------------------------------------------
# Compteurs en mémoire, par process. Suffisant en mono-worker Flask.
# Pour du multi-worker (gunicorn -w N), il faudra migrer vers Redis.

# --- Public : demandes de devis + messages de contact ---
PUBLIC_RATE_LIMIT = 10
PUBLIC_RATE_WINDOW = 24 * 3600  # secondes

_public_hits: dict = defaultdict(deque)   # ip -> deque[timestamps]
_public_lock = Lock()

# --- Admin : tentatives de connexion ---
ADMIN_MAX_ATTEMPTS = 3
ADMIN_BLOCK_SECONDS = 10 * 60

_admin_attempts: dict = defaultdict(list)  # ip -> [timestamps échec]
_admin_blocked: dict = {}                  # ip -> timestamp fin blocage
_admin_lock = Lock()


def _client_ip() -> str:
    """IP réelle du client, y compris derrière un reverse proxy."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


def _check_public_rate_limit() -> bool:
    """True si la requête est autorisée, False si quota IP dépassé."""
    ip = _client_ip()
    now = time.time()
    with _public_lock:
        dq = _public_hits[ip]
        while dq and now - dq[0] > PUBLIC_RATE_WINDOW:
            dq.popleft()
        if len(dq) >= PUBLIC_RATE_LIMIT:
            print(f"[rate-limit] IP {ip} bloquée : {len(dq)} demandes / 24h")
            return False
        dq.append(now)
        return True


def _admin_is_blocked() -> int:
    """Retourne le nb de secondes de blocage restant, ou 0 si non bloqué."""
    ip = _client_ip()
    now = time.time()
    with _admin_lock:
        until = _admin_blocked.get(ip, 0)
        if until and until > now:
            return int(until - now)
        if until and until <= now:
            _admin_blocked.pop(ip, None)
            _admin_attempts.pop(ip, None)
        return 0


def _admin_register_failure() -> None:
    ip = _client_ip()
    now = time.time()
    with _admin_lock:
        attempts = _admin_attempts[ip]
        # ne garder que les échecs récents
        attempts[:] = [t for t in attempts if now - t < ADMIN_BLOCK_SECONDS]
        attempts.append(now)
        if len(attempts) >= ADMIN_MAX_ATTEMPTS:
            _admin_blocked[ip] = now + ADMIN_BLOCK_SECONDS
            print(f"[admin] IP {ip} bloquée {ADMIN_BLOCK_SECONDS//60} min "
                  f"({len(attempts)} tentatives).")


def _admin_register_success() -> None:
    ip = _client_ip()
    with _admin_lock:
        _admin_attempts.pop(ip, None)
        _admin_blocked.pop(ip, None)


def _admin_remaining_attempts() -> int:
    ip = _client_ip()
    now = time.time()
    with _admin_lock:
        attempts = [t for t in _admin_attempts.get(ip, [])
                    if now - t < ADMIN_BLOCK_SECONDS]
        return max(0, ADMIN_MAX_ATTEMPTS - len(attempts))


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


# L'ancien /admin ne doit plus donner d'indice sur l'existence d'un espace
# admin : on renvoie un 404 générique.
@app.route("/admin")
@app.route("/admin/login")
@app.route("/admin/logout")
def _legacy_admin_disabled():
    abort(404)


# ---------------------------------------------------------------------------
# API - questionnaire
# ---------------------------------------------------------------------------

@app.route("/api/config")
def api_config():
    return jsonify(logic.get_public_config())


@app.route("/api/submit", methods=["POST"])
def api_submit():
    if not _check_public_rate_limit():
        return jsonify({
            "erreur": "Trop de demandes depuis votre connexion. "
                      "Merci de réessayer dans 24 heures ou de nous "
                      "contacter directement à yc.digital33@gmail.com."
        }), 429

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
            return jsonify({"erreur": "Format des réponses invalide."}), 400
        uploaded = request.files.getlist("fichiers")
    else:
        data = request.get_json(silent=True) or {}
        service = (data.get("service") or "").strip()
        reponses = data.get("reponses", {}) or {}
        contact = data.get("contact", {}) or {}
        uploaded = []

    print(f"[submit] content_type={ctype!r} service={service!r} "
          f"nb_fichiers={len(uploaded)} nom={contact.get('nom')!r}")

    if service not in logic.SERVICES:
        print(f"[submit] REJET service inconnu : reçu={service!r} "
              f"attendus={list(logic.SERVICES.keys())}")
        return jsonify({
            "erreur": f"Service inconnu ({service!r}). "
                      f"Attendus : {', '.join(logic.SERVICES.keys())}."
        }), 400

    if not contact.get("nom") or not contact.get("email"):
        return jsonify({"erreur": "Nom et e-mail sont obligatoires."}), 400

    recommandation = logic.compute_recommendation(service, reponses)
    booking_id = logic.save_booking(service, reponses, contact, recommandation)

    fichiers_sauves = []
    for fs in uploaded:
        info = logic.save_booking_file(booking_id, fs)
        if info:
            fichiers_sauves.append(info)

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
    if not _check_public_rate_limit():
        return jsonify({
            "erreur": "Trop de messages envoyés depuis votre connexion. "
                      "Merci de réessayer dans 24 heures ou de nous "
                      "contacter directement à yc.digital33@gmail.com."
        }), 429

    data = request.get_json(silent=True) or {}
    nom = (data.get("nom") or "").strip()
    email = (data.get("email") or "").strip()
    message = (data.get("message") or "").strip()

    if not nom or not email or not message:
        return jsonify({"erreur": "Le nom, l'e-mail et le message sont obligatoires."}), 400

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
    data = request.get_json(silent=True) or {}
    nouveau_statut = data.get("statut", "nouveau")
    ok = logic.update_booking_status(booking_id, nouveau_statut)
    if not ok:
        return jsonify({"erreur": "Demande introuvable."}), 404
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
def admin_login():
    erreur = None

    # 1) Blocage actif : on refuse même les GET (page + 429).
    blocage_restant = _admin_is_blocked()
    if blocage_restant > 0:
        minutes = (blocage_restant + 59) // 60
        erreur = (f"Trop de tentatives échouées. "
                  f"Réessayez dans {minutes} minute(s).")
        return render_template("admin_login.html", erreur=erreur), 429

    # 2) POST : on vérifie le mot de passe.
    if request.method == "POST":
        mot_de_passe = request.form.get("mot_de_passe", "")
        if check_password_hash(_ADMIN_PASSWORD_HASH, mot_de_passe):
            _admin_register_success()
            session.clear()
            session["admin_logged_in"] = True
            session.permanent = True
            dest = request.args.get("next") or url_for("admin_dashboard")
            return redirect(dest)

        # échec
        _admin_register_failure()
        restant = _admin_is_blocked()
        if restant > 0:
            minutes = (restant + 59) // 60
            erreur = (f"Trop de tentatives. Accès bloqué {minutes} minute(s).")
        else:
            restant_essais = _admin_remaining_attempts()
            erreur = (f"Mot de passe incorrect. "
                      f"Il vous reste {restant_essais} tentative(s) "
                      f"avant blocage.")

    return render_template("admin_login.html", erreur=erreur)


@app.route(f"/{ADMIN_URL_SLUG}/deconnexion", endpoint="admin_logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


if __name__ == "__main__":
    logic.init_db()
    app.run(debug=True, port=5000)
