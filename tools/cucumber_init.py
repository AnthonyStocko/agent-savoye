"""
Initialisation Cucumber propre à un client (onglet Cucumber, main.py --init-only), en Python seul (0 token).

Dans le Cucumber Standard, chaque module a son initialisation : features/<MOD>/01-Contexte/01-Initialisation.feature
(tag @INIT_<MOD>) et ses fichiers de données files/<MOD>/01-Contexte/, chargés par le step « INIT- Initialisation
globale par fichiers via l'écran » depuis le dossier qui correspond à celui de la feature. Chaque initialisation
purge tout l'environnement : ce sont des profils exclusifs, non cumulables. Le client reçoit donc une copie fidèle
de l'initialisation de chacun de ses modules, à lancer séparément par son tag @INIT_<CLIENT>_<MOD>. Les steps
restent ceux du Standard (dépendance test-jar du dépôt <client>-cucumber) : aucun code Java n'est copié.
"""
import os
import re
import shutil

from tools.cucumber_tools import (_git, add_worktree, cache_dir, format_patch, git_repos, init_patch_path,
                                  java_class_suffix, java_package_segment)

INIT_MODULES = ("WMS", "TMS", "OMS")
RESOURCES_DIR = "src/test/resources/cucumber"
INIT_DIR = "01-Contexte"
_MODULE_TOKEN = re.compile(r"[^a-z0-9]+")


def init_worktree_dir(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "worktree_init")


def init_branch(client_name: str) -> str:
    return f"cucumber_{client_name}_init"


def _modules_in(name: str) -> set:
    """Modules nommés par un segment de `name` (ex. apparel-tms-provider -> TMS, wms_content -> WMS)."""
    return {t.upper() for t in _MODULE_TOKEN.split(name.lower()) if t.upper() in INIT_MODULES}


def client_modules(client_root: str, inventory: dict) -> set:
    """
    Modules du client parmi WMS, TMS, OMS : segments des noms de ses dépôts, puis des dépôts et dossiers de premier
    niveau des fichiers de ses spécifiques (inventaire). Les noms de dépôts seuls ne suffisent pas : un module
    client (ex. apparel-tms-provider) peut vivre dans un dépôt au nom neutre.
    """
    names = list(git_repos(client_root))
    for spec in (inventory or {}).get("specifiques") or []:
        names.append(spec.get("depot") or "")
        for item in spec.get("fichiers") or []:
            names.append(item.get("repo") or "")
            names.append((item.get("path") or "").split("/", 1)[0])
    return set().union(*map(_modules_in, names)) if names else set()


def std_init_files(cucumber_std_dir: str) -> dict:
    """
    Initialisation du Standard par module : {module: [chemins relatifs]}, la feature d'abord puis ses fichiers de
    données. Seuls les modules dont la feature porte le tag @INIT_<MOD> sont retenus.
    """
    result = {}
    for module in INIT_MODULES:
        features = os.path.join(cucumber_std_dir, *RESOURCES_DIR.split("/"), "features", module, INIT_DIR)
        found = [f for f in sorted(os.listdir(features)) if f.endswith(".feature")
                 and f"@INIT_{module}" in _read_text(os.path.join(features, f)).split()] \
            if os.path.isdir(features) else []
        if not found:
            continue
        paths = [f"{RESOURCES_DIR}/features/{module}/{INIT_DIR}/{f}" for f in found]
        data = os.path.join(cucumber_std_dir, *RESOURCES_DIR.split("/"), "files", module, INIT_DIR)
        for folder, _, files in os.walk(data):
            rel = os.path.relpath(folder, cucumber_std_dir).replace(os.sep, "/")
            paths.extend(f"{rel}/{f}" for f in sorted(files))
        result[module] = paths
    return result


def client_path(std_path: str, client_name: str) -> str:
    """Chemin client d'un fichier d'initialisation : features/<MOD>/... -> features/<client>/<MOD>/... (idem files)."""
    segment = java_package_segment(client_name)
    for kind in ("features", "files"):
        prefix = f"{RESOURCES_DIR}/{kind}/"
        if std_path.startswith(prefix):
            return f"{prefix}{segment}/{std_path[len(prefix):]}"
    return std_path


def adapt_feature(text: str, module: str, client_name: str) -> str:
    """
    Feature d'initialisation au nom du client : tag @INIT_<MOD> -> @INIT_<CLIENT>_<MOD> plus le tag @<CLIENT> (pour
    ne pas être lancée avec l'initialisation Standard), titre préfixé du client comme ses autres features.
    Le contenu (steps, données) reste celui du Standard.
    """
    client_tag = java_package_segment(client_name).upper()
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        tokens = line.split()
        if tokens and tokens[0].startswith("@") and f"@INIT_{module}" in tokens:
            tokens = [f"@INIT_{client_tag}_{module}" if t == f"@INIT_{module}" else t for t in tokens]
            if f"@{client_tag}" not in tokens:
                tokens.insert(1, f"@{client_tag}")
            lines[i] = line[:len(line) - len(line.lstrip())] + " ".join(tokens) + _eol(line)
        elif line.lstrip().startswith("Feature:"):
            title = line.split("Feature:", 1)[1].strip()
            lines[i] = line.split("Feature:", 1)[0] + f"Feature: {java_class_suffix(client_name)} - {title}" + _eol(line)
            break
    return "".join(lines)


def build_client_init(cucumber_repo_dir: str, cucumber_std_dir: str, client_name: str, base: str,
                      modules: set) -> dict:
    """
    Écrit l'initialisation des `modules` retenus dans le worktree cucumber_<client>_init (repart de `base`), la
    commite en un seul commit et produit le patch init_patch_path(client). Renvoie {"modules", "fichiers", "patch",
    "absents"} ; "patch" vaut '' s'il n'y a rien à livrer. Le Cucumber Standard n'est pas modifié. Lève RuntimeError.
    """
    std = std_init_files(cucumber_std_dir)
    kept = sorted(m for m in modules if m in std)
    result = {"modules": kept, "fichiers": [], "patch": "", "absents": sorted(set(modules) - set(std))}
    output = init_patch_path(client_name)
    if os.path.exists(output):
        os.remove(output)
    if not kept:
        return result
    worktree = add_worktree(cucumber_repo_dir, init_worktree_dir(client_name), init_branch(client_name), base,
                            reset=True)
    for module in kept:
        for rel in std[module]:
            target = os.path.join(worktree, *client_path(rel, client_name).split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            source = os.path.join(cucumber_std_dir, *rel.split("/"))
            if rel.endswith(".feature"):
                with open(target, "w", encoding="utf-8", newline="") as f:
                    f.write(adapt_feature(_read_text(source), module, client_name))
            else:
                shutil.copyfile(source, target)
            result["fichiers"].append(client_path(rel, client_name))
    _git(worktree, "add", "-A", "--", f"{RESOURCES_DIR}/features", f"{RESOURCES_DIR}/files")
    if _git(worktree, "diff", "--cached", "--quiet").returncode == 0:
        return result
    commit = _git(worktree, "commit", "-m", f"Cucumber initialisation of {client_name} ({', '.join(kept)})")
    if commit.returncode != 0:
        raise RuntimeError(f"Commit de l'initialisation impossible : {commit.stderr.strip()[:300]}")
    if format_patch(worktree, f"{base}..HEAD", output):
        result["patch"] = output
    return result


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _eol(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
