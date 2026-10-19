"""
Interface web locale pour le scraper TetraHub.

    python web.py

puis ouvrir http://127.0.0.1:8765 (le navigateur s'ouvre tout seul).
Le serveur n'écoute que sur ta machine : personne d'autre ne peut y accéder.
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # trouve tetrahub_scraper.py même si on lance web.py depuis un autre dossier
import tetrahub_scraper as ts  # noqa: E402

HOST, PORT = "127.0.0.1", 8765
COOKIES = os.path.join(HERE, "session.json")
OUT = os.path.join(HERE, "frequences")
STATE = os.path.join(HERE, "etat.json")
UPDATE_EVERY = 6 * 3600  # vérification des mises à jour GitHub toutes les 6 h


def git(*args, timeout=30):
    """Lance une commande git dans le dossier du projet, sans jamais demander de mot de passe."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    r = subprocess.run(["git", "-C", HERE, *args], capture_output=True, text=True, encoding="utf-8",
                       timeout=timeout, env=env)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git {args[0]} a échoué")
    return r.stdout.strip()


def repo_url():
    """Adresse web du dépôt d'origine (https://github.com/...), ou None."""
    try:
        url = git("remote", "get-url", "origin")
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return None
    url = re.sub(r"^git@github\.com:", "https://github.com/", url)   # forme SSH -> web
    url = re.sub(r"\.git$", "", re.sub(r"https://[^@/]+@", "https://", url))  # sans .git ni identifiants
    return url if url.startswith("https://github.com/") else None


def check_updates():
    """Nombre de changements publiés sur GitHub et pas encore récupérés (git pull), avec leurs titres.

    Renvoie None si la vérification est impossible (pas de git, pas d'Internet, pas un dépôt...).
    Ne modifie rien : `git fetch` télécharge seulement l'historique distant.
    """
    try:
        git("fetch", "--quiet")
        behind = int(git("rev-list", "--count", "HEAD..@{u}"))
        titles = git("log", "--format=%s", "-n", "10", "HEAD..@{u}").splitlines() if behind else []
        return {"behind": behind, "titles": titles, "checked": time.time(), "url": repo_url()}
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        return None


class App:
    """État partagé entre le serveur web et le thread de scan."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hub = ts.TetraHub()
        self.hub.load_cookies(COOKIES)
        self.user = None
        self.forums = []
        self.thread = None
        self.stop = threading.Event()
        self.log_lines = []
        self.stats = {}
        self.started = None
        self.finished = None
        self.update = None
        self.last_refresh = 0
        self.refreshing = False

    def watch_updates(self):
        """Tourne en arrière-plan : vérifie GitHub au démarrage puis toutes les 6 h."""
        while True:
            self.update = check_updates()
            time.sleep(UPDATE_EVERY)

    def retry_refresh(self):
        """Démarré sans Internet : la liste des sections est vide. On réessaie en arrière-plan
        (au plus toutes les 30 s) pour qu'elle apparaisse dès que la connexion revient."""
        if self.forums or self.refreshing or self.running or time.time() - self.last_refresh < 30:
            return
        self.refreshing = True

        def run():
            try:
                self.refresh()
            finally:
                self.refreshing = False

        threading.Thread(target=run, daemon=True).start()

    def refresh(self):
        """Vérifie la connexion et récupère la liste des sections (au démarrage)."""
        self.last_refresh = time.time()
        try:
            self.user = self.hub.whoami()
            if self.user:
                self.hub.save_cookies(COOKIES)   # le forum a pu renouveler la clé "se souvenir de moi"
        except Exception as e:
            self.log(f"! Impossible de joindre le forum : {e}")
        try:
            self.forums = self.hub.list_forums()
        except Exception as e:
            self.log(f"! Impossible de lire la liste des sections : {e}")

    def log(self, msg):
        with self.lock:
            self.log_lines.append(f"{time.strftime('%H:%M:%S')}  {msg}")
            del self.log_lines[:-500]

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start_scan(self, forums, delay, max_pages, full):
        if self.running:
            raise RuntimeError("Un scan est déjà en cours")
        if not self.user:
            raise RuntimeError("Connecte-toi d'abord")
        # sections de l'accueil + sous-sections découvertes pendant les scans (ex. un département)
        known = {f["slug"] for f in self.forums}
        known |= {slug for subs in self.read_state()[1].values() for slug, _ in subs}
        forums = [f for f in forums if f in known]
        if not forums:
            raise RuntimeError("Choisis au moins une section")
        self.hub.delay = max(1.5, float(delay))
        self.hub.max_pages = max(0, int(max_pages))
        self.hub.pages = 0
        self.stop.clear()
        self.hub.should_stop = self.stop.is_set
        self.stats = {}
        self.started, self.finished = time.time(), None
        with self.lock:
            self.log_lines.clear()

        def run():
            try:
                ts.scan(self.hub, forums, out=OUT, state_path=STATE, full=full, log=self.log, stats=self.stats)
                if self.stats.get("session_expired"):
                    self.user = None   # la page réaffiche le formulaire de connexion
            except Exception as e:
                self.log(f"! Erreur : {e}")
                traceback.print_exc()
            finally:
                if self.user:
                    self.hub.save_cookies(COOKIES)  # garde la session à jour pour le prochain lancement
                self.finished = time.time()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def status(self):
        self.retry_refresh()
        with self.lock:
            log = list(self.log_lines)
        return {
            "user": self.user,
            "forums": self.forums,
            "recommended": ts.RECOMMENDED_FORUMS,
            "running": self.running,
            "stopping": self.running and self.stop.is_set(),
            "pages": self.hub.pages,
            "max_pages": self.hub.max_pages,
            "stats": self.stats,
            "started": self.started,
            "finished": self.finished,
            "log": log,
            "frequencies": self.count_results(),
            "update": self.update,
            "progress": self.read_state()[0],
            "tree": self.read_state()[1],
        }

    _state = (None, ({}, {}))

    def read_state(self):
        """D'après etat.json : (sujets déjà lus par section, sous-sections découvertes pendant les scans)."""
        mtime = os.path.getmtime(STATE) if os.path.exists(STATE) else None
        if mtime != self._state[0]:
            topics, tree = {}, {}
            if mtime:
                state = json.load(open(STATE, encoding="utf-8"))
                urls = {k.split("#")[0] for k in state.get("posts", [])} | set(state.get("topics", {}))
                for u in urls:
                    slug = u.replace(ts.BASE, "").split("/")[0]
                    topics[slug] = topics.get(slug, 0) + 1
                tree = state.get("subforums", {})
            self._state = (mtime, (topics, tree))
        return self._state[1]

    _count = (None, 0)

    def count_results(self):
        """Nombre de fréquences enregistrées (relu seulement quand le fichier change)."""
        path = f"{OUT}.json"
        mtime = os.path.getmtime(path) if os.path.exists(path) else None
        if mtime != self._count[0]:
            self._count = (mtime, len(ts.load_results(path)))
        return self._count[1]


