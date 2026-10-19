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
import io
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse, urlencode, parse_qsl, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://forum.tetrahub.net/"
COOKIE_DOMAIN = ".forum.tetrahub.net"   # domaine des cookies de connexion envoyés par le forum


def save_json(path, obj, **kw):
    """Écrit un fichier JSON d'un coup (fichier temporaire puis renommage) : une coupure pendant
    l'écriture ne peut pas laisser un fichier à moitié écrit, donc illisible."""
    write_atomic(path, json.dumps(obj, ensure_ascii=False, **kw))


def write_atomic(path, text, encoding="utf-8", newline=None):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding=encoding, newline=newline) as f:
        f.write(text)
    os.replace(tmp, path)

# Sous-forums de la catégorie "Spectre radio"
DEFAULT_FORUMS = [
    "freq-frequences-vhf-uhf",
    "freq-aeronautique",
    "freq-montagne",
    "maritime",
    "freq-ondes-courtes",
    "freq-frequences-etrangeres",
]

# Nombre décimal éventuellement suivi d'une unité (MHz, kHz, GHz).
# Accepte aussi l'écriture "428.187.50" (= 428,18750 MHz). Écarte les adresses IP ("192.168.1.1" :
# suivi de ".chiffre") et les grands nombres à milliers ("93,610,869" : suivi de ",ddd," ou ",ddd" final),
# mais garde les listes collées comme "145.500,145.600".
FREQ_RE = re.compile(
    r"(?<![\d.,])(\d{2,4}\.\d{3}\.\d{1,3}|\d{1,5}(?:[.,]\d{1,6})?)(?!\.\d)(?!,\d{3}(?:[,\s]|$))"
    r"\s*(GHz|MHz|kHz|Mhz|mhz|khz|ghz|MHZ|KHZ|GHZ)?(?![\d])"
)


def clean_url(url):
    """Retire le paramètre sid (identifiant de session phpBB) des URL."""
    parts = urlparse(urljoin(BASE, url))
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "sid"]
    return urlunparse(parts._replace(query=urlencode(query)))


def parse_frequencies(text):
    """Renvoie la liste des fréquences (en MHz) trouvées dans un texte."""
    # "145.500,145.600" : deux fréquences collées par une virgule -> on les sépare d'un espace
    text = re.sub(r"(\d\.\d{3,6}),(?=\d{2,4}\.\d)", r"\1, ", text)
    found = []
    for m in FREQ_RE.finditer(text):
        raw, unit = m.group(1), (m.group(2) or "").lower()
        if raw.count(".") == 2:          # "428.187.50" -> "428.18750"
            head, mid, tail = raw.split(".")
            raw = f"{head}.{mid}{tail}"
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


class StopScan(Exception):
    """Arrêt du scan (bouton Arrêter, limite de pages, connexion perdue, forum qui bloque...)."""


class SessionExpired(StopScan):
    """La connexion au forum a expiré pendant le scan : inutile de continuer."""


class NetworkError(Exception):
    """Page impossible à récupérer malgré plusieurs essais."""


POSTS_PER_PAGE = 35         # messages par page de sujet sur TetraHub (réglage du forum)
SAVE_EVERY = 10             # enregistre la progression tous les N sujets lus (~1 fois par minute)


def topic_page(topic_url, start):
    """Adresse de la page d'un sujet commençant au message n° `start` ("…-t3742.html" -> "…-t3742-35.html")."""
    if not start:
        return topic_url
    url, n = re.subn(r"(-t\d+)\.html$", rf"\1-{start}.html", topic_url)
    return url if n else topic_url


def replies_in(mark):
    """Nombre de réponses contenu dans une empreinte ("17 Réponses|2026-…"), ou None."""
    m = re.match(r"\s*(\d+)", mark or "")
    return int(m.group(1)) if m else None


