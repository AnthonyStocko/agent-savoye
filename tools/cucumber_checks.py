"""
Contrôles déterministes (0 token) des fichiers d'un dépôt <client>-cucumber : après chaque lot, et pour le bilan de
santé du dépôt avant génération.

- Fichiers de données : un step du Standard lit ses fichiers dans files/<dossier de la feature>/ (règles :
  rules/<dossier de la feature>/<nom>.json), le dossier étant celui de la feature sous features/ (BaseStep.setup,
  ManageFileUtil.transformFileToIntegrate). Le dépôt client n'a à l'exécution que ses propres fichiers : un fichier
  présent seulement dans le Cucumber Standard manque. Il est copié automatiquement depuis le Standard quand on l'y
  trouve ; sinon c'est une erreur de lot.
- Emplacement : toute nouvelle feature est sous la racine des features du client (ex. features/apparel/).
- Tags : le tag du client garde la casse de ses features existantes (ex. @APPAREL, jamais @apparel).
- Bilan de santé : ces contrôles sur les features existantes du client, plus compilation et dry-run de tout le projet.
"""
import os
import re
import shutil
from collections import Counter

from tools.cucumber_tools import (WORKSPACE_DIR, _git, _read_text_file, _write_text, add_worktree, blob_texts,
                                  build_dir, cache_dir, java_package_segment, ls_tree, verify_build)

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
    """Dossier d'une feature relatif à features/ (ex. apparel/WMS/Content), comme BaseStep.setup à l'exécution."""
    inner = rel[len(FEATURES_ROOT):] if rel.startswith(FEATURES_ROOT) else rel
    return inner.rsplit("/", 1)[0] if "/" in inner else ""


def resolved_path(feature_rel: str, ref: dict) -> str:
    """Chemin, relatif au dépôt, du fichier lu à l'exécution pour la référence `ref` de la feature `feature_rel`."""
    folder = feature_dir(feature_rel)
    prefix = f"{RULES_ROOT if ref['regle'] else FILES_ROOT}{folder + '/' if folder else ''}"
    return prefix + ref["nom"] + (".json" if ref["regle"] else "")


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


def client_features_root(worktree: str, base: str, client_name: str, cucumber_std_dir: str = "") -> str:
    """
    Racine des features du client (ex. src/test/resources/cucumber/features/apparel/) : premier dossier sous features/
    le plus fréquent parmi ses features existantes, hors dossiers du Standard ; à défaut le segment du client.
    """
    excluded = std_top_dirs(cucumber_std_dir) | {"WMS", "TMS", "OMS"}
    counts = Counter(p[len(FEATURES_ROOT):].split("/", 1)[0] for p in _base_features(worktree, base)
                     if "/" in p[len(FEATURES_ROOT):])
    found = [d for d, _ in counts.most_common() if d not in excluded]
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


def client_tag(worktree: str, base: str, client_name: str) -> str:
    """
    Tag du client avec sa casse (ex. @APPAREL) : graphie la plus fréquente, dans ses features existantes, des tags
    égaux au nom du client sans tenir compte de la casse ; à défaut @<CLIENT> en majuscules (comme l'initialisation).
    """
    wanted = f"@{java_package_segment(client_name)}".lower()
    texts = blob_texts(worktree, _base_features(worktree, base).values()).values()
    counts = Counter(t for text in texts for t in _tags(text) if t.lower() == wanted)
    return counts.most_common(1)[0][0] if counts else wanted.upper()


def check_tags(worktree: str, features: list, convention: str) -> list:
    """Tags du client dans une autre casse que `convention` : une erreur par feature et graphie."""
    errors = []
    for rel in features:
        text = _read_text_file(os.path.join(worktree, *rel.split("/")))
        for tag in sorted({t for t in _tags(text) if t.lower() == convention.lower() and t != convention}):
            errors.append(f"{rel} : tag {tag} : la convention du client est {convention} (même casse), remplace-le")
    return errors