app = App()


class Server(ThreadingHTTPServer):
    # Sous Windows, l'option par défaut laisserait un 2e web.py démarrer sur le même port :
    # deux serveurs tourneraient en même temps sans qu'on sache lequel répond.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):   # Windows : port réservé à ce seul programme
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # pas de ligne dans la console à chaque requête

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False))

    def host_ok(self):
        # refuse les requêtes qui n'arrivent pas par 127.0.0.1/localhost (protection "DNS rebinding")
        return self.headers.get("Host", "") in (f"{HOST}:{PORT}", f"localhost:{PORT}")

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, "Forbidden", "text/plain")
        path = self.path.split("?")[0]
        if path == "/":
            page = open(os.path.join(HERE, "interface.html"), encoding="utf-8").read()
            return self.send(200, page, "text/html; charset=utf-8")
        if path == "/frequences":
            names = {f["slug"]: f["name"] for f in app.forums} or None
            page = ts.render_html(ts.load_results(f"{OUT}.json"), names)
            return self.send(200, page, "text/html; charset=utf-8")
        if path == "/frequences.csv" and os.path.exists(f"{OUT}.csv"):
            self.send_response(200)
            data = open(f"{OUT}.csv", "rb").read()
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="frequences.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if path == "/api/status":
            return self.send_json(app.status())
        self.send(404, "Not found", "text/plain")

    def do_POST(self):
        # l'en-tête X-TetraHub ne peut pas être ajouté par un autre site sans autorisation du navigateur :
        # ça empêche une page web quelconque de piloter le scan à ta place
        if not self.host_ok() or self.headers.get("X-TetraHub") != "1":
            return self.send(403, "Forbidden", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "Requête invalide"}, 400)
        path = self.path.split("?")[0]
        try:
            if path == "/api/login":
                if app.running:
                    raise RuntimeError("Impossible pendant un scan")
                app.hub.login(body.get("username", ""), body.get("password", ""))
                app.hub.save_cookies(COOKIES)
                app.user = app.hub.whoami()
                app.log(f"Connecté en tant que {app.user}")
                # certaines sous-sections (ex. les régions de Fréquences VHF/UHF) n'apparaissent qu'une fois connecté
                app.forums = app.hub.list_forums()
            elif path == "/api/logout":
                if app.running:
                    raise RuntimeError("Impossible pendant un scan")
                if os.path.exists(COOKIES):
                    os.remove(COOKIES)
                app.hub = ts.TetraHub()
                app.user = None
                app.log("Déconnecté (session.json supprimé)")
                app.forums = app.hub.list_forums()
            elif path == "/api/scan":
                app.start_scan(body.get("forums", []), body.get("delay", 5),
                               body.get("max_pages", 300), bool(body.get("full")))
            elif path == "/api/stop":
                app.stop.set()
            else:
                return self.send(404, "Not found", "text/plain")
        except Exception as e:
            # visible aussi dans le journal de la page et dans le terminal (le mot de passe n'y figure jamais)
            app.log(f"! {path} : {e}")
            print(f"Erreur {path} : {e}")
            return self.send_json({"error": str(e)}, 400)
        self.send_json(app.status())


def main():
    url = f"http://{HOST}:{PORT}"
    try:
        server = Server((HOST, PORT), Handler)   # réservé d'abord : inutile d'aller sur le forum si déjà lancé
    except OSError:
        print(f"L'interface tourne déjà (port {PORT} occupé) : ouvre {url}\n"
              "ou ferme l'autre fenêtre où web.py tourne (Ctrl+C) avant de relancer.")
        sys.exit(1)
    threading.Thread(target=app.watch_updates, daemon=True).start()
    print("Vérification de la connexion au forum...")
    app.refresh()
    print(f"Connecté en tant que {app.user}" if app.user else "Pas connecté : connecte-toi depuis la page.")
    print(f"Interface prête sur {url}  (Ctrl+C pour quitter)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        app.stop.set()
        if app.thread:
            app.thread.join(timeout=10)  # laisse le scan enregistrer ce qu'il a récupéré
        print("\nArrêté.")


if __name__ == "__main__":
    main()
