"""
Serveur d'écoute des webhooks Bitbucket (Data Center / Server) post-merge.

À chaque merge de Pull Request (ou push sur une branche cible), il extrait le ticket Jira source
et lance `main.py --step generate-cucumber-postmerge`, qui crée le ticket SPEC [CUCUMBER] et y attache le patch.

Deux modes :
  python webhook_server.py            Webhook : Bitbucket appelle http://<hôte>:<port>/webhook/bitbucket
                                      (événements "Pull request > Merged" et/ou "Repository > Push").
  python webhook_server.py --poll     Interrogation périodique de l'API Bitbucket (PR fusionnées), sans webhook.
                                      À utiliser quand Bitbucket ne peut pas joindre la machine (réseau des postes).
      --interval 300                  période en secondes (défaut POLL_INTERVAL_SECONDS ou 300)
      --since "2026-10-01 14:00"      traite aussi les merges depuis cette date (rattrapage) ; sinon seuls
                                      les merges postérieurs au premier lancement sont traités.
      --projects "NOVALIA,OSCARO"     projets écoutés (remplace POLL_REPOSITORIES)
      --from-now                      ignore les merges survenus pendant l'arrêt de l'écoute
  En mode --poll, l'écoute se pilote depuis l'IHM (onglet "Écoute des merges") : démarrage, arrêt, journal.

Variables d'environnement (.env) :
  BITBUCKET_URL              URL du serveur Bitbucket (ex. https://git.kiscloud.io) — obligatoire
  POLL_REPOSITORIES          mode --poll : "PROJET" (tous ses dépôts) ou "PROJET/depot", séparés par des virgules — obligatoire
  POLL_INTERVAL_SECONDS      mode --poll : période par défaut, 300
  BITBUCKET_WEBHOOK_SECRET   secret partagé (signature X-Hub-Signature HMAC-SHA256) — obligatoire
  WEBHOOK_ALLOW_UNSIGNED     "true" pour accepter les requêtes non signées (tests locaux uniquement)
  WEBHOOK_HOST / WEBHOOK_PORT                 défaut 0.0.0.0 / 8085
  WEBHOOK_TARGET_BRANCHES    branches de merge surveillées, défaut "integration,dev"
  WEBHOOK_IGNORED_REPOS      motifs de dépôts ignorés, défaut "cucumber" (évite de reboucler sur nos propres patchs)
  WEBHOOK_STD_PROJECT        projet Bitbucket du socle standard (dépôts traités en STD_ONLY), défaut déduit de BITBUCKET_STD_REPO_PATH
  WEBHOOK_DRY_RUN            "true" pour journaliser la commande main.py sans l'exécuter
"""
import hashlib
import hmac
import json
import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(WORKSPACE_DIR, ".env"))

WEBHOOK_DIR = os.path.join(WORKSPACE_DIR, ".tmp", "webhook")
PROCESSED_FILE = os.path.join(WEBHOOK_DIR, "processed.json")
MAX_BODY_BYTES = 5 * 1024 * 1024
# Pas de \b : dans 'SPEC-1998_dev_reception', '_' est un caractère de mot et bloquerait la détection
TICKET_RE = re.compile(r"(?<![A-Z0-9])([A-Z][A-Z0-9]+-\d+)(?!\d)")

SECRET = os.getenv("BITBUCKET_WEBHOOK_SECRET", "").strip()
ALLOW_UNSIGNED = os.getenv("WEBHOOK_ALLOW_UNSIGNED", "false").lower() == "true"
DRY_RUN = os.getenv("WEBHOOK_DRY_RUN", "false").lower() == "true"
TARGET_BRANCHES = {b.strip() for b in os.getenv("WEBHOOK_TARGET_BRANCHES", "integration,dev").split(",") if b.strip()}
IGNORED_REPOS = [p.strip().lower() for p in os.getenv("WEBHOOK_IGNORED_REPOS", "cucumber").split(",") if p.strip()]
BITBUCKET_URL = os.getenv("BITBUCKET_URL", "").strip().rstrip("/")


def _log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}")


def _std_project_key() -> str:
    explicit = os.getenv("WEBHOOK_STD_PROJECT", "").strip().upper()
    if explicit:
        return explicit
    m = re.search(r"/projects/([^/]+)", os.getenv("BITBUCKET_STD_REPO_PATH", ""))
    return m.group(1).upper() if m else ""


# ---------------------------------------------------------------------------
# Sécurité, extraction des événements
# ---------------------------------------------------------------------------

