"""
Contrôles déterministes (0 token) des fichiers d'un dépôt <client>-cucumber : après chaque lot, et pour le bilan de
santé du dépôt avant génération.

- Fichiers de données : un step du Standard lit ses fichiers dans files/<dossier de la feature>/ (règles :
  rules/<dossier de la feature>/<nom>.json), le dossier étant celui de la feature sous features/ (BaseStep.setup,
  ManageFileUtil.transformFileToIntegrate). Le dépôt client n'a à l'exécution que ses propres fichiers : un fichier
  présent seulement dans le Cucumber Standard manque. Il est copié automatiquement depuis le Standard quand on l'y
  trouve ; sinon c'est une erreur de lot.
- Emplacement : toute nouvelle feature est sous la racine des features du client (ex. features/<client>/).
- Tags : le tag du client garde la casse de ses features existantes (ex. @CLIENT, jamais @client) ; chaque scénario
  actif le porte (directement ou par sa ligne Feature), aucun scénario désactivé n'en hérite ; deux dry-runs, sans
  filtre et avec le tag client, sélectionnent le même nombre de scénarios (« nombre attendu » de la livraison).
- Exécution : le job de vérification lit la branche verify_branch du profil ; le worktree y est commité et poussé
  avant chaque vérification (jamais de push forcé).
- Bilan de santé : ces contrôles sur les features existantes du client, plus compilation et dry-run de tout le projet.
"""
import os
import posixpath
import re
import shutil
from collections import Counter

from tools import cucumber_versions
from tools.cucumber_tools import (WORKSPACE_DIR, _git, _read_text_file, _write_text, a_revoir_tag, add_worktree,
                                  blob_texts, build_dir, cache_dir, dry_run_errors, java_package_segment, ls_tree,
                                  run_maven, verify_build)
from tools.cucumber_verify import _read_report, _report_files, expected_count, report_test_count, scenario_units

RESOURCES = "src/test/resources/cucumber/"
FEATURES_ROOT = RESOURCES + "features/"
FILES_ROOT = RESOURCES + "files/"
RULES_ROOT = RESOURCES + "rules/"
DATA_EXTENSIONS = ("xml", "xlsm", "xlsx", "xls", "csv", "json", "txt", "zip", "pdf", "edi")
_DATA_FILE = re.compile(rf"^[\w.\-/]+\.(?:{'|'.join(DATA_EXTENSIONS)})$", re.I)
_QUOTED = re.compile(r"\"([^\"]+)\"|'([^']+)'")
# Step du Standard qui lit des règles : colonne fileName sans extension, dans rules/<dossier de la feature>/
_RULE_STEP = re.compile(r"\bINIT- Intégration des règles\s*$")
_STEP_START = re.compile(r"^\s*(Given|When|Then|And|But|\*|Soit|Sachant que|[EÉ]tant donn[ée]e?s?|Quand|Lorsque|Alors|"
                         r"Et|Mais)\s+(.+?)\s*$")
_TEST_JAR_DEPENDENCY = re.compile(r"<dependency>(?:(?!</dependency>).)*?<type>test-jar</type>"
                                  r"(?:(?!</dependency>).)*?</dependency>", re.S)
_EXAMPLES = re.compile(r"^\s*(Examples|Exemples|Scenarios)\s*:")
_TAG_LINE = re.compile(r"^\s*@")


# ---------------------------------------------------------------------------
# Fichiers de données référencés par les features
# ---------------------------------------------------------------------------

def _cells(row: str) -> list:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _is_data_file(value: str) -> bool:
    """Nom de fichier de données littéral (pas de variable ${…} ni d'URL)."""
    return bool(_DATA_FILE.match(value)) and "://" not in value


def data_references(text: str) -> list:
    """
    Fichiers lus par les steps d'une feature : [{ligne, nom, regle}]. Arguments entre guillemets et cellules de tables
    portant une extension de données, colonne fileName du step des règles (sans extension) ; commentaires,
    descriptions et doc strings ignorés.
    """
    refs, in_doc, step, header = [], False, "", None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(('"""', "```")):
            in_doc = not in_doc
            continue
        if in_doc or not stripped or stripped.startswith("#"):
            continue
        if _EXAMPLES.match(line):
            step, header = "Examples", None
            continue
        match = _STEP_START.match(line)
        if match:
            step, header = match.group(2), None
            for quoted in _QUOTED.finditer(step):
                value = (quoted.group(1) or quoted.group(2)).strip()
                if _is_data_file(value):
                    refs.append({"ligne": number, "nom": value, "regle": False})
            continue
        if not stripped.startswith("|") or not step:
            step = "" if not stripped.startswith("|") else step
            continue
        cells = _cells(stripped)
        if header is None:
            header = cells
            continue
        for column, value in zip(header, cells):
            if _RULE_STEP.search(step) and column == "fileName" and value and "$" not in value:
                refs.append({"ligne": number, "nom": value, "regle": True})
            elif _is_data_file(value):
                refs.append({"ligne": number, "nom": value, "regle": False})
    return refs


