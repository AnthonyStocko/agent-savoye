"""
Assistant de l'onglet « 💬 Assistant » : questions libres sur les fonctionnalités de l'agent et sur ses résultats.

- results_index : carte des résultats disponibles (0 token) — par client Cucumber : état, imports et bilans, patchs,
  A_REVOIR.md, bilan de santé ; revues de PR ; journal technique ; consommation — donnée à l'agent pour qu'il
  sache où chercher sans tout parcourir ;
- history : conversation gardée dans .tmp/assistant/conversation.json (questions et réponses, les plus récentes
  rappelées à chaque question) ;
- l'agent lit en lecture seule (Read, Grep, Glob) le code de l'application et ces résultats ; jamais le .env.
"""
import json
import os
from datetime import datetime

from tools.secret_tools import mask_secrets

WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(WORKSPACE_DIR, ".tmp")
ROOT = os.path.join(TMP, "assistant")
HISTORY = os.path.join(ROOT, "conversation.json")


def _exists(path: str) -> bool:
    return bool(path) and os.path.exists(path)


def _when(path: str) -> str:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%d/%m %H:%M")
    except OSError:
        return "?"


def results_index() -> str:
    """Où sont les résultats, client par client (chemins absolus, date de mise à jour)."""
    lines = []
    cucumber = os.path.join(TMP, "cucumber")
    for client in sorted(os.listdir(cucumber)) if os.path.isdir(cucumber) else []:
        folder = os.path.join(cucumber, client)
        if not os.path.isdir(folder):
            continue
        lines.append(f"- Cucumber, client « {client} » : dossier '{folder}'")
        for name, what in (("etat.json", "état de la génération (lots, vérifications, corrections, mises de côté)"),
                           ("inventaire.json", "fonctionnalités spécifiques détectées"),
                           ("profil.json", "profil client"), ("decisions.json", "choix et décisions"),
                           ("detection.json", "détection automatique")):
            path = os.path.join(folder, name)
            if _exists(path):
                lines.append(f"  - {what} : '{path}' ({_when(path)})")
        imports = os.path.join(folder, "imports")
        numbers = sorted((int(n) for n in os.listdir(imports) if n.isdigit()), reverse=True) \
            if os.path.isdir(imports) else []
        if numbers:
            lines.append(f"  - runs Jenkins importés (n° {', '.join(map(str, numbers[:8]))}) : '{imports}/<n°>/' "
                         f"(analyse.md, bilan.md, analyse.json, logs-serveur.txt) — le plus récent : n° {numbers[0]}")
        review = os.path.join(folder, "worktree", "A_REVOIR.md")
        if _exists(review):
            lines.append(f"  - A_REVOIR.md livré (désactivés, défauts, bilan du dernier run) : '{review}'")
        lines.append(f"  - dépôt de travail de l'agent (features et steps écrits) : '{os.path.join(folder, 'worktree')}'")
        for name, what in ((f"{client}-cucumber.patch", "modifications à appliquer (tests)"),
                           (f"{client}-cucumber.patch.ecarts.txt", "modifications écartées du patch"),
                           (f"{client}-cucumber.patch.donnees-manquantes.txt", "fichiers de données introuvables"),
                           (f"{client}-cucumber-init.patch", "modifications d'initialisation"),
                           (f"{client}-cucumber-sante.md", "bilan de santé du dépôt"),
                           (f"{client}-cucumber-couverture.md", "récapitulatif de couverture")):
            path = os.path.join(WORKSPACE_DIR, name)
            if _exists(path):
                lines.append(f"  - {what} : '{path}' ({_when(path)})")
    reviews = os.path.join(TMP, "pr_review", "revues")
    if os.path.isdir(reviews) and os.listdir(reviews):
        lines.append(f"- Revues de PR (une par fonctionnalité, JSON) : '{reviews}'")
    for path, what in ((os.path.join(TMP, "journal_technique.log"), "journal technique (sortie brute des traitements)"),
                       (os.path.join(TMP, "token_usage.csv"), "consommation de tokens par exécution")):
        if _exists(path):
            lines.append(f"- {what} : '{path}' ({_when(path)})")
    return "\n".join(lines) or "(aucun résultat enregistré pour l'instant)"


def load_history() -> list:
    try:
        with open(HISTORY, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_history(messages: list) -> None:
    os.makedirs(ROOT, exist_ok=True)
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(messages[-200:], f, ensure_ascii=False, indent=1)


def clear_history() -> None:
    if os.path.exists(HISTORY):
        os.remove(HISTORY)


def recent_exchanges(messages: list, count: int = 6, max_chars: int = 1500) -> str:
    """Derniers échanges rappelés à l'agent (contexte de la conversation)."""
    out = []
    for message in messages[-2 * count:]:
        who = "Utilisateur" if message.get("role") == "user" else "Assistant"
        out.append(f"{who} : {str(message.get('content', ''))[:max_chars]}")
    return "\n\n".join(out) or "(début de la conversation)"


def clean_answer(text: str) -> str:
    """Réponse affichée : secrets masqués, espaces de fin retirés."""
    return mask_secrets((text or "").strip())[0]



def known_clients() -> list:
    cucumber = os.path.join(TMP, "cucumber")
    return sorted(d for d in os.listdir(cucumber) if os.path.isdir(os.path.join(cucumber, d))) \
        if os.path.isdir(cucumber) else []


def subject_project(question: str, answer: str = "") -> tuple:
    """(projet, client) de l'analyse : client cité dans la question (sinon dans la réponse), sinon projet par défaut."""
    for text in (question, answer):
        for client in known_clients():
            if client and client.lower() in (text or "").lower():
                return client.upper(), client
    return (os.getenv("JIRA_PROJECT_KEY", "").strip().upper() or "GENERAL"), ""


def analysis_markdown(question: str, answer: str) -> str:
    return f"# Question\n\n{question.strip()}\n\n# Réponse de l'assistant\n\n{answer.strip()}\n"