def verify_signature(body: bytes, signature_header: str) -> bool:
    """Bitbucket Data Center signe le corps avec le secret : X-Hub-Signature: sha256=<hex>."""
    if not SECRET:
        return ALLOW_UNSIGNED
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.split("=", 1)[1].strip())


def extract_ticket(*texts: str) -> str:
    """Première clé Jira trouvée dans la branche, le titre de PR ou le message de commit."""
    for text in texts:
        m = TICKET_RE.search((text or "").upper())
        if m:
            return m.group(1)
    return ""


def fetch_commit_message(project_key: str, repo_slug: str, commit_id: str) -> str:
    """Message du commit via l'API Bitbucket (un merge contient 'from <branche source>')."""
    token = os.getenv("BITBUCKET_HTTP_TOKEN", "").strip()
    url = f"{BITBUCKET_URL}/rest/api/1.0/projects/{project_key}/repos/{repo_slug}/commits/{commit_id}"
    try:
        response = requests.get(url, headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=10)
        if response.status_code == 200:
            return response.json().get("message", "")
    except requests.RequestException as e:
        _log(f"⚠️ Message du commit {commit_id[:8]} indisponible ({e})")
    return ""


def parse_bitbucket_event(event_key: str, payload: dict) -> list:
    """
    Transforme un événement Bitbucket en tâches post-merge.
    Gère 'pr:merged' et 'repo:refs_changed' (Bitbucket Data Center / Server).
    """
    jobs = []
    if event_key == "pr:merged":
        pr = payload.get("pullRequest", {})
        to_ref, from_ref = pr.get("toRef", {}), pr.get("fromRef", {})
        repo = to_ref.get("repository", {})
        merge_commit = (pr.get("properties", {}).get("mergeCommit") or {}).get("id") or to_ref.get("latestCommit", "")
        jobs.append({
            "bb_project": repo.get("project", {}).get("key", ""),
            "repo_slug": repo.get("slug", ""),
            "source_branch": from_ref.get("displayId", ""),
            "target_branch": to_ref.get("displayId", ""),
            "merge_commit": merge_commit,
            "base_commit": "",  # 1er parent du merge
            "title": pr.get("title", ""),
            # Le titre prime : une branche peut être réutilisée pour un autre ticket (ex: PR "SPEC-1999 …" depuis novalia_test_cucumber_SPEC-1982)
            "ticket": extract_ticket(pr.get("title", ""), from_ref.get("displayId", ""), pr.get("description", "")),
        })
    elif event_key == "repo:refs_changed":
        repo = payload.get("repository", {})
        project_key, slug = repo.get("project", {}).get("key", ""), repo.get("slug", "")
        for change in payload.get("changes", []):
            if change.get("type") != "UPDATE":
                continue
            message = fetch_commit_message(project_key, slug, change.get("toHash", ""))
            jobs.append({
                "bb_project": project_key,
                "repo_slug": slug,
                "source_branch": "",
                "target_branch": change.get("ref", {}).get("displayId", ""),
                "merge_commit": change.get("toHash", ""),
                "base_commit": change.get("fromHash", ""),
                "title": message.splitlines()[0] if message else "",
                "ticket": extract_ticket(message),
            })
    return jobs


def filter_job(job: dict) -> str:
    """Renvoie la raison d'ignorer la tâche, ou '' si elle doit être traitée."""
    if not job["repo_slug"] or not job["merge_commit"]:
        return "payload incomplet (dépôt ou commit manquant)"
    if TARGET_BRANCHES and job["target_branch"] not in TARGET_BRANCHES:
        return f"branche cible '{job['target_branch']}' non surveillée"
    if any(p in job["repo_slug"].lower() for p in IGNORED_REPOS):
        return f"dépôt '{job['repo_slug']}' ignoré (WEBHOOK_IGNORED_REPOS)"
    if job["title"].upper().startswith("[CUCUMBER]") or "_dev_cucumber_postmerge" in job["source_branch"]:
        return "merge d'un patch Cucumber post-merge (évite une boucle)"
    return ""


# ---------------------------------------------------------------------------
# Dédoublonnage et exécution séquentielle
# ---------------------------------------------------------------------------

_processed_lock = threading.Lock()


def _load_processed() -> dict:
    try:
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def claim_job(job: dict) -> bool:
    """Un merge déclenche à la fois pr:merged et refs_changed : un seul traitement par (dépôt, commit)."""
    key = f"{job['bb_project']}/{job['repo_slug']}@{job['merge_commit']}"
    with _processed_lock:
        processed = _load_processed()
        if key in processed:
            return False
        processed[key] = {"ticket": job["ticket"], "received_at": datetime.now().isoformat(timespec="seconds")}
        os.makedirs(WEBHOOK_DIR, exist_ok=True)
        tmp = PROCESSED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(processed, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PROCESSED_FILE)
    return True


