# -*- coding: utf-8 -*-
from __future__ import annotations
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

Note structurelle importante :
Historiquement, un seul jeu de "questions communes" était concaténé à la
suite de toutes les questions spécifiques. Ce n'était pas cohérent :
demander "ambiance visuelle" ou "logo/charte" pour un montage PC n'a pas
de sens. Désormais chaque service définit sa propre queue de questions
communes via COMMON_TAIL_WEB, COMMON_TAIL_APP, COMMON_TAIL_PC, et
get_public_config() les injecte dans service["questions"]. Le champ
questions_communes global est renvoyé vide pour rester compatible avec
le front (qui concatène toujours service.questions + questions_communes).
"""

import copy
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import resend

logger = logging.getLogger("yc_digital.logic")

DB_PATH = Path(os.environ.get("YC_DIGITAL_DB_PATH", Path(__file__).parent / "bookings.db"))
UPLOAD_DIR = Path(os.environ.get("YC_DIGITAL_UPLOAD_DIR", Path(__file__).parent / "uploads"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Taille max acceptée par fichier joint (25 Mo).
MAX_UPLOAD_SIZE = 25 * 1024 * 1024
MAX_UPLOAD_FILES = 8

VALID_STATUSES = {"nouveau", "en_cours", "devis_envoye", "termine", "annule"}

# Fichiers autorisés côté client ET côté serveur : images sûres + PDF.
# Les formats Office/Excel, archives ZIP et fichiers exécutables sont refusés.
ALLOWED_UPLOAD_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".avif", ".pdf"
}
ALLOWED_UPLOAD_MIMES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
    "image/avif",
    "application/pdf",
}


class UploadValidationError(ValueError):
    """Erreur contrôlée quand un fichier joint n'est pas acceptable."""

# ---------------------------------------------------------------------------
# 1. STRUCTURE DU QUESTIONNAIRE
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
                # On ne fait ni iOS/macOS, ni PWA / application web.
                # Les prestations proposées se limitent à Android et Windows Desktop.
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
                # Liste resserrée : on retire les fonctionnalités qui sont
                # systématiquement du sur-mesure et qui n'ont pas leur place
                # dans un questionnaire standard (paiement intégré, back-office,
                # rôles/permissions, statistiques, géolocalisation, API externes).
                # Ces prestations sont proposées directement dans le devis.
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

# ---------------------------------------------------------------------------
# Questions communes injectées EN FIN de service. On distingue trois queues
# de questions selon la nature du projet, pour éviter d'imposer les mêmes
# questions à un site web et à un montage PC.
# ---------------------------------------------------------------------------

_Q_BUDGET = {
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
}

_Q_DELAI = {
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
}

_Q_CONTEXTE = {
    "id": "contexte",
    "label": "Décrivez votre projet en quelques mots (contexte, objectif, cible)",
    "type": "zone_texte",
    "required": False,
}

_Q_INSPIRATIONS = {
    "id": "inspirations",
    "label": "Avez-vous des références ou sites qui vous plaisent ? (liens, exemples)",
    "type": "zone_texte",
    "required": False,
}

_Q_AMBIANCE = {
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
    "type": "fichiers",
    "required": False,
}

# --- Version spécifique montage PC ---
# Pas de charte graphique, pas d'"ambiance visuelle" au sens web : on
# demande plutôt une esthétique de boîtier (sobre / RGB / etc.) qui a
# du sens pour un build. Les fichiers restent proposés pour permettre
# d'envoyer une photo du setup existant ou d'un boîtier de référence.
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
    "type": "zone_texte",
    "required": False,
}

_Q_FICHIERS_PC = {
    "id": "fichiers",
    "label": "Ajoutez si besoin une photo de votre setup actuel, une liste de composants ou une référence de boîtier",
    "type": "fichiers",
    "required": False,
}

# Queues par service.
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

