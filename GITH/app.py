# -*- coding: utf-8 -*-
"""
logic.py
--------
Toute la "logique métier" de l'application vit ici, en Python pur :
  - la structure du questionnaire (facile à modifier sans toucher au HTML)
  - le moteur de recommandation
  - le stockage des demandes (SQLite en mode WAL — meilleure concurrence)
  - la gestion des fichiers joints (whitelist d'extensions/MIME)
  - la validation des entrées (format email, longueurs)
  - la purge RGPD des demandes anciennes
  - la suppression sur demande (droit à l'effacement)
  - les notifications e-mail (Resend)

L'API publique de ce module n'a pas changé : app.py continue d'appeler
les mêmes fonctions qu'avant. Les ajouts sont additifs.
"""

import copy
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import resend

logger = logging.getLogger("ycdigital.logic")

DB_PATH = Path(__file__).parent / "bookings.db"
UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Limites & validation d'entrée
# ---------------------------------------------------------------------------

# Taille max acceptée par fichier joint (25 Mo).
MAX_UPLOAD_SIZE = 25 * 1024 * 1024

# Longueurs max pour les champs texte libres — évite qu'un attaquant
# n'envoie plusieurs Mo de texte et sature la base.
MAX_LEN_NAME = 120
MAX_LEN_EMAIL = 254           # RFC 5321
MAX_LEN_PHONE = 40
MAX_LEN_CITY = 120
MAX_LEN_SHORT_TEXT = 500
MAX_LEN_LONG_TEXT = 5_000     # message, contexte, inspirations
MAX_ANSWER_VALUES = 30        # nb max d'options cochées par question
MAX_ANSWERS_KEYS = 40         # nb max de questions par soumission

# Whitelist d'extensions autorisées pour les pièces jointes.
# On accepte uniquement des types cohérents avec les prestations :
# images, PDF, documents bureautiques usuels, texte brut, archives légères.
ALLOWED_UPLOAD_EXTS = {
    # images
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".svg",
    # documents
    ".pdf", ".txt", ".md", ".rtf",
    ".doc", ".docx", ".odt",
    ".xls", ".xlsx", ".ods", ".csv",
    ".ppt", ".pptx", ".odp",
    # archives (cahier des charges zippé, maquettes)
    ".zip",
}

# Whitelist MIME correspondante (préfixes acceptés).
ALLOWED_MIME_PREFIXES = (
    "image/",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument",
    "application/vnd.oasis.opendocument",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/zip",
    "application/x-zip-compressed",
    "text/",
)

# Extensions strictement bloquées, même si l'utilisateur essaie de bidouiller.
BLOCKED_UPLOAD_EXTS = {
    ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".ps1", ".sh",
    ".php", ".phtml", ".phar", ".jsp", ".asp", ".aspx",
    ".js", ".mjs", ".vbs", ".jar", ".apk", ".dll", ".so",
    ".html", ".htm", ".xhtml",
}

# Rétention RGPD : les demandes non "gagne" plus vieilles que N jours sont
# purgées automatiquement. Configurable par ENV pour rester souple.
RETENTION_DAYS = int(os.environ.get("RGPD_RETENTION_DAYS", "365"))

# Regex e-mail volontairement pragmatique : couvre la quasi-totalité des
# adresses réelles sans être une usine à gaz. On complète avec une limite
# de longueur explicite.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
)


def is_valid_email(value: str) -> bool:
    if not value or not isinstance(value, str):
        return False
    v = value.strip()
    if len(v) > MAX_LEN_EMAIL:
        return False
    return bool(_EMAIL_RE.match(v))


def _clip(value, max_len: int) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return s[:max_len]


def sanitize_contact(contact: dict) -> dict:
    """Nettoie et borne les champs du bloc contact."""
    contact = contact or {}
    return {
        "nom": _clip(contact.get("nom"), MAX_LEN_NAME),
        "email": _clip(contact.get("email"), MAX_LEN_EMAIL),
        "telephone": _clip(contact.get("telephone"), MAX_LEN_PHONE),
        "ville": _clip(contact.get("ville"), MAX_LEN_CITY),
        "disponibilite": _clip(contact.get("disponibilite"), MAX_LEN_SHORT_TEXT),
        "message": _clip(contact.get("message"), MAX_LEN_LONG_TEXT),
    }


