"""
Récupère automatiquement les fréquences radio publiées sur forum.tetrahub.net.

Le forum exige d'être connecté pour lire les sujets : le script se connecte avec
TON compte (identifiants lus dans les variables d'environnement TETRAHUB_USER /
TETRAHUB_PASS, ou demandés au clavier).

Exemples :
    python tetrahub_scraper.py                       # sections "Spectre radio"
    python tetrahub_scraper.py --forums tetra dmr    # sections choisies
    python tetrahub_scraper.py --list-forums         # liste des sections
    python tetrahub_scraper.py --max-topics 5        # test rapide

Résultats : frequences.csv (une ligne par fréquence trouvée) et frequences.json.
Les messages déjà traités sont mémorisés dans etat.json : relancer le script
ne récupère que les nouveautés.
"""

import argparse
import csv
import getpass
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse, urlencode, parse_qsl, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://forum.tetrahub.net/"

# Sous-forums de la catégorie "Spectre radio"
DEFAULT_FORUMS = [
    "freq-frequences-vhf-uhf",
    "freq-aeronautique",
    "freq-montagne",
    "maritime",
    "freq-ondes-courtes",
    "freq-frequences-etrangeres",
]

# Nombre décimal éventuellement suivi d'une unité (MHz, kHz, GHz)
FREQ_RE = re.compile(
    r"(?<![\d.,])(\d{1,5}(?:[.,]\d{1,6})?)\s*(GHz|MHz|kHz|Mhz|mhz|khz|ghz|MHZ|KHZ|GHZ)?(?![\d])"
)


def clean_url(url):
    """Retire le paramètre sid (identifiant de session phpBB) des URL."""
    parts = urlparse(urljoin(BASE, url))
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "sid"]
    return urlunparse(parts._replace(query=urlencode(query)))


def parse_frequencies(text):
    """Renvoie la liste des fréquences (en MHz) trouvées dans un texte."""
    found = []
    for m in FREQ_RE.finditer(text):
        raw, unit = m.group(1), (m.group(2) or "").lower()
        value = float(raw.replace(",", "."))
        decimals = len(re.split(r"[.,]", raw)[1]) if re.search(r"[.,]", raw) else 0
        if unit == "ghz":
            mhz = value * 1000
        elif unit == "khz":
            if value < 30:
                continue  # "12,5 kHz", "6,25 kHz" : largeur de canal, pas une fréquence
            mhz = value / 1000
        elif unit == "mhz":
            mhz = value
        elif decimals >= 3 and 25 <= value <= 3000:
            # Sans unité : on n'accepte que "145.500" / "390.0125" pour éviter dates, prix...
            mhz = value
        else:
            continue
        if 0.01 <= mhz <= 30000:
            found.append((round(mhz, 6), m.group(0).strip()))
    return found


