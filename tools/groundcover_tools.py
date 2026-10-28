"""
Logs applicatifs de l'environnement de test dans groundcover (API logs v2 : POST /api/logs/v2/search, requête gcQL),
en lecture seule.

Configuration (onglet Configuration) :
- GROUNDCOVER_API_KEY : clé d'API (groundcover, Settings → Access → API Keys) ;
- GROUNDCOVER_BACKEND_ID : identifiant du backend (comptes multi-backend seulement) ;
- GROUNDCOVER_API_URL : défaut https://api.groundcover.com ;
- CUCUMBER_GROUNDCOVER_FILTER : filtre gcQL de l'environnement où Jenkins joue les tests (ex. « namespace:apparel-test »),
  ajouté à TOUTE requête : l'agent ne lit jamais les logs d'un autre environnement.

Deux usages :
1. window_extract (import d'un run, 0 token) : lignes de la fenêtre de chaque échec, au même format que les fichiers
   d'un répertoire de logs (cucumber_server_logs) ;
2. CLI pour le correcteur : python tools/groundcover_tools.py search --start … --end … --query "TOW-730" [--limit N],
   bornée (fenêtre de 30 minutes, 200 lignes), secrets masqués, journalisée (GROUNDCOVER_JOURNAL).
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from tools.secret_tools import MASK, mask_secrets  # noqa: E402

DEFAULT_URL = "https://api.groundcover.com"
MAX_WINDOW = timedelta(minutes=30)
MAX_LINES = 200
FIELDS = "_time, workload, level, content"
_LOG_SECRET = re.compile(r"(?i)(\b(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|authorization)\b\"?\s*[=:]\s*\"?)"
                         r"(?:Bearer\s+|Basic\s+)?[^\s\"',;&]+")
# Le correcteur ne peut ni changer de filtre d'environnement ni enchaîner d'autres pipes que les siens
_PIPE = re.compile(r"\|")


class GroundcoverError(RuntimeError):
    pass


def config() -> dict:
    return {"url": (os.getenv("GROUNDCOVER_API_URL", "").strip() or DEFAULT_URL).rstrip("/"),
            "key": os.getenv("GROUNDCOVER_API_KEY", "").strip(),
            "backend": os.getenv("GROUNDCOVER_BACKEND_ID", "").strip(),
            "filter": os.getenv("CUCUMBER_GROUNDCOVER_FILTER", "").strip()}


def configured() -> bool:
    """Clé ET filtre d'environnement : sans filtre, on lirait les logs de tous les environnements."""
    c = config()
    return bool(c["key"] and c["filter"])


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mask(text: str) -> str:
    return mask_secrets(_LOG_SECRET.sub(lambda m: m.group(1) + MASK, text or ""))[0]


def search(start: datetime, end: datetime, terms: str = "", limit: int = MAX_LINES, timeout: int = 30) -> list:
    """
    Lignes de logs de l'environnement de test entre `start` et `end` (UTC), filtre d'environnement imposé, plus
    `terms` (texte libre ou filtres gcQL, sans pipe). Tri chronologique. Renvoie [{_time, workload, level, content}].
    """
    c = config()
    if not configured():
        raise GroundcoverError("groundcover non configuré (GROUNDCOVER_API_KEY et CUCUMBER_GROUNDCOVER_FILTER)")
    if _PIPE.search(terms or ""):
        raise GroundcoverError("pipe interdit dans la recherche (tri, champs et limite sont imposés)")
    query = f"({c['filter']})" + (f" AND ({terms})" if terms and terms.strip() else "")
    query += f" | sort by (_time asc) | fields {FIELDS} | limit {max(1, int(limit))}"
    headers = {"Authorization": f"Bearer {c['key']}", "Content-Type": "application/json"}
    if c["backend"]:
        headers["X-Backend-Id"] = c["backend"]
    try:
        response = requests.post(f"{c['url']}/api/logs/v2/search", headers=headers, timeout=timeout,
                                 json={"start": _iso(start), "end": _iso(end), "query": query})
    except requests.RequestException as e:
        raise GroundcoverError(f"groundcover injoignable : {type(e).__name__}") from e
    if response.status_code != 200:
        raise GroundcoverError(f"groundcover a répondu HTTP {response.status_code} : {response.text[:200]}")
    try:
        rows = response.json()
    except ValueError as e:
        raise GroundcoverError("réponse groundcover illisible") from e
    return rows if isinstance(rows, list) else rows.get("data") or rows.get("results") or []


