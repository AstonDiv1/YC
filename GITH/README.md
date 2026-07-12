# Site vitrine + questionnaire de devis

## Structure
- `app.py` → serveur Flask (routes uniquement, aucune logique métier dedans)
- `logic.py` → **toute la logique** : questions du questionnaire, moteur de
  recommandation (profil / prix / délai), stockage SQLite
- `templates/index.html` → **un seul fichier** HTML + CSS + JS (comme demandé)
- `static/img/` → dépose ton logo ici (ex: `static/img/logo.png`)
- `bookings.db` → créée automatiquement au premier lancement, contient toutes
  les demandes reçues

## Lancer le site en local
```bash
pip install -r requirements.txt
python app.py
```
Puis ouvrir http://127.0.0.1:5000

## Ajouter ton logo
Dans `templates/index.html`, remplace le bloc :
```html
<div class="logo-placeholder"> ... </div>
```
par :
```html
<img src="/static/img/logo.png" alt="Logo" style="height:36px;">
```

## Modifier le questionnaire
Tout se passe dans `logic.py`, dictionnaire `SERVICES` : ajoute, retire ou
modifie une question (label, type, options) sans toucher au HTML — le site
s'adapte automatiquement. Types disponibles : `choix_unique`,
`choix_multiple`, `select`, `texte`, `zone_texte`.

## Ajuster le moteur de recommandation
Fonction `compute_recommendation()` dans `logic.py` : c'est là que se calcule
le profil (Starter / Pro / Sur-mesure), la fourchette de prix et le délai
estimé, en fonction du budget et du nombre de fonctionnalités cochées.
Adapte les seuils et libellés à tes propres tarifs.

## Espace admin (interface web + notifications e-mail)
Une vraie interface web protégée par mot de passe est disponible à l'adresse
`/admin` (ex: `http://127.0.0.1:5000/admin`, ou `https://ton-domaine.fr/admin`
une fois en ligne — accessible depuis n'importe où, pas besoin d'être sur le
même réseau que le serveur).

**Se connecter** : va sur `/admin`, tu seras redirigé vers `/admin/login`.
Mot de passe par défaut : `change-moi` — **à changer avant la mise en ligne**
en définissant la variable d'environnement `ADMIN_PASSWORD` :
```bash
export ADMIN_PASSWORD="ton_mot_de_passe_solide"
```
(sur Render/Railway/PythonAnywhere : à ajouter dans les "Environment
Variables" du service, pas dans le code)

Depuis le tableau de bord tu peux : voir toutes les demandes de devis et
messages de contact, consulter le détail de chaque demande (réponses au
questionnaire, coordonnées), et changer le statut d'une demande (nouveau /
en cours / devis envoyé / terminé / annulé) en un clic.

**Autres variables utiles :**
- `SECRET_KEY` : clé de signature des sessions. Sans elle, tout le monde est
  déconnecté à chaque redémarrage du serveur (sans danger, juste pénible).
  Génère-en une avec `python -c "import secrets; print(secrets.token_hex(32))"`.
- `ADMIN_TOKEN` : optionnel, permet d'accéder à `/api/bookings?token=...` et
  `/api/messages?token=...` sans passer par le formulaire de connexion
  (utile pour un script externe). Laisse vide pour désactiver.

## Recevoir un e-mail automatique à chaque nouvelle demande
En plus de l'interface `/admin`, tu peux recevoir un e-mail dès qu'un client
envoie une demande de devis ou un message de contact. Définis ces variables
d'environnement (avec une adresse Gmail par exemple, en utilisant un "mot de
passe d'application" plutôt que ton mot de passe normal) :
```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="ton.adresse@gmail.com"
export SMTP_PASSWORD="ton_mot_de_passe_application"
export ADMIN_EMAIL="ou.tu.veux.recevoir@gmail.com"
```
Si ces variables ne sont pas définies, les e-mails sont simplement
désactivés (aucune erreur, le site continue de fonctionner normalement) —
tu peux donc les ajouter plus tard sans rien casser.

## Déploiement
Fonctionne tel quel sur Render, Railway, PythonAnywhere ou un VPS avec
gunicorn (`gunicorn app:app`).