class TetraHub:
    def __init__(self, delay=1.5):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
        self.delay = delay

    def get(self, url):
        time.sleep(self.delay)  # rester poli avec le serveur
        r = self.s.get(urljoin(BASE, url), timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        if r.status_code == 403:
            # phpBB répond 403 quand le compte n'a pas le droit de lire : on récupère son message
            msg = soup.select_one("#message p, .panel p, h2")
            raise PermissionError(f"403 sur {r.url} : " + (msg.get_text(" ", strip=True) if msg else "accès refusé"))
        r.raise_for_status()
        return soup

    def login(self, username, password):
        soup = self.get("ucp.php?mode=login")
        form = soup.select_one("form#login")
        if form is None:
            raise RuntimeError("Formulaire de connexion introuvable")
        data = {i.get("name"): i.get("value", "") for i in form.select("input[name]")
                if i.get("type") not in ("checkbox", "submit")}
        data.update(username=username, password=password, autologin="on", login="Connexion")
        # phpBB refuse les formulaires envoyés trop vite après leur affichage
        time.sleep(2)
        r = self.s.post(urljoin(BASE, form.get("action")), data=data, timeout=30)
        r.raise_for_status()
        if "mode=logout" not in r.text:
            err = BeautifulSoup(r.text, "html.parser").select_one(".error")
            raise RuntimeError("Connexion échouée" + (f" : {err.get_text(strip=True)}" if err else ""))

    def is_logged_in(self):
        return "mode=logout" in self.s.get(BASE, timeout=30).text

    def save_cookies(self, path):
        json.dump(requests.utils.dict_from_cookiejar(self.s.cookies), open(path, "w", encoding="utf-8"))

    def load_cookies(self, path):
        if os.path.exists(path):
            self.s.cookies.update(json.load(open(path, encoding="utf-8")))

    def list_forums(self):
        soup = self.get("")
        return [(urlparse(clean_url(a["href"])).path.strip("/"), a.get_text(strip=True))
                for a in soup.select("a.forumtitle, a.subforum")]

    def topics(self, forum_slug):
        """Parcourt toutes les pages d'une section et renvoie (titre, url, empreinte) des sujets.

        L'empreinte (nombre de réponses + date du dernier message) change dès qu'un
        message est ajouté : elle permet de ne rouvrir que les sujets modifiés.
        """
        url = f"{forum_slug}/"
        seen = set()
        while url:
            soup = self.get(url)
            if "pas les permissions" in soup.get_text():
                raise PermissionError(f"Accès refusé à la section {forum_slug}")
            for row in soup.select("li.row"):
                a = row.select_one("a.topictitle")
                if a is None:
                    continue
                t = clean_url(a["href"])
                if t in seen:
                    continue
                seen.add(t)
                replies = row.select_one("dd.posts")
                last = row.select_one("dd.lastpost time")
                fingerprint = "|".join([
                    replies.get_text(" ", strip=True) if replies else "",
                    last.get("datetime", "") if last else "",
                ])
                yield a.get_text(strip=True), t, fingerprint
            nxt = soup.select_one(".pagination a[rel=next], .pagination li.next a")
            url = clean_url(nxt["href"]) if nxt else None

    def posts(self, topic_url):
        """Parcourt toutes les pages d'un sujet et renvoie les messages."""
        url = topic_url
        while url:
            soup = self.get(url)
            if soup.select_one("form#login") and not soup.select("div.post"):
                raise PermissionError("Connexion requise pour lire les sujets")
            for p in soup.select("div.post"):
                content = p.select_one("div.content")
                if content is None:
                    continue
                for br in content.find_all("br"):
                    br.replace_with("\n")
                author = p.select_one(".author .username, .author .username-coloured")
                date = p.select_one(".author time")
                yield {
                    "id": p.get("id", ""),
                    "auteur": author.get_text(strip=True) if author else "",
                    "date": date.get("datetime", date.get_text(strip=True)) if date else "",
                    "texte": content.get_text("\n"),
                    "tableaux": [
                        [[c.get_text(" ", strip=True) for c in tr.select("td, th")] for tr in tbl.select("tr")]
                        for tbl in content.select("table")
                    ],
                }
            nxt = soup.select_one(".pagination a[rel=next], .pagination li.next a")
            url = clean_url(nxt["href"]) if nxt else None


def extract(forum, topic_title, topic_url, post):
    """Transforme un message en lignes 'une fréquence par ligne' avec son contexte."""
    rows = []
    lines = [l.strip() for l in post["texte"].splitlines() if l.strip()]
    # Les lignes de tableaux sont souvent les plus riches (fréquence ; service ; ville...)
    lines += [" | ".join(r) for t in post["tableaux"] for r in t]
    seen = set()
    for line in lines:
        for mhz, raw in parse_frequencies(line):
            if (mhz, line) in seen:
                continue
            seen.add((mhz, line))
            rows.append({
                "section": forum,
                "sujet": topic_title,
                "frequence_mhz": mhz,
                "texte_trouve": raw,
                "contexte": line[:300],
                "auteur": post["auteur"],
                "date": post["date"],
                "lien": f"{topic_url}#{post['id']}" if post["id"] else topic_url,
            })
    return rows


SECTION_NAMES = {
    "freq-frequences-vhf-uhf": "Fréquences VHF/UHF",
    "freq-aeronautique": "Aéronautique",
    "freq-montagne": "Montagne",
    "maritime": "Maritime",
    "freq-ondes-courtes": "Ondes courtes",
    "freq-frequences-etrangeres": "Fréquences étrangères",
}


def load_results(path):
    """Relit les résultats en re-filtrant avec les règles actuelles de parse_frequencies,
    pour que les anciennes données profitent aussi des corrections du détecteur."""
    if not os.path.exists(path):
        return []
    return [r for r in json.load(open(path, encoding="utf-8")) if parse_frequencies(r["texte_trouve"])]


def write_html(results, path):
    """Génère une page autonome (recherche, filtres, tri) à ouvrir dans le navigateur."""
    template = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "page_template.html"),
                    encoding="utf-8").read()

    def embed(obj):
        # "</" empêcherait la balise <script> de se fermer au bon endroit
        return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")

    meta = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "sections": SECTION_NAMES}
    page = template.replace("__DATA__", embed(results)).replace("__META__", embed(meta))
    open(path, "w", encoding="utf-8").write(page)