def feature_dir(rel: str) -> str:
    """Dossier d'une feature relatif à features/ (ex. <client>/WMS/Content), comme BaseStep.setup à l'exécution."""
    inner = rel[len(FEATURES_ROOT):] if rel.startswith(FEATURES_ROOT) else rel
    return inner.rsplit("/", 1)[0] if "/" in inner else ""


def resolved_path(feature_rel: str, ref: dict) -> str:
    """Chemin, relatif au dépôt, du fichier lu à l'exécution pour la référence `ref` de la feature `feature_rel`."""
    folder = feature_dir(feature_rel)
    prefix = f"{RULES_ROOT if ref['regle'] else FILES_ROOT}{folder + '/' if folder else ''}"
    path = prefix + ref["nom"] + (".json" if ref["regle"] else "")
    # Nom relatif remontant d'un dossier (../) : chemin normalisé, comme le système de fichiers à l'exécution
    return posixpath.normpath(path) if ".." in ref["nom"] else path


def repo_files(worktree: str) -> set:
    """Fichiers suivis ou nouveaux (non ignorés) du worktree : ceux que le patch livre au dépôt client."""
    out = _git(worktree, "ls-files", "-co", "--exclude-standard", "-z").stdout
    return {p for p in out.split("\0") if p}


def std_data_index(cucumber_std_dir: str) -> dict:
    """{nom de fichier: [chemins relatifs]} des fichiers de données et de règles du Cucumber Standard."""
    index = {}
    for root in (FILES_ROOT, RULES_ROOT):
        top = os.path.join(cucumber_std_dir, *root.rstrip("/").split("/"))
        for directory, _, names in os.walk(top) if cucumber_std_dir and os.path.isdir(top) else ():
            for name in names:
                rel = os.path.relpath(os.path.join(directory, name), cucumber_std_dir).replace(os.sep, "/")
                index.setdefault(name, []).append(rel)
    return index


def _same_content(cucumber_std_dir: str, paths: list) -> bool:
    contents = set()
    for rel in paths:
        with open(os.path.join(cucumber_std_dir, *rel.split("/")), "rb") as f:
            contents.add(f.read())
    return len(contents) == 1


def std_source(cucumber_std_dir: str, index: dict, target: str, inspired: list = ()) -> tuple:
    """
    (chemin Standard à copier, candidats) pour le fichier `target` : même nom, de préférence dans un dossier d'une
    feature Standard dont le lot s'inspire ; sinon un candidat unique ou des candidats identiques. ('', candidats)
    si le choix est ambigu, ('', []) si le Standard ne l'a pas.
    """
    root = RULES_ROOT if target.startswith(RULES_ROOT) else FILES_ROOT
    candidates = [p for p in index.get(target.rsplit("/", 1)[-1], []) if p.startswith(root)]
    if not candidates:
        return "", []
    folders = {feature_dir(str(p).split(" / ", 1)[0].strip()) for p in inspired or () if p}
    preferred = [p for p in candidates if p[len(root):].rsplit("/", 1)[0] in folders]
    if len(preferred) == 1:
        return preferred[0], candidates
    if len(candidates) == 1 or _same_content(cucumber_std_dir, candidates):
        return candidates[0], candidates
    return "", candidates


def check_data_files(worktree: str, features: list, cucumber_std_dir: str, copy: bool = True,
                     inspired: list = ()) -> dict:
    """
    Fichiers de données des `features` absents du worktree. Présents au Standard : copiés au chemin résolu (si
    `copy`), sinon signalés « à copier depuis … ». Renvoie {"copies": [(cible, source)], "erreurs": [...]}.
    """
    present, index = repo_files(worktree), std_data_index(cucumber_std_dir)
    copies, errors, seen = [], [], set()
    for rel in features:
        text = _read_text_file(os.path.join(worktree, *rel.split("/")))
        for ref in data_references(text):
            target = resolved_path(rel, ref)
            if target in present or target in seen:
                continue
            seen.add(target)
            source, candidates = std_source(cucumber_std_dir, index, target, inspired)
            if source and copy:
                destination = os.path.join(worktree, *target.split("/"))
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.copyfile(os.path.join(cucumber_std_dir, *source.split("/")), destination)
                copies.append((target, source))
                continue
            what = "règle" if ref["regle"] else "fichier de données"
            hint = (f"fichier à copier depuis {source}" if source else
                    f"plusieurs fichiers du Standard portent ce nom ({', '.join(candidates[:3])}) : copie celui du "
                    f"scénario dont tu t'inspires" if candidates else
                    "introuvable au Standard : crée-le (contenu propre au client) ou corrige le nom")
            errors.append(f"{rel}:{ref['ligne']} {what} « {ref['nom']} » absent : attendu en {target} ; {hint}")
    return {"copies": copies, "erreurs": errors}


