"""
Initialisation Cucumber propre à un client (onglet Cucumber, main.py --init-only), en Python seul (0 token).

Dans le Cucumber Standard, chaque module a son initialisation : features/<MOD>/01-Contexte/01-Initialisation.feature
(tag @INIT_<MOD>) et ses fichiers de données files/<MOD>/01-Contexte/, chargés par le step « INIT- Initialisation
globale par fichiers via l'écran » depuis le dossier qui correspond à celui de la feature. Chaque initialisation
purge tout l'environnement : ce sont des profils exclusifs, non cumulables. Le client reçoit donc une copie fidèle
de l'initialisation de chacun de ses modules, à lancer séparément par son tag @INIT_<CLIENT>_<MOD>, sous
features/<client>/01-Contexte/<MOD>/ : Cucumber trie les features par chemin, l'initialisation passe ainsi avant
toutes les autres features du client. La purge utilise le clear CUCUMBER_CLEAR_NAME par un step client (seul code
Java ajouté, aucun step du Standard ne prend de clear nommé) ; les autres steps restent ceux du Standard.
"""
import os
import posixpath
import re
import shutil

from tools.cucumber_tools import (_git, add_worktree, cache_dir, format_patch, git_repos, init_patch_path,
                                  java_class_suffix, java_package_segment)

INIT_MODULES = ("WMS", "TMS", "OMS")
RESOURCES_DIR = "src/test/resources/cucumber"
INIT_DIR = "01-Contexte"
STD_PURGE_STEP = "INIT- Purge de l'environnement"
CLEAR_STEP = "INIT- Purge de l'environnement par le clear"
GLOBAL_INIT_STEP = "INIT- Initialisation globale par fichiers via l'écran"
SPEC_STEP = "INIT- Intégration des fichiers via l'écran"
_MODULE_TOKEN = re.compile(r"[^a-z0-9]+")


def init_worktree_dir(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "worktree_init")


def init_branch(client_name: str) -> str:
    return f"cucumber_{client_name}_init"


def _modules_in(name: str) -> set:
    """Modules nommés par un segment de `name` (ex. client-tms-provider -> TMS, wms_content -> WMS)."""
    return {t.upper() for t in _MODULE_TOKEN.split(name.lower()) if t.upper() in INIT_MODULES}


def client_modules(client_root: str, inventory: dict) -> set:
    """
    Modules du client parmi WMS, TMS, OMS : segments des noms de ses dépôts, puis des dépôts et dossiers de premier
    niveau des fichiers de ses spécifiques (inventaire). Les noms de dépôts seuls ne suffisent pas : un module
    client (ex. client-tms-provider) peut vivre dans un dépôt au nom neutre.
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


def clear_name(profile: dict = None) -> str:
    """
    Seul clear autorisé pour purger l'environnement : clear_name du profil client, à défaut CUCUMBER_CLEAR_NAME ;
    '' si aucun (propre à chaque environnement : jamais de valeur par défaut).
    """
    return ((profile or {}).get("clear_name") or os.getenv("CUCUMBER_CLEAR_NAME", "")).strip()


def client_path(std_path: str, client_name: str) -> str:
    """
    Chemin client d'un fichier d'initialisation : features/<MOD>/01-Contexte/... ->
    features/<client>/01-Contexte/<MOD>/... (idem files), trié avant les autres features du client.
    """
    segment = java_package_segment(client_name)
    for kind in ("features", "files"):
        prefix = f"{RESOURCES_DIR}/{kind}/"
        if std_path.startswith(prefix):
            module, _, rest = std_path[len(prefix):].partition("/")
            if rest.startswith(INIT_DIR + "/"):
                return f"{prefix}{segment}/{INIT_DIR}/{module}/{rest[len(INIT_DIR) + 1:]}"
            return f"{prefix}{segment}/{std_path[len(prefix):]}"
    return std_path


def clear_step_path(client_name: str) -> str:
    """Classe Java du step de purge par clear nommé, dans le package des Step Definitions du client."""
    return (f"src/test/java/cucumber/features/{java_package_segment(client_name)}/"
            f"Init{java_class_suffix(client_name)}Steps.java")


def clear_step_java(client_name: str) -> str:
    """Step client « INIT- Purge de l'environnement par le clear {string} » : même séquence que la purge Standard."""
    segment, suffix = java_package_segment(client_name), java_class_suffix(client_name)
    return f"""package cucumber.features.{segment};

import cucumber.endPoints.KIS.KISEndPoints;
import cucumber.features.BaseStep;
import cucumber.features.kis.QAFileIntegrationStep;
import cucumber.util.context.TestContext;
import io.cucumber.java.en.Given;

public class Init{suffix}Steps extends BaseStep {{

    private final QAFileIntegrationStep fileIntegration;

    public Init{suffix}Steps(TestContext testContext) {{
        super(testContext);
        fileIntegration = new QAFileIntegrationStep(testContext);
    }}

    /**
     * Purge de l'environnement par un clear nommé (propre à l'environnement), comme « {STD_PURGE_STEP} » :
     * données d'initialisation autorisées, jobs suspendus le temps du clear.
     */
    @Given("{CLEAR_STEP} {{string}}")
    public void purgeParClear(String clearName) throws Exception {{
        fileIntegration.enableInitData();
        KISEndPoints.disableAllJobs();
        try {{
            KISEndPoints.clear(clearName);
        }} finally {{
            KISEndPoints.enableAllJobs();
        }}
    }}
}}
"""


