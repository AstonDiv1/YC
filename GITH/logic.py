# -*- coding: utf-8 -*-
"""
logic.py
--------
Toute la "logique métier" de l'application vit ici, en Python pur :
  - la structure du questionnaire (facile à modifier sans toucher au HTML)
  - le moteur de recommandation (calcule un profil, une fourchette de prix,
    un délai estimé, en fonction des réponses du client)
  - le stockage des demandes (SQLite, aucune dépendance externe)
  - la gestion des fichiers joints à une demande

Le fichier templates/index.html ne fait QUE de l'affichage : il va chercher
la configuration du questionnaire via /api/config, l'affiche dynamiquement,
puis envoie les réponses à /api/submit. Si tu veux ajouter, retirer ou
modifier une question demain, tu n'as qu'à modifier le dictionnaire SERVICES
ci-dessous : le site s'adapte tout seul.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import resend

DB_PATH = Path(__file__).parent / "bookings.db"
UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Taille max acceptée par fichier joint (25 Mo). Le front prévient à 25 Mo
# aussi, mais on garde une garde côté serveur pour éviter les abus.
MAX_UPLOAD_SIZE = 25 * 1024 * 1024

# ---------------------------------------------------------------------------
# 1. STRUCTURE DU QUESTIONNAIRE
# ---------------------------------------------------------------------------
# Chaque service définit ses propres questions. Types de questions supportés
# côté front (voir index.html) : "choix_unique", "choix_multiple", "texte",
# "nombre", "select", "zone_texte", "fichiers".

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
                "options": [
                    "Site vitrine",
                    "E-commerce / boutique en ligne",
                    "Blog",
                    "Application web (avec compte utilisateur)",
                    "Autre / je ne sais pas encore",
                ],
                "required": True,
            },
            {
                "id": "fonctionnalites",
                "label": "Quelles fonctionnalités sont indispensables ?",
                "type": "choix_multiple",
                "options": [
                    "Système de réservation / prise de rendez-vous",
                    "Paiement en ligne",
                    "Espace client / connexion",
                    "Multilingue",
                    "Blog intégré",
                    "Référencement SEO avancé",
                    "Newsletter",
                    "Formulaire de contact avancé",
                    "Galerie / portfolio",
                    "Intégration Google Maps",
                    "Chat en direct",
                    "Import / export de données",
                ],
                "required": False,
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
        "description": "Applications Android, web (PWA) et Windows, sur-mesure.",
        "icone": "app",
        "questions": [
            {
                "id": "plateforme",
                "label": "Sur quelle(s) plateforme(s) ?",
                "type": "choix_multiple",
                # iOS et macOS retirés — on ne développe pas pour l'écosystème Apple.
                "options": [
                    "Android",
                    "Application web (PWA)",
                    "Desktop Windows",
                ],
                "required": True,
            },
            {
                "id": "fonctionnalites_app",
                "label": "Quelles fonctionnalités sont nécessaires ?",
                "type": "choix_multiple",
                "options": [
                    "Compte utilisateur / connexion",
                    "Notifications push",
                    "Paiement intégré",
                    "Géolocalisation",
                    "Mode hors-ligne",
                    "Back-office / tableau de bord admin",
                    "Chat / messagerie interne",
                    "Upload / gestion de fichiers",
                    "Génération de PDF / documents",
                    "Import / export de données (CSV, Excel)",
                    "Intégration API externe",
                    "Statistiques / tableau de bord",
                    "Multilingue",
                    "Notifications par e-mail",
                    "Système de rôles / permissions",
                    "Recherche avancée / filtres",
                ],
                "required": False,
            },
            {
                "id": "existant_app",
                "label": "Où en êtes-vous dans votre projet ?",
                # On ne fait pas d'amélioration d'app existante — l'option a été retirée.
                # Reste : "à partir de zéro", "maquettes/idée précise", ou "idée à structurer".
                "type": "choix_unique",
                "options": [
                    "Projet entièrement nouveau, à partir de zéro",
                    "J'ai déjà des maquettes ou une idée très précise",
                    "Je pars d'une idée générale à structurer avec vous",
                ],
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
                "options": [
                    "Gaming",
                    "Montage vidéo / photo / 3D",
                    "Bureautique / usage quotidien",
                    "Développement / programmation",
                    "Serveur / usage professionnel intensif",
                ],
                "required": True,
            },
            {
                "id": "type_prestation",
                "label": "De quoi avez-vous besoin ?",
                "type": "choix_multiple",
                "options": [
                    "Montage d'un PC neuf",
                    "Choix des composants (conseil)",
                    "Optimisation d'un PC existant",
                    "Mise à niveau (upgrade) de composants",
                    "Nettoyage / maintenance",
                ],
                "required": True,
            },
            {
                "id": "a_domicile",
                "label": "Souhaitez-vous une intervention à domicile ?",
                "type": "choix_unique",
                "options": [
                    "Oui",
                    "Non, je peux me déplacer ou envoyer le matériel",
                ],
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

# Questions communes à TOUS les services, posées à la fin (budget, délai,
# contexte, ambiance visuelle, fichiers). Enrichies pour mieux qualifier
# la demande avant même le premier échange.
QUESTIONS_COMMUNES = [
    {
        "id": "budget",
        "label": "Quel budget envisagez-vous ?",
        "type": "select",
        "options": [
            "Moins de 300 €",
            "300 € - 800 €",
            "800 € - 2000 €",
            "2000 € - 5000 €",
            "Plus de 5000 €",
            "Je ne sais pas encore",
        ],
        "required": True,
    },
    {
        "id": "delai",
        "label": "Dans quel délai souhaitez-vous être livré ?",
        "type": "choix_unique",
        "options": [
            "Urgent — dès que possible",
            "Sous 1 à 2 semaines",
            "Sous 2 à 3 semaines",
            "Pas de contrainte particulière",
        ],
        "required": True,
    },
    {
        "id": "contexte",
        "label": "Décrivez votre projet en quelques mots (contexte, objectif, cible)",
        "type": "zone_texte",
        "required": False,
    },
    {
        "id": "inspirations",
        "label": "Avez-vous des références ou sites qui vous plaisent ? (liens, exemples)",
        "type": "zone_texte",
        "required": False,
    },
    {
        "id": "ambiance_visuelle",
        "label": "Quelle ambiance visuelle imaginez-vous ?",
        "type": "choix_multiple",
        "options": [
            "Épurée / minimaliste",
            "Chaleureuse / naturelle",
            "Élégante / haut de gamme",
            "Dynamique / colorée",
            "Technique / corporate",
            "Créative / artistique",
            "Je ne sais pas encore — orientez-moi",
        ],
        "required": False,
    },
    {
        "id": "charte_existante",
        "label": "Avez-vous déjà un logo ou une charte graphique ?",
        # NOTE : nous ne créons pas la charte graphique — elle reste à la charge
        # du client (ou de son graphiste). On peut en revanche l'orienter dans
        # ses choix visuels. Cette question sert uniquement à savoir sur quoi
        # nous partons pour le développement.
        "type": "choix_unique",
        "options": [
            "Oui, tout est prêt (logo + couleurs + typos)",
            "J'ai seulement un logo",
            "Non, rien pour le moment",
        ],
        "required": False,
    },
    {
        "id": "fichiers",
        "label": "Ajoutez tout document utile (cahier des charges, maquettes, logo, photos, exemples…)",
        "type": "fichiers",
        "required": False,
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
    "Urgent — dès que possible": True,
    "Sous 1 à 2 semaines": False,
    "Sous 2 à 3 semaines": False,
    "Pas de contrainte particulière": False,
}


def _compter_fonctionnalites(reponses: dict) -> int:
    total = 0
    for _, valeur in reponses.items():
        if isinstance(valeur, list):
            total += len(valeur)
    return total


def compute_recommendation(service: str, reponses: dict) -> dict:
    budget_label = reponses.get("budget", "Je ne sais pas encore")
    score_budget = BUDGET_SCORE.get(budget_label, 2)
    nb_fonctionnalites = _compter_fonctionnalites(reponses)
    urgent = DELAI_URGENCE.get(reponses.get("delai"), False)

    score_total = score_budget + (nb_fonctionnalites // 2)
    if score_total <= 2:
        profil, fourchette = "Starter", "300 € - 800 €"
    elif score_total <= 4:
        profil, fourchette = "Pro", "800 € - 2500 €"
    else:
        profil, fourchette = "Sur-mesure", "2500 € et plus (devis détaillé nécessaire)"

    delai_estime = (
        "à définir en priorité avec vous" if urgent
        else "2 à 6 semaines en moyenne selon la charge"
    )

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
    # Table des fichiers joints à une demande. On stocke seulement le chemin
    # relatif (sous UPLOAD_DIR) et le nom original — le fichier lui-même
    # reste sur disque, ce qui permet d'éviter de gonfler la base SQLite.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS booking_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id TEXT NOT NULL,
            filename_original TEXT NOT NULL,
            filename_stored TEXT NOT NULL,
            mimetype TEXT,
            size_bytes INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(booking_id) REFERENCES bookings(id) ON DELETE CASCADE
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


def _safe_filename(name: str) -> str:
    """Nettoie un nom de fichier pour éviter les injections de chemin."""
    name = os.path.basename(name or "fichier")
    # Retire tout caractère problématique tout en gardant l'extension lisible.
    keep = "-_.() "
    cleaned = "".join(c for c in name if c.isalnum() or c in keep).strip()
    return cleaned or "fichier"


def save_booking_file(booking_id: str, file_storage) -> Optional[dict]:
    """
    Sauvegarde un fichier joint à une demande. `file_storage` est un
    werkzeug.datastructures.FileStorage (fourni par Flask via request.files).
    Renvoie un dict décrivant le fichier, ou None si le fichier est vide
    ou trop lourd.
    """
    if not file_storage or not file_storage.filename:
        return None

    original = _safe_filename(file_storage.filename)
    # Lit le contenu pour vérifier la taille (Flask ne renseigne pas
    # toujours .content_length côté multipart).
    data = file_storage.read()
    if not data:
        return None
    if len(data) > MAX_UPLOAD_SIZE:
        print(f"[upload] Fichier ignoré ({original}) : {len(data)} octets > {MAX_UPLOAD_SIZE}")
        return None

    # Dossier par booking_id, pour retrouver facilement les fichiers d'une demande.
    dest_dir = UPLOAD_DIR / booking_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex[:8]}_{original}"
    dest_path = dest_dir / stored_name
    dest_path.write_bytes(data)

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO booking_files
           (booking_id, filename_original, filename_stored, mimetype, size_bytes, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            booking_id,
            original,
            stored_name,
            (file_storage.mimetype or "application/octet-stream"),
            len(data),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    conn.close()

    return {
        "filename_original": original,
        "filename_stored": stored_name,
        "size_bytes": len(data),
        "mimetype": file_storage.mimetype or "application/octet-stream",
    }


def list_booking_files(booking_id: str) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM booking_files WHERE booking_id = ? ORDER BY id ASC",
        (booking_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_booking_file_path(booking_id: str, filename_stored: str) -> Optional[Path]:
    """Renvoie le chemin absolu d'un fichier joint, ou None s'il n'existe pas
    (ou n'appartient pas à ce booking)."""
    filename_stored = _safe_filename(filename_stored)
    booking_id = _safe_filename(booking_id)
    path = UPLOAD_DIR / booking_id / filename_stored
    if not path.exists() or not path.is_file():
        return None
    # Sécurité : on vérifie que le chemin résolu reste sous UPLOAD_DIR.
    try:
        path.resolve().relative_to(UPLOAD_DIR.resolve())
    except ValueError:
        return None
    return path


def list_bookings(statut: str = None) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if statut:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE statut = ? ORDER BY created_at DESC",
            (statut,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM bookings ORDER BY created_at DESC"
        ).fetchall()
    conn.close()

    resultats = []
    for row in rows:
        booking_id = row["id"]
        resultats.append({
            "id": booking_id,
            "created_at": row["created_at"],
            "service": row["service"],
            "reponses": json.loads(row["reponses_json"]),
            "contact": json.loads(row["contact_json"]),
            "profil": row["profil"],
            "fourchette_prix": row["fourchette_prix"],
            "delai_estime": row["delai_estime"],
            "statut": row["statut"],
            "fichiers": list_booking_files(booking_id),
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
# 4. MESSAGES DE CONTACT DIRECT
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
# ---------------------------------------------------------------------------

def _email_configure() -> Optional[dict]:
    api_key = os.environ.get("RESEND_API_KEY")
    admin_email = os.environ.get("ADMIN_EMAIL")
    if not (api_key and admin_email):
        return None
    return {
        "api_key": api_key,
        "admin_email": admin_email,
        "from": os.environ.get("RESEND_FROM", "onboarding@resend.dev"),
    }


def send_email_notification(subject: str, body_text: str) -> bool:
    cfg = _email_configure()
    if not cfg:
        print("[email] Notifications désactivées (variables RESEND_API_KEY / ADMIN_EMAIL absentes).")
        return False
    try:
        resend.api_key = cfg["api_key"]
        html_body = "<pre style=\"font-family:inherit;white-space:pre-wrap;\">" + \
            body_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + \
            "</pre>"
        resend.Emails.send({
            "from": cfg["from"],
            "to": cfg["admin_email"],
            "subject": subject,
            "html": html_body,
        })
        return True
    except Exception as exc:
        print(f"[email] Échec de l'envoi de la notification : {exc}")
        return False


def notify_new_booking(booking_id: str, service: str, contact: dict, recommandation: dict,
                       fichiers: Optional[list] = None) -> None:
    label = SERVICES.get(service, {}).get("label", service)
    liste_fichiers = ""
    if fichiers:
        liste_fichiers = "\nFichiers joints :\n" + "\n".join(
            f"  - {f['filename_original']} ({f.get('size_bytes', 0)//1024} Ko)"
            for f in fichiers
        ) + "\n"
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
        f"Délai estimé       : {recommandation.get('delai_estime', '—')}\n"
        f"{liste_fichiers}\n"
        f"Message du client :\n{contact.get('message') or '(aucun)'}\n\n"
        f"Connecte-toi à ton espace admin pour voir tous les détails, "
        f"télécharger les fichiers et changer le statut de cette demande."
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