# ---------------------------------------------------------------------------
# Emplacement des features et convention de tags du client
# ---------------------------------------------------------------------------

def _base_features(worktree: str, base: str) -> dict:
    """{chemin: blob} des features du dépôt client à `base` (avant tout lot)."""
    return {p: b for p, b in ls_tree(worktree, base).items() if p.startswith(FEATURES_ROOT) and p.endswith(".feature")}


def std_top_dirs(cucumber_std_dir: str) -> set:
    """Dossiers de premier niveau des features du Standard (WMS, TMS, OMS…)."""
    top = os.path.join(cucumber_std_dir, *FEATURES_ROOT.rstrip("/").split("/")) if cucumber_std_dir else ""
    return {d for d in os.listdir(top) if os.path.isdir(os.path.join(top, d))} if top and os.path.isdir(top) else set()


def client_features_root(worktree: str, base: str, client_name: str, cucumber_std_dir: str = "",
                         fallback: bool = True) -> str:
    """
    Racine des features du client (ex. src/test/resources/cucumber/features/<client>/) : premier dossier sous features/
    le plus fréquent parmi ses features existantes, hors dossiers du Standard ; à défaut le segment du client
    (ou '' si not `fallback`).
    """
    excluded = std_top_dirs(cucumber_std_dir) | {"WMS", "TMS", "OMS"}
    counts = Counter(p[len(FEATURES_ROOT):].split("/", 1)[0] for p in _base_features(worktree, base)
                     if "/" in p[len(FEATURES_ROOT):])
    found = [d for d, _ in counts.most_common() if d not in excluded]
    if not found and not fallback:
        return ""
    return f"{FEATURES_ROOT}{found[0] if found else java_package_segment(client_name)}/"


def suggested_location(rel: str, root: str, client_name: str) -> str:
    """Chemin sous la racine du client d'une feature mal placée : son dossier Standard reproduit sous la racine."""
    inner = rel[len(FEATURES_ROOT):] if rel.startswith(FEATURES_ROOT) else rel.rsplit("/", 1)[-1]
    segment = java_package_segment(client_name)
    parts = [p for p in inner.split("/") if p.lower() != segment]
    return root + "/".join(parts)


def check_locations(features: list, root: str, client_name: str, new: set) -> list:
    """Nouvelles features (`new`) hors de la racine du client : une erreur par feature, avec l'emplacement attendu."""
    return [f"{rel} : feature hors de la racine des features du client {root} (exécutée hors de son initialisation) ; "
            f"déplace-la en {suggested_location(rel, root, client_name)} et ses fichiers de données sous le dossier "
            f"files/ correspondant" for rel in features if rel in new and not rel.startswith(root)]


def _tags(text: str) -> list:
    return [t for line in text.splitlines() if _TAG_LINE.match(line) for t in line.split() if t.startswith("@")]


def client_tag(worktree: str, base: str, client_name: str, fallback: bool = True) -> str:
    """
    Tag du client avec sa casse (ex. @CLIENT) : graphie la plus fréquente, dans ses features existantes, des tags
    égaux au nom du client sans tenir compte de la casse ; à défaut @<CLIENT> en majuscules (comme l'initialisation),
    ou '' si not `fallback`.
    """
    wanted = f"@{java_package_segment(client_name)}".lower()
    texts = blob_texts(worktree, _base_features(worktree, base).values()).values()
    counts = Counter(t for text in texts for t in _tags(text) if t.lower() == wanted)
    return counts.most_common(1)[0][0] if counts else wanted.upper() if fallback else ""


def check_tags(worktree: str, features: list, convention: str) -> list:
    """Tags du client dans une autre casse que `convention` : une erreur par feature et graphie."""
    errors = []
    for rel in features:
        text = _read_text_file(os.path.join(worktree, *rel.split("/")))
        for tag in sorted({t for t in _tags(text) if t.lower() == convention.lower() and t != convention}):
            errors.append(f"{rel} : tag {tag} : la convention du client est {convention} (même casse), remplace-le")
    return errors


