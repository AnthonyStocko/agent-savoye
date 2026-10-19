"""
Onglet « Cucumber » : couverture Cucumber des spécifiques d'un client (main.py --step generate-cucumber-client).
Fonctions partagées par app.py et main.py.
"""
import os
import re

WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKTREE_ROOT = os.path.join(WORKSPACE_DIR, ".tmp", "cucumber_client", "worktrees")


def client_name_from_repo(repo: str) -> str:
    """
    Nom du client déduit du dépôt saisi, en minuscules :
    https://git.kiscloud.io/projects/NOVALIA -> novalia ; .../scm/novalia/odatio-novalia.git -> novalia ;
    clone local .tmp/client_NOVALIA ou C:/dev/novalia -> novalia.
    """
    repo = (repo or "").strip().rstrip("/\\")
    if not repo:
        return ""
    match = re.search(r"/projects/([^/]+)", repo) or re.search(r"/scm/([^/]+)/", repo)
    name = match.group(1) if match else re.split(r"[\\/]", repo)[-1]
    if name.lower().startswith("client_"):
        name = name[len("client_"):]
    return re.sub(r"[^a-z0-9_-]", "", name.lower())


def work_id(client_name: str) -> str:
    """Identifiant de travail de l'étape (à la place d'une clé de ticket Jira)."""
    return f"CUCUMBER-{client_name.upper()}"


def patch_path(client_name: str) -> str:
    return os.path.join(WORKSPACE_DIR, f"{client_name}-cucumber.patch")


def coverage_path(client_name: str) -> str:
    return os.path.join(WORKSPACE_DIR, f"{client_name}-cucumber-couverture.md")
