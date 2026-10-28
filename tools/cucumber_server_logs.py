"""
Logs applicatifs du serveur de l'environnement de test, déposés dans un répertoire (CUCUMBER_SERVER_LOG_DIR) :
lus à chaque import d'un run (0 token), sans export ni réimport manuel.

- Fichiers lus : *.log, *.txt, *.csv, *.out, leurs rotations (server.log.1, server.log.2026-10-28) et leurs versions
  .gz, dans le répertoire et ses sous-dossiers. Un fichier modifié pour la dernière fois avant la première fenêtre
  est ignoré (il ne peut pas la contenir).
- Lecture en flux : seules les lignes horodatées à CUCUMBER_SERVER_LOG_MARGIN secondes (120) au plus de l'heure
  d'un step en échec sont gardées, avec leurs lignes de continuation (pile d'exception) ; horodatages lus comme
  UTC ou comme heure locale (l'un des deux suffit), comme pour les exports. Plafond CUCUMBER_SERVER_LOG_MAX_LINES.
- Secrets masqués avant stockage. L'extrait est écrit dans le dossier de l'import (logs-serveur.txt) : l'analyse
  en tire les lignes ERROR / WARN de chaque échec, le correcteur peut le parcourir par Grep.
"""
import gzip
import os
import re
from datetime import datetime, timedelta, timezone

from tools.cucumber_verify import _LOG_STAMP, _timestamp
from tools.secret_tools import MASK, mask_secrets

LOG_FILE = re.compile(r"\.(log|txt|csv|out)(\.[\w.-]+)?(\.gz)?$|\.log\.\d+$", re.I)
EXTRACT_NAME = "logs-serveur.txt"
CONTINUATION_LINES = 40
# Secrets écrits en clair dans une ligne de log (clé=valeur, clé: valeur, "clé":"valeur")
_LOG_SECRET = re.compile(r"(?i)(\b(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|authorization)\b\"?\s*[=:]\s*\"?)"
                         r"(?:Bearer\s+|Basic\s+)?[^\s\"',;&]+")


def log_dir() -> str:
    return os.getenv("CUCUMBER_SERVER_LOG_DIR", "").strip()


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def _files(directory: str, earliest: datetime) -> list:
    out = []
    for root, _, names in os.walk(directory):
        for name in sorted(names):
            path = os.path.join(root, name)
            if not LOG_FILE.search(name):
                continue
            try:
                modified = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
            except OSError:
                continue
            if modified >= earliest:
                out.append(path)
    return out


def _open(path: str):
    if path.lower().endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def _readings(date: str, clock: str) -> tuple:
    naive = datetime.fromisoformat(f"{date}T{clock}")
    return naive.replace(tzinfo=timezone.utc), naive.astimezone()


def extract(directory: str, when: list) -> dict:
    """
    Lignes des logs du `directory` proches des heures `when` (ISO, UTC). Renvoie {"texte", "lignes", "fichiers",
    "tronque"} ; texte vide si rien ne correspond.
    """
    stamps = [s for s in (_timestamp(w) for w in when or []) if s]
    if not directory or not os.path.isdir(directory) or not stamps:
        return {"texte": "", "lignes": 0, "fichiers": [], "tronque": False, "plages": []}
    margin = timedelta(seconds=_int_env("CUCUMBER_SERVER_LOG_MARGIN", 120))
    cap = _int_env("CUCUMBER_SERVER_LOG_MAX_LINES", 20000)
    # Fenêtres fusionnées : [début, fin] autour de chaque échec
    windows = []
    for stamp in sorted(stamps):
        start, end = stamp - margin, stamp + margin
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    inside = lambda t: any(a <= t <= b for a, b in windows)  # noqa: E731
    kept, used, truncated, ranges = [], [], False, []
    # Marge d'une journée sur la date de modification : heure locale contre UTC
    for path in _files(directory, windows[0][0] - timedelta(days=1)):
        name, before, continuation = os.path.relpath(path, directory), len(kept), 0
        first = last = None
        try:
            with _open(path) as f:
                for line in f:
                    found = _LOG_STAMP.search(line[:64])
                    if found:
                        stamp = f"{found.group(1)}T{found.group(2)}"
                        first, last = first or stamp, stamp
                        keep = any(inside(t) for t in _readings(*found.groups()))
                        continuation = CONTINUATION_LINES if keep else 0
                        if not keep:
                            continue
                    elif continuation:
                        continuation -= 1
                    else:
                        continue
                    kept.append(f"[{name}] {line.rstrip()}")
                    if cap and len(kept) >= cap:
                        truncated = True
                        break
        except OSError:
            continue
        if first:
            ranges.append([first, last])
        if len(kept) > before:
            used.append(name)
        if truncated:
            break
    text = mask_secrets(_LOG_SECRET.sub(lambda m: m.group(1) + MASK, "\n".join(kept)))[0]
    return {"texte": text, "lignes": len(kept), "fichiers": used, "tronque": truncated, "plages": ranges}


def covers(ranges: list, when: str) -> bool:
    """L'heure `when` (UTC) du step est-elle dans la plage horaire d'un fichier lu (UTC ou heure locale) ? Un fichier
    lu jusqu'au bout couvre toute sa plage, contrairement à un export plafonné."""
    stamp = _timestamp(when)
    if not stamp:
        return False
    for first, last in ranges or []:
        (a_utc, a_local), (b_utc, b_local) = _readings(*first.split("T")), _readings(*last.split("T"))
        if a_utc <= stamp <= b_utc or a_local <= stamp <= b_local:
            return True
    return False
