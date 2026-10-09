# Déploiement YC DIGITAL

Version préparée le 9 octobre 2026. Dépôt : AstonDiv1/YC, branche main, application dans GITH.

## Première mise en ligne gratuite

Le service Flask fonctionne avec Gunicorn, région Francfort, un worker et deux threads. `CONTACT_EMAIL_ONLY=1` affiche l'adresse de contact et un lien vers la messagerie du visiteur. Aucun message ou devis n'est accepté par les API de stockage dans ce mode, et l'administration est désactivée. Le site ne promet donc pas un enregistrement qui serait perdu après un redémarrage.

Variables : `PYTHON_VERSION=3.12.10`, `SESSION_COOKIE_SECURE=1`, `CONTACT_EMAIL_ONLY=1`, `ADMIN_EMAIL=yc.digital33@gmail.com`, `LOG_LEVEL=INFO`. `SECRET_KEY` est générée hors du code et reste secrète. Aucun mot de passe ni clé Resend ne doit être ajouté à GitHub.

Le service gratuit peut se mettre en veille après une période d'inactivité ; le premier chargement peut être lent. Le tarif gratuit ne dispense pas de surveiller les quotas du compte Render. Source : https://render.com/docs/free

## Activer ensuite les formulaires persistants

Choisir un service payant compatible avec les disques, puis attacher un disque de 1 Go monté sur `/var/data`. Confirmer son prix dans Render avant création ou changement de plan. Le connecteur de création actuel ne propose pas d'option de disque : utiliser le Dashboard ou un Blueprint.

Configurer :

| Variable | Valeur / action |
|---|---|
| YC_DIGITAL_DB_PATH | /var/data/bookings.db |
| YC_DIGITAL_UPLOAD_DIR | /var/data/uploads |
| ADMIN_PASSWORD_HASH | Hash Werkzeug d'un mot de passe fort choisi par les associés ; jamais le défaut historique |
| SECRET_KEY | Conserver la clé stable existante |
| SESSION_COOKIE_SECURE | 1 |
| RESEND_API_KEY | Clé fournie par le propriétaire dans Render, jamais dans un message public |
| RESEND_FROM | Adresse d'un domaine vérifié dans Resend ; sinon vérifier les restrictions du domaine de test |
| ADMIN_EMAIL | yc.digital33@gmail.com |
| CONTACT_EMAIL_ONLY | 0 seulement après configuration du disque et test des notifications |

Tester une demande identifiée comme test, vérifier son apparition dans l'administration et la réception de la notification. Redéployer et vérifier que la demande demeure. Mettre en place une sauvegarde cohérente de SQLite et des pièces jointes ; un disque persistant seul n'est pas une sauvegarde.

## Vérification

`/healthz`, `/`, `/api/config`, `/conditions-utilisation` et les images doivent répondre en HTTP 200. Les API admin doivent refuser les visiteurs non authentifiés. En mode e-mail, `/api/contact` et `/api/submit` renvoient volontairement 503 et la page ne présente aucun formulaire de stockage.

Tests locaux : `python -m unittest discover -s GITH -p test_audit.py -v`, avec les dépendances de `GITH/requirements.txt`. Les tests utilisent une base temporaire et simulent les notifications sans envoyer d'e-mail.

## À compléter avant prospection

Identité légale des associés ou de l'entreprise, statut, immatriculation applicable, adresse professionnelle et coordonnées complètes de l'hébergeur. Ne pas récupérer ces informations chez une société homonyme « YC DIGITAL ». Ajouter des démonstrations présentées honnêtement comme telles, puis définir le périmètre exact des prix de départ et le régime de TVA.
