# -*- coding: utf-8 -*-
"""
logic.py
--------
Toute la "logique métier" de l'application vit ici, en Python pur :
  - la structure du questionnaire (facile à modifier sans toucher au HTML)
  - le moteur de recommandation (calcule un profil, une fourchette de prix,
    un délai estimé, en fonction des réponses du client)
  - le stockage des demandes (SQLite, aucune dépendance externe)

Le fichier templates/index.html ne fait QUE de l'affichage : il va chercher
la configuration du questionnaire via /api/config, l'affiche dynamiquement,
puis envoie les réponses à /api/submit. Si tu veux ajouter, retirer ou
modifier une question demain, tu n'as qu'à modifier le dictionnaire SERVICES
ci-dessous : le site s'adapte tout seul.
"""

import json
import os
import smtplib
import sqlite3
import uuid
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "bookings.db"


# ---------------------------------------------------------------------------
# 1. STRUCTURE DU QUESTIONNAIRE
# ---------------------------------------------------------------------------
# Chaque service définit ses propres questions. Types de questions supportés
# côté front (voir index.html) : "choix_unique", "choix_multiple", "texte",
# "nombre", "select".

SERVICES = {
    "site_web": {
        "label": "Création de site internet",
        "description": "Vitrine, e-commerce, blog, application web sur-mesure.",
        "icone": "web",
        "questions": [
            {
                "id": "type_site",
                "label": "Quel type de site souhaitez-vous ?",
                "type": "choix_unique",
                "options": ["Site vitrine", "E-commerce / boutique en ligne",
                            "Blog", "Application web (avec compte utilisateur)",
                            "Autre / je ne sais pas encore"],
                "required": True,
            },
            {
                "id": "fonctionnalites",
                "label": "Quelles fonctionnalités sont indispensables ?",
                "type": "choix_multiple",
                "options": ["Système de réservation / prise de rendez-vous",
                            "Paiement en ligne", "Espace client / connexion",
                            "Multilingue", "Blog intégré",
                            "Référencement SEO avancé", "Newsletter"],
                "required": False,
            },
            {
                "id": "design_existant",
                "label": "Avez-vous déjà une charte graphique / un logo ?",
                "type": "choix_unique",
                "options": ["Oui, tout est prêt", "Oui, mais à retravailler",
                            "Non, à créer entièrement"],
                "required": True,
            },
            {
                "id": "nb_pages",
                "label": "Nombre de pages estimé",
                "type": "select",
                "options": ["1 à 3 pages", "4 à 8 pages", "9 à 15 pages", "15+ pages"],
                "required": True,
            },
        ],
    },
    "application": {
        "label": "Création d'application",
        "description": "Applications mobiles et desktop, sur-mesure.",
        "icone": "app",
        "questions": [
            {
                "id": "plateforme",
                "label": "Sur quelle(s) plateforme(s) ?",
                "type": "choix_multiple",
                "options": ["iOS", "Android", "Application web (PWA)", "Desktop (Windows/Mac)"],
                "required": True,
            },
            {
                "id": "fonctionnalites_app",
                "label": "Quelles fonctionnalités sont nécessaires ?",
                "type": "choix_multiple",
                "options": ["Compte utilisateur", "Notifications push",
                            "Paiement intégré", "Géolocalisation",
                            "Mode hors-ligne", "Back-office / tableau de bord admin"],
                "required": False,
            },
            {
                "id": "existant_app",
                "label": "Partez-vous de zéro ou avez-vous déjà une base ?",
                "type": "choix_unique",
                "options": ["Projet entièrement nouveau", "J'ai déjà une app à améliorer",
                            "J'ai des maquettes / une idée précise"],
                "required": True,
            },
        ],
    },
    "montage_pc": {
        "label": "Montage & optimisation PC",
        "description": "Montage à domicile, choix des composants, optimisation.",
        "icone": "pc",
        "questions": [
            {
                "id": "usage_pc",
                "label": "Usage principal du PC",
                "type": "choix_unique",
                "options": ["Gaming", "Montage vidéo / photo / 3D",
                            "Bureautique / usage quotidien",
                            "Développement / programmation",
                            "Serveur / usage professionnel intensif"],
                "required": True,
            },
            {
                "id": "type_prestation",
                "label": "De quoi avez-vous besoin ?",
                "type": "choix_multiple",
                "options": ["Montage d'un PC neuf", "Choix des composants (conseil)",
                            "Optimisation d'un PC existant",
                            "Mise à niveau (upgrade) de composants",
                            "Nettoyage / maintenance"],
                "required": True,
            },
            {
                "id": "a_domicile",
                "label": "Souhaitez-vous une intervention à domicile ?",
                "type": "choix_unique",
                "options": ["Oui", "Non, je peux me déplacer ou envoyer le matériel"],
                "required": True,
            },
            {
                "id": "composants_deja_achetes",
                "label": "Avez-vous déjà des composants ?",
                "type": "choix_unique",
                "options": ["Oui, tout est acheté", "Partiellement", "Non, rien"],
                "required": True,
            },
        ],
    },
}

