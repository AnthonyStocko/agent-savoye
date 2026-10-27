"""
Contrôle de l'environnement de l'agent avant tout lot Cucumber (0 token, lecture seule).

Maven disponible ; dépôts clonés sans fusion en cours, sur une branche qui suit une branche distante et à jour avec
elle. Un problème = une phrase avec l'action à faire par l'utilisateur ; aucun lot n'est lancé. L'agent ne modifie
jamais un clone bloqué sans accord explicite : il peut contenir du travail en cours.
"""
import os
import shlex
import shutil
import subprocess

from tools.cucumber_tools import _git, git_repos


def maven_problem(compile_cmd: str) -> str:
    """Phrase si la commande de compilation (Maven) est introuvable ou ne répond pas ; '' sinon (ou sans commande)."""
    if not (compile_cmd or "").strip():
        return ""
    executable = shlex.split(compile_cmd, posix=os.name != "nt")[0].strip('"')
    path = shutil.which(executable)
    if not path:
        return (f"Maven indisponible : la commande « {executable} » est introuvable sur ce poste. Action : installez "
                f"Maven ou corrigez CUCUMBER_COMPILE_CMD (onglet Configuration), puis relancez.")
    try:
        result = subprocess.run([path, "-v"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"Maven ne répond pas ({type(e).__name__}). Action : vérifiez l'installation de Maven, puis relancez."
    if result.returncode != 0:
        first = next((line.strip() for line in (result.stdout + result.stderr).splitlines() if line.strip()), "")
        return (f"Maven ne répond pas (« {executable} -v » en échec : {first[:120]}). Action : vérifiez JAVA_HOME et "
                f"l'installation de Maven, puis relancez.")
    return ""


def repo_problems(path: str, name: str, need_upstream: bool = True) -> list:
    """Phrases : fusion en cours, branche sans suivi distant, retard sur la branche distante."""
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    conflicts = sorted({line.split("\t")[-1] for line in _git(path, "ls-files", "-u").stdout.splitlines()})
    merging = _git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0
    if conflicts or merging:
        return [f"Dépôt {name} : fusion inachevée sur la branche {branch}"
                + (f" ({len(conflicts)} fichier(s) en conflit)" if conflicts else "")
                + f". Action : terminez ou annulez la fusion dans {path} (l'agent n'y touche pas : travail en cours "
                  f"possible), puis relancez."]
    if not need_upstream or branch == "HEAD":
        return []
    upstream = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode != 0:
        return [f"Dépôt {name} : la branche {branch} ne suit aucune branche distante, sa mise à jour est impossible. "
                f"Action : remettez le clone {path} sur sa branche de travail suivie (ex. integration) ou indiquez à "
                f"l'agent qu'il peut le faire."]
    behind = _git(path, "rev-list", "--count", "HEAD..@{u}").stdout.strip()
    if behind.isdigit() and int(behind):
        return [f"Dépôt {name} : {behind} commit(s) de retard sur {upstream.stdout.strip()} (mise à jour impossible). "
                f"Action : mettez à jour le clone {path} (git pull), puis relancez."]
    return []


def check(client_root: str, compile_cmd: str, cucumber_std_dir: str = "", excluded=None) -> list:
    """
    Problèmes d'environnement avant tout lot : Maven, dépôts du client (hors dépôts exclus de l'inventaire par
    `excluded(nom)`), dépôt du Cucumber Standard (fusion seulement : il est extrait en HEAD détaché).
    """
    problems = [p for p in [maven_problem(compile_cmd)] if p]
    for name, path in git_repos(client_root).items():
        if excluded and excluded(name):
            continue
        problems += repo_problems(path, name)
    if cucumber_std_dir and os.path.isdir(os.path.join(cucumber_std_dir, ".git")):
        problems += repo_problems(cucumber_std_dir, os.path.basename(os.path.normpath(cucumber_std_dir)),
                                  need_upstream=False)
    return problems