def check_client_tags(texts: dict, client: str, disabled: str = "") -> list:
    """
    Tag client des scénarios ({feature: texte}) : chaque scénario actif le porte, directement ou par sa ligne
    Feature (init comprise) ; aucun scénario désactivé (tag `disabled`, a_revoir_tag par défaut) ne le porte ni n'en
    hérite. Un tag de Feature s'applique à tous ses scénarios : pour désactiver une partie d'une feature, le tag
    client descend sur les scénarios actifs. Une erreur par scénario.
    """
    disabled, errors = disabled or a_revoir_tag(), []
    for rel, text in texts.items():
        for unit in scenario_units(text):
            where = f"{rel}:{unit['ligne']} « {unit['titre']} »"
            if disabled not in unit["tags"] and client not in unit["tags"]:
                errors.append(f"{where} : scénario actif sans le tag client {client} (ni sur la ligne Feature) : il "
                              f"ne serait pas exécuté par un job filtré sur {client} ; ajoute-le au-dessus du scénario")
            elif disabled in unit["tags"] and client in unit["feature"]:
                errors.append(f"{where} : scénario désactivé ({disabled}) qui hérite du tag client {client} par sa "
                              f"ligne Feature : retire {client} de la ligne Feature et mets-le sur les seuls "
                              f"scénarios actifs")
            elif disabled in unit["tags"] and client in unit["propres"]:
                errors.append(f"{where} : scénario désactivé ({disabled}) qui porte le tag client {client} : "
                              f"retire-le")
    return errors


def _dry_run_count(build: str, dry_run_cmd: str, paths: list, tags: str = "") -> dict:
    """Dry-run strict des features `paths` (filtre `tags` s'il est donné) : {ok, nombre, erreurs, environnement}."""
    for old in _report_files(build):
        os.remove(old)
    command = f'{dry_run_cmd} "-Dcucumber.features={",".join(paths)}" -Dcucumber.execution.dry-run=true'
    code, output = run_maven(command + (f' "-Dcucumber.filter.tags={tags}"' if tags else ""), build)
    errors = dry_run_errors(output)
    report = [r for path in _report_files(build) for r in _read_report(path)]
    summary = re.findall(r"^\s*(\d+) Scenarios?\b", output, re.M)
    count = report_test_count(report) if report else int(summary[-1]) if summary else -1
    if code != 0 or errors or count < 0:
        return {"ok": False, "nombre": count, "environnement": not errors,
                "erreurs": errors or [line.strip() for line in output.strip().splitlines()[-3:]]
                or ["aucun rapport de dry-run"]}
    return {"ok": True, "nombre": count, "erreurs": [], "environnement": False}


def selection_check(build: str, dry_run_cmd: str, texts: dict, client: str) -> dict:
    """
    Deux dry-runs stricts des features du client ({chemin: texte}) : sans filtre (le runner exclut les scénarios
    désactivés) et avec le tag client. Même nombre de scénarios, égal au nombre attendu à la lecture des features
    (scénarios actifs) : ce nombre est le « nombre attendu » de la livraison. Renvoie {ok, sans_filtre, tag_client,
    attendu, nombre_attendu, erreurs, environnement}.
    """
    paths = []
    for directory in feature_dirs(sorted(texts)):
        # Dossier partagé avec d'autres features (init hors de la racine du client) : fichiers un par un
        on_disk = {os.path.relpath(os.path.join(d, n), build).replace("\\", "/")
                   for d, _, names in os.walk(os.path.join(build, *directory.split("/"))) for n in names
                   if n.endswith(".feature")}
        mine = sorted(f for f in texts if f.startswith(directory + "/"))
        paths += [directory] if on_disk <= set(mine) else mine
    expected = expected_count(texts.values(), "", a_revoir_tag())
    unfiltered = _dry_run_count(build, dry_run_cmd, paths)
    tagged = _dry_run_count(build, dry_run_cmd, paths, client) if unfiltered["ok"] else unfiltered
    out = {"ok": False, "sans_filtre": unfiltered["nombre"], "tag_client": tagged["nombre"], "attendu": expected,
           "nombre_attendu": 0, "erreurs": [], "environnement": False}
    if not (unfiltered["ok"] and tagged["ok"]):
        return {**out, "erreurs": [f"dry-run en échec : {e}" for e in (unfiltered["erreurs"] or tagged["erreurs"])],
                "environnement": unfiltered["environnement"] or tagged["environnement"]}
    if unfiltered["nombre"] != tagged["nombre"]:
        out["erreurs"].append(f"les deux dry-runs ne sélectionnent pas le même nombre de scénarios (sans filtre : "
                              f"{unfiltered['nombre']}, avec {client} : {tagged['nombre']}) : des scénarios actifs ne "
                              f"portent pas le tag client, ou le runner n'exclut pas {a_revoir_tag()}")
    elif unfiltered["nombre"] != expected:
        out["erreurs"].append(f"les dry-runs sélectionnent {unfiltered['nombre']} scénario(s) pour {expected} "
                              f"scénario(s) actif(s) dans les features : filtre de tags inopérant (propriété non "
                              f"transmise à Cucumber) ou runner qui n'exclut pas {a_revoir_tag()}")
    return {**out, "ok": not out["erreurs"], "nombre_attendu": 0 if out["erreurs"] else tagged["nombre"]}


