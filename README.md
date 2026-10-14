# tetrahub-scraper

Récupère automatiquement les fréquences radio publiées sur [forum.tetrahub.net](https://forum.tetrahub.net/)
et les range dans un fichier CSV (ouvrable dans Excel) et JSON.

## Installation

```bash
pip install requests beautifulsoup4
```

## Interface web (le plus simple)

```bash
python web.py
```

Le navigateur s'ouvre sur http://127.0.0.1:8765 : connexion au compte, choix des sections (les
sections conseillées sont cochées d'office), progression section par section, lancement et arrêt du
scan, journal en direct, et accès au tableau des fréquences. Le serveur n'écoute que sur ta machine.

Par défaut, un scan fait une pause de 5 s entre deux pages et s'arrête après 300 pages : relancer le
lendemain reprend là où il s'était arrêté.

## Ligne de commande

```bash
python tetrahub_scraper.py                    # sections "Spectre radio"
python tetrahub_scraper.py --max-topics 5     # test rapide
python tetrahub_scraper.py --forums tetra dmr # sections choisies
python tetrahub_scraper.py --list-forums      # liste des sections
python tetrahub_scraper.py --delay 5 --max-pages 300   # scan doux, étalé sur plusieurs jours
```

Le forum exige un compte. Au premier lancement, le script demande le pseudo et le mot de passe
(ou les lit dans `TETRAHUB_USER` / `TETRAHUB_PASS`), puis garde la connexion dans `session.json`.
Le mot de passe n'est jamais enregistré. Supprimer `session.json` pour se déconnecter.

## Résultats

- `frequences.html` : page à ouvrir dans le navigateur (double-clic) pour chercher, filtrer par
  section ou par bande (aéro, marine, TETRA…) et trier. Recréée à chaque scan ;
  `python tetrahub_scraper.py --html-only` la régénère sans scanner.
- `frequences.csv` / `frequences.json` : une ligne par fréquence trouvée (fréquence en MHz, phrase
  d'origine, section, sujet, auteur, date, lien vers le message).
- `etat.json` : messages déjà traités et « empreinte » de chaque sujet (nombre de réponses + date du
  dernier message). Une relance n'ouvre que les sujets qui ont changé ; `--full` force à tout rouvrir.