def format_row(row: dict) -> str:
    """Ligne au format des fichiers de logs : « [workload] 2026-10-28T17:28:30 LEVEL contenu »."""
    when = str(row.get("_time", ""))[:19]
    content = " ".join(str(row.get("content", "")).split())
    return mask(f"[{row.get('workload') or 'groundcover'}] {when} {str(row.get('level', '')).upper()} {content}")


def window_extract(when: list, margin_seconds: int = 120, per_window: int = 2000) -> dict:
    """
    Import d'un run : lignes de la fenêtre (± marge) de chaque échec (heures ISO, UTC). Fenêtres voisines fusionnées.
    Renvoie {"texte", "lignes", "fichiers", "tronque", "plages", "erreur"} (format de cucumber_server_logs.extract).
    """
    from tools.cucumber_verify import _timestamp
    empty = {"texte": "", "lignes": 0, "fichiers": [], "tronque": False, "plages": [], "erreur": ""}
    stamps = sorted(s for s in (_timestamp(w) for w in when or []) if s)
    if not stamps or not configured():
        return empty
    margin, windows = timedelta(seconds=margin_seconds), []
    for stamp in stamps:
        if windows and stamp - margin <= windows[-1][1]:
            windows[-1][1] = stamp + margin
        else:
            windows.append([stamp - margin, stamp + margin])
    lines, ranges, truncated = [], [], False
    for start, end in windows[:30]:
        try:
            rows = search(start, end, limit=per_window)
        except GroundcoverError as e:
            return {**empty, "texte": "\n".join(lines), "lignes": len(lines), "plages": ranges, "erreur": str(e)}
        lines += [format_row(r) for r in rows]
        if len(rows) >= per_window:
            # Tri chronologique : la fin de la fenêtre manque, seule la partie lue est couverte
            truncated = True
            last = str(rows[-1].get("_time", ""))[:19]
            ranges.append([start.strftime("%Y-%m-%dT%H:%M:%S"), last or start.strftime("%Y-%m-%dT%H:%M:%S")])
        else:
            ranges.append([start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")])
    return {"texte": "\n".join(lines), "lignes": len(lines), "fichiers": ["groundcover"] if lines else [],
            "tronque": truncated, "plages": ranges, "erreur": ""}


def _journal(entry: dict) -> None:
    path = os.getenv("GROUNDCOVER_JOURNAL", "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _parse_time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Recherche en lecture seule dans les logs groundcover de "
                                                 "l'environnement de test Cucumber")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("search")
    s.add_argument("--start", required=True, help="début, ISO 8601 (UTC si sans fuseau)")
    s.add_argument("--end", required=True, help="fin, ISO 8601 (UTC si sans fuseau)")
    s.add_argument("--query", default="", help="texte libre ou filtres gcQL (level:error, workload:x), sans pipe")
    s.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        start, end = _parse_time(args.start), _parse_time(args.end)
    except ValueError:
        print("GROUNDCOVER_ERROR=heure illisible (ISO 8601 attendu, ex. 2026-10-28T17:28:00Z)")
        return 2
    if end <= start or end - start > MAX_WINDOW:
        print(f"GROUNDCOVER_ERROR=fenêtre de {int(MAX_WINDOW.total_seconds() // 60)} minutes au plus, fin après début")
        return 2
    try:
        rows = search(start, end, args.query, min(max(1, args.limit), MAX_LINES))
    except GroundcoverError as e:
        _journal({"date": datetime.now().isoformat(timespec="seconds"), "ok": False, "erreur": str(e)[:300]})
        print(f"GROUNDCOVER_ERROR={e}")
        return 1
    lines = [format_row(r) for r in rows]
    _journal({"date": datetime.now().isoformat(timespec="seconds"), "ok": True, "debut": _iso(start),
              "fin": _iso(end), "recherche": args.query[:200], "lignes": lines})
    print("\n".join(lines) if lines else "(aucune ligne)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