def build_main_command(job: dict) -> list:
    """Arguments de main.py pour l'étape post-merge."""
    source = job["ticket"] or f"{job['repo_slug']}-{job['merge_commit'][:8]}"
    jira_project = source.split("-")[0] if job["ticket"] else job["bb_project"]
    is_std = job["bb_project"].upper() == _std_project_key()
    client_repo = "STD_ONLY" if is_std else f"{BITBUCKET_URL}/projects/{job['bb_project']}"
    cmd = [
        sys.executable, os.path.join(WORKSPACE_DIR, "main.py"),
        "--step", "generate-cucumber-postmerge",
        "--issue", source,
        "--project", jira_project,
        "--client-repo", client_repo,
        "--repo-slug", job["repo_slug"],
        "--merge-commit", job["merge_commit"],
        "--source-branch", job["source_branch"],
        "--target-branch", job["target_branch"],
    ]
    if job["base_commit"]:
        cmd += ["--base-commit", job["base_commit"]]
    return cmd


_jobs: "queue.Queue[dict]" = queue.Queue()


def enqueue_jobs(source: str, jobs: list) -> tuple:
    """Filtre, dédoublonne et met en file les tâches (commun aux modes webhook et --poll)."""
    queued, ignored = [], []
    for job in jobs:
        reason = filter_job(job) or ("" if claim_job(job) else "déjà traité (dépôt + commit)")
        label = f"{job['repo_slug']}@{job['merge_commit'][:8]} ({job['ticket'] or 'sans ticket'})"
        if reason:
            _log(f"⏭️ {source} {label} ignoré : {reason}")
            ignored.append({"job": label, "reason": reason})
        else:
            _log(f"📥 {source} {label} mis en file")
            _jobs.put(job)
            queued.append(label)
    return queued, ignored


def worker() -> None:
    """Un seul traitement à la fois : les dépôts locaux et la session Claude Code sont partagés."""
    while True:
        job = _jobs.get()
        cmd = build_main_command(job)
        label = f"{job['ticket'] or job['repo_slug']}-{job['merge_commit'][:8]}"
        log_path = os.path.join(WEBHOOK_DIR, "logs", f"{datetime.now():%Y%m%d-%H%M%S}-{label}.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        _log(f"▶️ Traitement {label} : {' '.join(cmd[1:])}")
        try:
            if DRY_RUN:
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write("DRY RUN\n" + json.dumps(cmd, ensure_ascii=False))
                _log(f"🧪 DRY RUN : commande non exécutée ({log_path})")
                continue
            with open(log_path, "w", encoding="utf-8") as log_file:
                result = subprocess.run(cmd, cwd=WORKSPACE_DIR, stdout=log_file, stderr=subprocess.STDOUT,
                                        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                outcome = next((l.strip() for l in f if l.startswith("POSTMERGE_RESULT")), "")
            status = "✅" if result.returncode == 0 else f"❌ (code {result.returncode})"
            _log(f"{status} {label} {outcome} — journal : {log_path}")
        except Exception as e:
            _log(f"❌ {label} : {e}")
        finally:
            _jobs.task_done()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "AgentScrumMasterWebhook/1.0"

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # journalisation via _log uniquement
        pass

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"status": "ok", "queued": _jobs.qsize(), "dry_run": DRY_RUN})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/webhook/bitbucket":
            return self._reply(404, {"error": "not found"})

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return self._reply(413 if length > MAX_BODY_BYTES else 400, {"error": "taille du corps invalide"})
        body = self.rfile.read(length)

        if not verify_signature(body, self.headers.get("X-Hub-Signature", "")):
            _log(f"⛔ Signature invalide depuis {self.client_address[0]}")
            return self._reply(401, {"error": "signature invalide"})

        event_key = self.headers.get("X-Event-Key", "")
        if event_key == "diagnostics:ping":
            _log("🏓 Ping Bitbucket reçu")
            return self._reply(200, {"status": "pong"})

        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            return self._reply(400, {"error": "JSON invalide"})

        if event_key not in ("pr:merged", "repo:refs_changed"):
            return self._reply(202, {"status": "ignored", "reason": f"événement '{event_key}' non géré"})

        queued, ignored = enqueue_jobs(event_key, parse_bitbucket_event(event_key, payload))

        # Réponse immédiate : Bitbucket coupe la connexion après quelques secondes
        self._reply(202, {"status": "accepted", "queued": queued, "ignored": ignored})


