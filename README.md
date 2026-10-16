# tetrahub-scraper

Récupère automatiquement les fréquences radio publiées sur [forum.tetrahub.net](https://forum.tetrahub.net/)
et les affiche dans un tableau où l'on peut chercher, filtrer et trier.

## Installation

```bash
pip install requests beautifulsoup4
```

## Lancer

```bash
python web.py
```

Le navigateur s'ouvre sur http://127.0.0.1:8765. Tout se fait depuis cette page :

1. **Se connecter** avec son compte TetraHub (le forum exige un compte pour lire les sujets).
   Le mot de passe n'est jamais enregistré : seule la connexion est gardée dans `session.json`.
2. **Cocher les sections** à scanner. Les sections conseillées sont cochées d'office ; chaque
   section indique combien de sujets ont déjà été lus.
3. **Lancer le scan** et suivre la progression en direct. Par défaut : 5 s de pause entre deux pages
   et arrêt après 300 pages, pour ménager le forum. Relancer le lendemain reprend là où ça s'était arrêté.
4. **Voir les fréquences** : tableau avec recherche, filtres par section et par bande (aéro, marine,
   TETRA…), tri, et lien vers chaque message d'origine. Le bouton « Télécharger le CSV » donne les
   mêmes données pour Excel.

`Ctrl+C` dans le terminal arrête le serveur ; un scan en cours enregistre d'abord ce qu'il a récupéré.

## Sur Android

Avec [Termux](https://f-droid.org/packages/com.termux/) (version F-Droid) :

```bash
pkg install -y python git gh && pip install requests beautifulsoup4
gh auth login
gh repo clone JustBluess/tetrahub-scraper
cd ~/tetrahub-scraper && termux-wake-lock && python web.py --no-browser
```

Puis ouvrir http://127.0.0.1:8765 dans Chrome. Mise à jour : `git pull`.

## Fichiers créés

Ils restent sur la machine et ne sont jamais envoyés sur GitHub (voir `.gitignore`).

| Fichier | Contenu |
|---|---|
| `session.json` | connexion au forum — à ne pas partager ; le supprimer pour se déconnecter |
| `frequences.html` | le tableau des fréquences, à ouvrir d'un double-clic (marche hors ligne) |
| `frequences.csv` / `.json` | les mêmes données pour Excel ou d'autres programmes |
| `etat.json` | ce qui a déjà été lu : une relance n'ouvre que les sujets qui ont de nouveaux messages |