def new_features(worktree: str, base: str, features: list) -> set:
    """Features absentes de `base` (créées par la génération)."""
    existing = set(_base_features(worktree, base))
    return {f for f in features if f not in existing}


def check_lot_files(worktree: str, base: str, files: list, client_name: str, cucumber_std_dir: str,
                    inspired: list = (), conventions: dict = None) -> dict:
    """
    Contrôles déterministes des features d'un lot : fichiers protégés de l'init globale (restaurés), emplacement,
    casse du tag client, fichiers de données (copiés du Standard quand c'est possible). Les fichiers d'une feature
    mal placée ne sont pas contrôlés : ils dépendent de son futur dossier. `conventions` (profil client) : racine,
    tag et fichiers protégés du client, racine et tag à défaut détectés. Renvoie {"copies", "restaures", "erreurs"}.
    """
    conventions = conventions or {}
    protected = check_protected_files(worktree, base, conventions.get("protected_files"))
    features = [f for f in files if f.endswith(".feature")
                and os.path.exists(os.path.join(worktree, *f.split("/")))]
    root = conventions.get("features_root") or client_features_root(worktree, base, client_name, cucumber_std_dir)
    misplaced = check_locations(features, root, client_name, new_features(worktree, base, features))
    placed = [f for f in features if not any(e.startswith(f + " :") for e in misplaced)]
    data = check_data_files(worktree, placed, cucumber_std_dir, inspired=inspired)
    tags = check_tags(worktree, features, conventions.get("client_tag") or client_tag(worktree, base, client_name))
    return {"copies": data["copies"], "restaures": protected["restaures"],
            "erreurs": protected["erreurs"] + misplaced + tags + data["erreurs"]}


# ---------------------------------------------------------------------------
# Fichiers protégés de l'initialisation globale et cohérence git du livrable
# ---------------------------------------------------------------------------

PROTECTED_REASON = ("fichier de l'init globale, partagé par tous les tests : créer la donnée dans la feature "
                    "(Background) ou dans une étape d'init dédiée")
# Fichiers d'un lot qui doivent être livrés : features, classes Java, données et règles
_DELIVERED_FILE = re.compile(rf"\.(feature|java|{'|'.join(DATA_EXTENSIONS)})$", re.I)
_BUILD_OUTPUT = re.compile(r"(^|/)target/")


def changed_since(worktree: str, base: str) -> set:
    """Fichiers suivis ajoutés, modifiés ou supprimés depuis `base` : commits du worktree et modifications en cours."""
    out = _git(worktree, "diff", "--name-only", "--no-renames", "-z", base, "--").stdout
    return {p for p in out.split("\0") if p}


def check_protected_files(worktree: str, base: str, protected: list, restore: bool = True) -> dict:
    """
    Fichiers protégés (features d'init et fichiers qu'elles intègrent) modifiés depuis `base` : une erreur bloquante
    par fichier. `restore` : fichier remis à son contenu de `base` (un binaire ne se corrige pas par Edit), la
    donnée manquante restant à créer dans la feature. Renvoie {"restaures", "erreurs"}.
    """
    touched = sorted(changed_since(worktree, base) & set(protected or ()))
    in_base = ls_tree(worktree, base) if restore and touched else {}
    restored = [p for p in touched if p in in_base]
    if restored:
        _git(worktree, "checkout", base, "--", *restored)
    return {"restaures": restored,
            "erreurs": [f"{p} : modifié alors que c'est un {PROTECTED_REASON}"
                        + (" ; modification annulée (fichier restauré depuis la base)" if p in restored else "")
                        for p in touched]}


def ignored_test_files(worktree: str) -> list:
    """
    Fichiers de test nouveaux (features, Java, données) ignorés par le .gitignore du dépôt client : sans ajout
    forcé, ils manqueraient au commit et au patch.
    """
    out = _git(worktree, "ls-files", "-o", "-i", "--exclude-standard", "-z", "--", "src/test").stdout
    return sorted(p for p in out.split("\0") if p and _DELIVERED_FILE.search(p) and not _BUILD_OUTPUT.search(p))


def uncommitted_files(worktree: str) -> list:
    """Fichiers modifiés ou nouveaux restés hors du dernier commit du worktree (hors sorties Maven target/)."""
    out = _git(worktree, "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames").stdout
    return sorted(p[3:] for p in out.split("\0") if len(p) > 3 and not _BUILD_OUTPUT.search(p[3:]))