def new_features(worktree: str, base: str, features: list) -> set:
    """Features absentes de `base` (créées par la génération)."""
    existing = set(_base_features(worktree, base))
    return {f for f in features if f not in existing}


def check_lot_files(worktree: str, base: str, files: list, client_name: str, cucumber_std_dir: str,
                    inspired: list = ()) -> dict:
    """
    Contrôles déterministes des features d'un lot : emplacement, casse du tag client, fichiers de données (copiés du
    Standard quand c'est possible). Les fichiers d'une feature mal placée ne sont pas contrôlés : ils dépendent de
    son futur dossier. Renvoie {"copies", "erreurs"}.
    """
    features = [f for f in files if f.endswith(".feature")
                and os.path.exists(os.path.join(worktree, *f.split("/")))]
    root = client_features_root(worktree, base, client_name, cucumber_std_dir)
    misplaced = check_locations(features, root, client_name, new_features(worktree, base, features))
    placed = [f for f in features if not any(e.startswith(f + " :") for e in misplaced)]
    data = check_data_files(worktree, placed, cucumber_std_dir, inspired=inspired)
    tags = check_tags(worktree, features, client_tag(worktree, base, client_name))
    return {"copies": data["copies"], "erreurs": misplaced + tags + data["erreurs"]}


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


def health_report(client_name: str, cucumber_repo_dir: str, base: str, cucumber_std_dir: str, compile_cmd: str,
                  dry_run_cmd: str) -> dict:
    """
    Bilan du dépôt client à `base`, avant génération : emplacement et tags de ses features, fichiers de données
    manquants, puis (si `compile_cmd`) compilation et dry-run de toutes ses features (steps non définis ou ambigus :
    le projet client est décalé du Standard). Écrit health_path(client). Renvoie {"ok", "decale", "sections",
    "fichier"}. Lève RuntimeError.
    """
    worktree = add_worktree(cucumber_repo_dir, health_worktree_dir(client_name), f"cucumber_{client_name}_sante",
                            base, reset=True)
    features = sorted(_base_features(worktree, base))
    root = client_features_root(worktree, base, client_name, cucumber_std_dir)
    convention = client_tag(worktree, base, client_name)
    sections = {
        "Emplacement": check_locations(features, root, client_name, set(features)),
        "Tags": check_tags(worktree, features, convention),
        "Fichiers de données": check_data_files(worktree, features, cucumber_std_dir, copy=False)["erreurs"],
    }
    build = {"ok": True, "etape": "", "erreurs": [], "environnement": False}
    if compile_cmd and features:
        build = verify_build(build_dir(client_name, worktree, cucumber_std_dir), compile_cmd,
                             feature_dirs(features), dry_run_cmd)
        label = "Compilation" if build["etape"] == "compilation" else "Dry-run (steps non définis ou ambigus)"
        sections[label + (" — hors code (environnement Maven)" if build["environnement"] else "")] = build["erreurs"]
    drifted = not build["ok"] and not build["environnement"]
    ok = not any(sections.values())
    lines = [f"# Bilan de santé de {client_name}-cucumber", "",
             f"- Base : `{base}` ; {len(features)} feature(s) ; racine des features : `{root}` ; tag client : "
             f"`{convention}`",
             "- Compilation et dry-run : " + ("non lancés (CUCUMBER_COMPILE_CMD vide)" if not compile_cmd else
                                              "OK" if build["ok"] else f"{build['etape']} en échec")]
    if drifted:
        lines.append("- ⚠️ **Le projet client est décalé du Standard** : ses features existantes ne compilent pas ou "
                     "ont des steps non définis ou ambigus ; leurs échecs à l'exécution ne viendront pas des tests "
                     "générés. Mettez à jour le dépôt client (ou la version du Standard) avant de générer.")
    for title, errors in sections.items():
        lines += ["", f"## {title} : {len(errors)}", ""] + [f"- {e}" for e in errors]
    _write_text(health_path(client_name), "\n".join(lines) + "\n")
    return {"ok": ok, "decale": drifted, "sections": sections, "fichier": health_path(client_name)}