RETRY_WAITS = (5, 15, 45)   # secondes d'attente avant chaque nouvel essai après une erreur réseau
MAX_DELAY = 60              # pause maximale entre deux pages quand le forum demande de ralentir
MIN_RETRY_AFTER = 30        # attente minimale (s) quand le forum répond "trop de requêtes"


class TetraHub:
    def __init__(self, delay=1.5):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
        self.delay = delay
        self.pages = 0          # pages téléchargées depuis le début du scan
        self.max_pages = 0      # 0 = pas de limite
        self.should_stop = lambda: False
        self.subforums = {}     # section -> [(slug, nom)] des sous-sections vues pendant le scan
        self.log = print        # le scan le remplace pour afficher les ralentissements dans le journal

    def wait(self, seconds):
        """Pause découpée pour réagir vite au bouton Arrêter."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.should_stop():
                raise StopScan("Scan arrêté")
            time.sleep(min(0.2, max(0, end - time.monotonic())))

    def fetch(self, url):
        """Télécharge une page en encaissant les coupures passagères et les demandes de ralentir."""
        retries = 0
        while True:
            try:
                r = self.s.get(url, timeout=30)
            except requests.RequestException as e:
                problem = f"réseau ({type(e).__name__})"
            else:
                if r.status_code == 403 and r.headers.get("cf-mitigated") == "challenge":
                    # Cloudflare exige une vérification "humaine" : on n'insiste pas
                    raise StopScan("Cloudflare bloque les requêtes automatiques pour le moment : "
                                   "réessaie plus tard (plusieurs heures), avec une pause plus longue")
                if r.status_code == 429 or (r.status_code == 503 and "Retry-After" in r.headers):
                    # le serveur demande de ralentir : on attend ce qu'il dit et on double la pause
                    try:
                        pause = int(r.headers.get("Retry-After", 60))
                    except ValueError:
                        pause = 60
                    pause = min(max(pause, MIN_RETRY_AFTER), 600)
                    self.delay = min(MAX_DELAY, max(self.delay * 2, 5))
                    if retries >= len(RETRY_WAITS):
                        raise StopScan("Le forum demande de ralentir à répétition : scan arrêté, "
                                       "réessaie plus tard")
                    retries += 1
                    self.log(f"  ~ le forum demande de ralentir : pause de {pause} s, "
                             f"puis {self.delay:g} s entre deux pages")
                    self.wait(pause)
                    continue
                if r.status_code < 500:
                    return r
                problem = f"serveur (erreur {r.status_code})"
            if retries >= len(RETRY_WAITS):
                raise NetworkError(f"{url} : problème {problem} persistant")
            self.log(f"  ~ problème {problem}, nouvel essai dans {RETRY_WAITS[retries]} s")
            self.wait(RETRY_WAITS[retries])
            retries += 1

    def get(self, url):
        if self.max_pages and self.pages >= self.max_pages:
            raise StopScan(f"Limite de {self.max_pages} pages atteinte, la suite au prochain scan")
        self.wait(self.delay)  # pause pour rester poli avec le serveur
        self.pages += 1
        r = self.fetch(urljoin(BASE, url))
        soup = BeautifulSoup(r.text, "html.parser")
        if r.status_code == 403:
            # phpBB répond 403 quand le compte n'a pas le droit de lire : on récupère son message
            msg = soup.select_one("#message p, .panel p, h2")
            raise PermissionError(f"403 sur {r.url} : " + (msg.get_text(" ", strip=True) if msg else "accès refusé"))
        if r.status_code >= 400:
            # ex. 404 : sujet supprimé entre la liste et la lecture -> on le saute, sans planter le scan
            raise NetworkError(f"{r.url} : page introuvable (erreur {r.status_code})")
        return soup

    def login(self, username, password):
        # téléchargement direct (pas self.get) : la connexion ne doit pas être bloquée par
        # la limite de pages ou le bouton Arrêter du scan précédent
        soup = BeautifulSoup(self.s.get(urljoin(BASE, "ucp.php?mode=login"), timeout=30).text, "html.parser")
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
        return self.whoami() is not None

    def whoami(self):
        """Pseudo du compte connecté, ou None."""
        html = self.s.get(BASE, timeout=30).text
        if "mode=logout" not in html:
            return None
        name = BeautifulSoup(html, "html.parser").select_one("#username_logged_in .username, "
                                                            "#username_logged_in .username-coloured")
        return name.get_text(strip=True) if name else "?"

    def save_cookies(self, path):
        """Enregistre les cookies AVEC leur domaine : sans lui, ceux relus au prochain lancement
        cohabiteraient avec ceux que le forum renvoie, et l'ancienne session serait envoyée en premier."""
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                   for c in self.s.cookies]
        save_json(path, cookies)

    def load_cookies(self, path):
        if not os.path.exists(path):
            return
        data = json.load(open(path, encoding="utf-8"))
        if isinstance(data, dict):
            # ancien format {nom: valeur}, sans domaine : on remet celui du forum
            data = [{"name": k, "value": v, "domain": COOKIE_DOMAIN, "path": "/"} for k, v in data.items()]
        for c in data:
            self.s.cookies.set(c["name"], c["value"], domain=c.get("domain") or COOKIE_DOMAIN,
                               path=c.get("path") or "/")

    def list_forums(self):
        """Sections du forum : [{slug, name, category, topics, sub}] (topics = None pour une sous-section)."""
        soup = BeautifulSoup(self.s.get(BASE, timeout=30).text, "html.parser")
        slug = lambda a: urlparse(clean_url(a["href"])).path.strip("/")
        forums = []
        for block in soup.select("div.forabg"):
            head = block.select_one("li.header a, li.header dt")
            category = head.get_text(strip=True) if head else ""
            for row in block.select("li.row"):
                a = row.select_one("a.forumtitle")
                if a is None:
                    continue
                count = row.select_one("dd.topics")
                digits = re.sub(r"\D", "", count.get_text()) if count else ""
                forums.append({"slug": slug(a), "name": a.get_text(strip=True), "category": category,
                               "topics": int(digits) if digits else None, "sub": False})
                for sub in row.select("a.subforum"):
                    forums.append({"slug": slug(sub), "name": sub.get_text(strip=True), "category": category,
                                   "topics": None, "sub": True, "parent": slug(a)})
        return forums

    def topics(self, forum_slug, known=None):
        """Parcourt les pages d'une section et renvoie (titre, url, empreinte) des sujets.

        L'empreinte (nombre de réponses + date du dernier message) change dès qu'un
        message est ajouté : elle permet de ne rouvrir que les sujets modifiés.

        `known` (empreintes déjà vues) n'est donné que pour une section déjà lue en entier :
        la liste étant triée du plus récent au plus ancien, le premier sujet non épinglé
        inchangé veut dire que tous ceux d'en dessous le sont aussi, donc on s'arrête là.
        """
        url = f"{forum_slug}/"
        seen = set()
        first = True
        while url:
            soup = self.get(url)
            if first:
                # les sous-sections sont listées en haut de la première page (bloc .forabg)
                self.subforums[forum_slug] = [
                    (urlparse(clean_url(a["href"])).path.strip("/"), a.get_text(strip=True))
                    for a in soup.select(".forabg a.forumtitle")
                ]
                first = False
            # le refus du forum s'affiche dans un encadré (.panel) : chercher le texte partout ferait
            # prendre un sujet intitulé "… pas les permissions …" pour un refus de toute la section
            denied = any("pas les permissions" in p.get_text() for p in soup.select(".panel"))
            if denied and not soup.select("li.row a.topictitle"):
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
                pinned = {"sticky", "announce", "global-announce"} & set(row.get("class", []))
                if known is not None and not pinned and known.get(t) == fingerprint:
                    return  # le reste de la liste est plus ancien, donc inchangé
                yield a.get_text(strip=True), t, fingerprint
            nxt = soup.select_one(".pagination a[rel=next], .pagination li.next a")
            url = clean_url(nxt["href"]) if nxt else None

    def posts(self, topic_url, start=0):
        """Parcourt les pages d'un sujet (à partir du message n° `start`) et renvoie les messages."""
        url = topic_page(topic_url, start)
        while url:
            soup = self.get(url)
            if soup.select_one("form#login") and not soup.select("div.post"):
                # le forum affiche le formulaire de connexion au lieu du sujet : la session a expiré
                raise SessionExpired("Connexion au forum perdue : reconnecte-toi puis relance le scan "
                                     "(il reprendra là où il s'est arrêté)")
            for p in soup.select("div.post"):
                content = p.select_one("div.content")
                if content is None:
                    continue
                for br in content.find_all("br"):
                    br.replace_with("\n")
                # Citations : gardées à part ("en réponse à") et retirées du texte, sinon les fréquences
                # citées seraient comptées une 2e fois et attribuées à celui qui répond.
                quotes = []
                for bq in content.find_all("blockquote"):
                    if bq.find_parent("blockquote"):
                        continue  # citation dans une citation : déjà comprise dans la citation externe
                    cite = bq.find("cite")
                    who = cite.get_text(" ", strip=True).split(" a écrit")[0] if cite else ""
                    if cite:
                        cite.decompose()
                    text = re.sub(r"\s+", " ", bq.get_text(" ", strip=True))
                    if text:
                        quotes.append(f"{who} : {text}" if who else text)
                for bq in content.find_all("blockquote"):
                    bq.decompose()
                # Tableaux : lus ligne par ligne ("col1 | col2 | col3"), puis retirés du texte, sinon
                # chaque fréquence de tableau ressortirait une 2e fois avec juste sa cellule comme contexte.
                tables = [[[c.get_text(" ", strip=True) for c in tr.select("td, th")] for tr in tbl.select("tr")]
                          for tbl in content.select("table")]
                for tbl in content.select("table"):
                    if not tbl.find_parent("table"):   # un tableau imbriqué part avec son tableau parent
                        tbl.decompose()
                author = p.select_one(".author .username, .author .username-coloured")
                date = p.select_one(".author time")
                yield {
                    "id": p.get("id", ""),
                    "auteur": author.get_text(strip=True) if author else "",
                    "date": date.get("datetime", date.get_text(strip=True)) if date else "",
                    "texte": content.get_text("\n"),
                    "citations": quotes,
                    "tableaux": tables,
                }
            nxt = soup.select_one(".pagination a[rel=next], .pagination li.next a")
            url = clean_url(nxt["href"]) if nxt else None


