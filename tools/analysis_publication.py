"""
Publication des analyses dans Jira, uniquement à la validation de l'utilisateur.

Avant validation, l'analyse n'existe que localement ('<TICKET>-cadrage.md' et mémoire de l'agent). La validation
publie le fichier dans la description du ticket (update_jira_cadrage, déterministe) et enregistre l'empreinte du
texte publié : une analyse révisée après publication ne correspond plus à cette empreinte et redevient un brouillon.
"""
import hashlib
import json
import os
from datetime import datetime

from tools import integration_tools
from tools.jira_tools import update_jira_cadrage

PUBLICATION_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp", "publication")


def cadrage_path(issue_key: str) -> str:
    return f"{issue_key}-cadrage.md"


def _fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").strip().replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _record_path(issue_key: str) -> str:
    return os.path.join(PUBLICATION_DIR, f"{issue_key}.json")


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def load_record(issue_key: str) -> dict:
    try:
        with open(_record_path(issue_key), "r", encoding="utf-8") as f:
            record = json.load(f)
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def record_publication(issue_key: str, text: str, when: datetime = None) -> dict:
    """Mémorise que `text` est la version publiée dans Jira (date et empreinte)."""
    record = {"empreinte": _fingerprint(text),
              "publiee_le": (when or datetime.now()).isoformat(timespec="minutes")}
    os.makedirs(PUBLICATION_DIR, exist_ok=True)
    with open(_record_path(issue_key), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return record


def status(issue_key: str, text: str = None) -> dict:
    """
    État de l'analyse : {"publiee": bool, "publiee_le": date de la dernière publication ou "", "libelle": texte}.
    `text` : analyse à évaluer (par défaut le fichier '<TICKET>-cadrage.md').
    """
    text = _read(cadrage_path(issue_key)) if text is None else text
    record = load_record(issue_key)
    published_at = record.get("publiee_le", "")
    published = bool(text.strip()) and record.get("empreinte") == _fingerprint(text)
    date_label = published_at.replace("T", " à ") if published_at else ""
    if published:
        label = f"Publiée dans Jira le {date_label}"
    elif published_at:
        label = f"Analyse non publiée (brouillon) : révisée depuis sa publication du {date_label}"
    else:
        label = "Analyse non publiée (brouillon)"
    return {"publiee": published, "publiee_le": published_at, "libelle": label}


def save_draft(issue_key: str, text: str) -> None:
    """Enregistre l'analyse localement, sans rien publier."""
    with open(cadrage_path(issue_key), "w", encoding="utf-8") as f:
        f.write(text)


def publish(issue_key: str, text: str = None, publisher=None) -> dict:
    """
    Publie l'analyse validée dans la description Jira (à la place de l'ancienne ; la section « Intégration du
    besoin » et la release note sont conservées). Sans effet si cette version est déjà publiée.
    Renvoie le statut ; lève RuntimeError (ou requests.RequestException) si Jira refuse.
    """
    text = _read(cadrage_path(issue_key)) if text is None else text
    if not text.strip():
        raise RuntimeError(f"Aucune analyse à publier pour {issue_key}.")
    if status(issue_key, text)["publiee"]:
        return status(issue_key, text)
    publisher = publisher or update_jira_cadrage
    publisher(issue_key, text, stop_titles=(integration_tools.SECTION_TITLE,))
    record_publication(issue_key, text)
    return status(issue_key, text)


def validate(issue_key: str, text: str, publisher=None) -> dict:
    """Validation de l'utilisateur : enregistre l'analyse relue puis la publie dans Jira."""
    save_draft(issue_key, text)
    return publish(issue_key, text, publisher)
