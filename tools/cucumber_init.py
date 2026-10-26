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

Chaque intégration échoue si un fichier finit en INTEGRATED_WITH_ERROR : les steps d'intégration sans attente du
résultat (NON_CHECKING_STEPS) sont remplacés par leur équivalent qui l'attend. Un step absent de la bibliothèque de
steps du client (ou de signature différente) est commenté avec « # TODO-VERSION: <raison> », jamais supprimé.
"""
import os
import posixpath
import re
import shutil

from tools import cucumber_environment, cucumber_recreation, cucumber_versions
from tools.cucumber_tools import (_git, add_worktree, cache_dir, format_patch, git_repos, init_patch_path,
                                  java_class_suffix, java_package_segment)
from tools.cucumber_verify import _STEP_LINE

INIT_MODULES = ("WMS", "TMS", "OMS")
RESOURCES_DIR = "src/test/resources/cucumber"
INIT_DIR = "01-Contexte"
STD_PURGE_STEP = "INIT- Purge de l'environnement"
CLEAR_STEP = "INIT- Purge de l'environnement par le clear"
GLOBAL_INIT_STEP = "INIT- Initialisation globale par fichiers via l'écran"
SPEC_STEP = "INIT- Intégration des fichiers via l'écran"
# Intégration sans attente du statut des fichiers -> équivalent qui échoue sur INTEGRATED_WITH_ERROR
NON_CHECKING_STEPS = {"INIT- Demande d'intégration des fichiers via l'écran": SPEC_STEP}
# Écarts de version qui empêchent le step de s'exécuter avec la bibliothèque du client : step commenté
BLOCKING_GAPS = ("absent_bibliotheque", "signature")
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
                  spec_files: list = (), feature_rel: str = "", recreation: list = ()) -> str:
    """
    Feature d'initialisation au nom du client, en trois étapes : clear `clear` (à la place de la purge Standard),
    initialisation globale par fichiers du Standard, puis intégration des `spec_files` (module spécifique du client,
    dans l'ordre) juste après l'initialisation globale. Tag @INIT_<MOD> -> @INIT_<CLIENT>_<MOD> plus le tag client
    `client_tag` (tag_client du profil, à défaut @<CLIENT>) pour ne pas être lancée avec l'initialisation Standard,
    titre préfixé du client comme ses autres features. Les autres steps et les données restent ceux du Standard.
    `recreation` (cucumber_recreation.plan) : recréation des données d'initializers effacées par le clear, entre
    l'initialisation globale et les fichiers du module spécifique.
    """
    client_tag = (client_tag or f"@{java_package_segment(client_name).upper()}").lstrip("@")
    lines = text.splitlines(keepends=True)
    global_init = next((i for i, line in enumerate(lines) if GLOBAL_INIT_STEP in line), None)
    if (spec_files or recreation) and global_init is not None:
        line = lines[global_init]
        index = global_init + 1
        while index < len(lines) and lines[index].strip().startswith("|"):
            index += 1
        indent, eol = line[:len(line) - len(line.lstrip())], _eol(line) or "\n"
        lines[index:index] = (cucumber_recreation.feature_lines(list(recreation), indent, eol)
                              + spec_steps(feature_rel, spec_files, indent, eol))
    for i, line in enumerate(lines):
        step = line.strip().split(" ", 1)[-1]
        if step in NON_CHECKING_STEPS:
            lines[i] = line.replace(step, NON_CHECKING_STEPS[step])
            continue
        if step == STD_PURGE_STEP:
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


def mark_version_gaps(text: str, library: dict, aligned: dict, version: str) -> tuple:
    """
    Steps d'une feature d'init incompatibles avec la bibliothèque `library` du client (instantané
    cucumber_versions.std_snapshot) par rapport au Standard aligné `aligned` : absents ou de signature différente,
    commentés (table et docstring comprises) sous « # TODO-VERSION: <raison> » ; autres écarts (endpoint, champs
    POJO) signalés par un « # TODO-VERSION: à vérifier » sans commenter le step. Rien n'est supprimé.
    Renvoie (texte, [raisons]).
    """
    lines, out, notes, i = text.splitlines(keepends=True), [], [], 0
    while i < len(lines):
        line = lines[i]
        i += 1
        step = _STEP_LINE.match(line)
        if not step or step.group(2).startswith(CLEAR_STEP):
            out.append(line)
            continue
        indent, blocking = line[:len(line) - len(line.lstrip())], False
        for d in cucumber_versions.step_differences(library, aligned, [step.group(2)]):
            stops = d["nature"] in BLOCKING_GAPS
            blocking = blocking or stops
            reason = (f"{cucumber_versions.KINDS[d['nature']]} (bibliothèque {version}) : {d['detail']}" if stops
                      else f"à vérifier — {cucumber_versions.KINDS[d['nature']]} : {d['detail']}")
            out.append(f"{indent}# {cucumber_versions.TODO_MARKER} {reason}{_eol(line) or chr(10)}")
            notes.append(f"{step.group(2)} : {reason}")
        if not blocking:
            out.append(line)
            continue
        out.append(_commented(line))
        # Table et docstring du step commentées avec lui
        in_doc = False
        while i < len(lines) and (in_doc or lines[i].strip().startswith(("|", '"""'))):
            in_doc = in_doc != lines[i].strip().startswith('"""')
            out.append(_commented(lines[i]))
            i += 1
    return "".join(out), notes


