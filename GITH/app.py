# -*- coding: utf-8 -*-
"""
app.py
------
Serveur Flask. Toute la logique est déléguée à logic.py : ce fichier ne fait
que router les requêtes HTTP. C'est le seul fichier "serveur" à lancer.

Lancement local :
    pip install -r requirements.txt
    python app.py
Puis ouvrir http://127.0.0.1:5000

Espace admin : http://127.0.0.1:5000/admin/login
  Identifiants par défaut si rien n'est configuré : mot de passe "change-moi"
  (voir la section "CONFIGURATION ADMIN" ci-dessous et le README).

Déploiement : ce code fonctionne tel quel sur Render, Railway, PythonAnywhere,
un VPS avec gunicorn, etc. (gunicorn app:app)
"""

import os
import secrets
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, session,
)
from werkzeug.security import generate_password_hash, check_password_hash

import logic

app = Flask(__name__)

# ---------------------------------------------------------------------------
# CONFIGURATION ADMIN
# ---------------------------------------------------------------------------
# SECRET_KEY : nécessaire pour que Flask puisse signer les sessions (cookie
# de connexion admin). En production, définis la variable d'environnement
# SECRET_KEY avec une valeur fixe et aléatoire (ex: `python -c "import
# secrets; print(secrets.token_hex(32))"`), sinon tout le monde sera
# déconnecté à chaque redémarrage du serveur (ce qui n'est pas grave, mais
# un peu pénible).
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# Mot de passe de l'espace admin. Deux façons de le définir en production :
#   - ADMIN_PASSWORD_HASH : un hash déjà généré (le plus sûr)
#   - ADMIN_PASSWORD      : le mot de passe en clair, hashé au démarrage
# Si aucune des deux variables n'est définie, le mot de passe par défaut
# "change-moi" est utilisé — à changer impérativement avant la mise en ligne.
_ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH")
if not _ADMIN_PASSWORD_HASH:
    _plain = os.environ.get("ADMIN_PASSWORD", "change-moi")
    _ADMIN_PASSWORD_HASH = generate_password_hash(_plain)
    if _plain == "change-moi":
        print("[admin] ATTENTION : mot de passe admin par défaut ('change-moi') "
              "utilisé. Définis la variable d'environnement ADMIN_PASSWORD "
              "avant de mettre le site en ligne.")

# Token conservé pour un usage API/scripts (ex: automatiser une récupération
# de données) sans passer par le formulaire de connexion. Optionnel : laisse
# vide pour désactiver cet accès et n'autoriser que la connexion par mot de
# passe.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def _is_authorized() -> bool:
    """Autorisé si connecté via la session admin, ou si un token valide est
    fourni (compatibilité API)."""
    if session.get("admin_logged_in"):
        return True
    token = request.args.get("token")
    return bool(ADMIN_TOKEN) and token == ADMIN_TOKEN


def admin_required(view):
    """Protège une page HTML : redirige vers /admin/login si non connecté."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _is_authorized():
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def api_admin_required(view):
    """Protège un endpoint JSON : renvoie une 401 si non autorisé."""
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
    """Le front va chercher ici la structure du questionnaire."""
    return jsonify(logic.get_public_config())


@app.route("/api/submit", methods=["POST"])
def api_submit():
    """Réception d'une demande de devis / réservation."""
    data = request.get_json(silent=True) or {}

    service = data.get("service")
    reponses = data.get("reponses", {})
    contact = data.get("contact", {})

    if service not in logic.SERVICES:
        return jsonify({"erreur": "Service inconnu."}), 400

    if not contact.get("nom") or not contact.get("email"):
        return jsonify({"erreur": "Nom et e-mail sont obligatoires."}), 400

    recommandation = logic.compute_recommendation(service, reponses)
    booking_id = logic.save_booking(service, reponses, contact, recommandation)

    # Notification par e-mail (silencieuse si SMTP non configuré, voir
    # logic.py section 5). Ne bloque jamais la réponse au client.
    logic.notify_new_booking(booking_id, service, contact, recommandation)

    return jsonify({
        "booking_id": booking_id,
        "recommandation": recommandation,
    })


# ---------------------------------------------------------------------------
# API - formulaire de contact direct (section "Contact" du site)
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
# API - données admin (protégées : session connectée OU token)
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


@app.route("/api/messages")
@api_admin_required
def api_messages():
    return jsonify(logic.list_contact_messages())


# ---------------------------------------------------------------------------
# Espace admin (interface HTML, accessible depuis n'importe où une fois le
# site en ligne : il suffit de se rendre sur https://ton-domaine.fr/admin
# et de se connecter avec le mot de passe admin).
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