def verify_branch_error(worktree: str, branch: str) -> str:
    """
    Le job de vérification lit `branch` : le worktree doit y être commité et poussé, sinon le job exécuterait une
    ancienne version. Renvoie '' si c'est le cas, sinon le message d'arrêt.
    """
    pending = uncommitted_files(worktree)
    if pending:
        return (f"{len(pending)} fichier(s) non commité(s) ({', '.join(pending[:3])}) : le job exécuterait une "
                f"ancienne version — commite et pousse sur {branch} avant la vérification")
    head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    remote = _git(worktree, "ls-remote", "origin", f"refs/heads/{branch}")
    pushed = remote.stdout.split()[0] if remote.stdout.split() else ""
    if remote.returncode != 0 or not pushed:
        return (f"branche {branch} introuvable sur origin : le job exécuterait une ancienne version (ou rien) — "
                f"pousse le worktree sur {branch} avant la vérification")
    if pushed != head:
        return (f"{branch} est à {pushed[:10]} sur origin, le worktree à {head[:10]} : le job exécuterait une "
                f"ancienne version — pousse sur {branch} avant la vérification")
    return ""


def push_for_verification(worktree: str, branch: str, message: str) -> tuple:
    """
    Commite le worktree (hors sorties Maven, fichiers de test ignorés par le .gitignore ajoutés de force) puis le
    pousse sur `branch` (origin), sans forcer. Renvoie (erreur d'arrêt ou '', commit créé) ; un push refusé (branche
    distante divergente, droits) arrête la vérification.
    """
    _git(worktree, "add", "-A", "--", ".", ":(exclude)target", ":(exclude)**/target/**")
    forced = ignored_test_files(worktree)
    if forced:
        _git(worktree, "add", "-f", "--", *forced)
    committed = (_git(worktree, "diff", "--cached", "--quiet").returncode != 0
                 and _git(worktree, "commit", "-m", message).returncode == 0)
    push = _git(worktree, "push", "origin", f"HEAD:refs/heads/{branch}")
    if push.returncode != 0:
        detail = " ".join((push.stderr.strip().splitlines() or ["?"])[-1].split())[:200]
        return (f"push sur {branch} refusé ({detail}) : le job exécuterait une ancienne version — intègre la branche "
                f"distante ou corrige les droits, puis relance"), committed
    return verify_branch_error(worktree, branch), committed


def patch_paths(worktree: str, patch: str) -> set:
    """Fichiers d'un patch format-patch, binaires compris (git apply --numstat, chemins non échappés)."""
    out = _git(worktree, "apply", "--numstat", "-z", patch).stdout.split("\0")
    paths, index = set(), 0
    while index < len(out):
        fields = out[index].split("\t")
        if len(fields) == 3 and fields[2]:
            paths.add(fields[2])
        elif len(fields) == 3 and index + 2 < len(out):
            # Renommage : chemins d'origine et de destination dans les deux entrées suivantes
            paths |= {out[index + 1], out[index + 2]}
            index += 2
        index += 1
    return paths


def check_patch_files(worktree: str, base: str, patch: str, client_name: str = "") -> list:
    """
    Fichiers ajoutés ou modifiés depuis `base` (commits du worktree) absents du patch : une erreur par fichier. Une
    feature identique à la base une fois les tags @agent_ retirés n'a rien à livrer.
    """
    from tools.cucumber_tools import strip_agent_tags
    expected = set(_git(worktree, "diff", "--name-only", "--no-renames", "-z", base, "HEAD", "--").stdout
                   .split("\0")) - {""}
    missing = expected - patch_paths(worktree, patch)
    errors = []
    for rel in sorted(missing):
        head, original = (_git(worktree, "show", f"{ref}:{rel}").stdout for ref in ("HEAD", base))
        if rel.endswith(".feature") and client_name and strip_agent_tags(head, client_name) == original:
            continue
        errors.append(f"{rel} : modifié dans les commits du worktree mais absent du patch {os.path.basename(patch)}")
    return errors


# ---------------------------------------------------------------------------
# Bilan de santé du dépôt client, avant génération
# ---------------------------------------------------------------------------

def health_path(client_name: str) -> str:
    return os.path.join(WORKSPACE_DIR, f"{client_name}-cucumber-sante.md")


def health_worktree_dir(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "worktree_sante")


def feature_dirs(features: list) -> list:
    """Dossiers minimaux couvrant `features` (un dossier inclus dans un autre de la liste est omis), pour Cucumber."""
    dirs = sorted({f.rsplit("/", 1)[0] for f in features if "/" in f})
    return [d for d in dirs if not any(d.startswith(other + "/") for other in dirs if other != d)]


def library_version(worktree: str) -> str:
    """Version de la dépendance test-jar (bibliothèque de steps, le Cucumber Standard) du pom.xml du client."""
    dependency = _TEST_JAR_DEPENDENCY.search(_read_text_file(os.path.join(worktree, "pom.xml")))
    version = re.search(r"<version>\s*([^<\s]+)\s*</version>", dependency.group(0)) if dependency else None
    return version.group(1) if version else ""