def _commented(line: str) -> str:
    indent = line[:len(line) - len(line.lstrip())]
    return f"{indent}# {line.lstrip()}" if line.strip() else line


def initializer_limits(profile: dict) -> list:
    """
    Limites de l'extraction des initializers serveur, affichées avec l'initialisation : créations indirectes non
    résolues (« partiel », jamais recréées), données au sort inconnu après le clear (non recréées).
    """
    data = list((profile or {}).get("server_initialized_data") or [])
    survive = (profile or {}).get("initializers_survive_clear", "")
    partial = [d for d in data if d.get("certitude") == "partiel" or not d.get("code")]
    unknown = [d for d in data if d not in partial and d.get("statut") == "inconnu" and survive == ""]
    out = []
    if partial:
        out.append(f"{len(partial)} création(s) d'initializer non résolue(s) (appel de service, code non littéral) : "
                   f"jamais devinées — " + ", ".join(f"{d['classe']}.{d.get('expression') or d['entite']}"
                                                    for d in partial[:5]))
    if unknown:
        out.append(f"{len(unknown)} donnée(s) d'initializer au sort inconnu après le clear : renseignez « données "
                   f"des initializers conservées après un clear » dans le profil client")
    return out


def build_client_init(cucumber_repo_dir: str, cucumber_std_dir: str, client_name: str, base: str,
                      modules: set, profile: dict = None, recreate=None, environment: dict = None) -> dict:
    """
    Écrit l'initialisation des `modules` retenus dans le worktree cucumber_<client>_init (repart de `base`), la
    commite en un seul commit et produit le patch init_patch_path(client). Renvoie {"modules", "fichiers", "patch",
    "absents", "recreation"} ; "patch" vaut '' s'il n'y a rien à livrer. Le Cucumber Standard n'est pas modifié.
    Données d'initializers effacées par le clear : recréées par un step du Standard reconnu, sinon par un step du
    projet que `recreate(worktree, items)` fait écrire (appel Claude borné) et renvoie les erreurs restantes ; une
    recréation non livrable reste commentée (TODO-INITIALIZER). `environment` ({controles, recreations, writer}) :
    scénarios de recréation des corrections manuelles puis de contrôle de l'environnement en lecture seule ajoutés
    à chaque feature d'init (cucumber_environment), steps écrits par `writer(worktree, controles, recreations)` ;
    non livrables, ils restent commentés (TODO-ENVIRONNEMENT). Lève RuntimeError, notamment sans clear_name
    (profil client ou CUCUMBER_CLEAR_NAME) : le clear est propre à l'environnement.
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
              "avertissements": ([] if spec_files else [
                  "aucun fichier du module spécifique dans le profil client : après le clear, les données propres au "
                  "client (documents, statuts, partenaires…) ne seront pas recréées"]) + initializer_limits(profile),
              "todo_version": [], "recreation": {}}
    output = init_patch_path(client_name)
    if os.path.exists(output):
        os.remove(output)
    if not kept:
        return result
    worktree = add_worktree(cucumber_repo_dir, init_worktree_dir(client_name), init_branch(client_name), base,
                            reset=True)
    library, aligned, version = _library_snapshots(cucumber_std_dir, (profile or {}).get("library_version", ""))
    items = recreation_plan(cucumber_std_dir, client_name, profile)
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
                text = adapt_feature(_read_text(source), module, client_name, clear, client_tag, spec_files,
                                     target_rel, items)
                if library:
                    text, notes = mark_version_gaps(text, library, aligned, version)
                    result["todo_version"] += [f"{target_rel} : {n}" for n in notes]
                with open(target, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
            else:
                shutil.copyfile(source, target)
            result["fichiers"].append(client_path(rel, client_name))
    java = clear_step_path(client_name)
    target = os.path.join(worktree, *java.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(clear_step_java(client_name))
    result["fichiers"].append(java)
    result["recreation"] = _recreate_project_steps(worktree, base, client_name, items, result["fichiers"], recreate)
    result["avertissements"] += result["recreation"]["avertissements"]
    if os.path.exists(os.path.join(worktree, *cucumber_recreation.java_path(client_name).split("/"))):
        result["fichiers"].append(cucumber_recreation.java_path(client_name))
    result["environnement"] = _environment_steps(worktree, base, client_name, result["fichiers"], environment)
    result["avertissements"] += result["environnement"]["avertissements"]
    if os.path.exists(os.path.join(worktree, *cucumber_environment.java_path(client_name).split("/"))):
        result["fichiers"].append(cucumber_environment.java_path(client_name))
    extra_java = [p for p in (cucumber_recreation.java_path(client_name), cucumber_environment.java_path(client_name))
                  if p in result["fichiers"]]
    _git(worktree, "add", "-A", "--", f"{RESOURCES_DIR}/features", f"{RESOURCES_DIR}/files", java, *extra_java)
    if _git(worktree, "diff", "--cached", "--quiet").returncode == 0:
        return result
    commit = _git(worktree, "commit", "-m", f"Cucumber initialisation of {client_name} ({', '.join(kept)})")
    if commit.returncode != 0:
        raise RuntimeError(f"Commit de l'initialisation impossible : {commit.stderr.strip()[:300]}")
    if format_patch(worktree, f"{base}..HEAD", output):
        result["patch"] = output
    return result


def recreation_plan(cucumber_std_dir: str, client_name: str, profile: dict) -> list:
    """Recréations des données d'initializers effacées (cucumber_recreation.plan) ; [] s'il n'y a rien à recréer."""
    if not cucumber_recreation.to_recreate(profile):
        return []
    root = os.path.join(cucumber_std_dir, *RESOURCES_DIR.split("/"), "features")
    texts = [_read_text(os.path.join(d, n)) for d, _, names in os.walk(root) for n in names if n.endswith(".feature")]
    steps = cucumber_versions.std_snapshot(cucumber_std_dir, "HEAD")["steps"] if os.path.isdir(
        os.path.join(cucumber_std_dir, ".git")) else {}
    return cucumber_recreation.plan(profile, client_name, cucumber_recreation.std_creation_steps(steps, texts))