# Questions communes à TOUS les services, posées à la fin (budget, délai, contact)
QUESTIONS_COMMUNES = [
    {
        "id": "budget",
        "label": "Quel budget envisagez-vous ?",
        "type": "select",
        "options": ["Moins de 300 €", "300 € - 800 €", "800 € - 2000 €",
                    "2000 € - 5000 €", "Plus de 5000 €", "Je ne sais pas encore"],
        "required": True,
    },
    {
        "id": "delai",
        "label": "Dans quel délai souhaitez-vous être livré ?",
        "type": "choix_unique",
        "options": ["Le plus vite possible (urgent)", "Sous 2 à 4 semaines",
                    "Sous 1 à 3 mois", "Pas de contrainte particulière"],
        "required": True,
    },
]

CONTACT_FIELDS = [
    {"id": "nom", "label": "Nom complet", "type": "texte", "required": True},
    {"id": "email", "label": "Adresse e-mail", "type": "texte", "required": True},
    {"id": "telephone", "label": "Téléphone", "type": "texte", "required": False},
    {"id": "ville", "label": "Ville", "type": "texte", "required": False},
    {"id": "disponibilite", "label": "Meilleur moment pour vous contacter", "type": "texte", "required": False},
    {"id": "message", "label": "Un détail à ajouter ? (optionnel)", "type": "zone_texte", "required": False},
]


def get_public_config():
    """Renvoie la config du questionnaire, telle que consommée par le front."""
    return {
        "services": SERVICES,
        "questions_communes": QUESTIONS_COMMUNES,
        "contact_fields": CONTACT_FIELDS,
    }


# ---------------------------------------------------------------------------
# 2. MOTEUR DE RECOMMANDATION
# ---------------------------------------------------------------------------

BUDGET_SCORE = {
    "Moins de 300 €": 1,
    "300 € - 800 €": 2,
    "800 € - 2000 €": 3,
    "2000 € - 5000 €": 4,
    "Plus de 5000 €": 5,
    "Je ne sais pas encore": 2,
}

DELAI_URGENCE = {
    "Le plus vite possible (urgent)": True,
    "Sous 2 à 4 semaines": False,
    "Sous 1 à 3 mois": False,
    "Pas de contrainte particulière": False,
}


def _compter_fonctionnalites(reponses: dict) -> int:
    """Compte le nombre total d'options cochées dans les questions à choix multiple."""
    total = 0
    for cle, valeur in reponses.items():
        if isinstance(valeur, list):
            total += len(valeur)
    return total


