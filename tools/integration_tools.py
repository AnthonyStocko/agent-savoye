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


def application_corpus_path(issue_key: str) -> str:
    """Fichier des tickets de référence du même Projet / Application, consulté par l'agent."""
    return os.path.join(INTEGRATION_DIR, f"{issue_key}.application.md")


def write_application_corpus(issue_key: str, applications: list, tickets: list) -> str:
    """Écrit description et commentaires des tickets de référence. Renvoie le chemin du fichier."""
    lines = [f"# Tickets du Projet / Application {', '.join(applications)} (référence pour {issue_key})", ""]
    for t in tickets:
        lines += [f"## {t['key']} — {t['summary']}",
                  f"Projet : {t['project']} | Type : {t['type']} | Statut : {t['status']} | Mis à jour : {t['updated']}",
                  "", "### Description", t["description"].strip() or "(vide)", ""]
        if t["comments"]:
            lines.append("### Commentaires")
            for c in t["comments"]:
                lines += [f"- {c['date']} {c['author']} : {c['text'].strip()}"]
            lines.append("")
    os.makedirs(INTEGRATION_DIR, exist_ok=True)
    path = application_corpus_path(issue_key)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def application_block(applications: list, tickets: list, corpus_path: str) -> str:
    """Index des tickets de référence injecté dans le prompt (le détail reste dans le fichier)."""
    if not tickets:
        return ""
    lines = [
        f"=== TICKETS DU PROJET / APPLICATION {', '.join(applications)} ({len(tickets)} tickets de référence) ===",
        f"Index ci-dessous ; descriptions et commentaires complets dans '{corpus_path}'.",
        "- Avant de poser une question, cherche sa réponse dans ce fichier : Grep sur les mots-clés métier ou une clé "
        "de ticket, puis lis uniquement les passages utiles (offset / limit). Ne lis pas le fichier en entier.",
        "- Sers-t'en pour reprendre le vocabulaire du client, les règles et paramétrages déjà en place, les décisions "
        "déjà prises et les dépendances avec d'autres tickets.",
        "- Cite les tickets utilisés dans le besoin reformulé (ex. « cf. SPEC-2055 »). Ne transpose pas une règle "
        "d'un autre ticket si elle ne s'applique pas clairement : pose plutôt la question en citant ce ticket.",
    ]
    lines += [f"- {t['key']} [{t['project']} · {t['type']} · {t['status']}] {t['summary']}" for t in tickets]
    lines.append("=== FIN DES TICKETS DE RÉFÉRENCE ===")
    return "\n".join(lines)


def cadrage_answers_block(state: dict) -> str:
    """Besoin reformulé et réponses obtenues, pour compléter le cadrage."""
    lines = ["=== BESOIN REFORMULÉ (onglet Intégration) ===", state.get("besoin", "").strip() or "(vide)",
             "=== RÉPONSES DU MÉTIER ==="]
    for q in answered(state):
        lines += [f"- Q{q['id']}. {q['question']}", f"  Réponse : {q['reponse'].strip()}"]
    lines.append("=== FIN DES RÉPONSES ===")
    return "\n".join(lines)


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