def std_library_version(cucumber_std_dir: str) -> str:
    """Version du projet Cucumber Standard (pom.xml de la branche retenue), hors parent et dépendances."""
    text = re.sub(r"<parent>.*?</parent>|<dependencies>.*?</dependencies>|<dependencyManagement>.*?"
                  r"</dependencyManagement>|<build>.*?</build>", "",
                  _read_text_file(os.path.join(cucumber_std_dir, "pom.xml")) if cucumber_std_dir else "", flags=re.S)
    version = re.search(r"<version>\s*([^<\s]+)\s*</version>", text)
    return version.group(1) if version else ""


def version_key(version: str) -> tuple:
    """Clé de tri d'une version (SNAPSHOT ignoré) : segments numériques comparés comme des nombres."""
    parts = re.split(r"[.\-_]", version.replace("-SNAPSHOT", "").strip())
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in parts if p)


def compare_versions(a: str, b: str) -> int:
    """-1, 0 ou 1 selon que la version `a` est antérieure, égale ou postérieure à `b` (SNAPSHOT ignoré)."""
    key_a, key_b = version_key(a), version_key(b)
    return (key_a > key_b) - (key_a < key_b)


def version_gap(client_version: str, std_version: str, server_version: str = "") -> str:
    """
    Écart entre la bibliothèque de steps du client et la version attendue, celle du serveur (à défaut celle du
    Standard de la branche), SNAPSHOT ignoré : message orienté (bibliothèque en retard ou en avance), '' si aucun.
    Signale aussi un Standard de la branche différent du serveur (catalogue de génération non aligné).
    """
    expected = server_version or std_version
    if not client_version or not expected:
        return (f"version de bibliothèque inconnue (client : {client_version or '?'}, attendue : "
                f"{expected or '?'}) : écart non vérifiable")
    notes = []
    order = compare_versions(client_version, expected)
    if order < 0:
        notes.append(f"le client utilise la bibliothèque de steps {client_version}, ANTÉRIEURE à la version attendue "
                     f"{expected} : des steps, colonnes, routes ou champs POJO du Standard aligné peuvent manquer ou "
                     f"différer à l'exécution")
    elif order > 0:
        notes.append(f"le client utilise la bibliothèque de steps {client_version}, POSTÉRIEURE à la version attendue "
                     f"{expected} : des routes ou champs peuvent être inconnus du serveur")
    if server_version and std_version and compare_versions(std_version, server_version):
        notes.append(f"le Cucumber Standard de la branche est en {std_version}, différent du serveur "
                     f"{server_version} : catalogue de génération non aligné")
    return " ; ".join(notes)


def runner_health(conventions: dict) -> list:
    """
    Cohérence du runner du profil avec la racine des features, l'init et le tag client, et rapport JSON
    (cucumber_tools.runner_gaps) ; un runner absent est signalé. Glue non contrôlée (package des steps inconnu ici).
    """
    from tools.cucumber_tools import runner_gaps
    runner = conventions.get("runner") or {}
    if not runner.get("classe"):
        return ["aucun runner détecté (ni runner du client, ni runner du Standard en surcouche) : à créer sur le "
                "modèle du Standard, en incluant les features d'init"]
    entry = {"glue": runner.get("glue") or [], "features": runner.get("features") or [],
             "tags": runner.get("tags") or [], "plugins": [runner["plugin_json"]] if runner.get("plugin_json") else []}
    label = runner["classe"] + (" (Standard, en surcouche)" if runner.get("surcouche") else "")
    return [f"{label} : {gap}" for gap in runner_gaps(entry, "", conventions)]