def sanitize_reponses(reponses: dict) -> dict:
    """Nettoie et borne le dictionnaire de réponses au questionnaire.

    - clés limitées en nombre et en longueur
    - valeurs texte tronquées à MAX_LEN_LONG_TEXT
    - listes tronquées à MAX_ANSWER_VALUES et chaque élément à MAX_LEN_SHORT_TEXT
    - autres types ignorés
    """
    reponses = reponses or {}
    out = {}
    for i, (k, v) in enumerate(reponses.items()):
        if i >= MAX_ANSWERS_KEYS:
            break
        key = _clip(k, 80)
        if not key:
            continue
        if isinstance(v, list):
            out[key] = [_clip(x, MAX_LEN_SHORT_TEXT) for x in v[:MAX_ANSWER_VALUES]]
        elif isinstance(v, (str, int, float, bool)):
            out[key] = _clip(v, MAX_LEN_LONG_TEXT)
        # dict / objets imbriqués : ignorés (le front n'en envoie pas)
    return out


def file_is_allowed(filename: str, mimetype: str) -> bool:
    """Vrai si le fichier est acceptable (whitelist ext + MIME)."""
    if not filename:
        return False
    ext = os.path.splitext(filename)[1].lower()
    if ext in BLOCKED_UPLOAD_EXTS:
        return False
    if ext not in ALLOWED_UPLOAD_EXTS:
        return False
    mt = (mimetype or "").lower()
    # Autoriser MIME générique (certains navigateurs envoient
    # application/octet-stream), mais uniquement si l'extension est whitelistée.
    if mt in ("", "application/octet-stream"):
        return True
    return any(mt.startswith(p) for p in ALLOWED_MIME_PREFIXES)