# ---------------------------------------------------------------------------
# Mode --poll : interrogation périodique de l'API Bitbucket
# ---------------------------------------------------------------------------

POLL_STATE_FILE = os.path.join(WEBHOOK_DIR, "poll_state.json")


def _bitbucket_get(path: str, params: dict = None) -> dict:
    token = os.getenv("BITBUCKET_HTTP_TOKEN", "").strip()
    response = requests.get(f"{BITBUCKET_URL}/rest/api/1.0{path}", params=params,
                            headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=20)
    response.raise_for_status()
    return response.json()


def _paged(path: str, params: dict = None):
    """Parcourt une ressource paginée Bitbucket Data Center."""
    start = 0
    while True:
        page = _bitbucket_get(path, {**(params or {}), "start": start, "limit": 50})
        yield from page.get("values", [])
        if page.get("isLastPage", True):
            return
        start = page.get("nextPageStart", start + 50)


def list_poll_repositories() -> list:
    """Dépôts surveillés (projet, slug) d'après POLL_REPOSITORIES, hors WEBHOOK_IGNORED_REPOS."""
    repos = []
    for entry in os.getenv("POLL_REPOSITORIES", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "/" in entry:
            project, slug = entry.split("/", 1)
            repos.append((project.upper(), slug))
        else:
            repos.extend((entry.upper(), r["slug"]) for r in _paged(f"/projects/{entry.upper()}/repos"))
    return [(p, s) for p, s in repos if not any(i in s.lower() for i in IGNORED_REPOS)]


def fetch_pr_merge_commit(project: str, slug: str, pr_id: int) -> str:
    """Bitbucket ne renvoie pas le commit de merge dans la liste des PR : il figure dans l'activité MERGED."""
    for activity in _paged(f"/projects/{project}/repos/{slug}/pull-requests/{pr_id}/activities"):
        if activity.get("action") == "MERGED":
            return (activity.get("commit") or {}).get("id", "")
    return ""


def fetch_merged_prs_since(project: str, slug: str, since_ms: int) -> list:
    """PR fusionnées après since_ms, de la plus ancienne à la plus récente."""
    prs = []
    for pr in _paged(f"/projects/{project}/repos/{slug}/pull-requests", {"state": "MERGED", "order": "NEWEST"}):
        if pr.get("closedDate", 0) <= since_ms:
            break
        prs.append(pr)
    return sorted(prs, key=lambda p: p.get("closedDate", 0))


def _load_poll_state() -> dict:
    try:
        with open(POLL_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_poll_state(state: dict) -> None:
    tmp = POLL_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, POLL_STATE_FILE)


def poll_once(state: dict, default_since_ms: int) -> None:
    """Un passage : met en file les PR fusionnées depuis le dernier passage, dépôt par dépôt."""
    for project, slug in list_poll_repositories():
        key = f"{project}/{slug}"
        since_ms = state.get(key, default_since_ms)
        try:
            for pr in fetch_merged_prs_since(project, slug, since_ms):
                pr.setdefault("properties", {})["mergeCommit"] = {"id": fetch_pr_merge_commit(project, slug, pr["id"])}
                pr["toRef"].setdefault("repository", {"slug": slug, "project": {"key": project}})
                enqueue_jobs(f"poll PR #{pr['id']}", parse_bitbucket_event("pr:merged", {"pullRequest": pr}))
                since_ms = max(since_ms, pr.get("closedDate", 0))
                state[key] = since_ms
                _save_poll_state(state)
        except requests.RequestException as e:
            _log(f"⚠️ {key} : interrogation Bitbucket impossible ({e}), nouvel essai au prochain passage")
            continue
        if key not in state:
            state[key] = since_ms
            _save_poll_state(state)


# Pilotage par l'IHM (tools/poller_control.py) : fichier d'état rafraîchi + fichier de demande d'arrêt
POLLER_STATUS_FILE = os.path.join(WEBHOOK_DIR, "poller_status.json")
POLLER_STOP_FILE = os.path.join(WEBHOOK_DIR, "poller.stop")
HEARTBEAT_SECONDS = 10


def _write_poller_status(status: dict) -> None:
    status["heartbeat"] = datetime.now().isoformat(timespec="seconds")
    tmp = POLLER_STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    os.replace(tmp, POLLER_STATUS_FILE)


def run_poll(interval: int, since: str, from_now: bool = False) -> None:
    import time

    state = _load_poll_state()
    if since:
        since_ms = int(datetime.fromisoformat(since).timestamp() * 1000)
        state = {}  # rattrapage explicite ; processed.json évite tout double traitement
    else:
        since_ms = int(time.time() * 1000)
        if from_now:
            state = {}  # ignore les merges survenus pendant l'arrêt de l'écoute

    if os.path.exists(POLLER_STOP_FILE):
        os.remove(POLLER_STOP_FILE)
    status = {
        "pid": os.getpid(),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "projects": os.getenv("POLL_REPOSITORIES", ""),
        "interval": interval,
        "state": "running",
        "last_poll": "",
        "repositories": [],
    }
    _write_poller_status(status)

    try:
        repos = list_poll_repositories()
        status["repositories"] = [f"{p}/{s}" for p, s in repos]
        _log(f"🔁 Mode --poll : {len(repos)} dépôt(s) surveillé(s) toutes les {interval}s "
             f"(branches : {', '.join(sorted(TARGET_BRANCHES)) or 'toutes'}{', DRY RUN' if DRY_RUN else ''})")
        _log("   " + ", ".join(status["repositories"]))
        if since:
            _log(f"   Rattrapage des merges depuis {since}")

        while not os.path.exists(POLLER_STOP_FILE):
            try:
                poll_once(state, since_ms)
                status["last_poll"] = datetime.now().isoformat(timespec="seconds")
            except Exception as e:
                _log(f"❌ Passage d'interrogation en échec : {e}")
            # Attente découpée : battement de cœur pour l'IHM et prise en compte rapide d'un arrêt
            deadline = time.time() + interval
            while time.time() < deadline and not os.path.exists(POLLER_STOP_FILE):
                status["queued"] = _jobs.qsize()
                _write_poller_status(status)
                time.sleep(min(HEARTBEAT_SECONDS, max(deadline - time.time(), 0.1)))

        status["state"] = "stopping"
        _log("⏹️ Arrêt demandé : plus aucun nouveau merge ne sera pris ; fin du traitement en cours…")
        while _jobs.unfinished_tasks:
            status["queued"] = _jobs.qsize()
            _write_poller_status(status)
            time.sleep(HEARTBEAT_SECONDS)
        _log("⏹️ Écoute des merges arrêtée.")
    finally:
        for path in (POLLER_STATUS_FILE, POLLER_STOP_FILE):
            if os.path.exists(path):
                os.remove(path)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Déclenchement post-merge de l'Agent Scrum Master")
    parser.add_argument("--poll", action="store_true", help="Interroger l'API Bitbucket au lieu d'attendre un webhook")
    parser.add_argument("--interval", type=int, default=int(os.getenv("POLL_INTERVAL_SECONDS", "300")))
    parser.add_argument("--since", default="", help="Rattrapage : date ISO, ex. '2026-10-01 14:00'")
    parser.add_argument("--projects", default="", help="Remplace POLL_REPOSITORIES, ex. 'NOVALIA,OSCARO/odatio-oscaro'")
    parser.add_argument("--from-now", action="store_true", help="Ignorer les merges survenus pendant l'arrêt de l'écoute")
    args = parser.parse_args()

    if not BITBUCKET_URL:
        sys.exit("BITBUCKET_URL est absent du .env : renseignez-le (onglet Configuration de l'IHM).")
    if args.poll:
        if args.projects:
            os.environ["POLL_REPOSITORIES"] = args.projects
        if not os.getenv("POLL_REPOSITORIES", "").strip():
            sys.exit("Aucun projet à écouter : renseignez POLL_REPOSITORIES ou --projects (ex. 'NOVALIA,OSCARO/odatio-oscaro').")

    os.makedirs(WEBHOOK_DIR, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    if args.poll:
        run_poll(max(args.interval, 30), args.since, args.from_now)
        return

    if not SECRET and not ALLOW_UNSIGNED:
        sys.exit("BITBUCKET_WEBHOOK_SECRET est absent du .env. Définissez-le (et dans Bitbucket), "
                 "ou WEBHOOK_ALLOW_UNSIGNED=true pour un test local. Sans webhook possible : --poll.")
    host = os.getenv("WEBHOOK_HOST", "0.0.0.0")
    port = int(os.getenv("WEBHOOK_PORT", "8085"))
    _log(f"🚀 Écoute des webhooks Bitbucket sur http://{host}:{port}/webhook/bitbucket "
         f"(branches : {', '.join(sorted(TARGET_BRANCHES)) or 'toutes'}{', DRY RUN' if DRY_RUN else ''})")
    if not SECRET:
        _log("⚠️ Requêtes non signées acceptées (WEBHOOK_ALLOW_UNSIGNED=true) : à réserver aux tests.")
    ThreadingHTTPServer((host, port), WebhookHandler).serve_forever()


if __name__ == "__main__":
    main()
