# -*- coding: utf-8 -*-
"""
app.py
------
Serveur Flask. Toute la logique est déléguée à logic.py : ce fichier ne fait
que router les requêtes HTTP.

Lancement local :
    pip install -r requirements.txt
    python app.py
Puis ouvrir http://127.0.0.1:5000

Espace admin : http://127.0.0.1:5000/admin/login
"""

import json
import os
import secrets
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, session, send_from_directory, abort,
)
from werkzeug.security import generate_password_hash, check_password_hash

import logic

app = Flask(__name__)

# Taille max d'une requête (utile pour les uploads). On autorise ~110 Mo
# côté Flask pour laisser de la marge à plusieurs fichiers de 25 Mo dans
# une même requête multipart. La vraie limite par fichier est appliquée
# dans logic.save_booking_file (MAX_UPLOAD_SIZE).
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


# ---------------------------------------------------------------------------
# API - questionnaire
# ---------------------------------------------------------------------------

@app.route("/api/config")
def api_config():
    return jsonify(logic.get_public_config())


@app.route("/api/submit", methods=["POST"])
def api_submit():
    """Réception d'une demande de devis.

    Supporte deux formats :
      - JSON classique (aucun fichier joint) : Content-Type application/json
      - multipart/form-data (avec fichiers joints)  : champs `service`,
        `reponses` (JSON stringifié), `contact` (JSON stringifié) et un
        ou plusieurs fichiers dans le champ `fichiers`.
    """
    ctype = (request.content_type or "").lower()
    # Détection robuste : on considère "multipart" dès qu'un fichier OU un champ
    # de formulaire est présent (le header seul peut être trompeur).
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
    """Télécharge un fichier joint à une demande (admin uniquement)."""
    path = logic.get_booking_file_path(booking_id, filename)
    if not path:
        abort(404)
    return send_from_directory(path.parent, path.name, as_attachment=True)


@app.route("/api/messages")
@api_admin_required
def api_messages():
    return jsonify(logic.list_contact_messages())


# ---------------------------------------------------------------------------
# Espace admin
# ---------------------------------------------------------------------------

@app.route("/admin")
@admin_required
def admin_dashboard():
    return render_template("admin_dashboard.html")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    erreur = None
    if request.method == "POST":
        mot_de_passe = request.form.get("mot_de_passe", "")
        if check_password_hash(_ADMIN_PASSWORD_HASH, mot_de_passe):
            session.clear()
            session["admin_logged_in"] = True
            session.permanent = True
            dest = request.args.get("next") or url_for("admin_dashboard")
            return redirect(dest)
        erreur = "Mot de passe incorrect."
    return render_template("admin_login.html", erreur=erreur)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


if __name__ == "__main__":
    logic.init_db()
    app.run(debug=True, port=5000)
