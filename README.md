# Garmin Venu Health MVP (Hackathon)

MVP démontrable en temps réel:
- Ingestion FC en live via BLE (Broadcast Heart Rate)
- Fallback simulateur réaliste (aucun blocage en démo)
- Traitement fenêtres glissantes + indicateurs dérivés
- Dashboard web responsive : indicateurs en cartes, graphique local et mesures temps réel via WebSocket ou HTTP automatique
- Pré-triage médical assisté par un modèle Ollama local, avec repli déterministe

## Démarrage rapide

1) Créer/activer un environnement Python
2) Installer les dépendances:

```bash
pip install -r requirements.txt
```

3) Lancer l'app:

```bash
uvicorn app.main:app --reload --port 8000
```

4) Ouvrir:

- http://127.0.0.1:8000

## Pré-triage Ollama local

Installer et démarrer Ollama, puis télécharger un modèle:

```bash
ollama pull llama3.1
ollama serve
```

L'application utilise par défaut `http://127.0.0.1:11434` et le modèle `llama3.1`.
Pour changer ces valeurs:

```bash
set OLLAMA_BASE_URL=http://127.0.0.1:11434
set OLLAMA_MODEL=llama3.1
```

L'analyse reçoit automatiquement un instantané des dernières mesures du serveur : FC, SpO₂, température, fréquence respiratoire et tension lorsqu'elles sont disponibles, ainsi que l'activité, la RMSSD, le stress, les tendances de FC et les alertes. La source et l'heure sont affichées dans la fiche. Les données simulées sont explicitement identifiées.

Les mesures de plus de 10 secondes ou provenant d'une ancienne source sont exclues. Une température saisie manuellement est prioritaire sur celle du flux. Les constantes affichées restent celles de l'instantané, même si le modèle en invente d'autres. Les appels à Ollama ne bloquent pas la réception des mesures.

La fiche affiche les critères vérifiés et une action cohérente avec la priorité retenue. L'explication et l'orientation proposées librement par le modèle sont disponibles séparément dans une section à vérifier par l'IAO.

Sur une machine sans accélération GPU, le premier appel peut être lent. Le délai serveur vaut 90 secondes par défaut et peut être configuré avec `OLLAMA_TIMEOUT_SECONDS` (5 à 300 secondes). Un échec ou une réponse JSON invalide utilise les règles locales ; la fiche précise le moteur effectivement utilisé.

Le résultat apparaît dans une fiche de pré-triage avec une priorité P1 à P4, le motif, les symptômes, la douleur EVA, les signes d'alerte, les constantes et l'action recommandée. Les constantes absentes sont indiquées comme non renseignées. Le JSON reste accessible dans les détails repliables de la fiche.

La fiche doit être validée par l'IAO. Une red flag positive force toujours P1 côté serveur, même si le modèle répond mal ou si Ollama est indisponible.

Des garde-fous sur les constantes empêchent le modèle de diminuer la priorité minimale du prototype. Les bandes d'alerte physiologiques se réfèrent à la [grille NEWS2 du Royal College of Physicians](https://rcp.ac.uk/media/alxev00t/news2-chart-1_the-news-scoring-system_0_0.pdf). La correspondance P1–P4 est une règle de démonstration à valider cliniquement ; elle ne constitue pas un score NEWS2 complet. L'âge, l'état de conscience, l'oxygénothérapie et les valeurs habituelles du patient ne sont pas évalués.

Accès direct au pré-triage : `http://127.0.0.1:8000/#triage`.

## Alerte P1 et contact d’urgence

Chaque nouvelle analyse P1 ouvre automatiquement un pop-up avec le motif, les raisons et les constantes figées. Un P1 confirmé par les règles locales est renvoyé immédiatement, sans attendre Ollama. Les autres résultats restent analysés par le modèle. Après fermeture, le bouton « Ouvrir les actions d’urgence » permet de retrouver le pop-up ; une même analyse ne le rouvre pas automatiquement.

Le bouton d’appel utilise le **51288215**, numéro fourni pour ce projet. Il ouvre l’application téléphone via `tel:` uniquement sur clic. Le numéro peut être modifié dans Configuration → Contact d’urgence pour la page courante. Pour changer durablement la valeur au démarrage, définir `EMERGENCY_PHONE`, par exemple dans PowerShell :

```powershell
$env:EMERGENCY_PHONE = "51288215"
python test.py
```

« Notifier dans l’application » enregistre une alerte dans le journal P1, visible dans Configuration et actualisé toutes les cinq secondes. La notification conserve le résultat validé côté serveur et les constantes de cette analyse. Elle persiste en SQLite ; les tentatives répétées ne créent pas de doublon. Aucun SMS, email ou contact externe n’est envoyé par cette action. Une analyse non encore notifiée expire du cache après 100 nouvelles analyses ou un redémarrage ; il faut alors relancer le pré-triage.

