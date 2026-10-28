"""
Correction autonome des compléments d'initialisation écrits par l'agent (0 token pour les contrôles ; un appel
Claude par tentative). L'init du client est protégée : la génération n'y touche jamais. Seule exception : quand
l'init échoue sur un scénario de SES PROPRES compléments (feature 99-Complements-initialisation*.feature, steps
Init<Client>EnvironnementSteps / Init<Client>InitializerSteps), l'agent peut corriger ces fichiers-là et eux seuls.

1. own_files : fichiers corrigeables (compléments présents dans le worktree et classes de steps de l'agent).
2. fixable : l'init échoue uniquement dans ces compléments (jamais 01-Initialisation ni un fichier de données).
3. check_correction : après l'appel, tout fichier hors de la liste est rétabli (erreur), puis contrôles Python —
   aucun échec sur une dépendance non confirmée, contrôle en lecture seule, aucun scénario ni tag retiré.
4. commit : un commit dédié, livré dans le patch et signalé dans le récapitulatif (fichiers à relire en premier).
L'appelant (main.py) relance l'init seule sur Jenkins après chaque correction : au plus CUCUMBER_INIT_FIX_ATTEMPTS.
"""
import os
import re

from tools import cucumber_environment, cucumber_recreation
from tools.cucumber_init import COMPLEMENTS_NAME
from tools.cucumber_tools import _git, _java_methods, _read_text_file

_COMPLEMENTS = re.compile(rf"(^|/){re.escape(COMPLEMENTS_NAME)}(-\d+)?\.feature$")
_WRITE_CALL = re.compile(r"\b(?:post|put|patch|delete)\s*\(|\bHttpMethod\.(?:POST|PUT|PATCH|DELETE)\b", re.I)


def enabled() -> bool:
    """CUCUMBER_INIT_SELF_FIX (défaut 1) : l'agent corrige ses propres compléments d'init en échec."""
    return os.getenv("CUCUMBER_INIT_SELF_FIX", "1").strip() != "0"


def attempts() -> int:
    try:
        return max(0, int(os.getenv("CUCUMBER_INIT_FIX_ATTEMPTS", "").strip() or 2))
    except ValueError:
        return 2


def is_complements(path: str) -> bool:
    return bool(_COMPLEMENTS.search((path or "").replace("\\", "/")))


def own_files(worktree: str, client_name: str) -> list:
    """Compléments d'init de l'agent présents dans le worktree, et ses classes de steps d'init (si elles existent)."""
    tracked = _git(worktree, "ls-files", "--", "*.feature").stdout.splitlines()
    files = [p for p in tracked if is_complements(p)]
    for java in (cucumber_environment.java_path(client_name), cucumber_recreation.java_path(client_name)):
        if os.path.exists(os.path.join(worktree, *java.split("/"))):
            files.append(java)
    return files


def fixable(failures: list, files: list) -> bool:
    """L'init échoue, et uniquement dans des scénarios des compléments de l'agent."""
    owned = {f for f in files if f.endswith(".feature")}
    return bool(failures) and all(f.get("fichier") in owned for f in failures)


def _scenarios(text: str) -> set:
    return {line.strip() for line in text.splitlines() if re.match(r"^\s*(Scenario|Scénario)", line)}


def _tags(text: str) -> set:
    return {t for line in text.splitlines() if line.strip().startswith("@") for t in line.split()}


def check_correction(worktree: str, client_name: str, files: list, before: dict) -> list:
    """
    Contrôles d'une correction (0 token). Fichiers modifiés hors de `files` : rétablis (erreur). `before` : contenu
    des fichiers autorisés avant l'appel. Renvoie les erreurs (vide : correction acceptée).
    """
    errors = []
    changed = set(_git(worktree, "diff", "--name-only", "HEAD").stdout.splitlines())
    changed |= set(_git(worktree, "ls-files", "--others", "--exclude-standard").stdout.splitlines())
    outside = sorted(p for p in changed if p not in files and not p.startswith("target/") and "/target/" not in p)
    for path in outside:
        if _git(worktree, "cat-file", "-e", f"HEAD:{path}").returncode == 0:
            _git(worktree, "checkout", "HEAD", "--", path)
        else:
            try:
                os.remove(os.path.join(worktree, *path.split("/")))
            except OSError:
                pass
        errors.append(f"{path} : hors des compléments d'init de l'agent, modification annulée")
    expression = cucumber_environment.check_expression(client_name)
    for path in files:
        text = _read_text_file(os.path.join(worktree, *path.split("/")))
        old = before.get(path, "")
        if path.endswith(".java"):
            errors += [f"{path} : méthode {name} : une dépendance non confirmée doit être un avertissement (log du "
                       f"scénario), l'échec est réservé à une absence prouvée"
                       for name in cucumber_environment.unconfirmed_failure_methods(text, expression)]
            for method in _java_methods(text):
                if expression in method["steps"] and _WRITE_CALL.search(method["body"]):
                    errors.append(f"{path} : méthode {method['name']} : le contrôle de l'environnement doit rester "
                                  f"en lecture seule")
        elif path.endswith(".feature") and old:
            removed = _scenarios(old) - _scenarios(text)
            errors += [f"{path} : scénario retiré ({s}) — corrige-le au lieu de le supprimer" for s in sorted(removed)]
            lost = {t for t in _tags(old) - _tags(text) if not t.startswith("@agent_")}
            errors += [f"{path} : tag retiré ({t})" for t in sorted(lost)]
    return errors


def snapshot(worktree: str, files: list) -> dict:
    return {p: _read_text_file(os.path.join(worktree, *p.split("/"))) for p in files}


def commit(worktree: str, files: list, client_name: str, attempt: int) -> bool:
    """Commit dédié de la correction (fichiers autorisés seulement). Renvoie True si quelque chose a changé."""
    _git(worktree, "add", "--", *files)
    if _git(worktree, "diff", "--cached", "--quiet").returncode == 0:
        return False
    return _git(worktree, "commit", "-q", "-m",
                f"Cucumber : correction des compléments d'initialisation de {client_name} par l'agent "
                f"(tentative {attempt})").returncode == 0


def failure_lines(failures: list) -> str:
    lines = []
    for f in failures:
        lines.append(f"- {f.get('fichier')} :: {f.get('titre')} — step « {f.get('step')} » — {f.get('message')}")
        if f.get("journal"):
            lines.append(f"  journal du scénario : {f['journal'][:1500]}")
    return "\n".join(lines) or "- (aucun détail)"