def main():
    ap = argparse.ArgumentParser(description="Extraction des fréquences de forum.tetrahub.net")
    ap.add_argument("--forums", nargs="+", default=DEFAULT_FORUMS, help="sections à parcourir (slug d'URL)")
    ap.add_argument("--list-forums", action="store_true", help="affiche les sections et quitte")
    ap.add_argument("--max-topics", type=int, default=0, help="limite de sujets par section (0 = tous)")
    ap.add_argument("--delay", type=float, default=1.5, help="pause entre deux pages, en secondes")
    ap.add_argument("--out", default="frequences", help="préfixe des fichiers de sortie")
    ap.add_argument("--state", default="etat.json", help="fichier mémorisant les messages déjà traités")
    ap.add_argument("--html-only", action="store_true", help="régénère seulement la page HTML, sans scanner")
    ap.add_argument("--full",action="store_true", help="rouvre tous les sujets, même inchangés")
    ap.add_argument("--cookies", default="session.json", help="fichier gardant la connexion (supprime-le pour te déconnecter)")
    args = ap.parse_args()

    hub = TetraHub(delay=args.delay)

    if args.html_only:
        results = load_results(f"{args.out}.json")
        write_html(results, f"{args.out}.html")
        print(f"Page générée -> {args.out}.html")
        return

    if args.list_forums:
        for slug, name in hub.list_forums():
            print(f"{slug:45} {name}")
        return

    # On réutilise la connexion précédente si elle est encore valable : le mot de passe
    # n'est jamais enregistré, seulement le cookie "se souvenir de moi" du forum.
    hub.load_cookies(args.cookies)
    if hub.is_logged_in():
        print("Connecté (session enregistrée).")
    else:
        user = os.environ.get("TETRAHUB_USER") or input("Nom d'utilisateur TetraHub : ")
        pwd = os.environ.get("TETRAHUB_PASS") or getpass.getpass("Mot de passe : ")
        hub.login(user, pwd)
        hub.save_cookies(args.cookies)
        print("Connecté.")

    state = {"posts": [], "topics": {}}
    if os.path.exists(args.state):
        state.update(json.load(open(args.state, encoding="utf-8")))
    done = set(state["posts"])
    topic_marks = {} if args.full else state["topics"]
    skipped = 0

    json_path = f"{args.out}.json"
    results = load_results(json_path)
    new = 0

    try:
        for forum in args.forums:
            print(f"\n== Section {forum}")
            try:
                for n, (title, turl, mark) in enumerate(hub.topics(forum), 1):
                    if args.max_topics and n > args.max_topics:
                        break
                    if mark and topic_marks.get(turl) == mark:
                        skipped += 1  # aucun nouveau message depuis la dernière fois
                        continue
                    print(f"  [{n}] {title}")
                    try:
                        for post in hub.posts(turl):
                            key = f"{turl}#{post['id']}"
                            if key in done:
                                continue
                            rows = extract(forum, title, turl, post)
                            results.extend(rows)
                            new += len(rows)
                            done.add(key)
                        # mémorisé seulement si le sujet a été lu en entier
                        topic_marks[turl] = mark
                    except PermissionError as e:
                        print(f"      ! {e}")
            except PermissionError as e:
                print(f"  ! {e}")
    except KeyboardInterrupt:
        print("\nInterrompu, sauvegarde de ce qui a été récupéré...")
    finally:
        json.dump(results, open(json_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        json.dump({"posts": sorted(done), "topics": topic_marks}, open(args.state, "w", encoding="utf-8"))
        fields = ["section", "sujet", "frequence_mhz", "texte_trouve", "contexte", "auteur", "date", "lien"]
        # utf-8-sig + ";" pour que le fichier s'ouvre directement dans Excel (version FR)
        with open(f"{args.out}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields, delimiter=";")
            w.writeheader()
            w.writerows(results)
        write_html(results, f"{args.out}.html")
        print(f"\n{skipped} sujets inchangés ignorés")
        print(f"{new} nouvelles fréquences, {len(results)} au total -> {args.out}.html / .csv / .json")


if __name__ == "__main__":
    sys.exit(main())
