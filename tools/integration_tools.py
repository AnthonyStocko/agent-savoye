"""
Onglet « Intégration » : besoin reformulé par l'agent et questions ouvertes, avec leurs réponses.

L'état de chaque ticket est conservé dans .tmp/integration/<CLÉ>.json :
    {"issue": "SPEC-1", "besoin": "<markdown>", "updated_at": "...",
     "questions": [{"id": 1, "question": "...", "pourquoi": "...", "reponse": ""}]}
Il est publié dans la section « Intégration du besoin » de la description Jira, le reste de la
description restant intact.
"""
import json
import os
import re
from datetime import datetime

from tools.jira_tools import update_jira_section

INTEGRATION_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp", "integration")
SECTION_TITLE = "Intégration du besoin"


def state_path(issue_key: str) -> str:
    return os.path.join(INTEGRATION_DIR, f"{issue_key}.json")


def agent_output_path(issue_key: str) -> str:
    """Fichier JSON écrit par l'agent à l'étape « besoin »."""
    return os.path.join(INTEGRATION_DIR, f"{issue_key}.agent.json")


def load_state(issue_key: str) -> dict:
    try:
        with open(state_path(issue_key), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(issue_key: str, state: dict) -> None:
    os.makedirs(INTEGRATION_DIR, exist_ok=True)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(state_path(issue_key), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_agent_output(path: str) -> dict:
    """Lit et valide le JSON de l'agent. Lève ValueError s'il est absent ou mal formé."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise ValueError(f"fichier '{path}' introuvable ({e})") from e
    except ValueError as e:
        raise ValueError(f"JSON invalide dans '{path}' ({e})") from e
    if not isinstance(data, dict) or not str(data.get("besoin", "")).strip():
        raise ValueError(f"'{path}' ne contient pas de champ « besoin » rédigé")
    questions = data.get("questions") or []
    if not isinstance(questions, list):
        raise ValueError(f"« questions » doit être une liste dans '{path}'")
    data["questions"] = [q for q in questions if isinstance(q, dict) and str(q.get("question", "")).strip()]
    return data


def answered(state: dict) -> list:
    return [q for q in state.get("questions", []) if q.get("reponse", "").strip()]


def merge_agent_output(previous: dict, agent: dict) -> dict:
    """
    Nouvel état : besoin rédigé par l'agent, questions déjà répondues conservées avec leur réponse,
    puis questions de l'agent. Une question encore ouverte reposée par l'agent (même id) garde son numéro.
    """
    kept = answered(previous)
    kept_ids = {q["id"] for q in kept}
    open_ids = {q["id"] for q in previous.get("questions", []) if q["id"] not in kept_ids}
    next_id = max([q["id"] for q in previous.get("questions", [])] or [0]) + 1
    questions = list(kept)
    for q in agent.get("questions", []):
        qid = q.get("id")
        if not (isinstance(qid, int) and qid in open_ids):
            qid, next_id = next_id, next_id + 1
        open_ids.discard(qid)
        questions.append({"id": qid, "question": str(q["question"]).strip(),
                          "pourquoi": str(q.get("pourquoi", "")).strip(), "reponse": ""})
    return {"issue": previous.get("issue") or agent.get("issue", ""),
            "besoin": str(agent["besoin"]).strip(), "questions": questions}


def _demote_headings(markdown: str) -> str:
    """Titres du besoin en niveau 4 minimum : ils restent à l'intérieur de la section Jira."""
    return re.sub(r"^\s*#{1,3}(?=\s)", "####", markdown, flags=re.MULTILINE)


def render_section_markdown(state: dict) -> str:
    """Contenu Markdown de la section « Intégration du besoin » (sans son titre)."""
    lines = ["### Besoin reformulé", "", _demote_headings(state.get("besoin", "").strip()), ""]
    questions = state.get("questions", [])
    pending = len(questions) - len(answered(state))
    lines += [f"### Questions ({pending} en attente de réponse)" if questions else "### Questions", ""]
    if not questions:
        lines.append("Aucune question ouverte.")
    for q in questions:
        lines.append(f"**Q{q['id']}.** {q['question']}")
        if q.get("pourquoi"):
            lines.append(f"Pourquoi : {q['pourquoi']}")
        reponse = q.get("reponse", "").strip()
        if reponse:
            first, *rest = reponse.splitlines()
            lines.append(f"**Réponse :** {first}")
            lines += [line for line in rest if line.strip()]
        else:
            lines.append("*En attente de réponse.*")
        lines.append("")
    return "\n".join(lines).strip()


def publish_state(issue_key: str, state: dict) -> None:
    """Publie la section dans la description Jira. Lève RuntimeError en cas d'échec."""
    update_jira_section(issue_key, SECTION_TITLE, render_section_markdown(state))


def previous_qa_block(state: dict) -> str:
    """Rappel, pour l'agent, des questions déjà posées et des réponses obtenues."""
    if not state.get("questions"):
        return ""
    lines = ["=== QUESTIONS DÉJÀ POSÉES LORS D'UNE RÉDACTION PRÉCÉDENTE ==="]
    for q in state["questions"]:
        reponse = q.get("reponse", "").strip()
        lines.append(f"- [id {q['id']}] {q['question']}")
        lines.append(f"  Réponse : {reponse}" if reponse else "  (sans réponse à ce jour)")
    lines.append("=== FIN DES QUESTIONS PRÉCÉDENTES ===")
    return "\n".join(lines)