# ---------------------------------------------------------------------------
# 1. STRUCTURE DU QUESTIONNAIRE  (inchangée)
# ---------------------------------------------------------------------------

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
        "description": "Applications Android et Windows Desktop, sur-mesure.",
        "icone": "app",
        "questions": [
            {
                "id": "plateforme",
                "label": "Sur quelle(s) plateforme(s) ?",
                "type": "choix_multiple",
                "options": [
                    "Android",
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
                    "Mode hors-ligne",
                    "Chat / messagerie interne",
                    "Upload / gestion de fichiers",
                    "Génération de PDF / documents",
                    "Import / export de données (CSV, Excel)",
                    "Recherche avancée / filtres",
                    "Multilingue",
                    "Notifications par e-mail",
                ],
                "required": False,
            },
            {
                "id": "existant_app",
                "label": "Où en êtes-vous dans votre projet ?",
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

_Q_BUDGET = {
    "id": "budget",
    "label": "Quel budget envisagez-vous ?",
    "type": "select",
    "options": [
        "Moins de 300 €", "300 € - 800 €", "800 € - 2000 €",
        "2000 € - 5000 €", "Plus de 5000 €", "Je ne sais pas encore",
    ],
    "required": True,
}
_Q_DELAI = {
    "id": "delai",
    "label": "Dans quel délai souhaitez-vous être livré ?",
    "type": "choix_unique",
    "options": [
        "Urgent — dès que possible", "Sous 1 à 2 semaines",
        "Sous 2 à 3 semaines", "Pas de contrainte particulière",
    ],
    "required": True,
}
_Q_CONTEXTE = {
    "id": "contexte",
    "label": "Décrivez votre projet en quelques mots (contexte, objectif, cible)",
    "type": "zone_texte", "required": False,
}
_Q_INSPIRATIONS = {
    "id": "inspirations",
    "label": "Avez-vous des références ou sites qui vous plaisent ? (liens, exemples)",
    "type": "zone_texte", "required": False,
}
_Q_AMBIANCE = {
    "id": "ambiance_visuelle",
    "label": "Quelle ambiance visuelle imaginez-vous ?",
    "type": "choix_multiple",
    "options": [
        "Épurée / minimaliste", "Chaleureuse / naturelle",
        "Élégante / haut de gamme", "Dynamique / colorée",
        "Technique / corporate", "Créative / artistique",
        "Je ne sais pas encore — orientez-moi",
    ],
    "required": False,
}
_Q_CHARTE = {
    "id": "charte_existante",
    "label": "Avez-vous déjà un logo ou une charte graphique ?",
    "type": "choix_unique",
    "options": [
        "Oui, tout est prêt (logo + couleurs + typos)",
        "J'ai seulement un logo",
        "Non, rien pour le moment",
    ],
    "required": False,
}
_Q_FICHIERS = {
    "id": "fichiers",
    "label": "Ajoutez tout document utile (cahier des charges, maquettes, logo, photos, exemples…)",
    "type": "fichiers", "required": False,
}
_Q_ESTHETIQUE_PC = {
    "id": "esthetique_pc",
    "label": "Quelle esthétique souhaitez-vous pour la machine ?",
    "type": "choix_unique",
    "options": [
        "Sobre / discrète (aucun éclairage)",
        "Sobre avec un peu de LED (blanc ou une seule couleur)",
        "RGB modéré (quelques ventilateurs / barrettes)",
        "RGB assumé (setup gaming complet)",
        "Format compact / mini-ITX",
        "Peu importe — orientez-moi",
    ],
    "required": False,
}
_Q_CONTEXTE_PC = {
    "id": "contexte",
    "label": "Détails complémentaires (jeux / logiciels visés, résolution d'écran, configuration actuelle…)",
    "type": "zone_texte", "required": False,
}
_Q_FICHIERS_PC = {
    "id": "fichiers",
    "label": "Ajoutez si besoin une photo de votre setup actuel, une liste de composants ou une référence de boîtier",
    "type": "fichiers", "required": False,
}

COMMON_TAIL_WEB = [_Q_BUDGET, _Q_DELAI, _Q_CONTEXTE, _Q_INSPIRATIONS,
                   _Q_AMBIANCE, _Q_CHARTE, _Q_FICHIERS]
COMMON_TAIL_APP = [_Q_BUDGET, _Q_DELAI, _Q_CONTEXTE, _Q_INSPIRATIONS,
                   _Q_AMBIANCE, _Q_CHARTE, _Q_FICHIERS]
COMMON_TAIL_PC = [_Q_BUDGET, _Q_DELAI, _Q_CONTEXTE_PC,
                  _Q_ESTHETIQUE_PC, _Q_FICHIERS_PC]

COMMON_TAILS = {
    "site_web": COMMON_TAIL_WEB,
    "application": COMMON_TAIL_APP,
    "montage_pc": COMMON_TAIL_PC,
}
QUESTIONS_COMMUNES = []

CONTACT_FIELDS = [
    {"id": "nom", "label": "Nom complet", "type": "texte", "required": True},
    {"id": "email", "label": "Adresse e-mail", "type": "texte", "required": True},
    {"id": "telephone", "label": "Téléphone", "type": "texte", "required": False},
    {"id": "ville", "label": "Ville", "type": "texte", "required": False},
    {"id": "disponibilite", "label": "Meilleur moment pour vous contacter", "type": "texte", "required": False},
    {"id": "message", "label": "Un détail à ajouter ? (optionnel)", "type": "zone_texte", "required": False},
]


def get_public_config():
    services_out = {}
    for key, svc in SERVICES.items():
        svc_copy = copy.deepcopy(svc)
        tail = COMMON_TAILS.get(key, COMMON_TAIL_WEB)
        svc_copy["questions"] = list(svc_copy.get("questions", [])) + copy.deepcopy(tail)
        services_out[key] = svc_copy
    return {
        "services": services_out,
        "questions_communes": [],
        "contact_fields": CONTACT_FIELDS,
    }


# ---------------------------------------------------------------------------
# 2. MOTEUR DE RECOMMANDATION  (inchangé)
# ---------------------------------------------------------------------------

BUDGET_SCORE = {
    "Moins de 300 €": 1, "300 € - 800 €": 2, "800 € - 2000 €": 3,
    "2000 € - 5000 €": 4, "Plus de 5000 €": 5, "Je ne sais pas encore": 2,
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
# 3. STOCKAGE (SQLite, mode WAL pour meilleure concurrence)
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    """Ouvre une connexion SQLite avec un timeout raisonnable.

    Le timeout laisse à la connexion 30 s pour attendre la libération d'un
    verrou d'écriture concurrent avant de lever 'database is locked'.
    Le mode WAL est activé une seule fois par init_db().
    """
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    # Foreign keys ne sont pas activées par défaut en SQLite.
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    conn = _connect()
    # WAL = concurrence lecture/écriture nettement meilleure sur SQLite.
    # NORMAL = bon compromis durabilité / performance en WAL.
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
    except sqlite3.Error as exc:
        logger.warning("Impossible d'activer WAL sur SQLite : %s", exc)

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
    conn = _connect()
    try:
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
    finally:
        conn.close()
    return booking_id


def _safe_filename(name: str) -> str:
    name = os.path.basename(name or "fichier")
    keep = "-_.() "
    cleaned = "".join(c for c in name if c.isalnum() or c in keep).strip()
    return cleaned or "fichier"


def save_booking_file(booking_id: str, file_storage) -> Optional[dict]:
    if not file_storage or not file_storage.filename:
        return None

    original = _safe_filename(file_storage.filename)
    mimetype = (file_storage.mimetype or "application/octet-stream")

    if not file_is_allowed(original, mimetype):
        logger.warning(
            "Fichier refusé (type non autorisé) booking=%s name=%r mime=%r",
            booking_id, original, mimetype,
        )
        return None

    data = file_storage.read()
    if not data:
        return None
    if len(data) > MAX_UPLOAD_SIZE:
        logger.warning(
            "Fichier ignoré (%s) : %d octets > %d",
            original, len(data), MAX_UPLOAD_SIZE,
        )
        return None

    dest_dir = UPLOAD_DIR / booking_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex[:8]}_{original}"
    dest_path = dest_dir / stored_name
    try:
        dest_path.write_bytes(data)
    except OSError as exc:
        logger.error("Écriture fichier joint impossible (%s) : %s", dest_path, exc)
        return None

    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO booking_files
               (booking_id, filename_original, filename_stored, mimetype, size_bytes, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                booking_id, original, stored_name, mimetype, len(data),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "filename_original": original,
        "filename_stored": stored_name,
        "size_bytes": len(data),
        "mimetype": mimetype,
    }


def list_booking_files(booking_id: str) -> list:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM booking_files WHERE booking_id = ? ORDER BY id ASC",
            (booking_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_booking_file_path(booking_id: str, filename_stored: str) -> Optional[Path]:
    filename_stored = _safe_filename(filename_stored)
    booking_id = _safe_filename(booking_id)
    path = UPLOAD_DIR / booking_id / filename_stored
    if not path.exists() or not path.is_file():
        return None
    try:
        path.resolve().relative_to(UPLOAD_DIR.resolve())
    except ValueError:
        return None
    return path


def list_bookings(statut: str = None) -> list:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        if statut:
            rows = conn.execute(
                "SELECT * FROM bookings WHERE statut = ? ORDER BY created_at DESC",
                (statut,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bookings ORDER BY created_at DESC"
            ).fetchall()
    finally:
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
    # Whitelist des statuts pour éviter qu'on stocke n'importe quoi.
    allowed = {"nouveau", "en_cours", "gagne", "perdu", "archive"}
    if statut not in allowed:
        return False
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE bookings SET statut = ? WHERE id = ?",
            (statut, booking_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. MESSAGES DE CONTACT DIRECT
# ---------------------------------------------------------------------------

def save_contact_message(nom: str, email: str, message: str) -> str:
    message_id = str(uuid.uuid4())[:8]
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO messages (id, created_at, nom, email, message, statut) VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, datetime.now().isoformat(timespec="seconds"),
             nom, email, message, "nouveau"),
        )
        conn.commit()
    finally:
        conn.close()
    return message_id


def list_contact_messages() -> list:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM messages ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 5. RGPD — purge automatique + droit à l'effacement
# ---------------------------------------------------------------------------

def _delete_booking_files_from_disk(booking_id: str) -> None:
    dossier = UPLOAD_DIR / _safe_filename(booking_id)
    if not dossier.exists():
        return
    try:
        for f in dossier.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        dossier.rmdir()
    except OSError as exc:
        logger.warning("Nettoyage disque partiel pour %s : %s", booking_id, exc)


def delete_booking(booking_id: str) -> bool:
    """Supprime définitivement une demande + ses fichiers (droit à l'effacement)."""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        conn.execute("DELETE FROM booking_files WHERE booking_id = ?", (booking_id,))
        conn.commit()
        deleted = cur.rowcount > 0
    finally:
        conn.close()
    if deleted:
        _delete_booking_files_from_disk(booking_id)
    return deleted


def delete_by_email(email: str) -> dict:
    """Efface toutes les demandes et messages associés à un e-mail.

    Utilisé pour honorer une demande de droit à l'effacement (RGPD art. 17).
    Retourne un compte-rendu {bookings: n, messages: n}.
    """
    if not is_valid_email(email):
        return {"bookings": 0, "messages": 0, "erreur": "email invalide"}

    email = email.strip().lower()
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        # Les bookings stockent le contact en JSON — on récupère les ids
        # correspondant à l'email en Python (volume attendu très faible).
        rows = conn.execute("SELECT id, contact_json FROM bookings").fetchall()
        to_delete = []
        for r in rows:
            try:
                c = json.loads(r["contact_json"]) or {}
            except (TypeError, ValueError):
                continue
            if (c.get("email") or "").strip().lower() == email:
                to_delete.append(r["id"])

        for bid in to_delete:
            conn.execute("DELETE FROM booking_files WHERE booking_id = ?", (bid,))
            conn.execute("DELETE FROM bookings WHERE id = ?", (bid,))

        cur_msg = conn.execute(
            "DELETE FROM messages WHERE LOWER(email) = ?", (email,)
        )
        nb_msg = cur_msg.rowcount
        conn.commit()
    finally:
        conn.close()

    for bid in to_delete:
        _delete_booking_files_from_disk(bid)

    logger.info(
        "RGPD: suppression email=%s bookings=%d messages=%d",
        email, len(to_delete), nb_msg,
    )
    return {"bookings": len(to_delete), "messages": nb_msg}


def purge_old_data(retention_days: int = None) -> dict:
    """Purge les demandes et messages plus vieux que N jours.

    On ne purge PAS les demandes marquées 'gagne' (dossier client actif).
    À appeler périodiquement (au démarrage de l'app et/ou via cron).
    """
    days = retention_days if retention_days is not None else RETENTION_DAYS
    if days <= 0:
        return {"bookings": 0, "messages": 0}

    seuil = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        # Bookings à purger (sauf 'gagne')
        rows = conn.execute(
            "SELECT id FROM bookings WHERE created_at < ? AND statut != 'gagne'",
            (seuil,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        for bid in ids:
            conn.execute("DELETE FROM booking_files WHERE booking_id = ?", (bid,))
            conn.execute("DELETE FROM bookings WHERE id = ?", (bid,))

        cur = conn.execute("DELETE FROM messages WHERE created_at < ?", (seuil,))
        nb_msg = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    for bid in ids:
        _delete_booking_files_from_disk(bid)

    if ids or nb_msg:
        logger.info(
            "Purge RGPD (>%d j) : %d bookings, %d messages",
            days, len(ids), nb_msg,
        )
    return {"bookings": len(ids), "messages": nb_msg}


# ---------------------------------------------------------------------------
# 6. NOTIFICATIONS PAR E-MAIL (Resend)
# ---------------------------------------------------------------------------

def _email_configure() -> Optional[dict]:
    api_key = os.environ.get("RESEND_API_KEY")
    admin_email = os.environ.get("ADMIN_EMAIL")
    if not api_key:
        logger.info("RESEND_API_KEY absente — notifications désactivées.")
        return None
    if not admin_email:
        logger.info("ADMIN_EMAIL absente — notifications désactivées.")
        return None
    return {
        "api_key": api_key,
        "admin_email": admin_email,
        "from": os.environ.get("RESEND_FROM", "onboarding@resend.dev"),
    }


def send_email_notification(subject: str, body_text: str) -> bool:
    cfg = _email_configure()
    if not cfg:
        return False

    resend.api_key = cfg["api_key"]
    html_body = (
        "<pre style=\"font-family:Arial,sans-serif;white-space:pre-wrap;"
        "font-size:14px;line-height:1.5;\">"
        + body_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        + "</pre>"
    )
    payload = {
        "from": cfg["from"],
        "to": cfg["admin_email"],
        "subject": subject,
        "html": html_body,
    }
    try:
        response = resend.Emails.send(payload)
        logger.info("Email envoyé — sujet=%r to=%s response=%s",
                    subject, cfg["admin_email"], response)
        return True
    except Exception as exc:
        logger.exception("Échec envoi email — sujet=%r erreur=%s: %s",
                         subject, type(exc).__name__, exc)
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
        f"consulter les photos jointes et changer le statut de cette demande."
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
