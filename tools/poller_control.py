"""
Pilotage depuis l'IHM de l'écoute des merges Bitbucket (`webhook_server.py --poll`).

L'écoute tourne dans un processus détaché : elle survit aux rechargements de Streamlit.
Son état est lu dans .tmp/webhook/poller_status.json, rafraîchi toutes les 10 s par le processus.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBHOOK_DIR = os.path.join(WORKSPACE_DIR, ".tmp", "webhook")
STATUS_FILE = os.path.join(WEBHOOK_DIR, "poller_status.json")
STOP_FILE = os.path.join(WEBHOOK_DIR, "poller.stop")
LOG_FILE = os.path.join(WEBHOOK_DIR, "server.log")
STALE_AFTER_SECONDS = 45


def get_poller_status() -> dict:
    """État de l'écoute, ou {} si elle est arrêtée (absence ou battement de cœur trop ancien)."""
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            status = json.load(f)
    except (OSError, ValueError):
        return {}
    age = (datetime.now() - datetime.fromisoformat(status.get("heartbeat", "1970-01-01T00:00:00"))).total_seconds()
    return status if age <= STALE_AFTER_SECONDS else {}


def start_poller(projects: str, interval_seconds: int, catch_up: bool) -> tuple:
    """Démarre l'écoute en arrière-plan. Renvoie (succès, message)."""
    if get_poller_status():
        return False, "L'écoute est déjà active."
    os.makedirs(WEBHOOK_DIR, exist_ok=True)
    for path in (STOP_FILE, STATUS_FILE):
        if os.path.exists(path):
            os.remove(path)

    cmd = [sys.executable, "-u", os.path.join(WORKSPACE_DIR, "webhook_server.py"), "--poll",
           "--projects", projects, "--interval", str(interval_seconds)]
    if not catch_up:
        cmd.append("--from-now")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    log = open(LOG_FILE, "a", encoding="utf-8")
    subprocess.Popen(cmd, cwd=WORKSPACE_DIR, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    log.close()

    for _ in range(30):  # le processus écrit son état dès son démarrage
        time.sleep(0.5)
        if get_poller_status():
            return True, "Écoute démarrée."
    return False, "L'écoute n'a pas démarré : consultez le journal."


def request_stop() -> None:
    """Arrêt propre : plus de nouveau merge, le traitement en cours va jusqu'au bout."""
    os.makedirs(WEBHOOK_DIR, exist_ok=True)
    open(STOP_FILE, "w").close()


def force_stop() -> str:
    """Arrêt immédiat de l'écoute ET de l'agent éventuellement en cours (arbre de processus)."""
    status = get_poller_status()
    if not status:
        return "L'écoute n'est pas active."
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(status["pid"]), "/T", "/F"], capture_output=True)
    else:
        os.killpg(os.getpgid(status["pid"]), 9)
    for path in (STATUS_FILE, STOP_FILE):
        if os.path.exists(path):
            os.remove(path)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ⛔ Écoute arrêtée de force depuis l'IHM.\n")
    return "Écoute arrêtée de force."


def tail_log(lines: int = 40) -> str:
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return ""