def extract(forum, topic_title, topic_url, post, section_name="", question=""):
    """Transforme un message en lignes 'une fréquence par ligne' avec son contexte.

    `question` : début du premier message du sujet, quand `post` est une réponse.
    """
    rows = []
    lines = [l.strip() for l in post["texte"].splitlines() if l.strip()]
    # Les lignes de tableaux sont souvent les plus riches (fréquence ; service ; ville...).
    # None marque le début de chaque tableau : son titre repart de zéro (puis de sa ligne d'en-tête).
    for table in post["tableaux"]:
        lines += [None] + [" | ".join(r) for r in table]
    citation = " / ".join(post.get("citations", []))[:300]
    seen = set()
    heading = ""   # dernière ligne sans fréquence rencontrée : souvent le titre d'une liste ("Pompiers :")
    for line in lines:
        if line is None:
            heading = ""
            continue
        found = parse_frequencies(line)
        if not found:
            heading = line[:120] if len(line) <= 120 else ""
            continue
        for mhz, raw in found:
            if (mhz, line) in seen:
                continue
            seen.add((mhz, line))
            rows.append({
                "section": forum,
                "section_nom": section_name or SECTION_NAMES.get(forum, forum),
                "sujet": topic_title,
                "frequence_mhz": mhz,
                "texte_trouve": raw,
                "contexte": line[:300],
                "titre_bloc": heading,
                "citation": citation,
                "question": question[:300],
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
    "decodage-donnees-analogique-numerique": "Décodage données",
    "digital-audio-broadcasting-dab": "DAB",
}

# Sections conseillées : "Spectre radio" + les deux sections de décodage riches en fréquences
RECOMMENDED_FORUMS = DEFAULT_FORUMS + ["decodage-donnees-analogique-numerique", "digital-audio-broadcasting-dab"]


def load_results(path):
    """Relit les résultats en re-filtrant avec les règles actuelles de parse_frequencies,
    pour que les anciennes données profitent aussi des corrections du détecteur."""
    if not os.path.exists(path):
        return []
    return [r for r in json.load(open(path, encoding="utf-8")) if parse_frequencies(r["texte_trouve"])]


def render_html(results, section_names=None):
    """Page autonome (recherche, filtres, tri) contenant les résultats."""
    template = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tableau.html"),
                    encoding="utf-8").read()

    def embed(obj):
        # "</" empêcherait la balise <script> de se fermer au bon endroit
        return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")

    meta = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "sections": section_names or SECTION_NAMES}
    return template.replace("__DATA__", embed(results)).replace("__META__", embed(meta))