def _recreate_project_steps(worktree: str, base: str, client_name: str, items: list, files: list, recreate) -> dict:
    """
    Steps « projet » de recréation : écrits par `recreate` (erreurs restantes renvoyées), sinon commentés dans les
    features d'init (TODO-INITIALIZER) et classe de recréation retirée. Renvoie {standard, projet, non_livrees,
    avertissements}.
    """
    project = [e for e in items if e["mode"] == "projet"]
    out = {"standard": len(items) - len(project), "projet": len(project), "non_livrees": [], "avertissements": []}
    if not project:
        return out
    errors = recreate(worktree, items) if recreate else ["aucun moyen d'écrire les steps du projet"]
    if not errors:
        return out
    reason = errors[0][:150]
    for rel in (f for f in files if f.endswith(".feature")):
        path = os.path.join(worktree, *rel.split("/"))
        text = cucumber_recreation.comment_project_steps(_read_text(path), items, reason)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    java = cucumber_recreation.java_path(client_name)
    if _git(worktree, "cat-file", "-e", f"{base}:{java}").returncode == 0:
        _git(worktree, "checkout", base, "--", java)
    elif os.path.exists(os.path.join(worktree, *java.split("/"))):
        os.remove(os.path.join(worktree, *java.split("/")))
    out["non_livrees"] = [f"{e['donnee'].get('entite')} {e['donnee']['code']}" for e in project]
    out["avertissements"].append(f"{len(project)} recréation(s) de donnée d'initializer non livrée(s), commentée(s) "
                                 f"sous {cucumber_recreation.TODO_MARKER} : {reason}")
    return out