def spec_file_name(feature_rel: str, spec_file: str) -> str:
    """
    fileName d'un fichier du module spécifique pour le step d'intégration de la feature `feature_rel` : chemin
    relatif au dossier de données de la feature (files/<dossier de la feature>/), comme le résout le Standard.
    """
    inner = feature_rel.split("/features/", 1)[-1]
    folder = f"{RESOURCES_DIR}/files/{inner.rsplit('/', 1)[0]}" if "/" in inner else f"{RESOURCES_DIR}/files"
    return posixpath.relpath(spec_file, folder)


def spec_steps(feature_rel: str, spec_files: list, indent: str, eol: str) -> list:
    """
    Lignes Gherkin d'intégration des fichiers du module spécifique, un step par fichier dans l'ordre : le step
    Standard « INIT- Intégration des fichiers via l'écran » échoue si un fichier finit en INTEGRATED_WITH_ERROR.
    """
    lines = []
    for spec in spec_files:
        name = spec_file_name(feature_rel, spec)
        lines += [f"{indent}And {SPEC_STEP}{eol}", f"{indent}  | fileName |{eol}", f"{indent}  | {name} |{eol}"]
    return lines


def adapt_feature(text: str, module: str, client_name: str, clear: str, client_tag: str = "",
                  spec_files: list = (), feature_rel: str = "") -> str:
    """
    Feature d'initialisation au nom du client, en trois étapes : clear `clear` (à la place de la purge Standard),
    initialisation globale par fichiers du Standard, puis intégration des `spec_files` (module spécifique du client,
    dans l'ordre) juste après l'initialisation globale. Tag @INIT_<MOD> -> @INIT_<CLIENT>_<MOD> plus le tag client
    `client_tag` (tag_client du profil, à défaut @<CLIENT>) pour ne pas être lancée avec l'initialisation Standard,
    titre préfixé du client comme ses autres features. Les autres steps et les données restent ceux du Standard.
    """
    client_tag = (client_tag or f"@{java_package_segment(client_name).upper()}").lstrip("@")
    lines = text.splitlines(keepends=True)
    global_init = next((i for i, line in enumerate(lines) if GLOBAL_INIT_STEP in line), None)
    if spec_files and global_init is not None:
        line = lines[global_init]
        index = global_init + 1
        while index < len(lines) and lines[index].strip().startswith("|"):
            index += 1
        lines[index:index] = spec_steps(feature_rel, spec_files, line[:len(line) - len(line.lstrip())],
                                        _eol(line) or "\n")
    for i, line in enumerate(lines):
        if line.strip().split(" ", 1)[-1] == STD_PURGE_STEP:
            keyword = line.strip().split(" ", 1)[0]
            lines[i] = line[:len(line) - len(line.lstrip())] + f'{keyword} {CLEAR_STEP} "{clear}"' + _eol(line)
            continue
        tokens = line.split()
        if tokens and tokens[0].startswith("@") and f"@INIT_{module}" in tokens:
            tokens = [f"@INIT_{client_tag}_{module}" if t == f"@INIT_{module}" else t for t in tokens]
            if f"@{client_tag}" not in tokens:
                tokens.insert(1, f"@{client_tag}")
            lines[i] = line[:len(line) - len(line.lstrip())] + " ".join(tokens) + _eol(line)
        elif line.lstrip().startswith("Feature:"):
            title = line.split("Feature:", 1)[1].strip()
            lines[i] = line.split("Feature:", 1)[0] + f"Feature: {java_class_suffix(client_name)} - {title}" + _eol(line)
    return "".join(lines)