def write_html(results, path):
    write_atomic(path, render_html(results))


def read_topic(hub, forum, name, title, turl, old_mark, done, openings, results):
    """Lit les messages pas encore traités d'un sujet, ajoute leurs fréquences et renvoie leur nombre.

    Si le sujet a déjà été lu, on va directement à la page de son dernier message connu
    (les nouveaux messages sont forcément après) au lieu de tout relire depuis la page 1.
    """
    old = replies_in(old_mark)   # réponses lors de la dernière lecture : le dernier message lu porte ce n°
    start = (old // POSTS_PER_PAGE) * POSTS_PER_PAGE if old is not None and turl in openings else 0
    while True:
        found, seen = 0, False
        for i, post in enumerate(hub.posts(turl, start)):
            seen = True
            key = f"{turl}#{post['id']}"
            if start and i == 0 and key not in done:
                break  # la page ne commence pas par un message connu (décalage) : on relit tout
            first = not start and i == 0
            if first:
                openings[turl] = re.sub(r"\s+", " ", post["texte"]).strip()[:300]
            if key in done:
                continue
            rows = extract(forum, title, turl, post, name, "" if first else openings.get(turl, ""))
            results.extend(rows)
            found += len(rows)
            done.add(key)
        else:
            if seen or not start:
                return found
            # page visée vide (messages supprimés entre-temps ?) : on relit tout le sujet
        start = 0


def scan(hub, forums, out="frequences", state_path="etat.json", full=False, max_topics=0,
         log=print, stats=None):
    """Parcourt les sections et enregistre les résultats (même en cas d'arrêt ou d'erreur).

    `stats` est mis à jour au fil de l'eau, ce qui permet à l'interface web d'afficher la progression.
    """
    stats = stats if stats is not None else {}
    stats.update(new=0, skipped=0, topics=0, forum="", total=0, errors=0, session_expired=False)
    hub.log = log
    failures = 0   # erreurs réseau d'affilée : au-delà de 5, le forum est sans doute en panne

    state = {"posts": [], "topics": {}}
    if os.path.exists(state_path):
        state.update(json.load(open(state_path, encoding="utf-8")))
    done = set(state["posts"])
    topic_marks = state["topics"]                  # empreintes enregistrées (toutes les sections)
    known = {} if full else dict(topic_marks)      # empreintes utilisées pour sauter les sujets inchangés
    complete = set(state.get("complete", []))      # sections déjà lues en entier une fois
    openings = state.get("openings", {})           # sujet -> début de son premier message (la question)

    json_path = f"{out}.json"
    results = load_results(json_path)
    tree = state.get("subforums", {})   # section -> [[slug, nom]] : mémorisé pour l'interface
    names = {}                          # noms des sous-sections découvertes pendant ce scan
    for subs in tree.values():
        names.update(dict(map(tuple, subs)))

    def checkpoint():
        """Enregistre la progression. Appelé régulièrement : si le programme est tué en route
        (fenêtre fermée, coupure...), seul le travail depuis le dernier point est perdu.
        Les résultats d'abord : une coupure entre les deux écritures donnerait au pire quelques
        doublons, jamais des fréquences perdues."""
        save_json(json_path, results, indent=1)
        save_json(state_path, {"posts": sorted(done), "topics": topic_marks, "subforums": tree,
                               "complete": sorted(complete), "openings": openings})

    def queue_subforums(forum):
        """Sous-sections repérées sur la première page : on les parcourt juste après
        (même si la section n'a aucun sujet à elle, ou si on n'a pas le droit de lire les siens)."""
        subs = hub.subforums.get(forum, [])
        if subs:
            tree[forum] = [list(s) for s in subs]
            names.update(dict(subs))
            log(f"  sous-sections : {', '.join(sub_name for _, sub_name in subs)}")
            queue[0:0] = [s for s, _ in subs if s not in visited]

    queue, visited = list(forums), set()
    try:
        while queue:
            forum = queue.pop(0)
            if forum in visited:
                continue
            visited.add(forum)
            if forum not in SECTION_NAMES and forum not in names:
                try:  # noms affichés sur l'accueil du forum, lus une seule fois
                    names.update({f["slug"]: f["name"] for f in hub.list_forums()})
                except requests.RequestException:
                    pass
                names.setdefault(forum, forum)
            name = SECTION_NAMES.get(forum) or names[forum]
            stats["forum"] = forum
            log(f"== Section {name}")
            whole = True   # la section a-t-elle été lue sans rien sauter ?
            try:
                # section déjà lue en entier : on arrête la liste au premier sujet inchangé
                listing = hub.topics(forum, known if forum in complete and not full else None)
                for n, (title, turl, mark) in enumerate(listing, 1):
                    if max_topics and n > max_topics:
                        whole = False
                        break
                    if mark and known.get(turl) == mark:
                        stats["skipped"] += 1  # aucun nouveau message depuis la dernière fois
                        continue
                    log(f"  [{n}] {title}")
                    try:
                        found = read_topic(hub, forum, name, title, turl, known.get(turl), done, openings, results)
                        # mémorisé seulement si le sujet a été lu en entier
                        topic_marks[turl] = mark
                        stats["topics"] += 1
                        stats["new"] += found
                        failures = 0
                        if found:
                            log(f"      +{found} fréquences")
                        if stats["topics"] % SAVE_EVERY == 0:
                            checkpoint()
                    except PermissionError as e:
                        whole = False
                        log(f"      ! {e}")
                    except NetworkError as e:
                        # sujet sauté (pas marqué comme lu) : il sera repris au prochain scan
                        whole = False
                        stats["errors"] += 1
                        failures += 1
                        log(f"      ! sujet sauté, {e}")
                        if failures >= 5:
                            raise StopScan("Le forum ne répond plus (5 erreurs d'affilée) : scan arrêté, "
                                           "réessaie plus tard")
                # arrêt rapide autorisé aux prochains scans seulement si rien n'a été sauté
                (complete.add if whole else complete.discard)(forum)
                queue_subforums(forum)
            except PermissionError as e:
                complete.discard(forum)
                log(f"  ! {e}")
                queue_subforums(forum)   # ses sous-sections peuvent, elles, être autorisées
            except NetworkError as e:
                complete.discard(forum)
                stats["errors"] += 1
                failures += 1
                log(f"  ! section sautée, {e}")
                if failures >= 5:
                    raise StopScan("Le forum ne répond plus (5 erreurs d'affilée) : scan arrêté, "
                                   "réessaie plus tard")
    except StopScan as e:
        stats["session_expired"] = isinstance(e, SessionExpired)
        log(("! " if not str(e).startswith(("Limite", "Scan arrêté")) else "") + str(e))
    finally:
        checkpoint()
        fields = ["section", "section_nom", "sujet", "frequence_mhz", "texte_trouve", "contexte",
                  "titre_bloc", "citation", "question", "auteur", "date", "lien"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields, delimiter=";", lineterminator="\r\n")
        w.writeheader()
        w.writerows(results)
        # Fichiers "de lecture" : s'ils sont ouverts (Excel, navigateur...), Windows les verrouille.
        # On le signale sans faire échouer le scan : les données, elles, sont déjà enregistrées.
        for label, write in (
            # utf-8-sig + ";" pour que le fichier s'ouvre directement dans Excel (version FR)
            ("csv", lambda: write_atomic(f"{out}.csv", buf.getvalue(), encoding="utf-8-sig", newline="")),
            ("html", lambda: write_html(results, f"{out}.html")),
        ):
            try:
                write()
            except OSError:
                log(f"! {os.path.basename(out)}.{label} non mis à jour : il est sans doute ouvert "
                    f"(Excel ?). Ferme-le, il sera recréé au prochain scan.")
        stats["total"] = len(results)
        log(f"{stats['skipped']} sujets inchangés ignorés, {stats['topics']} sujets lus")
        name = os.path.basename(out)
        log(f"{stats['new']} nouvelles fréquences, {len(results)} au total -> {name}.html / .csv / .json")
    return stats


def main():
    ap = argparse.ArgumentParser(description="Extraction des fréquences de forum.tetrahub.net")
    ap.add_argument("--forums", nargs="+", default=DEFAULT_FORUMS, help="sections à parcourir (slug d'URL)")
    ap.add_argument("--list-forums", action="store_true", help="affiche les sections et quitte")
    ap.add_argument("--max-topics", type=int, default=0, help="limite de sujets par section (0 = tous)")
    ap.add_argument("--delay", type=float, default=1.5, help="pause entre deux pages, en secondes")
    ap.add_argument("--out", default="frequences", help="préfixe des fichiers de sortie")
    ap.add_argument("--state", default="etat.json", help="fichier mémorisant les messages déjà traités")
    ap.add_argument("--max-pages", type=int, default=0, help="arrête le scan après N pages (0 = pas de limite)")
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
        for f in hub.list_forums():
            print(f"{f['slug']:45} {'  ' if f['sub'] else ''}{f['name']}")
        return

    # On réutilise la connexion précédente si elle est encore valable : le mot de passe
    # n'est jamais enregistré, seulement le cookie "se souvenir de moi" du forum.
    hub.load_cookies(args.cookies)
    if hub.is_logged_in():
        hub.save_cookies(args.cookies)   # le forum a pu renouveler la clé "se souvenir de moi"
        print("Connecté (session enregistrée).")
    else:
        user = os.environ.get("TETRAHUB_USER") or input("Nom d'utilisateur TetraHub : ")
        pwd = os.environ.get("TETRAHUB_PASS") or getpass.getpass("Mot de passe : ")
        hub.login(user, pwd)
        hub.save_cookies(args.cookies)
        print("Connecté.")

    hub.max_pages = args.max_pages
    try:
        scan(hub, args.forums, out=args.out, state_path=args.state, full=args.full,
             max_topics=args.max_topics)
    except KeyboardInterrupt:
        print("\nInterrompu : ce qui a été récupéré est enregistré.")
    finally:
        hub.save_cookies(args.cookies)


if __name__ == "__main__":
    sys.exit(main())