# Conservé pour compatibilité avec d'anciens appels éventuels.
# Le front reçoit désormais une liste vide dans questions_communes :
# les questions communes sont injectées directement dans chaque service.
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
    """Renvoie la config du questionnaire, telle que consommée par le front.

    On construit une copie de SERVICES dans laquelle chaque service voit
    sa queue de questions communes concaténée à ses propres questions.
    Le front reste inchangé : il fait service.questions + questions_communes,
    et questions_communes est désormais vide.
    """
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
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    # Mode WAL : évite les erreurs "database is locked" quand plusieurs workers
    # gunicorn écrivent en concurrence sur la même base SQLite.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error as exc:
        logger.warning("Impossible d'activer le mode WAL SQLite : %s", exc)
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS public_rate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_public_rate_events_ip_endpoint_created
        ON public_rate_events(ip, endpoint, created_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_admin_login_attempts_ip_created
        ON admin_login_attempts(ip, created_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_ip_blocks (
            ip TEXT PRIMARY KEY,
            blocked_until REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def check_public_rate_limit(ip: str, endpoint: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Rate-limit persistant en SQLite : utile même après redémarrage du serveur."""
    now = time.time()
    cutoff = now - window_seconds
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("DELETE FROM public_rate_events WHERE created_at < ?", (cutoff,))
        rows = conn.execute(
            """SELECT created_at FROM public_rate_events
               WHERE ip = ? AND endpoint = ? AND created_at >= ?
               ORDER BY created_at ASC""",
            (ip, endpoint, cutoff),
        ).fetchall()
        if len(rows) >= limit:
            retry_after = max(1, int(window_seconds - (now - float(rows[0][0]))))
            conn.commit()
            return False, retry_after
        conn.execute(
            "INSERT INTO public_rate_events(ip, endpoint, created_at) VALUES (?, ?, ?)",
            (ip, endpoint, now),
        )
        conn.commit()
        return True, 0
    finally:
        conn.close()


def admin_block_remaining(ip: str) -> int:
    now = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        row = conn.execute(
            "SELECT blocked_until FROM admin_ip_blocks WHERE ip = ?",
            (ip,),
        ).fetchone()
        if not row:
            return 0
        blocked_until = float(row[0])
        if blocked_until > now:
            return max(1, int(blocked_until - now))
        conn.execute("DELETE FROM admin_ip_blocks WHERE ip = ?", (ip,))
        conn.execute("DELETE FROM admin_login_attempts WHERE ip = ?", (ip,))
        conn.commit()
        return 0
    finally:
        conn.close()


def admin_register_failure(ip: str, max_attempts: int, block_seconds: int) -> None:
    now = time.time()
    cutoff = now - block_seconds
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("DELETE FROM admin_login_attempts WHERE ip = ? AND created_at < ?", (ip, cutoff))
        conn.execute("INSERT INTO admin_login_attempts(ip, created_at) VALUES (?, ?)", (ip, now))
        count = conn.execute(
            "SELECT COUNT(*) FROM admin_login_attempts WHERE ip = ? AND created_at >= ?",
            (ip, cutoff),
        ).fetchone()[0]
        if count >= max_attempts:
            conn.execute(
                """INSERT INTO admin_ip_blocks(ip, blocked_until, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(ip) DO UPDATE SET
                       blocked_until = excluded.blocked_until,
                       updated_at = excluded.updated_at""",
                (ip, now + block_seconds, now),
            )
        conn.commit()
    finally:
        conn.close()


def admin_register_success(ip: str) -> None:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("DELETE FROM admin_login_attempts WHERE ip = ?", (ip,))
        conn.execute("DELETE FROM admin_ip_blocks WHERE ip = ?", (ip,))
        conn.commit()
    finally:
        conn.close()


def admin_remaining_attempts(ip: str, max_attempts: int, block_seconds: int) -> int:
    now = time.time()
    cutoff = now - block_seconds
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM admin_login_attempts WHERE ip = ? AND created_at >= ?",
            (ip, cutoff),
        ).fetchone()[0]
        return max(0, max_attempts - int(count))
    finally:
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
    name = os.path.basename(name or "fichier")
    name = name.replace("\x00", "")
    keep = "-_.() "
    cleaned = "".join(c for c in name if c.isalnum() or c in keep).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:120]
    return cleaned or "fichier"


def _file_extension(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def _sniff_mime(data: bytes, fallback: str = "") -> str:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"heic", b"heix", b"hevc", b"hevx", b"mif1"}:
        return "image/heic"
    return (fallback or "application/octet-stream").split(";", 1)[0].lower()


def validate_uploaded_files(files: list) -> tuple[bool, str]:
    files = [f for f in (files or []) if f and f.filename]
    if len(files) > MAX_UPLOAD_FILES:
        return False, f"Vous pouvez joindre {MAX_UPLOAD_FILES} fichiers maximum."
    for file_storage in files:
        name = _safe_filename(file_storage.filename)
        ext = _file_extension(name)
        if ext not in ALLOWED_UPLOAD_EXTENSIONS:
            return False, (
                f"Fichier refusé ({name}). Formats autorisés : images JPG, PNG, WebP, GIF, HEIC, AVIF et PDF."
            )
        try:
            data = file_storage.read()
            _validate_upload_content(name, data, file_storage.mimetype or "application/octet-stream")
        except UploadValidationError as exc:
            return False, str(exc)
        finally:
            try:
                file_storage.stream.seek(0)
            except Exception:
                pass
    return True, ""


def _validate_upload_content(original: str, data: bytes, browser_mime: str) -> str:
    if not data:
        raise UploadValidationError(f"Le fichier {original} est vide.")
    if len(data) > MAX_UPLOAD_SIZE:
        raise UploadValidationError(f"Le fichier {original} dépasse la limite de 25 Mo.")

    ext = _file_extension(original)
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise UploadValidationError(
            f"Fichier refusé ({original}). Formats autorisés : images JPG, PNG, WebP, GIF, HEIC, AVIF et PDF."
        )

    detected = _sniff_mime(data, browser_mime)
    if detected not in ALLOWED_UPLOAD_MIMES:
        raise UploadValidationError(
            f"Fichier refusé ({original}). Le contenu ne correspond pas à une image ou un PDF autorisé."
        )

    if ext == ".pdf" and detected != "application/pdf":
        raise UploadValidationError(f"Fichier refusé ({original}). Le PDF semble invalide.")
    if ext in {".jpg", ".jpeg"} and detected != "image/jpeg":
        raise UploadValidationError(f"Fichier refusé ({original}). L'image JPG semble invalide.")
    if ext == ".png" and detected != "image/png":
        raise UploadValidationError(f"Fichier refusé ({original}). L'image PNG semble invalide.")
    if ext == ".gif" and detected != "image/gif":
        raise UploadValidationError(f"Fichier refusé ({original}). L'image GIF semble invalide.")
    if ext == ".webp" and detected != "image/webp":
        raise UploadValidationError(f"Fichier refusé ({original}). L'image WebP semble invalide.")
    if ext == ".avif" and detected != "image/avif":
        raise UploadValidationError(f"Fichier refusé ({original}). L'image AVIF semble invalide.")
    if ext in {".heic", ".heif"} and detected not in {"image/heic", "image/heif"}:
        raise UploadValidationError(f"Fichier refusé ({original}). L'image HEIC/HEIF semble invalide.")

    return detected


def save_booking_file(booking_id: str, file_storage) -> Optional[dict]:
    if not file_storage or not file_storage.filename:
        return None

    original = _safe_filename(file_storage.filename)
    data = file_storage.read()
    detected_mime = _validate_upload_content(
        original,
        data,
        file_storage.mimetype or "application/octet-stream",
    )

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
            detected_mime,
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
        "mimetype": detected_mime,
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
    if not api_key:
        logger.warning("RESEND_API_KEY absente — notifications désactivées.")
        return None
    if not admin_email:
        logger.warning("ADMIN_EMAIL absente — notifications désactivées.")
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
        logger.info(
            "Email envoyé — sujet=%r to=%s from=%s response=%s",
            subject, cfg["admin_email"], cfg["from"], response,
        )
        return True
    except Exception as exc:
        logger.error(
            "Échec envoi email — sujet=%r to=%s from=%s erreur=%s: %s",
            subject, cfg["admin_email"], cfg["from"], type(exc).__name__, exc,
        )
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