« Préparer un SMS au contact d’urgence » ouvre séparément l’application Messages, avec le même numéro et un brouillon contenant la priorité P1, le motif, la fréquence cardiaque, la saturation en oxygène et la tension, chacun sur sa ligne. La date est présentée au format `11/10/2026 à 00:31 (heure de Tunis)`, à partir de l’heure réelle de l’analyse. Les données simulées et l’action simulée sont signalées dans le texte. L’utilisateur vérifie et envoie le message lui-même. Les liens d’appel et de SMS nécessitent des applications compatibles sur l’appareil ; l’application web ne confirme pas leur réception ni l’arrivée d’un soignant.

Le pop-up et le journal identifient les résultats issus de la simulation comme **Démonstration**. Le lien d’appel reste une action téléphonique réelle, indiquée explicitement, et ne se déclenche jamais automatiquement.

Pour tester : cocher un signe d’alerte dans le pré-triage puis lancer l’analyse, ou sélectionner le profil Désaturation/Hypotension et analyser les mesures. Le pop-up P1 s’ouvre ; publier une notification l’ajoute au journal. Les tests automatisés ne déclenchent aucun appel ni message externe.

## Assistant de recherche Consensus

L’**icône de discussion en bas à droite** ouvre l’assistant Consensus dans un petit pop-up. Un second clic sur l’icône, la croix ou la touche Échap le referme. Le dashboard reste utilisable pendant la discussion, et fermer puis rouvrir le chat conserve l’historique de la page et une recherche en cours. Saisir une question scientifique et cliquer sur Rechercher : chaque question interroge l’[API officielle Consensus](https://github.com/Consensus-NLP/consensus-api) et affiche jusqu’à cinq publications, leurs points clés lorsqu’ils sont disponibles, la revue, l’année et les liens vers les sources. Les résumés sont ceux retournés par Consensus ; le panneau n’invente pas une conclusion quand ces informations manquent. Chaque question est une recherche indépendante.

La clé est lue uniquement côté Python depuis `CONSENSUS_API_KEY`, ou depuis le fichier `.env` à la racine du projet si la variable d’environnement n’est pas définie. La configuration locale fournie pour ce projet se trouve dans ce fichier, exclu de Git. Pour une nouvelle installation, copier `.env.example` vers `.env`, puis renseigner la clé. Ne pas placer de clé dans `app/static`, un dépôt ou un fichier partagé. Aucun paquet supplémentaire n’est requis : le client utilise la bibliothèque standard Python.

Le serveur appelle `GET https://api.consensus.app/v1/search`, avec `x-api-key` et les paramètres `query` et `page_size=5`. Seule la question du panneau est envoyée à Consensus ; les symptômes, constantes, données de la montre et résultats de triage ne sont pas ajoutés. Les requêtes HTTP s’exécutent hors de la boucle du flux en direct. Les erreurs d’authentification, quota, connexion et délai sont affichées et permettent une nouvelle tentative. Une recherche identique est réutilisée pendant dix minutes afin d’éviter des appels inutiles. Le statut du panneau vérifie la présence de la clé sans consommer de recherche ; le premier résultat confirme que le service accepte la clé.

Les tests utilisent de fausses clés et des réponses contrôlées, sans requêtes externes ni consommation du quota Consensus.

## Simulation

Les profils Calme, Activité, Stress, Récupération et Sommeil produisent des signaux corrélés avec des transitions progressives. Le profil Mixte alterne les états. La vitesse accélère le scénario, tandis que les paquets restent émis chaque seconde réelle.

Les pas sont calculés à partir d'une cadence ; les intervalles RR suivent les battements simulés. Le stress seul ne provoque pas de désaturation. La réserve d'énergie évolue sur des minutes. `SIM_SEED` permet de reproduire une démonstration.

Les profils Fièvre, Désaturation et Hypotension permettent de tester l'effet des constantes sur le triage. La tension et la température du simulateur sont synthétiques. Le service BLE standard utilisé ici fournit la FC et les intervalles RR, sans inventer les autres mesures.

Le graphique fonctionne sans chargement externe. Si l'installation Uvicorn n'a pas de transport WebSocket, le dashboard reçoit les mêmes mesures via `/api/metrics/latest` toutes les secondes.

## Vérifications

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
node tests/test_dashboard.cjs
```

Les tests vérifient les profils, la vitesse, les constantes transmises au modèle, les garde-fous, les valeurs absentes/périmées, l'affichage HTTP et la continuité du flux pendant l'inférence. Ils utilisent une base temporaire et ne modifient pas la base de démonstration. Les tests du dashboard utilisent des doubles DOM et ne remplacent pas une vérification visuelle dans un navigateur.

## Mode BLE

- Active **Broadcast Heart Rate** sur ta Garmin Venu.
- Dans l'UI, choisis `BLE Garmin HR` puis `Démarrer`.
- Optionnel: renseigne l'adresse BLE de la montre.

## Notes

- Outil de bien-être, pas un dispositif médical.
- Si BLE échoue, repasse en `simulateur` pour la démo.