def health_report(client_name: str, cucumber_repo_dir: str, base: str, cucumber_std_dir: str, compile_cmd: str,
                  dry_run_cmd: str, conventions: dict = None) -> dict:
    """
    Bilan du dépôt client à `base`, avant génération : emplacement et tags de ses features, fichiers de données
    manquants, cohérence du runner (profil fourni), écart de version de la bibliothèque de steps avec la version
    attendue (serveur), écarts des steps
    utilisés entre cette bibliothèque et le Standard aligné, marqueurs TODO-VERSION, puis (si `compile_cmd`)
    compilation et dry-run strict de toutes ses features (steps non définis ou ambigus : projet client décalé de sa
    bibliothèque). `conventions` (profil client) : racine, tag et versions, à défaut détectés. Écrit
    health_path(client). Tag client de chaque scénario actif, et deux dry-runs (sans filtre, avec le tag client) qui
    donnent le nombre attendu. Renvoie {"ok", "decale", "ecart_version", "ecarts_steps", "todo_version", "sections",
    "fichier", "nombre_attendu"} ; les écarts de steps et les marqueurs informent sans rendre le dépôt malsain. Lève RuntimeError.
    """
    conventions = conventions or {}
    worktree = add_worktree(cucumber_repo_dir, health_worktree_dir(client_name), f"cucumber_{client_name}_sante",
                            base, reset=True)
    blobs = _base_features(worktree, base)
    features = sorted(blobs)
    root = conventions.get("features_root") or client_features_root(worktree, base, client_name, cucumber_std_dir)
    convention = conventions.get("client_tag") or client_tag(worktree, base, client_name)
    versions = (conventions.get("library_version") or library_version(worktree), std_library_version(cucumber_std_dir))
    server = conventions.get("server_version", "")
    gap = version_gap(*versions, server)
    steps_report = {"ref_bibliotheque": "", "ecarts": [], "avertissement": ""}
    if versions[0] and versions[1] and compare_versions(*versions):
        steps_report = cucumber_versions.version_report(cucumber_std_dir, versions[0],
                                                        blob_texts(worktree, blobs.values()).values())
    markers = cucumber_versions.todo_markers(worktree, base)
    contents = blob_texts(worktree, blobs.values())
    texts = {rel: contents.get(blob, "") for rel, blob in blobs.items()}
    sections = {
        "Emplacement": check_locations(features, root, client_name, set(features)),
        "Tags": check_tags(worktree, features, convention) + check_client_tags(texts, convention),
        "Fichiers de données": check_data_files(worktree, features, cucumber_std_dir, copy=False)["erreurs"],
    }
    if conventions:
        sections["Runner"] = runner_health(conventions)
    build = {"ok": True, "etape": "", "erreurs": [], "environnement": False}
    selection = {"nombre_attendu": 0, "attendu": expected_count(texts.values(), "", a_revoir_tag())}
    if compile_cmd and features:
        directory = build_dir(client_name, worktree, cucumber_std_dir)
        build = verify_build(directory, compile_cmd, feature_dirs(features), dry_run_cmd)
        label = "Compilation" if build["etape"] == "compilation" else "Dry-run (steps non définis ou ambigus)"
        sections[label + (" — hors code (environnement Maven)" if build["environnement"] else "")] = build["erreurs"]
        if build["ok"] and dry_run_cmd:
            selection = selection_check(directory, dry_run_cmd, texts, convention)
            sections["Sélection des scénarios (dry-runs sans filtre et avec le tag client)" + (
                " — hors code (environnement Maven)" if selection["environnement"] else "")] = selection["erreurs"]
    drifted = not build["ok"] and not build["environnement"]
    ok = not any(sections.values())
    lines = [f"# Bilan de santé de {client_name}-cucumber", "",
             f"- Base : `{base}` ; {len(features)} feature(s) ; racine des features : `{root}` ; tag client : "
             f"`{convention}`",
             f"- Bibliothèque de steps : client {versions[0] or '?'} ; Cucumber Standard {versions[1] or '?'} ; "
             f"serveur {server or '?'}" + (f" — ⚠️ {gap}" if gap else " — alignés"),
             "- Compilation et dry-run : " + ("non lancés (CUCUMBER_COMPILE_CMD vide)" if not compile_cmd else
                                              "OK" if build["ok"] else f"{build['etape']} en échec"),
             f"- Scénarios actifs : {selection['attendu']} à la lecture des features" + (
                 f" ; nombre attendu (dry-runs) : {selection['nombre_attendu']}" if selection["nombre_attendu"]
                 else "")]
    if drifted:
        lines.append("- ⚠️ **Le projet client est décalé de sa bibliothèque de steps** : ses features EXISTANTES ne "
                     "compilent pas ou ont des steps non définis ou ambigus. Ce n'est pas un problème de génération : "
                     "leurs échecs à l'exécution ne viendront pas des tests générés. Mettez à jour le dépôt client (ou "
                     "la version de sa bibliothèque) avant de générer.")
    for title, errors in sections.items():
        lines += ["", f"## {title} : {len(errors)}", ""] + [f"- {e}" for e in errors]
    if steps_report["ref_bibliotheque"] or steps_report["avertissement"]:
        lines += ["", f"## Écarts des steps utilisés entre la bibliothèque et le Standard aligné : "
                      f"{len(steps_report['ecarts'])}", ""]
        lines += ([f"- ⚠️ {steps_report['avertissement']}"] if steps_report["avertissement"] else
                  [f"- Référence de la bibliothèque : `{steps_report['ref_bibliotheque']}`"])
        lines += [f"- {cucumber_versions.difference_line(d)}" for d in steps_report["ecarts"]]
    if markers:
        lines += ["", f"## Marqueurs {cucumber_versions.TODO_MARKER.rstrip(':')} : {len(markers)}", ""]
        lines += [f"- {m['fichier']}:{m['ligne']} — {m['texte']}" for m in markers]
    _write_text(health_path(client_name), "\n".join(lines) + "\n")
    return {"ok": ok, "decale": drifted, "ecart_version": gap, "ecarts_steps": steps_report, "todo_version": markers,
            "sections": sections, "fichier": health_path(client_name), "nombre_attendu": selection["nombre_attendu"]}
