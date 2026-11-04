"""
Garde-fou Jira des étapes d'analyse : tant que l'utilisateur n'a pas validé l'analyse, l'agent ne modifie
jamais un ticket Jira. Pendant ces étapes, le sous-processus Claude :
- ne charge aucun serveur MCP ni connecteur claude.ai (donc aucun outil Jira) ;
- reçoit un environnement en lecture seule : jeton Jira de lecture (JIRA_READ_EMAIL / JIRA_READ_API_TOKEN) s'il
  est configuré, sinon aucun jeton, et AGENT_JIRA_READ_ONLY=1 qui fait refuser toute écriture par tools.jira_tools ;
- ne peut pas lire le fichier .env (où se trouve le jeton d'écriture).
La description des tickets concernés est relevée avant et après l'étape : toute modification est une erreur.
"""
import json
import os

from tools.jira_tools import READ_ONLY_ENV, get_epic_children, get_jira_description

# Étapes dont le sous-processus Claude n'écrit jamais dans Jira (epic-cadrage couvre aussi la révision d'épopée)
READ_ONLY_STEPS = ("cadrage", "refine", "cadrage-reponses", "epic-cadrage")

def applies(step: str) -> bool:
    return step in READ_ONLY_STEPS


def sandbox_env(env: dict) -> dict:
    """
    Copie de `env` sans jeton d'écriture Jira. Les variables restent définies (vides ou jeton de lecture) :
    un load_dotenv() lancé par l'agent ne les recharge pas depuis le .env.
    """
    env = dict(env)
    read_token = env.get("JIRA_READ_API_TOKEN", "").strip()
    env["JIRA_API_TOKEN"] = read_token
    env["JIRA_EMAIL"] = env.get("JIRA_READ_EMAIL", "").strip() if read_token else ""
    env[READ_ONLY_ENV] = "1"
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"   # connecteurs claude.ai (Atlassian…) non chargés
    return env


def _rule_path(path: str) -> str:
    """Chemin absolu au format des règles de permission de Claude Code (« //c/dossier/fichier »)."""
    posix = os.path.abspath(path).replace("\\", "/")
    if len(posix) > 1 and posix[1] == ":":
        posix = "/" + posix[0].lower() + posix[2:]
    return "/" + posix


def deny_rules(workspace_dir: str) -> list:
    """Règles --disallowedTools : ni lecture ni écriture du .env de l'agent (aucun MCP : --strict-mcp-config)."""
    env_file = _rule_path(os.path.join(workspace_dir, ".env"))
    return [f"Read({env_file})", f"Edit({env_file})", f"Write({env_file})"]


def claude_args(workspace_dir: str, extra_args: tuple = ()) -> tuple:
    """
    Arguments de `claude` en lecture seule Jira : --strict-mcp-config (aucun serveur MCP) et les règles
    --disallowedTools, fusionnées avec celles déjà présentes dans `extra_args` (une seule option).
    """
    args, denied = [], list(deny_rules(workspace_dir))
    items = list(extra_args)
    i = 0
    while i < len(items):
        if items[i] == "--disallowedTools":
            i += 1
            while i < len(items) and not str(items[i]).startswith("--"):
                denied.append(items[i])
                i += 1
            continue
        args.append(items[i])
        i += 1
    return tuple(args) + ("--strict-mcp-config", "--disallowedTools", *dict.fromkeys(denied))


def watched_keys(target_issue: str, step: str, manifest: dict = None) -> list:
    """Tickets dont la description est contrôlée : le ticket, et pour une épopée ses tâches déjà créées."""
    keys = [target_issue]
    if step == "epic-cadrage":
        keys += [t["key"] for t in (manifest or {}).get("tasks", []) if t.get("key")]
    return list(dict.fromkeys(keys))


def snapshot(keys: list, step: str = "", read=None, children=None) -> dict:
    """
    État Jira relevé : {clé: description sérialisée} (None si illisible) et, pour une épopée, la liste de ses
    tâches enfants (« enfants:<clé> ») pour détecter une création.
    """
    read = read or get_jira_description
    children = children or get_epic_children
    state = {}
    for key in keys:
        try:
            state[key] = json.dumps(read(key), sort_keys=True, ensure_ascii=False)
        except Exception:
            state[key] = None
    if step == "epic-cadrage" and keys:
        state[f"enfants:{keys[0]}"] = json.dumps(sorted(c["key"] for c in children(keys[0])))
    return state


def changes(before: dict, after: dict) -> list:
    """Éléments modifiés entre deux relevés (ceux illisibles avant ou après ne sont pas comparés)."""
    return [key for key, value in before.items()
            if value is not None and after.get(key) is not None and after[key] != value]


def unreadable(state: dict) -> list:
    return [key for key, value in state.items() if value is None]