def _environment_steps(worktree: str, base: str, client_name: str, files: list, environment: dict) -> dict:
    """
    Contrôles d'environnement et recréations des corrections manuelles (points 9g, 9h) ajoutés à la fin de chaque
    feature d'init ; steps écrits par environment["writer"], sinon scénarios commentés (TODO-ENVIRONNEMENT) et
    classe retirée. Renvoie {controles, recreations, livres, avertissements}.
    """
    checks = list((environment or {}).get("controles") or [])
    recreations = list((environment or {}).get("recreations") or [])
    out = {"controles": len(checks), "recreations": len(recreations), "livres": False, "avertissements": []}
    if not checks and not recreations:
        return out
    lines = cucumber_environment.scenario_lines(client_name, checks, recreations)
    originals = {}
    for rel in (f for f in files if f.endswith(".feature")):
        path = os.path.join(worktree, *rel.split("/"))
        if GLOBAL_INIT_STEP not in _read_text(path):
            continue
        originals[path] = _read_text(path)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(originals[path].rstrip("\r\n") + "\n" + "\n".join(lines) + "\n")
    writer = (environment or {}).get("writer")
    errors = writer(worktree, checks, recreations) if writer else ["aucun moyen d'écrire les steps du projet"]
    if not errors:
        out["livres"] = True
        return out
    reason = errors[0][:150]
    for path, text in originals.items():
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text.rstrip("\r\n") + "\n" + "\n".join(cucumber_environment.commented(lines, reason)) + "\n")
    java = cucumber_environment.java_path(client_name)
    if _git(worktree, "cat-file", "-e", f"{base}:{java}").returncode == 0:
        _git(worktree, "checkout", base, "--", java)
    elif os.path.exists(os.path.join(worktree, *java.split("/"))):
        os.remove(os.path.join(worktree, *java.split("/")))
    out["avertissements"].append(f"contrôle de l'environnement et recréations non livrés, commentés sous "
                                 f"{cucumber_environment.TODO_MARKER} : {reason}")
    return out


def _library_snapshots(cucumber_std_dir: str, version: str) -> tuple:
    """(bibliothèque, Standard aligné, version) : instantanés à comparer, ({}, {}, version) s'ils sont identiques
    ou si la bibliothèque est introuvable dans le dépôt Standard."""
    ref = cucumber_versions.library_ref(cucumber_std_dir, version)
    if not ref:
        return {}, {}, version
    library, aligned = (cucumber_versions.std_snapshot(cucumber_std_dir, ref),
                        cucumber_versions.std_snapshot(cucumber_std_dir, "HEAD"))
    return ({}, {}, version) if library["commit"] == aligned["commit"] else (library, aligned, version)


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _eol(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