def compute_recommendation(service: str, reponses: dict) -> dict:
    """
    Calcule un profil de projet (Starter / Pro / Sur-mesure), une fourchette
    de prix indicative, un délai estimé, et un petit message personnalisé.

    C'est ici que se trouve la vraie "logique métier" : à toi de l'affiner
    avec tes propres tarifs et règles au fil du temps.
    """
    budget_label = reponses.get("budget", "Je ne sais pas encore")
    score_budget = BUDGET_SCORE.get(budget_label, 2)
    nb_fonctionnalites = _compter_fonctionnalites(reponses)
    urgent = DELAI_URGENCE.get(reponses.get("delai"), False)

    score_total = score_budget + (nb_fonctionnalites // 2)

    if score_total <= 2:
        profil = "Starter"
        fourchette = "300 € - 800 €"
    elif score_total <= 4:
        profil = "Pro"
        fourchette = "800 € - 2500 €"
    else:
        profil = "Sur-mesure"
        fourchette = "2500 € et plus (devis détaillé nécessaire)"

    delai_estime = "à définir en priorité avec vous" if urgent else "2 à 6 semaines en moyenne selon la charge"

    messages = {
        "site_web": "Merci pour ces précisions sur votre site internet.",
        "application": "Merci pour ces précisions sur votre application.",
        "montage_pc": "Merci pour ces précisions sur votre configuration PC.",
    }

    return {
        "profil": profil,
        "fourchette_prix": fourchette,
        "delai_estime": delai_estime,
        "urgent": urgent,
        "message": messages.get(service, "Merci pour ces précisions."),
    }


# ---------------------------------------------------------------------------
# 3. STOCKAGE (SQLite, sans dépendance externe)
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            service TEXT NOT NULL,
            reponses_json TEXT NOT NULL,
            contact_json TEXT NOT NULL,
            profil TEXT,
            fourchette_prix TEXT,
            delai_estime TEXT,
            statut TEXT DEFAULT 'nouveau'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            nom TEXT NOT NULL,
            email TEXT NOT NULL,
            message TEXT NOT NULL,
            statut TEXT DEFAULT 'nouveau'
        )
    """)
    conn.commit()
    conn.close()


def save_booking(service: str, reponses: dict, contact: dict, recommandation: dict) -> str:
    booking_id = str(uuid.uuid4())[:8]
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO bookings
           (id, created_at, service, reponses_json, contact_json,
            profil, fourchette_prix, delai_estime, statut)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            booking_id,
            datetime.now().isoformat(timespec="seconds"),
            service,
            json.dumps(reponses, ensure_ascii=False),
            json.dumps(contact, ensure_ascii=False),
            recommandation["profil"],
            recommandation["fourchette_prix"],
            recommandation["delai_estime"],
            "nouveau",
        ),
    )
    conn.commit()
    conn.close()
    return booking_id


def list_bookings(statut: str = None) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if statut:
        rows = conn.execute("SELECT * FROM bookings WHERE statut = ? ORDER BY created_at DESC", (statut,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM bookings ORDER BY created_at DESC").fetchall()
    conn.close()

    resultats = []
    for row in rows:
        resultats.append({
            "id": row["id"],
            "created_at": row["created_at"],
            "service": row["service"],
            "reponses": json.loads(row["reponses_json"]),
            "contact": json.loads(row["contact_json"]),
            "profil": row["profil"],
            "fourchette_prix": row["fourchette_prix"],
            "delai_estime": row["delai_estime"],
            "statut": row["statut"],
        })
    return resultats


def update_booking_status(booking_id: str, statut: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("UPDATE bookings SET statut = ? WHERE id = ?", (statut, booking_id))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


# ---------------------------------------------------------------------------
# 4. MESSAGES DE CONTACT DIRECT (formulaire "Contact", plus léger que le
#    questionnaire de devis : pas de qualification, juste un message).
# ---------------------------------------------------------------------------

def save_contact_message(nom: str, email: str, message: str) -> str:
    message_id = str(uuid.uuid4())[:8]
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO messages (id, created_at, nom, email, message, statut) VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, datetime.now().isoformat(timespec="seconds"), nom, email, message, "nouveau"),
    )
    conn.commit()
    conn.close()
    return message_id


def list_contact_messages() -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM messages ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 5. NOTIFICATIONS PAR E-MAIL
#    Envoie un e-mail à toi-même (ADMIN) dès qu'une demande arrive, où que
#    le site soit hébergé. Tout est piloté par variables d'environnement :
#    aucune info sensible n'est écrite en dur dans le code.
#
#    Variables à définir (ex: sur Render/Railway/PythonAnywhere, dans les
#    "Environment Variables" du service) :
#      SMTP_HOST       ex: smtp.gmail.com
#      SMTP_PORT       ex: 587
#      SMTP_USER       l'adresse qui envoie le mail
#      SMTP_PASSWORD   mot de passe / mot de passe d'application
#      ADMIN_EMAIL     l'adresse où TU veux recevoir les notifications
#      SMTP_FROM_NAME  (optionnel) nom affiché comme expéditeur
#
#    Si SMTP_HOST / SMTP_USER / SMTP_PASSWORD / ADMIN_EMAIL ne sont pas
#    tous définis, les notifications sont simplement désactivées (aucune
#    erreur, un message est juste affiché dans les logs du serveur) : le
#    site continue de fonctionner normalement sans email.
# ---------------------------------------------------------------------------

def _email_configure() -> Optional[dict]:
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    admin_email = os.environ.get("ADMIN_EMAIL")
    if not (host and user and password and admin_email):
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": user,
        "password": password,
        "admin_email": admin_email,
        "from_name": os.environ.get("SMTP_FROM_NAME", "Site web"),
    }


def send_email_notification(subject: str, body_text: str) -> bool:
    """Envoie un e-mail texte simple à ADMIN_EMAIL. Ne lève jamais
    d'exception : en cas d'échec (config manquante, réseau, etc.), la
    fonction renvoie False et écrit un avertissement dans les logs, sans
    jamais faire planter la requête HTTP en cours."""
    cfg = _email_configure()
    if not cfg:
        print("[email] Notifications désactivées (variables SMTP_* / ADMIN_EMAIL absentes).")
        return False

    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["from_name"], cfg["user"]))
    msg["To"] = cfg["admin_email"]

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=10) as server:
            server.starttls()
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["user"], [cfg["admin_email"]], msg.as_string())
        return True
    except Exception as exc:  # on ne veut jamais casser le site pour un email raté
        print(f"[email] Échec de l'envoi de la notification : {exc}")
        return False


def notify_new_booking(booking_id: str, service: str, contact: dict, recommandation: dict) -> None:
    label = SERVICES.get(service, {}).get("label", service)
    corps = (
        f"Nouvelle demande de devis reçue sur le site.\n\n"
        f"Référence  : {booking_id}\n"
        f"Service    : {label}\n"
        f"Nom        : {contact.get('nom', '—')}\n"
        f"E-mail     : {contact.get('email', '—')}\n"
        f"Téléphone  : {contact.get('telephone', '—')}\n"
        f"Ville      : {contact.get('ville', '—')}\n"
        f"Profil estimé      : {recommandation.get('profil', '—')}\n"
        f"Fourchette de prix : {recommandation.get('fourchette_prix', '—')}\n"
        f"Délai estimé       : {recommandation.get('delai_estime', '—')}\n\n"
        f"Message du client :\n{contact.get('message') or '(aucun)'}\n\n"
        f"Connecte-toi à ton espace admin pour voir tous les détails et "
        f"changer le statut de cette demande."
    )
    send_email_notification(f"Nouvelle demande — {label} ({contact.get('nom', '—')})", corps)


def notify_new_message(message_id: str, nom: str, email: str, message: str) -> None:
    corps = (
        f"Nouveau message reçu via le formulaire de contact du site.\n\n"
        f"Référence : {message_id}\n"
        f"Nom       : {nom}\n"
        f"E-mail    : {email}\n\n"
        f"Message :\n{message}"
    )
    send_email_notification(f"Nouveau message de contact — {nom}", corps)