def build_client_init(cucumber_repo_dir: str, cucumber_std_dir: str, client_name: str, base: str,
                      modules: set, profile: dict = None) -> dict:
    """
    Écrit l'initialisation des `modules` retenus dans le worktree cucumber_<client>_init (repart de `base`), la
    commite en un seul commit et produit le patch init_patch_path(client). Renvoie {"modules", "fichiers", "patch",
    "absents"} ; "patch" vaut '' s'il n'y a rien à livrer. Le Cucumber Standard n'est pas modifié. Lève RuntimeError,
    notamment sans clear_name (profil client ou CUCUMBER_CLEAR_NAME) : le clear est propre à l'environnement.
    """
    clear = clear_name(profile)
    if not clear:
        raise RuntimeError("clear_name vide : renseignez-le dans le profil client (ou CUCUMBER_CLEAR_NAME) ; "
                           "aucune initialisation n'est générée sans le clear propre à l'environnement.")
    client_tag = (profile or {}).get("tag_client", "")
    spec_files = list((profile or {}).get("spec_files") or [])
    std = std_init_files(cucumber_std_dir)
    kept = sorted(m for m in modules if m in std)
    result = {"modules": kept, "fichiers": [], "patch": "", "absents": sorted(set(modules) - set(std)),
              "avertissements": [] if spec_files else [
                  "aucun fichier du module spécifique dans le profil client : après le clear, les données propres au "
                  "client (documents, statuts, partenaires…) ne seront pas recréées"]}
    output = init_patch_path(client_name)
    if os.path.exists(output):
        os.remove(output)
    if not kept:
        return result
    worktree = add_worktree(cucumber_repo_dir, init_worktree_dir(client_name), init_branch(client_name), base,
                            reset=True)
    missing = [s for s in spec_files if not os.path.exists(os.path.join(worktree, *s.split("/")))]
    if missing:
        result["avertissements"].append(f"fichier(s) du module spécifique absent(s) de la base : {', '.join(missing)}")
    for module in kept:
        for rel in std[module]:
            target_rel = client_path(rel, client_name)
            target = os.path.join(worktree, *target_rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            source = os.path.join(cucumber_std_dir, *rel.split("/"))
            if rel.endswith(".feature"):
                with open(target, "w", encoding="utf-8", newline="") as f:
                    f.write(adapt_feature(_read_text(source), module, client_name, clear, client_tag, spec_files,
                                          target_rel))
            else:
                shutil.copyfile(source, target)
            result["fichiers"].append(client_path(rel, client_name))
    java = clear_step_path(client_name)
    target = os.path.join(worktree, *java.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(clear_step_java(client_name))
    result["fichiers"].append(java)
    _git(worktree, "add", "-A", "--", f"{RESOURCES_DIR}/features", f"{RESOURCES_DIR}/files", java)
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
