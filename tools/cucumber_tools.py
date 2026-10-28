"""
Onglet « Cucumber » : couverture Cucumber des spécifiques d'un client (main.py --step generate-cucumber-client).

Tout ce qui est déterministe est calculé ici, sans token : inventaire des spécifiques, catalogues Cucumber
(Standard et client), extraits Jira par spécifique, git (worktree, commits, patch), lots et récapitulatif.
L'agent n'écrit que les tests, lot par lot. Fichiers de travail : .tmp/cucumber/<client>/.
"""
import difflib
import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import unicodedata
from collections import Counter
from datetime import datetime

from tools import cucumber_studio

WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_ROOT = os.path.join(WORKSPACE_DIR, ".tmp", "cucumber")
STD_CACHE_DIR = os.path.join(CACHE_ROOT, "_standard")

# Branche de référence de chaque dépôt : intégration côté client, développement côté Standard
CLIENT_REFS = ("origin/integration", "origin/dev", "origin/HEAD", "HEAD")
STD_REFS = ("origin/development", "origin/dev", "origin/HEAD", "HEAD")

DEFAULT_EXCLUDE_GLOBS = (
    "pom.xml,kis.descriptor.yaml,*.md,*.iml,.git*,Jenkinsfile,Dockerfile,*.lock,package.json,package-lock.json,"
    "angular.json,tsconfig*.json,.editorconfig,.browserslistrc,karma.conf.js,export.metadata,"
    "target/*,*/target/*,.idea/*,*/.idea/*,node_modules/*,*/node_modules/*,.mvn/*,mvnw,mvnw.cmd,*.sh,*.bat,*.cmd,"
    "*/src/test/*,src/test/*,*Test.java,*IT.java,*.spec.ts,*.adoc,*/src/docs/*,"
    "application*.properties,application*.yml,application*.yaml,bootstrap*.yml,logback*.xml,log4j*,"
    "*.png,*.jpg,*.jpeg,*.gif,*.svg,*.ico,*.woff,*.woff2,*.ttf,*.eot,*.css,*.scss,*.less,*/assets/*,*.min.js,"
    "kis.db.*,bundle.list,import.*.sql,*AppServer.java,*ci.yaml,*ci.yml,*.xlsx,*.xls,*.docx,*.pdf"
)
DEFAULT_PARAM_GLOBS = "*.properties,*i18n*,*messages*,*.sql,*param*.xml,*parameter*,*.csv"
# Dépôts front (écrans AngularJS) : aucun step du Cucumber Standard ne pilote l'interface web
DEFAULT_EXCLUDE_REPOS = "*-frontend,*-front,*-ui"
# Ligne de journal de main.py lue par l'onglet Cucumber : référence Git retenue du Cucumber Standard
STD_REF_LOG = "[CUCUMBER STD] Référence Git retenue :"

STUDIO_FILES = re.compile(r"^(customized\.(xml|json)|original\.xml|i18n\..+\.txt)$")
STEP_PATTERN = r"@(Given|When|Then|And|But)\("
STOPWORDS = {
    "impl", "service", "services", "manager", "default", "abstract", "interface", "util", "utils", "helper",
    "java", "main", "core", "spec", "test", "tests", "config", "configuration", "model", "entity", "dto",
    "pojo", "request", "response", "rest", "controller", "repository", "datarepository", "builder", "factory",
    "customized", "original", "process", "interactive", "listener", "backend", "frontend", "domain", "component",
    "module", "html", "json", "string", "list", "data", "type", "value", "values", "com", "asis", "odatio",
}


# ---------------------------------------------------------------------------
# Nom du client et chemins
# ---------------------------------------------------------------------------

def client_name_from_repo(repo: str) -> str:
    """
    Nom du client déduit du dépôt saisi, en minuscules :
    https://git.kiscloud.io/projects/CLIENT -> client ; .../scm/client/odatio-client.git -> client ;
    clone local .tmp/client_CLIENT ou C:/dev/client -> client.
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


def init_patch_path(client_name: str) -> str:
    return os.path.join(WORKSPACE_DIR, f"{client_name}-cucumber-init.patch")


def coverage_path(client_name: str) -> str:
    return os.path.join(WORKSPACE_DIR, f"{client_name}-cucumber-couverture.md")


def cache_dir(client_name: str) -> str:
    path = os.path.join(CACHE_ROOT, client_name)
    os.makedirs(path, exist_ok=True)
    return path


def worktree_dir(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "worktree")


def inventory_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "inventaire.json")


def state_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "etat.json")


def _read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def load_inventory(client_name: str) -> dict:
    return _read_json(inventory_path(client_name), {}) or {}


def save_inventory(client_name: str, inventory: dict) -> None:
    _write_json(inventory_path(client_name), inventory)


def load_state(client_name: str) -> dict:
    return _read_json(state_path(client_name), {}) or {}


def save_state(client_name: str, state: dict) -> None:
    state["mis_a_jour"] = datetime.now().isoformat(timespec="seconds")
    _write_json(state_path(client_name), state)


def pending_lots(state: dict) -> list:
    return [lot for lot in state.get("lots", []) if lot.get("statut") not in ("termine", "exclu")]


def drop_excluded(state: dict, spec_ids) -> list:
    """
    Reprise après un nouvel inventaire : retire des lots restants les spécifiques qui n'y figurent plus (dépôt
    ou fichiers désormais exclus). Un lot vidé passe au statut « exclu ». Renvoie les identifiants retirés.
    """
    dropped = []
    for lot in pending_lots(state):
        kept = [i for i in lot["ids"] if i in spec_ids]
        if len(kept) < len(lot["ids"]):
            lot["exclus"] = lot.get("exclus", []) + [i for i in lot["ids"] if i not in spec_ids]
            dropped += [i for i in lot["ids"] if i not in spec_ids]
            lot["ids"] = kept
            if not kept:
                lot["statut"] = "exclu"
    return dropped


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def _git(repo: str, *args, stdin: str = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], input=stdin, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def git_repos(root: str) -> dict:
    """Dépôts git de `root` (lui-même ou ses sous-dossiers directs) : {nom: chemin}."""
    if not root or not os.path.isdir(root):
        return {}
    if os.path.exists(os.path.join(root, ".git")):
        return {os.path.basename(os.path.normpath(root)): root}
    return {name: os.path.join(root, name) for name in sorted(os.listdir(root))
            if os.path.exists(os.path.join(root, name, ".git"))}


def pick_ref(repo: str, candidates) -> str:
    for ref in candidates:
        if _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0:
            return ref
    return ""


def rev_parse(repo: str, ref: str) -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


def ls_tree(repo: str, ref: str) -> dict:
    """{chemin: blob} des fichiers de `ref`."""
    out = _git(repo, "ls-tree", "-r", "-z", ref).stdout
    files = {}
    for entry in out.split("\0"):
        if "\t" in entry:
            meta, path = entry.split("\t", 1)
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob":
                files[path] = parts[2]
    return files


def known_blobs(repo: str, blobs) -> set:
    """Blobs présents dans la base d'objets de `repo` (un contenu identique y a existé un jour)."""
    blobs = list(blobs)
    if not blobs:
        return set()
    out = _git(repo, "cat-file", "--batch-check", stdin="\n".join(blobs) + "\n").stdout
    return {line.split()[0] for line in out.splitlines() if line and not line.endswith("missing")}


def blob_texts(repo: str, blobs) -> dict:
    """Contenu texte de plusieurs blobs en un seul appel git (git cat-file --batch) : {blob: texte}."""
    blobs = list(dict.fromkeys(blobs))
    if not blobs:
        return {}
    data = subprocess.run(["git", "-C", repo, "cat-file", "--batch"], input=("\n".join(blobs) + "\n").encode(),
                          capture_output=True).stdout
    texts, pos = {}, 0
    for blob in blobs:
        end = data.find(b"\n", pos)
        header = data[pos:end].split()
        pos = end + 1
        if len(header) < 3 or header[1] == b"missing":
            continue
        size = int(header[2])
        texts[blob] = data[pos:pos + size].decode("utf-8", errors="replace")
        pos += size + 1
    return texts


# ---------------------------------------------------------------------------
# Inventaire des spécifiques
# ---------------------------------------------------------------------------

def _globs(env_name: str, default: str) -> list:
    value = os.getenv(env_name, "").strip() or default
    return [g.strip().lower() for g in value.split(",") if g.strip()]


def outside_inventory(repo_name: str) -> bool:
    """Dépôt du client que l'inventaire ignore : dépôts Cucumber et dépôts exclus (CUCUMBER_EXCLUDE_REPOS)."""
    return "cucumber" in repo_name.lower() or _matches(repo_name,
                                                       _globs("CUCUMBER_EXCLUDE_REPOS", DEFAULT_EXCLUDE_REPOS))


def _matches(path: str, globs: list) -> bool:
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(lowered, g) or fnmatch.fnmatchcase(name, g) for g in globs)


_JAVA_DECL = re.compile(r"^\s*(?:@\w+\s+)*(?:(?:public|protected|private|static|final|abstract|synchronized|default)\s+)+"
                        r"[\w<>\[\],.?\s]+?\s+(\w+)\s*\(")
_JAVA_TYPE = re.compile(r"\b(?:class|interface|enum|record)\s+(\w+)")
_XML_NODE = re.compile(r"<([A-Za-z][\w:.-]*)\b[^>]*?(?:\b(?:id|name|code|key)=\"([^\"]+)\")?")


def diff_summary(old_text: str, new_text: str, path: str) -> dict:
    """Diff résumé (jamais le diff complet) : +ajouts -suppressions et éléments modifiés (méthodes, nœuds XML)."""
    old, new = old_text.splitlines(), new_text.splitlines()
    added = removed = 0
    changed_lines = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if tag == "equal":
            continue
        added += j2 - j1
        removed += i2 - i1
        changed_lines.append(j1)
    is_java = path.endswith((".java", ".kt"))
    elements = []
    for line_no in changed_lines:
        for k in range(min(line_no, len(new) - 1), -1, -1):
            line = new[k] if new else ""
            match = (_JAVA_DECL.match(line) or _JAVA_TYPE.search(line)) if is_java else _XML_NODE.search(line)
            if match:
                label = match.group(1) if is_java else (
                    f"{match.group(1)}[{match.group(2)}]" if match.group(2) else match.group(1))
                if label not in elements:
                    elements.append(label)
                break
        if len(elements) >= 12:
            break
    return {"stat": f"+{added} -{removed}", "elements": elements}


def _std_index(std_repos: dict) -> dict:
    """Index {nom de fichier Java: (dépôt, ref, chemin, blob)} du Standard, mis en cache selon les commits de référence."""
    refs = {name: pick_ref(path, STD_REFS) for name, path in std_repos.items()}
    heads = {name: rev_parse(std_repos[name], ref) for name, ref in refs.items() if ref}
    key = hashlib.sha1(json.dumps(heads, sort_keys=True).encode()).hexdigest()[:12]
    cache_file = os.path.join(STD_CACHE_DIR, f"index_java_{key}.json")
    cached = _read_json(cache_file)
    if cached is not None:
        return cached
    index = {}
    for name, ref in refs.items():
        if not ref:
            continue
        for path, blob in ls_tree(std_repos[name], ref).items():
            if path.endswith((".java", ".kt")) and "/src/main/" in f"/{path}":
                index.setdefault(path.rsplit("/", 1)[-1], [name, ref, path, blob])
    _write_json(cache_file, index)
    return index


# Segments de package ou de dossier sans valeur métier : ignorés pour nommer l'objet métier d'un fichier
GENERIC_SEGMENTS = {
    "action", "actions", "core", "service", "services", "impl", "rest", "api", "internal", "util", "utils", "model",
    "dto", "pojo", "pojos", "datarepository", "repository", "datamodel", "datautil", "config", "spec", "common",
    "create", "update", "delete", "get", "save", "load", "manage", "search", "validate", "identify", "count",
    "estimate", "cancel", "csv", "resources", "main", "java", "src", "com", "asis", "odatio", "constant",
    "constants", "data", "enum", "enums", "exception", "exceptions", "mapper", "mappers", "converter", "pair",
    "controllers", "controller", "templates", "template", "detail", "list", "modal", "steps", "views", "components",
    "directives", "filters", "app", "js", "html", "i18n", "db", "sql", "migration", "derby", "oracle", "postgres",
    "postgresql", "sql_server", "sqlserver", "mysql",
}


def _business_key(segments: list) -> str:
    """Dernier segment métier d'un chemin de package ou de dossier (ex. .../estimate/productionorderline)."""
    for segment in reversed(segments):
        lowered = segment.lower()
        if lowered not in GENERIC_SEGMENTS and not re.fullmatch(r"v\d+(_\d+)*", lowered) and len(lowered) > 2:
            return segment
    return segments[-1] if segments else "."


def _is_label_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name.startswith("i18n") or "messages" in name


def _group_key(repo: str, path: str) -> tuple:
    """
    Spécifique fonctionnel d'un fichier : processus Studio, plugin d'interface, ou objet métier (dernier segment
    métier du package Java ou du dossier de ressources), tous modules du dépôt confondus.
    """
    parts = path.split("/")
    if STUDIO_FILES.match(parts[-1]) and len(parts) >= 2:
        return repo, "/".join(parts[:-1]), "processus"
    if parts[-1].lower().endswith(".sql"):
        return repo, "scripts SQL", "scripts SQL"
    plugin = next((i for i, p in enumerate(parts[:-1]) if p.endswith("-plugin")), None)
    if plugin is not None:
        return repo, parts[plugin], "plugin"
    for marker, nature in (("src/main/java/", "objet métier"), ("src/main/resources/", "objet métier")):
        if f"/{marker}" in f"/{path}":
            package = path.split(marker, 1)[1].split("/")[:-1]
            return repo, _business_key(package), nature
    # Interface et autres fichiers : module le plus profond (segment composé, ex. wms-preparation-ui-dispatchunit)
    module = next((p for p in reversed(parts[:-1]) if "-" in p), "")
    return (repo, module, "module") if module else (repo, _business_key(parts[:-1]), "dossier")


def _words(text: str) -> list:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return [w for w in re.split(r"[^A-Za-zÀ-ÿ0-9]+", text.lower()) if len(w) >= 4 and w not in STOPWORDS
            and not w.isdigit()]


def _keywords(group_dir: str, files: list) -> list:
    words = []
    for f in files:
        base = re.sub(r"\.[^.]+$", "", f["path"].rsplit("/", 1)[-1])
        if base in ("customized", "original") or base.startswith("i18n"):
            base = f["path"].split("/")[-2]
        words += _words(base) + [w for e in f.get("diff", {}).get("elements", []) for w in _words(e)]
    words += _words(group_dir.rsplit("/", 1)[-1])
    seen = []
    for w in words:
        if w not in seen:
            seen.append(w)
    return seen[:15]


def build_inventory(client_name: str, client_root: str, std_root: str, max_files_per_group: int = 40) -> dict:
    """
    Fichiers spécifiques du client : contenu absent du Standard (un contenu identique à une version passée du
    Standard est un écart de version, ignoré). override = même chemin (dépôt dérivé du Standard), même nom de
    classe dans le Standard ou processus Studio avec original.xml ; extension = absent du Standard ;
    parametrage = paramètres et messages. Regroupés en spécifiques fonctionnels.
    """
    exclude, params = _globs("CUCUMBER_EXCLUDE_GLOBS", DEFAULT_EXCLUDE_GLOBS), _globs("CUCUMBER_PARAM_GLOBS",
                                                                                      DEFAULT_PARAM_GLOBS)
    excluded_repos = _globs("CUCUMBER_EXCLUDE_REPOS", DEFAULT_EXCLUDE_REPOS)
    client_repos = {n: p for n, p in git_repos(client_root).items()
                    if "cucumber" not in n.lower() and not _matches(n, excluded_repos)}
    std_repos = git_repos(std_root)
    std_index = _std_index(std_repos)
    refs, files = {}, []
    for repo, repo_dir in client_repos.items():
        ref = pick_ref(repo_dir, CLIENT_REFS)
        if not ref:
            continue
        refs[repo] = {"ref": ref, "commit": rev_parse(repo_dir, ref)}
        tree = {p: b for p, b in ls_tree(repo_dir, ref).items() if not _matches(p, exclude)}
        std_dir = std_repos.get(repo)
        std_ref = pick_ref(std_dir, STD_REFS) if std_dir else ""
        std_tree = ls_tree(std_dir, std_ref) if std_ref else {}
        if std_dir:
            refs[repo]["standard"] = f"{repo}@{std_ref}"
        candidates = {p: b for p, b in tree.items() if std_tree.get(p) != b}
        drift = known_blobs(std_dir, set(candidates.values())) if std_dir else set()
        for path, blob in candidates.items():
            # original.* : référence Standard d'un processus Studio, comparée à son customized.*
            if blob in drift or path.rsplit("/", 1)[-1].startswith("original."):
                continue
            entry = {"repo": repo, "path": path, "blob": blob}
            name = path.rsplit("/", 1)[-1]
            # Diff résumé calculé ensuite, en lisant les contenus par lots (_diff : dépôt et blob de la référence)
            if _matches(path, params):
                entry["kind"] = "parametrage"
            elif std_dir and path in std_tree:
                entry.update(kind="override", standard=f"{repo}:{path}", _diff=(std_dir, std_tree[path]))
            elif name.startswith("customized.") and path[:-len(name)] + "original" + name[10:] in tree:
                original = path[:-len(name)] + "original" + name[10:]
                entry.update(kind="override", standard=f"original{name[10:]} (processus Studio standard)",
                             _diff=(repo_dir, tree[original]))
            elif not std_dir and name in std_index:
                std_repo, _, std_path, std_blob = std_index[name]
                entry.update(kind="override", standard=f"{std_repo}:{std_path}", _diff=(std_repos[std_repo], std_blob))
            else:
                entry["kind"] = "extension"
            entry["_dir"] = repo_dir
            files.append(entry)

    wanted = {}
    for entry in files:
        if "_diff" in entry:
            wanted.setdefault(entry["_diff"][0], set()).add(entry["_diff"][1])
            wanted.setdefault(entry["_dir"], set()).add(entry["blob"])
    texts = {directory: blob_texts(directory, blobs) for directory, blobs in wanted.items()}
    for entry in files:
        reference = entry.pop("_diff", None)
        directory = entry.pop("_dir")
        if reference:
            # Version Standard du fichier overridé (dépôt, blob) : exportée par export_sources pour l'agent
            entry["ref_standard"] = {"dir": reference[0], "blob": reference[1]}
            entry["diff"] = diff_summary(texts[reference[0]].get(reference[1], ""),
                                         texts[directory].get(entry["blob"], ""), entry["path"])

    # Une unité (processus, plugin, module, objet métier) regroupe tous ses fichiers, quelle que soit leur nature
    groups, natures = {}, {}
    for entry in files:
        repo, unit, nature = _group_key(entry["repo"], entry["path"])
        groups.setdefault((repo, unit), []).append(entry)
        if natures.get((repo, unit)) in (None, "module", "dossier"):
            natures[(repo, unit)] = nature
    # Unités dont seuls les libellés diffèrent : réunies en un spécifique « libellés » par dépôt
    for key in [k for k, group in groups.items() if all(_is_label_file(f["path"]) for f in group)]:
        groups.setdefault((key[0], "libellés (i18n)"), []).extend(groups.pop(key))
        natures[(key[0], "libellés (i18n)")] = "libellés"
    specifics = []
    for (repo, group_dir), group_files in sorted(groups.items()):
        nature = natures[(repo, group_dir)]
        kinds = {f["kind"] for f in group_files}
        kind = "override" if "override" in kinds else ("parametrage" if kinds == {"parametrage"} else "extension")
        spec_id = "sp-" + hashlib.sha1(f"{repo}:{group_dir}".encode()).hexdigest()[:8]
        specifics.append({
            "id": spec_id, "libelle": f"{repo} · {group_dir}", "type": kind, "nature": nature, "depot": repo,
            "dossier": group_dir, "nb_fichiers": len(group_files),
            "fichiers": sorted(group_files, key=lambda f: f["path"])[:max_files_per_group],
            "mots_cles": [w for w in _keywords(group_dir, group_files) if w not in _words(client_name)],
        })
    return {"client": client_name, "genere_le": datetime.now().isoformat(timespec="seconds"),
            "depot_client": client_root, "standard": std_root, "references": refs, "specifiques": specifics}


def export_sources(inventory: dict) -> None:
    """
    Copie, à la version de référence, le contenu des fichiers spécifiques dans .tmp/cucumber/<client>/sources/ et,
    pour un override, celui de sa version Standard (dépôt BITBUCKET_STD_REPO_PATH, ou original.* d'un processus
    Studio) dans .tmp/cucumber/<client>/standard/ (f["local_standard"]) : le clone du socle Standard est hors de
    l'accès de l'agent, qui doit pourtant savoir ce que faisait le Standard pour tester ce que l'override change.
    """
    root = os.path.join(cache_dir(inventory["client"]), "sources")
    std_root = os.path.join(cache_dir(inventory["client"]), "standard")
    repos = git_repos(inventory["depot_client"])
    missing, missing_std = {}, {}
    for spec in inventory["specifiques"]:
        for f in spec["fichiers"]:
            f["local"] = os.path.join(root, f["repo"], *f["path"].split("/"))
            if not os.path.exists(f["local"]):
                missing.setdefault(f["repo"], []).append(f)
            reference = f.get("ref_standard")
            if reference and os.path.isdir(reference.get("dir", "")):
                # Blob dans le chemin : une nouvelle version du Standard n'est jamais confondue avec l'ancienne
                f["local_standard"] = os.path.join(std_root, f["repo"], reference["blob"][:10], *f["path"].split("/"))
                if not os.path.exists(f["local_standard"]):
                    missing_std.setdefault(reference["dir"], []).append(f)
    for repo, entries in missing.items():
        texts = blob_texts(repos[repo], [f["blob"] for f in entries])
        for f in entries:
            _write_text(f["local"], texts.get(f["blob"], ""))
    for directory, entries in missing_std.items():
        texts = blob_texts(directory, [f["ref_standard"]["blob"] for f in entries])
        for f in entries:
            _write_text(f["local_standard"], texts.get(f["ref_standard"]["blob"], ""))


# ---------------------------------------------------------------------------
# Catalogues Cucumber (Standard et client)
# ---------------------------------------------------------------------------

_JAVA_STRING = r'"(?:[^"\\]|\\.)*"'
_STEP_OPERAND = rf'(?:{_JAVA_STRING}|(?:[A-Z]\w*\.)?[A-Z][A-Z0-9_]*)'
# Argument d'une annotation de step : littéral, constante (CONSTANTE, Classe.CONSTANTE) ou concaténation des deux
_STEP_ANNOTATION = re.compile(r'@(?:Given|When|Then|And|But|Soit|Quand|Alors|Et|Mais)\s*\(\s*(?:value\s*=\s*)?'
                              rf'({_STEP_OPERAND}(?:\s*\+\s*{_STEP_OPERAND})*)\s*[,)]')


def resolve_step_argument(argument: str, local: dict, class_constants) -> str:
    """
    Expression d'un argument d'annotation de step ('' si non résolue) : littéral tel quel, sinon constantes
    `static final String` du fichier (`local`) ou de la classe référencée (class_constants(Classe) -> dict).
    """
    if re.fullmatch(_JAVA_STRING, argument):
        return argument[1:-1]
    shared = {}
    for cls in dict.fromkeys(re.findall(r"\b([A-Z]\w*)\.[A-Z][A-Z0-9_]*\b", argument)):
        shared.update(class_constants(cls))
    # Constante qualifiée : celle de la classe nommée l'emporte sur un homonyme du fichier
    qualified = {name for name in re.findall(r"\b[A-Z]\w*\.([A-Z][A-Z0-9_]*)\b", argument)}
    resolved = _resolve(argument, {k: v for k, v in local.items() if k not in qualified}, shared)
    return "" if not resolved or "{…}" in resolved else resolved


def step_expressions(text: str, class_constants=lambda cls: {}, local: dict = None) -> tuple:
    """
    (expressions, arguments non résolus) des annotations de step d'un texte Java ; `local` : constantes du fichier
    (par défaut celles de `text`).
    """
    found, unresolved = [], []
    for match in _STEP_ANNOTATION.finditer(text):
        argument = match.group(1)
        if local is None and not re.fullmatch(_JAVA_STRING, argument):
            local = _string_constants(text)
        expr = resolve_step_argument(argument, local or {}, class_constants)
        (found if expr else unresolved).append(expr or argument)
    return found, unresolved


def _class_constants_loader(repo_dir: str, ref: str, tree: dict):
    """class_constants(Classe) -> {NOM: expression} lu dans <Classe>.java de `ref` (mis en cache)."""
    by_class, cache = {}, {}
    for path, blob in tree.items():
        if path.endswith(".java"):
            by_class.setdefault(path.rsplit("/", 1)[-1][:-5], blob)

    def load(cls: str) -> dict:
        if cls not in cache:
            blob = by_class.get(cls)
            cache[cls] = _string_constants(blob_texts(repo_dir, [blob]).get(blob, "")) if blob else {}
        return cache[cls]
    return load


def disk_class_constants(root: str):
    """class_constants(Classe) -> {NOM: expression} lu dans <Classe>.java sous `root` (sur disque, mis en cache)."""
    by_class, cache = {}, {}
    for directory, _, names in os.walk(root) if os.path.isdir(root) else ():
        for name in names:
            if name.endswith(".java"):
                by_class.setdefault(name[:-5], os.path.join(directory, name))

    def load(cls: str) -> dict:
        if cls not in cache:
            cache[cls] = _string_constants(_read_text_file(by_class[cls])) if cls in by_class else {}
        return cache[cls]
    return load


def report_unresolved_steps(label: str, catalogue: dict) -> None:
    """Journal des steps dont l'expression (constante) n'a pas pu être résolue."""
    unresolved = catalogue.get("steps_non_resolus") or []
    if unresolved:
        print(f"[CUCUMBER] ⚠️ {label} : {len(unresolved)} step(s) par constante non résolu(s) : "
              + ", ".join(f"{u['file'].rsplit('/', 1)[-1]}:{u['line']} {u['argument']}" for u in unresolved[:5])
              + (" …" if len(unresolved) > 5 else ""))


def extract_cucumber(repo_dir: str, ref: str) -> dict:
    """
    Steps (expression, fichier:ligne) et features (titre, scénarios, tags) de `ref`, par git grep. Steps déclarés par
    constante (@Given(CONSTANTE), @Given(Classe.CONSTANTE)) : constante résolue dans le fichier ou la classe
    référencée ; ceux qui ne le sont pas sont listés dans steps_non_resolus.
    """
    steps, unresolved, pending = [], [], []
    out = _git(repo_dir, "grep", "-nE", STEP_PATTERN, ref, "--", "*.java", "*.kt").stdout
    for line in out.splitlines():
        line = line[len(ref) + 1:] if line.startswith(f"{ref}:") else line
        parts = line.split(":", 2)
        if len(parts) == 3:
            expr = re.search(r'\(\s*"((?:[^"\\]|\\.)*)"', parts[2])
            if expr:
                steps.append({"expr": expr.group(1), "file": parts[0], "line": int(parts[1])})
            else:
                pending.append(parts)
    if pending:
        tree = ls_tree(repo_dir, ref)
        texts = blob_texts(repo_dir, [tree[p[0]] for p in pending if p[0] in tree])
        class_constants = _class_constants_loader(repo_dir, ref, tree)
        constants = {}
        for path, number, content in pending:
            match = _STEP_ANNOTATION.search(content)
            if not match:
                unresolved.append({"file": path, "line": int(number), "argument": content.strip()[:120]})
                continue
            if path not in constants:
                constants[path] = _string_constants(texts.get(tree.get(path), ""))
            expr = resolve_step_argument(match.group(1), constants[path], class_constants)
            if expr:
                steps.append({"expr": expr, "file": path, "line": int(number)})
            else:
                unresolved.append({"file": path, "line": int(number), "argument": match.group(1)})
        steps.sort(key=lambda s: (s["file"], s["line"]))
    features = {}
    out = _git(repo_dir, "grep", "-nE", r"^\s*(@|Feature:|Fonctionnalité:|Scenario|Scénario)", ref,
               "--", "*.feature").stdout
    for line in out.splitlines():
        line = line[len(ref) + 1:] if line.startswith(f"{ref}:") else line
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        feature = features.setdefault(parts[0], {"path": parts[0], "feature": "", "scenarios": [], "tags": [],
                                                  "lines": 0})
        text = parts[2].strip()
        feature["lines"] = max(feature["lines"], int(parts[1]))
        if text.startswith("@"):
            feature["tags"] += [t for t in text.split() if t.startswith("@") and t not in feature["tags"]]
        elif ":" in text and text.split(":", 1)[0] in ("Feature", "Fonctionnalité"):
            feature["feature"] = text.split(":", 1)[1].strip()
        elif ":" in text:
            feature["scenarios"].append(text.split(":", 1)[1].strip())
    return {"ref": ref, "commit": rev_parse(repo_dir, ref), "steps": steps, "features": list(features.values()),
            "steps_non_resolus": unresolved}


def _step_prefix(expr: str) -> str:
    match = re.match(r"^([A-Z][A-Z0-9_]*)-", expr)
    return match.group(1) if match else "(sans préfixe)"


def _steps_md(title: str, steps: list) -> str:
    lines = [f"# {title}", "Une ligne par step : expression — fichier:ligne.", ""]
    for prefix in sorted({_step_prefix(s["expr"]) for s in steps}):
        lines.append(f"## {prefix}")
        lines += [f"- {s['expr']} — {s['file']}:{s['line']}" for s in steps if _step_prefix(s["expr"]) == prefix]
        lines.append("")
    return "\n".join(lines)


def _features_md(title: str, features: list) -> str:
    lines = [f"# {title}", "Une ligne par feature : chemin | Feature | tags | scénarios.", ""]
    for f in sorted(features, key=lambda x: x["path"]):
        lines.append(f"- {f['path']} | {f['feature']} | {' '.join(f['tags'])} | " + " ; ".join(f["scenarios"]))
    return "\n".join(lines)


# À incrémenter quand le contenu du catalogue change (steps par constante résolus) : invalide le cache
STD_CATALOGUE_FORMAT = 2


def standard_catalogue(cucumber_std_dir: str, odatio_version: str) -> dict:
    """
    Catalogue du Cucumber Standard, mis en cache par version Odatio et régénéré seulement si son HEAD change :
    sommaire (quelques Ko, lu par l'agent), steps et features détaillés (consultés par Grep), exemples .feature.
    """
    version = re.sub(r"[^\w.-]", "_", odatio_version or "inconnue")
    head = rev_parse(cucumber_std_dir, "HEAD")
    paths = {name: os.path.join(STD_CACHE_DIR, f"{name}_{version}.md")
             for name in ("catalogue_std", "steps_std", "features_std")}
    meta_file = os.path.join(STD_CACHE_DIR, f"catalogue_std_{version}.json")
    meta = _read_json(meta_file, {}) or {}
    if meta.get("head") == head and meta.get("format") == STD_CATALOGUE_FORMAT and all(
            os.path.exists(p) for p in paths.values()):
        return meta
    catalogue = extract_cucumber(cucumber_std_dir, "HEAD")
    steps, features = catalogue["steps"], catalogue["features"]
    report_unresolved_steps("Cucumber Standard", catalogue)
    # Exemples : les deux features les plus courtes ayant au moins deux scénarios, de domaines différents
    examples, domains = [], set()
    for f in sorted((f for f in features if len(f["scenarios"]) >= 2 and 30 <= f["lines"] <= 150),
                    key=lambda f: f["lines"]):
        domain = f["path"].split("features/", 1)[-1].split("/")[0]
        if domain not in domains:
            domains.add(domain)
            target = os.path.join(STD_CACHE_DIR, f"exemples_{version}", f["path"].rsplit("/", 1)[-1])
            _write_text(target, _git(cucumber_std_dir, "show", f"HEAD:{f['path']}").stdout)
            examples.append(target)
        if len(examples) == 2:
            break
    by_prefix = {}
    for s in steps:
        entry = by_prefix.setdefault(_step_prefix(s["expr"]), [0, set()])
        entry[0] += 1
        entry[1].add(s["file"].rsplit("/", 1)[-1])
    by_folder = {}
    for f in features:
        folder = "/".join(f["path"].split("/")[-3:-1]) if f["path"].count("/") >= 3 else f["path"]
        entry = by_folder.setdefault(folder, [0, 0])
        entry[0] += 1
        entry[1] += len(f["scenarios"])
    tags = sorted({t for f in features for t in f["tags"]})
    summary = [
        f"# Catalogue Cucumber Standard (Odatio {odatio_version}, commit {head[:10]})", "",
        f"## Steps réutilisables : {len(steps)} — détail dans '{paths['steps_std']}'",
        "Cherche-y les steps par préfixe ou mot-clé (Grep), avant d'en créer un nouveau."
        + (f" {len(catalogue['steps_non_resolus'])} step(s) déclarés par une constante non résolue n'y figurent pas."
           if catalogue["steps_non_resolus"] else ""), "",
        "| Préfixe | Steps | Fichiers Step Definitions |", "|---|---|---|",
    ]
    summary += [f"| {p} | {n} | {', '.join(sorted(files)[:6])}{' …' if len(files) > 6 else ''} |"
                for p, (n, files) in sorted(by_prefix.items(), key=lambda kv: -kv[1][0])]
    summary += ["", f"## Features : {len(features)} — détail dans '{paths['features_std']}'", "",
                "| Dossier | Features | Scénarios |", "|---|---|---|"]
    summary += [f"| {d} | {n} | {s} |" for d, (n, s) in sorted(by_folder.items())]
    summary += ["", "## Tags", " ".join(tags), "", "## Exemples (à lire une seule fois)"]
    summary += [f"- '{e}'" for e in examples]
    _write_text(paths["catalogue_std"], "\n".join(summary) + "\n")
    _write_text(paths["steps_std"], _steps_md(f"Steps du Cucumber Standard (Odatio {odatio_version})", steps))
    _write_text(paths["features_std"], _features_md(f"Features du Cucumber Standard (Odatio {odatio_version})",
                                                    features))
    meta = {"head": head, "format": STD_CATALOGUE_FORMAT, "version": odatio_version, "examples": examples,
            "nb_steps": len(steps),
            "nb_steps_non_resolus": len(catalogue["steps_non_resolus"]), "nb_features": len(features), **paths}
    _write_json(meta_file, meta)
    return meta


def client_coverage(client_name: str, cucumber_repo_dir: str, ref: str) -> dict:
    """Catalogue du dépôt Cucumber client (couverture_existante.md)."""
    catalogue = extract_cucumber(cucumber_repo_dir, ref)
    report_unresolved_steps(f"dépôt Cucumber {client_name}", catalogue)
    path = os.path.join(cache_dir(client_name), "couverture_existante.md")
    _write_text(path, _features_md(f"Features existantes du dépôt Cucumber {client_name} ({ref})",
                                   catalogue["features"]) + "\n\n"
                + _steps_md(f"Steps existants du dépôt Cucumber {client_name}", catalogue["steps"]))
    catalogue["fichier"] = path
    return catalogue


def match_coverage(inventory: dict, catalogue: dict) -> None:
    """
    Rapproche chaque spécifique des scénarios existants par mots-clés : deja_couvert (au moins 60 % des
    mots-clés trouvés dans une même feature), partiel (au moins 30 %), sinon non_couvert.
    """
    documents = []
    for f in catalogue["features"]:
        text = " ".join([f["path"], f["feature"], " ".join(f["scenarios"]), " ".join(f["tags"])])
        documents.append((f["path"], f["feature"] or f["path"], set(_words(text))))
    for spec in inventory["specifiques"]:
        keywords = set(spec["mots_cles"])
        scored = sorted(((len(keywords & words) / len(keywords), path, title) for path, title, words in documents
                         if keywords and keywords & words), reverse=True)
        best = scored[0][0] if scored else 0
        spec["couverture"] = "deja_couvert" if best >= 0.6 else ("partiel" if best >= 0.3 else "non_couvert")
        spec["scenarios_existants"] = [f"{path} ({title})" for score, path, title in scored[:5] if score >= 0.3]


# ---------------------------------------------------------------------------
# Tests Standard équivalents à chaque spécifique
# ---------------------------------------------------------------------------

# À incrémenter quand le format de l'index change : invalide le cache
STD_TESTS_FORMAT = 2
DEFAULT_STD_MATCH_MIN = 0.4
MAX_STD_TESTS = 3
# Somme d'idf donnant l'évidence maximale des mots-clés : deux mots présents dans moins d'une feature sur dix
KEYWORD_EVIDENCE = 5.0
MAX_EXTRACT_LINES = 150
_GHERKIN_STEP = re.compile(r"^\s*(?:Given|When|Then|And|But|\*|Soit|Sachant que|[EÉ]tant donn[ée]e?s?|Quand|Lorsque|"
                           r"Alors|Et|Mais)\s+(.+?)\s*$")
_GHERKIN_BLOCK = re.compile(r"^\s*(Background|Contexte|Scenario Outline|Scenario Template|Scenario|Example|"
                            r"Plan du [Ss]cénario|Scénario|Exemple)\s*:", re.I)
# Suffixes et préfixes techniques retirés du nom d'une classe overridée pour retrouver son objet métier
_CLASS_NOISE = re.compile(r"^(Spec|Abstract|Default)|(ServiceImpl|Service|Impl|UiRest|Rest|Action|Manager|Repository|"
                          r"Helper|Controller|Validator|Builder|Mapper|V\d+(_\d+)*)+$")


def _norm(text: str) -> str:
    """Texte réduit à ses lettres et chiffres, en minuscules (recherche d'un nom collé : handlingunitdeclarative)."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _expression_regex(expr: str):
    """Regex d'une expression de step (Cucumber expression ou regex ^...$), ou None si elle ne compile pas."""
    if expr.startswith("^") or expr.endswith("$"):
        pattern = expr.replace("\\\\", "\\")
    else:
        pattern = re.escape(expr.replace("\\\\", "\\"))
        pattern = re.sub(r"\\\{(string)\\\}", r"(?:\"[^\"]*\"|'[^']*')", pattern)
        pattern = re.sub(r"\\\{(int|long|short|byte|biginteger)\\\}", r"-?\\d+", pattern)
        pattern = re.sub(r"\\\{(float|double|bigdecimal)\\\}", r"-?[\\d.,]+", pattern)
        pattern = re.sub(r"\\\{word\\\}", r"\\S+", pattern)
        pattern = re.sub(r"\\\{[^}]*\\\}", r".*", pattern)
        pattern = re.sub(r"\\\(([^()]*)\\\)", r"(?:\1)?", pattern)  # texte optionnel : step(s)
        pattern = re.sub(r"(\w+)/(\w+)", r"(?:\1|\2)", pattern)    # alternative : a/b
    try:
        return re.compile(f"^{pattern.strip('^$')}$")
    except re.error:
        return None


def _step_class_vocabulary(cucumber_std_dir: str, step_files: set) -> dict:
    """
    Vocabulaire anglais de chaque classe Step Definitions : son nom, les classes cucumber.* qu'elle importe
    (endpoints, POJO) et les chemins REST de ses endpoints. Fait le pont avec les noms Java des spécifiques.
    """
    tree = ls_tree(cucumber_std_dir, "HEAD")
    texts = blob_texts(cucumber_std_dir, [tree[f] for f in step_files if f in tree])
    step_texts = {f: texts.get(tree.get(f), "") for f in step_files}
    imports = {f: re.findall(r"^import\s+(cucumber\.[\w.]+)\s*;", t, re.M) for f, t in step_texts.items()}
    endpoint_paths = {i: "src/test/java/" + i.replace(".", "/") + ".java" for v in imports.values() for i in v
                      if ".endPoints." in i or ".endpoints." in i}
    endpoint_texts = blob_texts(cucumber_std_dir, [tree[p] for p in endpoint_paths.values() if p in tree])
    vocabulary = {}
    for f in step_files:
        package = f.rsplit("/", 1)[0].split("src/test/java/", 1)[-1].replace("/", ".")
        parts = [f.rsplit("/", 1)[-1][:-5]] + [i.rsplit(".", 1)[-1] for i in imports.get(f, [])]
        for i in imports.get(f, []):
            text = endpoint_texts.get(tree.get(endpoint_paths.get(i, "")), "")
            parts += [s for s in re.findall(r'"([A-Za-z][\w./-]*/[\w./{}-]*)"', text)]
        vocabulary[f] = {"package": package, "domain": package.rsplit(".", 1)[-1], "text": " ".join(parts)}
    return vocabulary


def std_test_index(cucumber_std_dir: str, odatio_version: str) -> dict:
    """
    Index des tests du Cucumber Standard pour le rapprochement (en cache par version Odatio et HEAD, comme le
    catalogue) : par feature, ses mots (texte, données, vocabulaire des Step Definitions utilisées), ses domaines
    dominants et ses Step Definitions ; domaines connus (packages des Step Definitions).
    """
    version = re.sub(r"[^\w.-]", "_", odatio_version or "inconnue")
    head = rev_parse(cucumber_std_dir, "HEAD")
    cache_file = os.path.join(STD_CACHE_DIR, f"tests_std_{version}.json")
    cached = _read_json(cache_file, {}) or {}
    if cached.get("head") == head and cached.get("format") == STD_TESTS_FORMAT:
        return cached
    catalogue = extract_cucumber(cucumber_std_dir, "HEAD")
    by_prefix = {}
    for step in catalogue["steps"]:
        regex = _expression_regex(step["expr"])
        if regex:
            by_prefix.setdefault(_step_prefix(step["expr"]), []).append((regex, step["file"]))
    tree = ls_tree(cucumber_std_dir, "HEAD")
    texts = blob_texts(cucumber_std_dir, [tree[f["path"]] for f in catalogue["features"] if f["path"] in tree])
    usage = {}
    for feature in catalogue["features"]:
        counts = {}
        for line in texts.get(tree.get(feature["path"]), "").splitlines():
            match = _GHERKIN_STEP.match(line)
            if not match:
                continue
            step = match.group(1)
            for regex, file in by_prefix.get(_step_prefix(step), []) + by_prefix.get("(sans préfixe)", []):
                if regex.match(step):
                    counts[file] = counts.get(file, 0) + 1
                    break
        usage[feature["path"]] = counts
    vocabulary = _step_class_vocabulary(cucumber_std_dir, {f for c in usage.values() for f in c})
    root = _most_common(v["package"].rsplit(".", 1)[0] for v in vocabulary.values())
    features = []
    for feature in catalogue["features"]:
        text = texts.get(tree.get(feature["path"]), "")
        counts = usage[feature["path"]]
        steps_text = " ".join(vocabulary[f]["text"] for f in counts)
        # Domaines dominants : packages de Step Definitions (hors racine commune) d'au moins 25 % des steps
        domains = {}
        for file, n in counts.items():
            if vocabulary[file]["package"] != root:
                domains[vocabulary[file]["domain"]] = domains.get(vocabulary[file]["domain"], 0) + n
        total = sum(domains.values())
        features.append({
            "path": feature["path"], "feature": feature["feature"], "scenarios": feature["scenarios"],
            "tags": feature["tags"], "lines": text.count("\n") + 1,
            "words": sorted(set(_words(f"{feature['path']} {text} {steps_text}"))),
            "norm": _norm(f"{feature['path']} {text} {steps_text}"),
            "domains": sorted(d for d, n in domains.items() if total and n / total >= 0.25),
            "step_definitions": [f for f, _ in sorted(counts.items(), key=lambda kv: -kv[1])],
        })
    index = {"head": head, "format": STD_TESTS_FORMAT, "version": odatio_version, "features": features,
             "domains": sorted({v["domain"] for v in vocabulary.values() if v["package"] != root})}
    _write_json(cache_file, index)
    return index


def _spec_name(name: str, client_name: str) -> str:
    """Nom collé sans le préfixe spec ni le nom du client (specdeclarativecubing -> declarativecubing)."""
    name = _norm(name)
    for noise in (_norm(client_name), "spec"):
        name = name.replace(noise, "") if noise else name
    return name


def _override_targets(spec: dict, client_name: str = "") -> list:
    """
    Noms collés de ce que le spécifique teste ou remplace : objet métier ou processus (dernier segment de son
    dossier), processus Studio overridé, classes Java du Standard overridées.
    """
    parts = spec["dossier"].rsplit("/", 1)[-1].split("-")
    targets = [_spec_name(parts[-1], client_name)]
    if spec["nature"] == "processus":
        targets.append(_spec_name("".join(parts[-2:]), client_name))
    for f in spec["fichiers"]:
        if f.get("kind") == "override" and ":" in f.get("standard", "") and f["path"].endswith(".java"):
            name = f["standard"].rsplit("/", 1)[-1][:-5]
            targets.append(_norm(_CLASS_NOISE.sub("", _CLASS_NOISE.sub("", name))))
    return [t for t in dict.fromkeys(targets) if len(t) >= 6 and t not in _TECHNICAL_WORDS]


# Mots techniques des noms Java / Studio, sans valeur pour rapprocher un test
_TECHNICAL_WORDS = {"datautil", "datamodel", "transition", "transitions", "action", "load", "executor", "initializer",
                    "listener", "event", "variable", "input", "output", "info", "request", "save", "manage", "check",
                    "create", "from", "send", "handler", "interactive", "found", "after", "before", "with", "without",
                    "into", "when", "then", "process"}

# Synonymes FR / EN (sans accents) : les noms Java et Studio du client sont en anglais, les features du Standard
# souvent en français. Extensible par CUCUMBER_SYNONYMS (« a|b|c;d|e »)
DEFAULT_SYNONYMS = ("interactif|interactive|interaction;processus|process;ecran|screen;emplacement|slot|location;"
                    "quantite|quantity|qty;document|edition|report;repack|reconditionnement|reconditionner;"
                    "contenu|content;reception|goodsin;expedition|shipping|dispatch;transporteur|carrier;"
                    "fournisseur|supplier;produit|product;inventaire|stocktaking|inventory;etiquette|label;"
                    "colis|parcel;statut|status;commande|order")


def _unaccent(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def synonym_groups() -> list:
    """Groupes de synonymes (ensembles de mots sans accents, en minuscules) : défaut puis CUCUMBER_SYNONYMS."""
    groups = []
    for text in (DEFAULT_SYNONYMS, os.getenv("CUCUMBER_SYNONYMS", "")):
        for group in text.split(";"):
            words = {_unaccent(w.strip().lower()) for w in group.split("|") if w.strip()}
            if len(words) >= 2:
                groups.append(words)
    return groups


def synonyms(word: str, groups: list = None) -> set:
    """Le mot et ses synonymes (union des groupes qui le contiennent)."""
    word = _unaccent(word.lower())
    found = {word}
    for group in synonym_groups() if groups is None else groups:
        if word in group:
            found |= group
    return found


def _segment(word: str, vocabulary: set) -> list:
    """Découpe un nom collé en mots connus du Standard (handlingunitdeclarative -> handling unit declarative)."""
    best = {0: []}
    for end in range(1, len(word) + 1):
        for start in range(max(0, end - 20), end - 3):
            if start in best and word[start:end] in vocabulary and (end not in best or
                                                                    len(best[start]) + 1 < len(best[end])):
                best[end] = best[start] + [word[start:end]]
    return best.get(len(word), [])


def related_std_tests(spec: dict, catalogue: dict, java_std: list = None, min_score: float = None,
                      client_name: str = "") -> list:
    """
    Features du Standard les plus proches d'un spécifique (au plus 3, score >= CUCUMBER_STD_MATCH_MIN), `catalogue`
    étant l'index std_test_index. Score sur 1 : 0,6 x évidence des mots-clés trouvés dans la feature et ses Step
    Definitions (somme de leurs idf / KEYWORD_EVIDENCE, plafonnée à 1), + jusqu'à 0,3 si l'objet métier ou l'élément
    overridé y est nommé (selon sa rareté), + 0,1 si même domaine. `java_std` : domaines (défaut catalogue["domains"]).
    """
    if min_score is None:
        try:
            min_score = float(os.getenv("CUCUMBER_STD_MATCH_MIN", "").strip() or DEFAULT_STD_MATCH_MIN)
        except ValueError:
            min_score = DEFAULT_STD_MATCH_MIN
    features = catalogue["features"]
    groups = synonym_groups()
    for feature in features:
        # Mots avec et sans accents : « ecran » (synonyme) retrouve « écran »
        feature.setdefault("words_set", set(feature["words"]) | {_unaccent(w) for w in feature["words"]})
    keywords = [k for k in dict.fromkeys(_unaccent(_spec_name(k, client_name) if len(k) >= 8 else k)
                                         for k in spec["mots_cles"]) if len(k) >= 4 and k not in _TECHNICAL_WORDS]

    if "vocabulary" not in catalogue:
        # Les synonymes découpent aussi les noms collés : repackcontent -> repack content
        catalogue["vocabulary"] = {w for f in features for w in f["words_set"]} | {w for g in groups for w in g}
    targets = _override_targets(spec, client_name)
    expanded = []
    for k in keywords:
        # Nom collé absent du Standard : remplacé par ses mots connus
        parts = _segment(k, catalogue["vocabulary"]) if len(k) >= 8 and k not in catalogue["vocabulary"] else []
        expanded += [p for p in parts if p not in _TECHNICAL_WORDS] if len(parts) >= 2 else [k]
    keywords = list(dict.fromkeys(expanded))

    def compound(k):
        # Nom collé fait d'autres mots-clés (activityreport = activity + report) : déjà compté par ses parties
        rest = k
        for other in sorted((o for o in keywords if o != k and o in k), key=len, reverse=True):
            rest = rest.replace(other, "")
        return len(k) >= 8 and len(rest) < 3
    # Cibles (objet métier, élément overridé) comptées par leur propre bonus
    keywords = [k for k in keywords if not compound(k) and k not in targets]
    # Un mot-clé et ses synonymes forment une seule notion, comptée une fois (le premier mot-clé la représente)
    forms, concepts = {}, set()
    for k in keywords:
        group = synonyms(k, groups)
        if not concepts & group:
            forms[k] = group
            concepts |= group
    keywords = list(forms)
    # Mot présent : mot de la feature, ou nom collé (8 caractères et plus) dans son texte réduit. Chaque synonyme a
    # sa propre rareté : un mot-clé vaut, dans une feature, l'idf du plus rare de ses synonymes qui y figure
    # (« slot » rare garde son poids, « emplacement » présent partout ne le dilue pas)
    form_hits = {w: {i for i, f in enumerate(features) if w in f["words_set"] or (len(w) >= 8 and w in f["norm"])}
                 for w in {w for k in keywords for w in forms[k]}}
    form_weights = {w: math.log((1 + len(features)) / len(found)) for w, found in form_hits.items() if found}
    hits = {k: set().union(*(form_hits[w] for w in forms[k])) for k in keywords}
    weights = {k: True for k, found in hits.items() if found}

    def weight(k, i):
        return max(form_weights[w] for w in forms[k] if w in form_weights and i in form_hits[w])
    # Bonus de cible pondéré par sa rareté : preparationorder, présent partout, ne désigne aucun test en particulier
    target_hits = {t: [i for i, f in enumerate(features) if t in f["norm"]] for t in targets}
    target_bonus = {t: 0.3 * min(1.0, math.log((1 + len(features)) / len(found)) / 3) for t, found in
                    target_hits.items() if found}
    spec_text = _norm(" ".join([spec["depot"], spec["dossier"]] + [f["path"] for f in spec["fichiers"][:10]]))
    domains = [d for d in (java_std or catalogue["domains"]) if len(d) >= 5 and _norm(d) in spec_text]
    scored = []
    for i, feature in enumerate(features):
        found = [k for k in keywords if k in weights and i in hits[k]]
        target = max((t for t in target_bonus if i in target_hits[t]), key=target_bonus.get, default="")
        domain = next((d for d in domains if d in feature["domains"]), "")
        # Évidence absolue : des mots génériques (preparation, unit, status...) ne suffisent jamais seuls
        evidence = min(1.0, sum(weight(k, i) for k in found) / KEYWORD_EVIDENCE)
        score = 0.6 * evidence + (target_bonus[target] if target else 0) + (0.1 if domain else 0)
        if score >= min_score and (found or target):
            reasons = ([f"mots-clés {', '.join(found)}"] if found else []) + (
                [f"cible {target}"] if target else []) + ([f"domaine {domain}"] if domain else [])
            # Départage : Step Definitions principales de la feature portant la cible ou un mot-clé (le cas testé
            # est le sujet de la feature, pas une étape de mise en place)
            terms = [t for t in [target] + found if len(t) >= 6]
            focus = sum(1 for f in feature["step_definitions"][:3] if any(t in _norm(f.rsplit("/", 1)[-1])
                                                                          for t in terms))
            scored.append((round(score, 2), focus, len(found), -feature["lines"], feature, " ; ".join(reasons),
                           [w for k in found for w in sorted(forms[k])] + [target]))
    scored.sort(key=lambda s: s[:4], reverse=True)
    # Une feature par dossier d'abord (variantes TOW-551-2/-3/-4 d'un même cas), complétée si besoin
    folders, kept = set(), []
    for entry in scored:
        folder = entry[4]["path"].rsplit("/", 1)[0]
        if folder not in folders and len(kept) < MAX_STD_TESTS:
            folders.add(folder)
            kept.append(entry)
    kept += [e for e in scored if e not in kept][:MAX_STD_TESTS - len(kept)]
    kept.sort(key=lambda s: s[:4], reverse=True)
    return [{"path": f["path"], "feature": f["feature"], "scenarios": f["scenarios"], "score": score,
             "raison": reason, "step_definitions": f["step_definitions"][:6], "_match": [m for m in match if m]}
            for score, _, _, _, f, reason, match in kept]


def _gherkin_extract(text: str, matches: list, max_lines: int = MAX_EXTRACT_LINES) -> str:
    """
    Feature bornée : entière si courte ; sinon en-tête, Background et scénarios dont le nom (à défaut le corps)
    contient un mot-clé, dans la limite de `max_lines` lignes.
    """
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    starts = [i for i, line in enumerate(lines) if _GHERKIN_BLOCK.match(line)]
    if not starts:
        return "\n".join(lines[:max_lines]) + "\n# … feature tronquée\n"
    # Un bloc commence à ses tags et commentaires immédiatement au-dessus
    bounds = []
    for n, start in enumerate(starts):
        begin = start
        while begin > (starts[n - 1] + 1 if n else 0) and lines[begin - 1].strip().startswith(("@", "#")):
            begin -= 1
        bounds.append(begin)
    blocks = [(lines[bounds[n]:bounds[n + 1] if n + 1 < len(bounds) else len(lines)]) for n in range(len(bounds))]
    header, kept = lines[:bounds[0]], []
    terms = [_norm(m) for m in matches if m]

    def hit(block, whole):
        target = _norm(" ".join(block) if whole else block[next(i for i, l in enumerate(block)
                                                                  if _GHERKIN_BLOCK.match(l))])
        return any(t in target for t in terms)
    background = [b for b in blocks if re.match(r"^\s*(Background|Contexte)\s*:", "\n".join(
        l for l in b if _GHERKIN_BLOCK.match(l)), re.I)]
    scenarios = [b for b in blocks if b not in background]
    selected = [b for b in scenarios if hit(b, False)] or [b for b in scenarios if hit(b, True)] or scenarios[:1]
    budget = max_lines - len(header) - sum(len(b) for b in background)
    for block in selected:
        if len(block) <= budget or not kept:
            kept.append(block[:max(budget, 20)])
            budget -= len(block)
    omitted = len(scenarios) - len(kept)
    # Borne stricte, ligne d'avertissement comprise (Background très long, premier scénario tronqué)
    out = (header + [l for b in background for l in b] + [l for b in kept for l in b])[:max_lines - 1]
    out.append(f"# … extrait borné à {max_lines} lignes" + (f" : {omitted} autre(s) scénario(s) omis" if omitted
                                                           else ""))
    return "\n".join(out) + "\n"


def attach_std_tests(inventory: dict, cucumber_std_dir: str, odatio_version: str) -> None:
    """spec["tests_std"] de chaque spécifique, et extraits bornés des features retenues dans <client>/std/."""
    index = std_test_index(cucumber_std_dir, odatio_version)
    directory = os.path.join(cache_dir(inventory["client"]), "std")
    shutil.rmtree(directory, ignore_errors=True)
    tree = ls_tree(cucumber_std_dir, "HEAD")
    for spec in inventory["specifiques"]:
        tests = related_std_tests(spec, index, client_name=inventory["client"])
        texts = blob_texts(cucumber_std_dir, [tree[t["path"]] for t in tests if t["path"] in tree])
        for test in tests:
            relative = test["path"].split("features/", 1)[-1]
            # Un extrait par spécifique : le choix des scénarios dépend de ses mots-clés
            target = os.path.join(directory, spec["id"], *relative.split("/"))
            _write_text(target, _gherkin_extract(texts.get(tree.get(test["path"]), ""), test.pop("_match")))
            test["extrait"] = target
        spec["tests_std"] = tests


# ---------------------------------------------------------------------------
# Recettes de test (prompts/cucumber_recettes.md) : cas connus et steps Standard à utiliser
# ---------------------------------------------------------------------------

RECIPES_FILE = "cucumber_recettes.md"
_RECIPE_FIELDS = {"natures": "natures", "mots": "mots", "chemins": "chemins", "contenu": "contenu",
                  "steps standard": "steps", "exemple": "exemple", "statut": "statut", "consigne": "consigne"}
MAX_RECIPE_CONTENT = 300_000


def recipes_path() -> str:
    """cucumber_recettes.md de PROMPTS_DIR s'il existe, sinon celui du projet ('' si aucun)."""
    custom_dir = os.getenv("PROMPTS_DIR", "").strip()
    for directory in ([custom_dir] if custom_dir else []) + [os.path.join(WORKSPACE_DIR, "prompts")]:
        candidate = os.path.join(directory, RECIPES_FILE)
        if os.path.exists(candidate):
            return candidate
    return ""


def load_recipes(path: str = None) -> list:
    """
    Recettes : une section « ## <titre> » par cas, avec des lignes « - Champ : valeur » (Natures, Mots, Chemins,
    Contenu, Steps Standard, Exemple, Statut) et une « Consigne : » qui peut continuer sur les lignes suivantes.
    """
    path = recipes_path() if path is None else path
    text = _read_text_file(path) if path else ""
    recipes, current, field = [], None, ""
    for line in text.splitlines():
        if line.startswith("## "):
            current, field = {"titre": line[3:].strip()}, ""
            recipes.append(current)
            continue
        if current is None:
            continue
        match = re.match(r"^\s*(?:-\s*)?([^:`]{3,20}?)\s*:\s*(.*)$", line)
        key = _RECIPE_FIELDS.get(_unaccent(match.group(1).strip().lower())) if match else None
        if key:
            field, current[key] = key, match.group(2).strip()
        elif field == "consigne" and line.strip():
            current["consigne"] = f"{current['consigne']} {line.strip()}".strip()
    for recipe in recipes:
        for key in ("natures", "mots"):
            recipe[key] = [_unaccent(v.strip().lower()) for v in re.split(r"[,|]", recipe.get(key, "")) if v.strip()]
        for key in ("chemins", "contenu"):
            recipe[key] = recipe.get(key, "").strip().strip("`")
    return [r for r in recipes if r.get("consigne") or r.get("steps")]


def _spec_tokens(spec: dict) -> tuple:
    """Mots (sans accents, noms camelCase découpés) et texte réduit du spécifique : libellé, dossier, fichiers."""
    text = " ".join([spec.get("libelle", ""), spec.get("dossier", "")] + list(spec.get("mots_cles", []))
                    + [f["path"] for f in spec.get("fichiers", [])])
    text = _unaccent(re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)).lower()
    return {w for w in re.split(r"[^a-z0-9]+", text) if len(w) >= 3}, _norm(text)


def matching_recipes(spec: dict, recipes: list, groups: list = None) -> list:
    """
    Recettes applicables au spécifique : sa nature fait partie de « Natures » (si renseigné) ET un de ses mots
    (synonymes compris, même logique que related_std_tests), un de ses chemins ou le contenu d'un de ses fichiers
    correspond (si au moins un de ces critères est renseigné).
    """
    groups = synonym_groups() if groups is None else groups
    words, norm = _spec_tokens(spec)
    contents = None
    found = []
    for recipe in recipes:
        if recipe["natures"] and _unaccent(spec.get("nature", "").lower()) not in recipe["natures"]:
            continue
        criteria = recipe["mots"] or recipe["chemins"] or recipe["contenu"]
        hit = not criteria
        forms = {w for m in recipe["mots"] for w in synonyms(m, groups)}
        hit = hit or any(w in words or (len(w) >= 8 and w in norm) for w in forms)
        if not hit and recipe["chemins"]:
            hit = any(re.search(recipe["chemins"], p, re.I) for p in [spec.get("dossier", "")]
                      + [f["path"] for f in spec.get("fichiers", [])])
        if not hit and recipe["contenu"]:
            if contents is None:
                contents = [_read_text_file(f["local"])[:MAX_RECIPE_CONTENT] for f in spec.get("fichiers", [])
                            if f.get("local")]
            hit = any(re.search(recipe["contenu"], c) for c in contents)
        if hit:
            found.append(recipe)
    return found


def export_recipe_examples(recipes: list, cucumber_std_dir: str) -> None:
    """recipe["exemple_local"] : la feature Standard citée par « Exemple » (nom ou fin de chemin), copiée bornée."""
    wanted = {r["exemple"].strip().strip("`'\"") for r in recipes if r.get("exemple", "").strip()}
    if not wanted or not cucumber_std_dir:
        return
    tree = ls_tree(cucumber_std_dir, "HEAD")
    paths = {w: next((p for p in tree if p.endswith(".feature") and (p == w or p.endswith("/" + w))), "")
             for w in wanted}
    texts = blob_texts(cucumber_std_dir, [tree[p] for p in paths.values() if p])
    directory = os.path.join(STD_CACHE_DIR, "recettes")
    for recipe in recipes:
        path = paths.get(recipe.get("exemple", "").strip().strip("`'\""), "")
        if not path:
            continue
        target = os.path.join(directory, path.rsplit("/", 1)[-1])
        steps = re.findall(r"`([^`]+)`", recipe.get("steps", ""))
        _write_text(target, _gherkin_extract(texts.get(tree[path], ""), [s.split("{")[0] for s in steps]))
        recipe["exemple_local"], recipe["exemple_std"] = target, path


def attach_recipes(inventory: dict, cucumber_std_dir: str) -> int:
    """spec["recettes"] de chaque spécifique (recettes applicables, exemple exporté). Renvoie le nombre concerné."""
    recipes = load_recipes()
    export_recipe_examples(recipes, cucumber_std_dir)
    groups = synonym_groups()
    for spec in inventory.get("specifiques", []):
        spec["recettes"] = [{k: r.get(k, "") for k in ("titre", "steps", "exemple_local", "exemple_std", "statut",
                                                        "consigne")} for r in matching_recipes(spec, recipes, groups)]
    return sum(1 for s in inventory.get("specifiques", []) if s["recettes"])


# ---------------------------------------------------------------------------
# Steps candidats : les steps Standard les plus proches de chaque spécifique, donnés dans le prompt du lot
# ---------------------------------------------------------------------------

MAX_CANDIDATE_STEPS = 15
_STEP_LINE = re.compile(r"^- (.+) — (\S+?):(\d+)\s*$")


def std_steps(steps_md: str, std_endpoints: dict = None) -> list:
    """
    Steps du Standard lus dans steps_std.md (une ligne « - expression — fichier:ligne »), avec leur classe, les
    endpoints qu'ils appellent (catalogue standard_endpoints), leurs mots et leur regex.
    """
    called = {}
    for e in (std_endpoints or {}).get("endpoints", []):
        for expr in e["steps"]:
            called.setdefault(expr, []).append(f"{e['methode']} {e['chemin']}")
    steps = []
    for line in _read_text_file(steps_md).splitlines():
        match = _STEP_LINE.match(line)
        if not match:
            continue
        expr, file = match.group(1), match.group(2)
        cls = file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        endpoints = list(dict.fromkeys(called.get(expr, [])))
        text = f"{expr} {cls} {' '.join(endpoints)}"
        words = set(_words(text))
        steps.append({"expr": expr, "classe": cls, "fichier": file, "endpoints": endpoints,
                      "words": words | {_unaccent(w) for w in words}, "norm": _norm(_unaccent(text)),
                      "prefix": _step_prefix(expr), "regex": _expression_regex(expr)})
    return steps


def _used_steps(spec: dict, steps: list) -> dict:
    """Steps Standard utilisés par les extraits des tests équivalents du spécifique : {expression: poids}."""
    by_prefix = {}
    for step in steps:
        if step["regex"]:
            by_prefix.setdefault(step["prefix"], []).append(step)
    used = {}
    for test in spec.get("tests_std") or []:
        for line in _read_text_file(test.get("extrait", "")).splitlines():
            match = _GHERKIN_STEP.match(line)
            if not match:
                continue
            text = match.group(1)
            for step in by_prefix.get(_step_prefix(text), []) + by_prefix.get("(sans préfixe)", []):
                if step["regex"].match(text):
                    used[step["expr"]] = max(used.get(step["expr"], 0), test.get("score") or 0.5)
                    break
    return used


def candidate_steps(spec: dict, steps: list, groups: list = None, client_name: str = "",
                    limit: int = MAX_CANDIDATE_STEPS) -> list:
    """
    Les `limit` steps Standard les plus proches du spécifique : steps des recettes applicables (+3), steps
    utilisés par ses tests Standard équivalents (+2 x score du test), mots-clés et synonymes présents dans
    l'expression, la classe ou l'endpoint (idf), cible (objet métier, élément overridé) nommée (idf), classe
    Step Definitions d'un test équivalent (+0,5). [{"expr", "classe", "endpoints"}].
    """
    if not steps:
        return []
    groups = synonym_groups() if groups is None else groups
    vocabulary = {w for s in steps for w in s["words"]} | {w for g in groups for w in g}
    keywords = []
    for k in spec.get("mots_cles", []):
        k = _unaccent(_spec_name(k, client_name) if len(k) >= 8 else k)
        if len(k) < 4 or k in _TECHNICAL_WORDS:
            continue
        parts = _segment(k, vocabulary) if len(k) >= 8 and k not in vocabulary else []
        keywords += [p for p in parts if p not in _TECHNICAL_WORDS] if len(parts) >= 2 else [k]
    forms = {k: synonyms(k, groups) for k in dict.fromkeys(keywords)}
    targets = _override_targets(spec, client_name)

    def idf(found: int) -> float:
        return math.log((1 + len(steps)) / found) if found else 0.0
    form_idf = {w: idf(sum(1 for s in steps if w in s["words"] or (len(w) >= 8 and w in s["norm"])))
                for w in {w for f in forms.values() for w in f}}
    target_idf = {t: idf(sum(1 for s in steps if t in s["norm"])) for t in targets}
    recipe_steps = {" ".join(s.split()) for r in spec.get("recettes") or []
                    for s in re.findall(r"`([^`]+)`", r.get("steps", ""))}
    used = _used_steps(spec, steps)
    test_classes = {f.rsplit("/", 1)[-1].rsplit(".", 1)[0] for t in spec.get("tests_std") or []
                    for f in t.get("step_definitions", [])}
    scored = []
    for n, step in enumerate(steps):
        score = sum(max((form_idf[w] for w in group if w in step["words"] or (len(w) >= 8 and w in step["norm"])),
                        default=0.0) for group in forms.values())
        score += sum(v for t, v in target_idf.items() if t in step["norm"])
        score += 3.0 if " ".join(step["expr"].split()) in recipe_steps else 0.0
        score += 2.0 * used.get(step["expr"], 0)
        if score > 0 and step["classe"] in test_classes:
            score += 0.5
        if score > 0:
            scored.append((-score, n, step))
    scored.sort(key=lambda s: s[:2])
    return [{"expr": s["expr"], "classe": s["classe"], "fichier": s["fichier"], "endpoints": s["endpoints"][:2]}
            for _, _, s in scored[:limit]]


def attach_candidate_steps(inventory: dict, steps_md: str, std_endpoints: dict) -> int:
    """spec["steps_candidats"] de chaque spécifique (0 token). Renvoie le nombre de spécifiques concernés."""
    steps = std_steps(steps_md, std_endpoints)
    groups = synonym_groups()
    for spec in inventory.get("specifiques", []):
        spec["steps_candidats"] = candidate_steps(spec, steps, groups, inventory.get("client", ""))
    return sum(1 for s in inventory.get("specifiques", []) if s["steps_candidats"])


def jira_extracts(inventory: dict, tickets: list, per_spec: int = 5, max_chars: int = 500) -> None:
    """Pour chaque spécifique, les tickets Jira qui citent ses mots-clés (jira_<id>.md), au plus `per_spec`."""
    directory = cache_dir(inventory["client"])
    lowered = [(t, f"{t.get('summary', '')}\n{t.get('description', '')}".lower()) for t in tickets]
    for name in os.listdir(directory):
        if name.startswith("jira_sp-") and name.endswith(".md"):
            os.remove(os.path.join(directory, name))
    # Pondération par rareté : un mot-clé présent dans plus de 15 % des tickets ne discrimine rien
    all_keywords = {k for spec in inventory["specifiques"] for k in spec["mots_cles"]}
    frequency = {k: sum(1 for _, text in lowered if k in text) for k in all_keywords}
    limit = max(3, 0.15 * len(lowered))
    weight = {k: math.log(len(lowered) / n) for k, n in frequency.items() if 0 < n <= limit}
    for spec in inventory["specifiques"]:
        keywords = [k for k in spec["mots_cles"] if k in weight]
        scored = sorted(((sum(text.count(k) * weight[k] for k in keywords), t, text) for t, text in lowered),
                        key=lambda x: -x[0])
        selected = [(score, t, text) for score, t, text in scored if score > 0][:per_spec]
        path = os.path.join(directory, f"jira_{spec['id']}.md")
        if not selected:
            spec["jira"] = ""
            if os.path.exists(path):
                os.remove(path)
            continue
        lines = [f"# Tickets Jira liés à {spec['libelle']}", ""]
        for _, t, text in selected:
            description = (t.get("description") or "").strip()
            first = min((i for i in (description.lower().find(k) for k in keywords) if i >= 0), default=0)
            start = max(0, first - max_chars // 4)
            excerpt = description[start:start + max_chars].replace("\n", " ")
            lines += [f"## {t['key']} — {t.get('summary', '')} [{t.get('status', '')}]",
                      ("… " if start else "") + excerpt + (" …" if start + max_chars < len(description) else ""), ""]
        _write_text(path, "\n".join(lines))
        spec["jira"] = path


# ---------------------------------------------------------------------------
# Catalogues Java (Standard et client) : signatures seulement, jamais les corps
# ---------------------------------------------------------------------------

_JAVA_NOISE = re.compile(r'//[^\n]*|/\*.*?\*/|"""(?:.|\n)*?"""|"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'', re.S)
_ANNOTATION = re.compile(r"@([\w.]+)\s*(?:\((?:[^()]|\([^()]*\))*\))?")
_MODIFIERS = {"public", "protected", "private", "static", "final", "abstract", "synchronized", "default", "native",
              "transient", "volatile", "strictfp", "sealed", "non-sealed"}
STEP_ANNOTATIONS = {"Given", "When", "Then", "And", "But", "Soit", "Etantdonné", "Quand", "Alors", "Et", "Mais"}
HOOK_ANNOTATIONS = {"Before", "After", "BeforeStep", "AfterStep", "BeforeAll", "AfterAll"}
RUNNER_ANNOTATIONS = {"CucumberOptions", "Suite", "CucumberContextConfiguration"}
INJECT_ANNOTATIONS = {"Autowired", "Inject", "Resource"}
# À incrémenter quand le format de java_std.md change : invalide le cache
JAVA_CATALOGUE_FORMAT = 3
POJO_METHOD = re.compile(r"^(get|set|is|with|has)[A-Z]|^(equals|hashCode|toString|builder|build|canEqual)$")


def _strip_java(text: str) -> str:
    """Code sans commentaires, littéraux réduits à "" (les accolades des chaînes ne faussent plus la profondeur)."""
    return _JAVA_NOISE.sub(lambda m: '""' if m.group(0)[0] in "\"'" else " ", text)


def _annotations(header: str) -> tuple:
    """(noms des annotations, en-tête sans annotations, espaces normalisés)."""
    names = [n.rsplit(".", 1)[-1] for n in _ANNOTATION.findall(header)]
    return names, " ".join(_ANNOTATION.sub(" ", header).split())


def _member(header: str, class_name: str):
    """Méthode ou constructeur d'un en-tête de membre ({...} ou ;) : dict, ou None (champ, initialiseur...)."""
    annotations, header = _annotations(header)
    match = re.match(r"^(.*?)\b(\w+)\s*\((.*)\)\s*(?:throws\b.*)?$", header, re.S)
    if not match or "=" in match.group(1) or match.group(2) in ("if", "for", "while", "switch", "catch", "return"):
        return None
    words = match.group(1).split()
    modifiers = [w for w in words if w in _MODIFIERS]
    rest = " ".join(w for w in words if w not in _MODIFIERS)
    rest = re.sub(r"^<.*?>\s*", "", rest)  # paramètres de type d'une méthode générique
    params = ", ".join(" ".join(p.replace("final ", "").split()) for p in re.split(r",(?![^<]*>)", match.group(3))
                       if p.strip())
    return {"name": match.group(2), "returns": rest, "params": params, "modifiers": modifiers,
            "annotations": annotations, "constructor": not rest and match.group(2) == class_name}


def java_outline(text: str) -> dict:
    """
    Plan d'une classe Java (type de premier niveau) : package, nom, nature, extends / implements, annotations,
    champs injectés, constructeurs et méthodes (signatures), constantes publiques. None si aucun type trouvé.
    """
    code = _strip_java(text)
    package = re.search(r"^\s*package\s+([\w.]+)\s*;", code, re.M)
    outline = {"package": package.group(1) if package else "", "imports_cucumber": "io.cucumber" in code,
               "members": [], "injected": [], "constants": []}
    depth, start, in_type, enum_head, parens = 0, 0, False, False, 0
    for i, char in enumerate(code):
        if char in "()":
            parens += 1 if char == "(" else -1
            continue
        # Accolades et ; entre parenthèses : tableaux d'annotation, lambdas et classes anonymes en argument
        if char not in "{};" or parens > 0:
            continue
        header = code[start:i]
        start = i + 1
        if enum_head and depth == 1:
            # Premier segment d'une enum : ses constantes (arguments retirés)
            enum_head = False
            body = header
            while re.search(r"\([^()]*\)", body):
                body = re.sub(r"\([^()]*\)", "", body)
            names = [n.strip() for n in body.split(",") if re.fullmatch(r"\s*[A-Z][A-Z0-9_]*\s*", n)]
            if names:
                outline["constants"] = names
                if char != ";":
                    depth += 1 if char == "{" else -1
                    if depth == 0:
                        break
                continue
        if char == "}":
            depth -= 1
            if depth == 0 and in_type:
                break
            continue
        if depth == 0 and char == "{" and not in_type:
            annotations, clean = _annotations(header.split(";")[-1])
            match = re.search(r"\b(class|interface|enum|record)\s+(\w+)(.*)$", clean)
            if match:
                tail = match.group(3)
                extends = re.search(r"\bextends\s+(.+?)(?=\bimplements\b|$)", tail)
                implements = re.search(r"\bimplements\s+(.+)$", tail)
                outline.update(kind=match.group(1), name=match.group(2), annotations=annotations,
                               extends=" ".join(extends.group(1).split()) if extends else "",
                               implements=" ".join(implements.group(1).split()) if implements else "",
                               raw_annotations=" ".join(header.split()))
                in_type, enum_head = True, match.group(1) == "enum"
        elif depth == 1 and in_type:
            member = _member(header, outline["name"])
            if member:
                outline["members"].append(member)
            elif char == ";":
                annotations, clean = _annotations(header)
                words = clean.split("=")[0].split()
                if set(annotations) & INJECT_ANNOTATIONS and len(words) >= 2:
                    outline["injected"].append(" ".join(w for w in words if w not in _MODIFIERS))
                elif {"public", "static", "final"} <= set(words) and len(words) >= 2:
                    outline["constants"].append(words[-1])
        if char == "{":
            depth += 1
    return outline if in_type else None


def _visible(member: dict, kind: str) -> bool:
    modifiers = member["modifiers"]
    return ("public" in modifiers or "protected" in modifiers
            or (kind == "interface" and "private" not in modifiers))


def java_category(outline: dict, path: str) -> str:
    """steps (@Given/@When/@Then), config (runner, contexte Cucumber, hooks), pojo (données) ou support."""
    annotations = set(outline.get("annotations", []))
    member_annotations = {a for m in outline["members"] for a in m["annotations"]}
    if annotations & RUNNER_ANNOTATIONS or "Cucumber.class" in outline.get("raw_annotations", ""):
        return "config"
    if member_annotations & STEP_ANNOTATIONS:
        return "steps"
    if member_annotations & HOOK_ANNOTATIONS and outline["imports_cucumber"]:
        return "config"
    methods = [m for m in outline["members"] if not m["constructor"] and _visible(m, outline["kind"])]
    if ".pojo" in f".{outline['package']}".lower() or "/pojo/" in path.lower() or (
            outline["kind"] in ("class", "record") and methods and all(POJO_METHOD.match(m["name"]) for m in methods)):
        return "pojo"
    return "support"


def java_classes(files: dict) -> list:
    """Plans des classes de src/test/java ({chemin: texte}), avec leur catégorie et leur nombre de lignes."""
    classes = []
    for path, text in sorted(files.items()):
        outline = java_outline(text)
        if outline:
            outline.update(path=path, lines=text.count("\n") + 1, category=java_category(outline, path))
            outline["fqcn"] = f"{outline['package']}.{outline['name']}".lstrip(".")
            classes.append(outline)
    return classes


def _signature(member: dict, class_name: str) -> str:
    flags = [m for m in member["modifiers"] if m in ("static", "protected", "abstract")]
    text = f"{class_name}{'' if member['constructor'] else '.' + member['name']}({member['params']})"
    if member["returns"] and member["returns"] != "void":
        text += f" : {member['returns']}"
    return text + (f" [{' '.join(flags)}]" if flags else "")


def _class_md(c: dict) -> list:
    head = f"### {c['fqcn']}"
    if c["extends"]:
        head += f" extends {c['extends']}"
    if c["implements"]:
        head += f" implements {c['implements']}"
    annotations = [a for a in c["annotations"] if a not in ("Override", "SuppressWarnings")]
    lines = [head + (f" @{' @'.join(annotations)}" if annotations else "") + f" — {c['path']}"]
    name = c["name"]
    lines += [f"- {name} injecte {field}" for field in c["injected"]]
    steps = [m for m in c["members"] if set(m["annotations"]) & STEP_ANNOTATIONS]
    for member in c["members"]:
        if member in steps or not _visible(member, c["kind"]):
            continue
        hook = [a for a in member["annotations"] if a in HOOK_ANNOTATIONS]
        lines.append(f"- {'@' + hook[0] + ' ' if hook else ''}{_signature(member, name)}")
    if steps:
        lines.append(f"- {name} : {len(steps)} step(s), expressions dans le catalogue des steps")
    if c["constants"]:
        # Constantes d'enum toutes listées (clés de contexte...), constantes static final limitées à 40
        limit = len(c["constants"]) if c["kind"] == "enum" else 40
        lines.append(f"- {name} constantes : {', '.join(c['constants'][:limit])}"
                     + (" …" if len(c["constants"]) > limit else ""))
    return lines


def java_catalogue_md(title: str, classes: list, intro: list = ()) -> str:
    """Catalogue Java : une ligne par membre, préfixée du nom de classe (une recherche Grep se suffit à elle-même)."""
    by_category = {}
    for c in classes:
        by_category.setdefault(c["category"], []).append(c)
    lines = [f"# {title}", "Signatures seulement (jamais les corps). Une ligne par membre : Classe.méthode(paramètres) "
             ": retour [static|protected|abstract]. À interroger par Grep (nom de classe, de méthode, mot-clé).", ""]
    lines += list(intro)
    labels = {"config": "Configuration (runners, contexte Cucumber, hooks)", "steps": "Step Definitions",
              "support": "Classes d'appui (endpoints, helpers, contextes, builders, constantes)"}
    for category in ("config", "steps", "support"):
        if by_category.get(category):
            lines += ["", f"## {labels[category]} : {len(by_category[category])}"]
            for c in by_category[category]:
                lines += _class_md(c)
    pojos = by_category.get("pojo", [])
    if pojos:
        lines += ["", f"## POJO (données) : {len(pojos)}, une ligne par package"]
        packages = {}
        for c in pojos:
            packages.setdefault(c["package"], []).append(c["name"] + (f"<{c['extends']}" if c["extends"] else ""))
        lines += [f"- {package} : {', '.join(names)}" for package, names in sorted(packages.items())]
    if not classes:
        lines.append("Aucune classe Java.")
    return "\n".join(lines) + "\n"


def _test_java_files(repo_dir: str, ref: str) -> dict:
    """{chemin: texte} des fichiers .java de src/test/java à `ref`, lus en un seul appel git."""
    tree = {p: b for p, b in ls_tree(repo_dir, ref).items() if p.endswith(".java") and "src/test/java/" in f"/{p}"}
    texts = blob_texts(repo_dir, tree.values())
    return {p: texts.get(b, "") for p, b in tree.items()}


def _runner_glue(c: dict, text: str, properties: str) -> list:
    """Packages de glue d'un runner : glue = {...}, GLUE_PROPERTY_NAME, cucumber.glue, sinon son propre package."""
    match = re.search(r"\bglue\s*=\s*\{?([^})]*)", text)
    glue = re.findall(r'"([\w.]+)"', match.group(1)) if match else []
    glue += re.findall(r'GLUE_PROPERTY_NAME\s*,\s*value\s*=\s*"([^"]+)"', text)
    glue += [g.strip() for g in re.findall(r"^\s*cucumber\.glue\s*=\s*(.+)$", properties, re.M)
             for g in g.split(",")]
    return [g.strip() for g in glue if g.strip()] or [c["package"]]


def _option_values(text: str, properties: str, option: str, constant: str, prop: str) -> list:
    """Valeurs d'une option d'un runner : @CucumberOptions(option = ...), @ConfigurationParameter, propriétés."""
    match = re.search(rf"\b{option}\s*=\s*(\{{[^}}]*\}}|\"[^\"]*\")", text)
    values = re.findall(r'"([^"]*)"', match.group(1)) if match else []
    values += re.findall(rf'{constant}\s*,\s*value\s*=\s*"([^"]+)"', text)
    values += [v for line in re.findall(rf"^\s*{re.escape(prop)}\s*=\s*(.+)$", properties, re.M)
               for v in line.split(",")]
    return [v.strip() for v in values if v.strip()]


def runner_entries(classes: list, files: dict, properties: str = "") -> list:
    """Runners Cucumber parmi `classes` : chemin, classe, glue, chemins de features, filtres de tags, plugins."""
    out = []
    for c in classes:
        if c["category"] != "config" or not (set(c["annotations"]) & {"CucumberOptions", "Suite"}
                                             or "Cucumber.class" in c["raw_annotations"]):
            continue
        text = files[c["path"]]
        out.append({"path": c["path"], "fqcn": c["fqcn"], "glue": _runner_glue(c, text, properties),
                    "features": _option_values(text, properties, "features", "FEATURES_PROPERTY_NAME",
                                               "cucumber.features"),
                    "tags": _option_values(text, properties, "tags", "FILTER_TAGS_PROPERTY_NAME",
                                           "cucumber.filter.tags"),
                    "plugins": _option_values(text, properties, "plugin", "PLUGIN_PROPERTY_NAME", "cucumber.plugin")})
    return out


def _runners(repo_dir: str, ref: str, classes: list, files: dict) -> list:
    tree = ls_tree(repo_dir, ref)
    props = [b for p, b in tree.items() if p.rsplit("/", 1)[-1] in ("junit-platform.properties", "cucumber.properties")]
    return runner_entries(classes, files, "\n".join(blob_texts(repo_dir, props).values()))


# Tag générique des scénarios désactivés (RÈGLE 3f), exclu par le runner du client
DEFAULT_A_REVOIR_TAG = "@A_REVOIR"


def a_revoir_tag() -> str:
    tag = os.getenv("CUCUMBER_TAG_A_REVOIR", "").strip() or DEFAULT_A_REVOIR_TAG
    return tag if tag.startswith("@") else "@" + tag


def runner_tag_expression(expressions) -> str:
    """Expression de tags effective d'un runner (filtres combinés par « and »), '' sans filtre."""
    expressions = [e for e in expressions or [] if e.strip()]
    return expressions[0] if len(expressions) == 1 else " and ".join(f"({e})" for e in expressions)


def filters_on_tag(expressions, tag: str) -> bool:
    """Le filtre exige `tag` : un scénario sans ce tag n'est pas sélectionné (alors qu'il l'est sans filtre)."""
    expression = runner_tag_expression(expressions)
    return bool(tag and expression) and not tags_match(expression, set()) and tags_match(expression, {tag})


def excludes_tag(expressions, tag: str) -> bool:
    """Le filtre exclut les scénarios portant `tag` et garde les scénarios sans tag."""
    expression = runner_tag_expression(expressions)
    return bool(expression) and tags_match(expression, set()) and not tags_match(expression, {tag})


def tags_match(expression: str, tags) -> bool:
    """Une expression de tags Cucumber (and, or, not, parenthèses) sélectionne-t-elle un scénario portant `tags` ?"""
    tokens = re.findall(r"\(|\)|@[^\s()]+|\band\b|\bor\b|\bnot\b", expression or "")
    position = 0

    def take():
        nonlocal position
        position += 1
        return tokens[position - 1] if position <= len(tokens) else ""

    def peek():
        return tokens[position] if position < len(tokens) else ""

    def factor():
        token = take()
        if token == "not":
            return not factor()
        if token == "(":
            value = disjunction()
            take()
            return value
        return token in tags

    def conjunction():
        value = factor()
        while peek() == "and":
            take()
            value = factor() and value
        return value

    def disjunction():
        value = conjunction()
        while peek() == "or":
            take()
            value = conjunction() or value
        return value
    return disjunction() if tokens else True


def _feature_dir(path: str) -> str:
    """Chemin de features d'un runner rapporté au dépôt (classpath: -> src/test/resources/), terminé par '/'."""
    path = path.strip().replace("\\", "/")
    path = "src/test/resources/" + path[len("classpath:"):].lstrip("/") if path.startswith("classpath:") else path
    return re.sub(r"^\./", "", path).rstrip("/") + "/"


def runner_gaps(runner: dict, package_steps: str, conventions: dict) -> list:
    """
    Écarts d'un runner pour les tests du client : glue sans le package des steps, chemin de features qui n'inclut
    pas la racine du client ou son init, filtre sur le tag client, scénarios désactivés (a_revoir_tag) non exclus,
    pas de rapport JSON.
    `conventions` : {"features_root", "client_tag", "init": [chemins], "init_tags": [tags]} ; glue non contrôlée
    sans `package_steps`.
    """
    gaps = []
    if package_steps and not any(package_steps == g or package_steps.startswith(g + ".") for g in runner["glue"]):
        gaps.append(f"ajoute la glue '{package_steps}' (glue actuelle : {', '.join(runner['glue'])})")
    root = conventions.get("features_root", "")
    dirs = [_feature_dir(p) for p in runner.get("features") or []]
    for needed in [root] + list(conventions.get("init") or []):
        if needed and dirs and not any(needed.startswith(d) for d in dirs):
            path = needed if needed.endswith("/") else needed.rsplit("/", 1)[0] + "/"
            # Init sous la racine déjà demandée : un seul chemin à ajouter
            dirs.append(path)
            gaps.append(f"ajoute le chemin de features '{path}' (actuel : {', '.join(runner['features'])}) pour "
                        f"inclure {needed}" + (" (initialisation du client)" if needed != root else ""))
    # Tags : jamais de filtre sur le tag client (des scénarios actifs ne le portent pas) ; exclusion des scénarios
    # désactivés dans le runner, le filtre passé au job pouvant ne pas atteindre Cucumber
    client_tag, disabled = conventions.get("client_tag", ""), a_revoir_tag()
    expressions = [e for e in runner.get("tags") or [] if e.strip()]
    kept = [e for e in expressions if tags_match(e, set())]
    for expression in (e for e in expressions if e not in kept):
        gaps.append((f"retire le filtre sur le tag client {client_tag} (« {expression} ») : des scénarios actifs "
                     f"(init, features plus anciennes) peuvent ne pas le porter"
                     if client_tag and filters_on_tag([expression], client_tag) else
                     f"retire le filtre « {expression} » : il écarte les scénarios du client qui ne portent pas ce "
                     f"tag"))
    if not excludes_tag(kept, disabled):
        wanted = f"({runner_tag_expression(kept)}) and not {disabled}" if kept else f"not {disabled}"
        gaps.append(f"exclus les scénarios désactivés dans le runner : tags = \"{wanted}\"")
    if not any(p.startswith("json:") for p in runner.get("plugins") or []):
        gaps.append("ajoute le plugin \"json:target/cucumber-reports/Cucumber.json\" (rapport JSON lu par la "
                    "vérification par exécution)")
    return gaps


def depends_on_std_test_jar(worktree: str) -> bool:
    """pom.xml du dépôt client avec une dépendance test-jar (le Cucumber Standard)."""
    return "<type>test-jar</type>" in _read_text_file(os.path.join(worktree, "pom.xml"))


def _parent_package(package: str) -> str:
    return package.rsplit(".", 1)[0] if "." in package else package


def _most_common(values) -> str:
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=lambda v: (counts[v], -len(v))) if counts else ""


def java_package_segment(client_name: str) -> str:
    segment = re.sub(r"[^a-z0-9_]", "", client_name.lower())
    return segment if segment and not segment[0].isdigit() else f"c{segment}"


def java_class_suffix(client_name: str) -> str:
    """Nom du client en PascalCase (suffixe des classes, ex. client-2 -> Client2)."""
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[^A-Za-z0-9]+", client_name) if p)


def standard_java_catalogue(cucumber_std_dir: str, odatio_version: str) -> dict:
    """
    Catalogue Java du Cucumber Standard (java_std.md), en cache par version Odatio et régénéré seulement si son
    HEAD change, avec une Step Definitions exemplaire et le runner copiés pour être lus une seule fois.
    """
    version = re.sub(r"[^\w.-]", "_", odatio_version or "inconnue")
    head = rev_parse(cucumber_std_dir, "HEAD")
    meta_file = os.path.join(STD_CACHE_DIR, f"java_std_{version}.json")
    meta = _read_json(meta_file, {}) or {}
    if meta.get("head") == head and meta.get("format") == JAVA_CATALOGUE_FORMAT             and os.path.exists(meta.get("java_std", "")):
        return meta
    files = _test_java_files(cucumber_std_dir, "HEAD")
    classes = java_classes(files)
    steps = [c for c in classes if c["category"] == "steps"]
    base = _most_common(c["extends"] for c in steps if c["extends"])
    examples_dir = os.path.join(STD_CACHE_DIR, f"exemples_java_{version}")
    shutil.rmtree(examples_dir, ignore_errors=True)

    def copy_example(c: dict) -> str:
        if not c:
            return ""
        target = os.path.join(examples_dir, c["path"].rsplit("/", 1)[-1])
        _write_text(target, files[c["path"]])
        return target

    # Step Definitions exemplaire : la plus courte héritant de la classe de base dominante, avec au moins 2 steps,
    # de préférence passant par une classe d'appui et par le contexte partagé du scénario
    def step_count(c):
        return sum(1 for m in c["members"] if set(m["annotations"]) & STEP_ANNOTATIONS)

    def representative(c):
        text = files[c["path"]]
        return bool(re.search(r"Context\(\)|Context\.", text)) and bool(re.search(r"\b[A-Z]\w+\.\w+\(", text))
    candidates = sorted((c for c in steps if step_count(c) >= 2 and 30 <= c["lines"] <= 200),
                        key=lambda c: (c["extends"] != base, not representative(c), c["lines"]))
    runners = _runners(cucumber_std_dir, "HEAD", classes, files)
    runner = next((c for c in classes if runners and c["path"] == runners[0]["path"]), None)
    steps_example = copy_example(candidates[0] if candidates else (min(steps, key=lambda c: c["lines"])
                                                                    if steps else None))
    runner_example = copy_example(runner)
    path = os.path.join(STD_CACHE_DIR, f"java_std_{version}.md")
    support = [c for c in classes if c["category"] == "support"]
    intro = [f"Classe de base des Step Definitions : {base or '(aucune)'} ; "
             f"exemple : '{steps_example or 'aucun'}' ; runner : '{runner_example or 'aucun'}'."]
    _write_text(path, java_catalogue_md(f"Classes Java du Cucumber Standard (Odatio {odatio_version}, commit "
                                        f"{head[:10]})", classes, intro))
    meta = {"head": head, "format": JAVA_CATALOGUE_FORMAT, "version": odatio_version, "java_std": path, "steps_example": steps_example,
            "runner_example": runner_example, "base_steps": base, "runners": runners,
            "steps_root": _most_common(_parent_package(c["package"]) for c in steps
                                       if c["package"].count(".") >= 1 and c["extends"]),
            "support_root": _most_common(_parent_package(c["package"]) for c in support if "." in c["package"]),
            "nb_classes": len(classes)}
    _write_json(meta_file, meta)
    return meta


def runner_status_for(package_steps: str, client_runners: list, std_runners: list, suffix: str,
                      conventions: dict) -> tuple:
    """
    (consigne sur le runner, runner cible) : un runner client existant est complété au besoin (runner_gaps), jamais
    doublé ; en surcouche du Standard (`std_runners`), son runner s'applique ; sinon un seul runner est à créer.
    """
    root = conventions.get("features_root") or "src/test/resources/cucumber/features/"
    covering = [r for r in client_runners if any(package_steps == g or package_steps.startswith(g + ".")
                                                 for g in r["glue"])]
    target = (covering or client_runners or [None])[0]
    if target:
        gaps = runner_gaps(target, package_steps, conventions)
        if gaps:
            return (f"complète le runner {target['fqcn']} ({target['path']}) par une modification minimale (Edit), "
                    f"sur le modèle du runner du Standard : {' ; '.join(gaps)}. N'en crée JAMAIS un second : Surefire "
                    f"les exécuterait tous (double exécution)."), target
        return (f"le runner {target['fqcn']} (glue : {', '.join(target['glue'])}) couvre déjà '{package_steps}' et "
                f"les features du client : n'en crée aucun."), target
    std = next((r for r in std_runners if any(package_steps == g or package_steps.startswith(g + ".")
                                             for g in r["glue"])), None)
    if std:
        return (f"le dépôt client s'exécute en surcouche du Cucumber Standard : le runner du Standard {std['fqcn']} "
                f"(features : {', '.join(std.get('features') or ['défaut'])} ; glue : {', '.join(std['glue'])}) "
                f"exécute ses features, filtrées par -Dcucumber.filter.tags. N'en crée aucun (double exécution)."), std
    init = ", ".join(conventions.get("init") or []) or "aucune détectée"
    tag = conventions.get("client_tag") or "le tag du client"
    return (f"aucun runner ne couvre '{package_steps}' : crée {package_steps}.Run{suffix}CucumberTest sur le modèle du "
            f"runner exemple (glue '{package_steps}' et celle du Standard, features {root} — qui contient "
            f"l'initialisation du client : {init} —, tags = \"not {a_revoir_tag()}\" sans filtre sur {tag}, plugin "
            f"\"json:target/cucumber-reports/Cucumber.json\"), un seul pour toute l'exécution."), None


def client_java_context(client_name: str, worktree: str, std_java: dict, conventions: dict = None) -> dict:
    """
    Catalogue Java du dépôt Cucumber client (java_client.md), recalculé avant chaque lot depuis le HEAD du worktree
    (classes des lots précédents comprises), packages cibles et état du runner / de la glue (runner_status_for).
    Dépôt client sans pom.xml, ou sans runner propre et dépendant du test-jar du Standard : exécuté en surcouche du
    Standard, dont le runner s'applique. `conventions` : racine des features, tag et init du client (runner_gaps).
    """
    files = _test_java_files(worktree, "HEAD")
    classes = java_classes(files)
    segment, suffix = java_package_segment(client_name), java_class_suffix(client_name)
    client_steps = [c["package"] for c in classes if c["category"] == "steps"]
    client_support = [c["package"] for c in classes if c["category"] == "support"]
    package_steps = _most_common(client_steps) or ".".join(
        p for p in (std_java.get("steps_root") or "cucumber.features", segment) if p)
    package_support = _most_common(client_support) or ".".join(
        p for p in (std_java.get("support_root") or "cucumber.util", segment) if p)
    # Dépôt client sans pom.xml : exécuté par-dessus le Standard, dont le runner et la glue s'appliquent aussi
    overlay = not os.path.exists(os.path.join(worktree, "pom.xml"))
    client_runners = _runners(worktree, "HEAD", classes, files)
    conventions = {"features_root": f"src/test/resources/cucumber/features/{segment}/",
                   **{k: v for k, v in (conventions or {}).items() if v}}
    # Sans runner propre et dépendant du test-jar du Standard (ou déclaré en surcouche dans le profil client) :
    # exécuté lui aussi en surcouche du Standard
    overlay_run = overlay or (not client_runners and (depends_on_std_test_jar(worktree)
                                                      or bool((conventions.get("runner") or {}).get("surcouche"))))
    runner_status, runner = runner_status_for(package_steps, client_runners,
                                              std_java.get("runners", []) if overlay_run else [], suffix, conventions)
    path = os.path.join(cache_dir(client_name), "java_client.md")
    mode = ("surcouche du Cucumber Standard (pas de pom.xml)" if overlay else
            "surcouche du Cucumber Standard à l'exécution (test-jar, pas de runner propre)" if overlay_run else
            "projet Maven autonome")
    _write_text(path, java_catalogue_md(f"Classes Java du dépôt Cucumber {client_name} (worktree)", classes,
                                        [f"Mode : {mode}."]))
    return {"java_client": path, "package_steps": package_steps, "package_support": package_support,
            "runner_status": runner_status, "class_suffix": suffix, "overlay": overlay, "overlay_run": overlay_run,
            "runner": runner}


def lot_runner_errors(worktree: str, base: str, files: list, java: dict, conventions: dict) -> list:
    """
    Runners après un lot : un second runner (un runner client existe à `base`, ou le runner du Standard
    s'applique en surcouche) est une erreur ; un runner créé faute d'en avoir doit couvrir la glue, la racine et
    l'init du client, exclure les scénarios désactivés et produire le rapport JSON (runner_gaps). Le runner client
    à compléter (java["runner"]) est relu sur disque : un écart restant est une erreur ; aucun runner alors qu'il
    en faut un (ni runner client, ni surcouche) aussi.
    """
    texts = {f: _read_text_file(os.path.join(worktree, *f.split("/"))) for f in files
             if f.endswith(".java") and os.path.exists(os.path.join(worktree, *f.split("/")))}
    existing = {r["path"] for r in _runners(worktree, base, java_classes(_test_java_files(worktree, base)),
                                            _test_java_files(worktree, base))}
    created = [r for r in runner_entries(java_classes(texts), texts) if r["path"] not in existing]
    reference = (java or {}).get("runner")
    errors = []
    for runner in created:
        if existing or (java or {}).get("overlay_run"):
            other = sorted(existing)[0] if existing else (reference or {}).get("fqcn", "le runner du Standard")
            errors.append(f"{runner['path']} : second runner (Surefire exécuterait les deux : double exécution) ; "
                          f"supprime-le et complète {other} si besoin")
        else:
            errors += [f"{runner['path']} : {gap}" for gap in runner_gaps(runner, java.get("package_steps", ""),
                                                                           conventions)]
    if (java or {}).get("overlay_run"):
        return errors
    target = reference if reference and reference.get("path") in existing else None
    if target and os.path.exists(os.path.join(worktree, *target["path"].split("/"))):
        current = _disk_runner(worktree, target["path"])
        gaps = runner_gaps(current, java.get("package_steps", ""), conventions) if current else [
            "n'est plus un runner Cucumber (annotations retirées) : rétablis-le"]
        errors += [f"{target['path']} : runner existant encore incomplet — {gap}" for gap in gaps]
    elif java and not existing and not created:
        instruction, _ = runner_status_for(java.get("package_steps", ""), [], [], java.get("class_suffix", ""),
                                           conventions)
        errors.append(f"aucun runner n'exécute les features du client : {instruction}")
    return errors


def _disk_runner(worktree: str, path: str) -> dict:
    """Runner `path` tel qu'il est sur disque (propriétés junit-platform / cucumber du worktree comprises)."""
    text = _read_text_file(os.path.join(worktree, *path.split("/")))
    resources = os.path.join(worktree, "src", "test", "resources")
    properties = "\n".join(_read_text_file(os.path.join(resources, name))
                           for name in ("junit-platform.properties", "cucumber.properties")
                           if os.path.exists(os.path.join(resources, name)))
    entries = runner_entries(java_classes({path: text}), {path: text}, properties)
    return entries[0] if entries else {}


def business_signatures(path: str, max_members: int = 20) -> list:
    """Signatures publiques d'une classe métier (DTO : champs déduits des accesseurs ; enum : constantes)."""
    outline = java_outline(_read_text_file(path))
    if not outline:
        return []
    head = f"{outline['kind']} {outline['name']}"
    head += f" extends {outline['extends']}" if outline["extends"] else ""
    head += f" implements {outline['implements']}" if outline["implements"] else ""
    # API publique seulement : ce qu'un test peut appeler ou construire
    members = [m for m in outline["members"] if "public" in m["modifiers"]
               or (outline["kind"] == "interface" and "private" not in m["modifiers"])]
    lines = [head]
    if outline["constants"]:
        lines.append(f"constantes : {', '.join(outline['constants'][:30])}")
    accessors = [m for m in members if POJO_METHOD.match(m["name"]) and not m["constructor"]]
    if members and len(accessors) == len(members):
        fields = dict.fromkeys(re.sub(r"^(get|set|is|with|has)", "", m["name"]) for m in accessors
                               if re.match(r"^(get|is)[A-Z]", m["name"]))
        lines.append(f"champs (accesseurs) : {', '.join(fields) or '—'}")
        return lines
    lines += [_signature(m, outline["name"]) for m in members[:max_members]]
    if len(members) > max_members:
        lines.append(f"… {len(members) - max_members} autre(s) méthode(s)")
    return lines


def _read_text_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Endpoints : appels d'API du Cucumber Standard, contrôleurs du code client
# ---------------------------------------------------------------------------

# À incrémenter quand le format du catalogue des endpoints change : invalide le cache
ENDPOINTS_FORMAT = 3
_JAVA_MASK = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'', re.S)
_METHOD_HEAD = re.compile(r"(?:(?:public|protected|private|static|final|synchronized|abstract)\s+)+"
                          r"(?:<[^>{;]*>\s*)?[\w<>\[\],.?\s]+?\s+(\w+)\s*\(")
# Helpers HTTP du Standard (EndPoints : sendGetRest, sendPostRestToDisplayList...) et RestAssured direct
_HTTP_CALL = re.compile(r"\b(?:send(Get|Post|Put|Delete|Patch|Count)\w*|request\s*\.\s*(get|post|put|delete|patch))\s*\(")
_REST_ANNOTATION = re.compile(r'@(GET|POST|PUT|DELETE|PATCH)\s*\(\s*(?:value\s*=\s*)?"([^"]*)"\s*\)')
_SPRING_MAPPING = re.compile(r'@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:(?:value|path)\s*=\s*)?\{?\s*'
                             r'"([^"]*)"')


def _mask_java(text: str) -> str:
    """Texte de même longueur : commentaires en espaces, contenu des chaînes en x (indices alignés sur l'original)."""
    def mask(m):
        s = m.group(0)
        if s[0] in "\"'":
            return s[0] + "x" * (len(s) - 2) + s[-1]
        return re.sub(r"[^\n]", " ", s)
    return _JAVA_MASK.sub(mask, text)


def _matching(masked: str, start: int, opening: str = "{", closing: str = "}") -> int:
    """Index juste après le délimiteur fermant celui ouvert en `start`."""
    depth = 0
    for i in range(start, len(masked)):
        if masked[i] == opening:
            depth += 1
        elif masked[i] == closing:
            depth -= 1
            if depth == 0:
                return i + 1
    return len(masked)


def _java_methods(text: str, class_constants=lambda cls: {}) -> list:
    """
    Méthodes d'un fichier Java : [{name, body (texte original), steps (expressions des annotations de step, constantes
    résolues), steps_non_resolus}].
    """
    masked, methods, pos, previous_end = _mask_java(text), [], 0, 0
    constants = _string_constants(text) if re.search(r"@(?:Given|When|Then|And|But|Soit|Quand|Alors|Et|Mais)\s*\(\s*"
                                                     r"(?:value\s*=\s*)?[A-Z]", text) else {}
    while True:
        match = _METHOD_HEAD.search(masked, pos)
        if not match:
            return methods
        close = _matching(masked, match.end() - 1, "(", ")")
        brace = re.match(r"\s*(?:throws\s+[\w.,\s]+)?\{", masked[close:])
        if not brace or match.group(1) in ("if", "for", "while", "switch", "catch", "return", "new"):
            pos = match.end()
            continue
        start = close + brace.end() - 1
        end = _matching(masked, start)
        before = text[previous_end:match.start()]
        steps, unresolved = step_expressions(before, class_constants, constants)
        methods.append({"name": match.group(1), "body": text[start:end], "masked": masked[start:end], "steps": steps,
                        "steps_non_resolus": unresolved, "signature": " ".join(text[match.start():close].split()),
                        "debut": text.count("\n", 0, match.start()) + 1})
        pos = previous_end = end


def _string_constants(text: str) -> dict:
    """Constantes String d'une classe : {NOM: expression d'origine} (résolues ensuite, concaténations comprises)."""
    masked = _mask_java(text)
    return {m.group(1): text[m.start(2):m.end(2)].strip()
            for m in re.finditer(r"\bString\s+([A-Z][A-Z0-9_]*)\s*=\s*([^;]+);", masked)}


def _resolve(expression: str, local: dict, shared: dict, depth: int = 0) -> str:
    """
    Chemin d'une expression d'URL : littéraux et constantes résolus, le reste remplacé par {…}. '' si aucune partie
    littérale (URL entièrement variable : paramètre d'un helper générique).
    """
    if depth > 4:
        return ""
    masked = _mask_java(expression)
    parts, level, start = [], 0, 0
    for i, char in enumerate(masked + "+"):
        if char in "([{":
            level += 1
        elif char in ")]}":
            level -= 1
        elif char == "+" and level == 0:
            parts.append(expression[start:i].strip())
            start = i + 1
    path, literal = "", False
    for part in parts:
        constant = re.fullmatch(r"(?:\w+\.)?([A-Z][A-Z0-9_]*)", part)
        if re.fullmatch(r'"(?:[^"\\]|\\.)*"', part):
            path, literal = path + part[1:-1], True
        elif constant and (constant.group(1) in local or constant.group(1) in shared):
            value = _resolve(local.get(constant.group(1)) or shared[constant.group(1)], local, shared, depth + 1)
            path, literal = path + (value or "{…}"), literal or bool(value)
        elif part:
            path += "{…}"
    return path if literal else ""


def _first_argument(masked: str, text: str, open_index: int) -> str:
    level = 0
    for i in range(open_index, len(masked)):
        if masked[i] in "([{":
            level += 1
        elif masked[i] in ")]}":
            level -= 1
            if level == 0:
                return text[open_index + 1:i]
        elif masked[i] == "," and level == 1:
            return text[open_index + 1:i]
    return ""


def _http_path(path: str) -> str:
    path = re.sub(r"/{2,}", "/", "/" + path.strip().lstrip("/"))
    return re.sub(r"\{…\}(?=/|$)", "{…}", path)


def extract_std_endpoints(files: dict) -> list:
    """
    Endpoints appelés par le Cucumber Standard ({chemin: texte} de src/test/java) : méthode HTTP, chemin, méthodes
    Java qui l'encapsulent (directement ou par appel interne) et expressions des steps qui les utilisent ;
    et {Classe#méthode: ["MÉTHODE /chemin"]} des méthodes qui y mènent.
    """
    texts = {p: t for p, t in files.items() if "/pojo/" not in p.lower()}
    constants = {p: _string_constants(t) for p, t in texts.items()}
    shared = {}
    for values in constants.values():
        for name, value in values.items():
            shared.setdefault(name, value)
    classes = {p.rsplit("/", 1)[-1][:-5]: p for p in texts}
    methods = {p: _java_methods(t, lambda cls: constants.get(classes.get(cls, ""), {})) for p, t in texts.items()}
    # Appels directs : (classe, méthode) -> {(MÉTHODE, chemin)}
    direct = {}
    for path, items in methods.items():
        cls = path.rsplit("/", 1)[-1][:-5]
        for method in items:
            for call in _HTTP_CALL.finditer(method["masked"]):
                verb = (call.group(1) or call.group(2)).upper()
                verb = "GET" if verb == "COUNT" else verb
                argument = _first_argument(method["masked"], method["body"], call.end() - 1)
                resolved = _resolve(argument, constants[path], shared)
                if resolved:
                    direct.setdefault((cls, method["name"]), set()).add((verb, _http_path(resolved)))
    # Appels indirects (méthode de la même classe ou Classe.méthode) jusqu'au point fixe
    calls = {}
    for path, items in methods.items():
        cls = path.rsplit("/", 1)[-1][:-5]
        names = {m["name"] for m in items}
        for method in items:
            targets = {(cls, n) for n in re.findall(r"(?<![.\w])(\w+)\s*\(", method["masked"]) if n in names}
            targets |= {(c, n) for c, n in re.findall(r"\b([A-Z]\w+)\s*\.\s*(\w+)\s*\(", method["masked"])
                        if c in classes}
            calls[(cls, method["name"])] = targets - {(cls, method["name"])}
    reach = {key: set(value) for key, value in direct.items()}
    changed = True
    while changed:
        changed = False
        for key, targets in calls.items():
            gained = set().union(*(reach.get(t, set()) for t in targets)) if targets else set()
            if gained - reach.get(key, set()):
                reach.setdefault(key, set()).update(gained)
                changed = True
    endpoints = {}
    for (cls, name), found in direct.items():
        for endpoint in found:
            endpoints.setdefault(endpoint, {"methode": endpoint[0], "chemin": endpoint[1], "java": [], "steps": []})[
                "java"].append(f"{cls}#{name}")
    for path, items in methods.items():
        cls = path.rsplit("/", 1)[-1][:-5]
        for method in items:
            for endpoint in reach.get((cls, method["name"]), set()) if method["steps"] else ():
                endpoints[endpoint]["steps"] += [s for s in method["steps"] if s not in endpoints[endpoint]["steps"]]
    # Méthodes Java (Classe#méthode) menant à chaque endpoint, directement ou non : rattache un step client
    # qui appelle un helper du Standard aux endpoints qu'il sollicite
    callers = {f"{cls}#{name}": sorted(f"{verb} {path}" for verb, path in found)
               for (cls, name), found in reach.items() if found}
    return sorted(endpoints.values(), key=lambda e: (e["chemin"], e["methode"])), callers


def _endpoint_line(e: dict, max_steps: int = 4) -> str:
    steps = " ; ".join(e["steps"][:max_steps]) + (f" … (+{len(e['steps']) - max_steps})"
                                                  if len(e["steps"]) > max_steps else "")
    return f"{e['methode']} {e['chemin']} — {', '.join(e['java'][:3])} — steps : {steps or 'aucun (helper)'}"


def standard_endpoints(cucumber_std_dir: str, odatio_version: str) -> dict:
    """Catalogue des endpoints du Cucumber Standard (endpoints_std.md), en cache par version Odatio et HEAD."""
    version = re.sub(r"[^\w.-]", "_", odatio_version or "inconnue")
    head = rev_parse(cucumber_std_dir, "HEAD")
    meta_file = os.path.join(STD_CACHE_DIR, f"endpoints_std_{version}.json")
    meta = _read_json(meta_file, {}) or {}
    if meta.get("head") == head and meta.get("format") == ENDPOINTS_FORMAT and os.path.exists(meta.get("fichier", "")):
        return meta
    endpoints, callers = extract_std_endpoints(_test_java_files(cucumber_std_dir, "HEAD"))
    path = os.path.join(STD_CACHE_DIR, f"endpoints_std_{version}.md")
    lines = [f"# Endpoints appelés par le Cucumber Standard (Odatio {odatio_version}, commit {head[:10]})",
             "Une ligne par endpoint : MÉTHODE /chemin — Classe#méthode qui l'encapsule — steps qui l'utilisent. "
             "{…} = partie variable. À interroger par Grep (chemin, objet métier, classe), jamais en entier.", ""]
    lines += [f"- {_endpoint_line(e)}" for e in endpoints]
    _write_text(path, "\n".join(lines) + "\n")
    meta = {"head": head, "format": ENDPOINTS_FORMAT, "version": odatio_version, "fichier": path,
            "endpoints": endpoints, "appelants": callers}
    _write_json(meta_file, meta)
    return meta


# Code des steps du Standard injecté dans les lots : sans lui, l'agent ne connaît que l'expression d'un step et
# devine les colonnes de ses tables de données, les clés de contexte qu'il lit ou écrit et les helpers qu'il appelle
STEP_CODE_FORMAT = 1
MAX_STEP_CODE_LINES = 40
DEFAULT_INLINE_STEP_CODE_CHARS = 16_000
STEP_CODE_PER_SPEC = 8
SOURCE_SUFFIXES = (".java", ".feature", ".properties")


def _step_code(method: dict, path: str, max_lines: int = MAX_STEP_CODE_LINES) -> str:
    """Signature et corps d'une méthode de step, indentation retirée, borné à `max_lines` lignes."""
    lines = f"{method['signature']} {method['body']}".splitlines()
    tail = [line for line in lines[1:] if line.strip()]
    indent = min((len(line) - len(line.lstrip()) for line in tail), default=0)
    lines = [lines[0]] + [line[indent:] if len(line) >= indent else line.lstrip() for line in lines[1:]]
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"// … {len(lines) - max_lines} ligne(s) de plus : '{path}'"]
    return "\n".join(lines)


def std_step_code(cucumber_std_dir: str, odatio_version: str) -> dict:
    """
    Code des Step Definitions du Cucumber Standard, en cache par version Odatio et HEAD : {"steps": {expression:
    {"fichier", "ligne", "code"}}, "sources": dossier}. Copie aussi les sources de test du Standard (src/test : .java,
    .feature, .properties) dans .tmp/cucumber/_standard/sources_<version>/, seul endroit où l'agent d'un lot peut
    lire le corps d'une classe d'appui ou d'un step non injecté (le clone du Standard est hors de son accès).
    """
    version = re.sub(r"[^\w.-]", "_", odatio_version or "inconnue")
    head = rev_parse(cucumber_std_dir, "HEAD")
    meta_file = os.path.join(STD_CACHE_DIR, f"step_code_{version}.json")
    sources = os.path.join(STD_CACHE_DIR, f"sources_{version}")
    meta = _read_json(meta_file, {}) or {}
    if meta.get("head") == head and meta.get("format") == STEP_CODE_FORMAT and os.path.isdir(sources):
        return meta
    tree = {p: b for p, b in ls_tree(cucumber_std_dir, "HEAD").items()
            if "src/test/" in f"/{p}" and p.endswith(SOURCE_SUFFIXES)}
    texts = blob_texts(cucumber_std_dir, tree.values())
    shutil.rmtree(sources, ignore_errors=True)
    for path, blob in tree.items():
        _write_text(os.path.join(sources, *path.split("/")), texts.get(blob, ""))
    files = {p: texts.get(b, "") for p, b in tree.items() if p.endswith(".java") and "src/test/java/" in f"/{p}"}
    constants = {p: _string_constants(t) for p, t in files.items()}
    classes = {p.rsplit("/", 1)[-1][:-5]: p for p in files}
    steps = {}
    for path, text in sorted(files.items()):
        if not re.search(r"@(?:Given|When|Then|And|But|Soit|Quand|Alors|Et|Mais)\s*\(", text):
            continue
        for method in _java_methods(text, lambda cls: constants.get(classes.get(cls, ""), {})):
            for expr in method["steps"]:
                steps.setdefault(expr, {"fichier": path, "ligne": method["debut"], "code": _step_code(method, path)})
    meta = {"head": head, "format": STEP_CODE_FORMAT, "version": odatio_version, "sources": sources, "steps": steps}
    _write_json(meta_file, meta)
    return meta


def step_code_block(specifics: list, step_code: dict, max_chars: int = DEFAULT_INLINE_STEP_CODE_CHARS,
                    per_spec: int = STEP_CODE_PER_SPEC) -> tuple:
    """
    Code des steps candidats du lot (les `per_spec` premiers de chaque spécifique, pris à tour de rôle, sans
    doublon), dans la limite de `max_chars`. Renvoie (texte, expressions injectées).
    """
    known = (step_code or {}).get("steps") or {}
    queue = [[c["expr"] for c in (spec.get("steps_candidats") or [])[:per_spec]] for spec in specifics]
    parts, done, used = [], set(), 0
    for rank in range(per_spec):
        for exprs in queue:
            if rank >= len(exprs) or exprs[rank] in done or exprs[rank] not in known:
                continue
            entry = known[exprs[rank]]
            text = f"`{exprs[rank]}` — {entry['fichier']}:{entry['ligne']}\n```java\n{entry['code']}\n```"
            if used + len(text) > max_chars:
                continue
            parts.append(text)
            done.add(exprs[rank])
            used += len(text)
    return "\n".join(parts), done


def rest_endpoints(text: str) -> list:
    """Endpoints exposés par un contrôleur (annotations Restx @POST("/...") ou Spring @PostMapping) : [(MÉTHODE, chemin, méthode Java)]."""
    found, masked = [], _mask_java(text)
    prefix = ""
    head = re.search(r'@RequestMapping\s*\(\s*(?:(?:value|path)\s*=\s*)?\{?\s*"([^"]*)"[^)]*\)\s*(?:public\s+)?'
                     r'(?:abstract\s+)?class', text)
    if head:
        prefix = head.group(1)
    for regex, spring in ((_REST_ANNOTATION, False), (_SPRING_MAPPING, True)):
        for match in regex.finditer(text):
            if masked[match.start()] != "@" or (spring and head and match.start() == head.start()):
                continue
            verb = match.group(1).upper()
            verb = "GET" if verb == "REQUEST" else verb
            name = _METHOD_HEAD.search(masked, match.end())
            found.append((verb, _http_path((prefix if spring else "") + "/" + match.group(2)),
                          name.group(1) if name else ""))
    return found


def _path_key(path: str) -> list:
    """Segments d'un chemin, paramètres (:id, {id}, {…}) remplacés par *."""
    return ["*" if re.fullmatch(r":\w+|\{[^}]*\}", s) else s.lower() for s in path.strip("/").split("/") if s]


def _same_path(a: str, b: str) -> bool:
    """Même endpoint, à un préfixe de module près (/wms/preparationunits ~ /preparationunits)."""
    ka, kb = _path_key(a), _path_key(b)
    short, long_ = sorted((ka, kb), key=len)
    literals = [s for s in short if s != "*"]
    # Au moins un segment littéral discriminant ; un paramètre ne remplace jamais un segment littéral
    if not short or short[0] == "*" or not literals or (len(literals) == 1 and len(literals[0]) < 8):
        return False
    return all(x == y or (x == "*" and y == "*") or x == "*" for x, y in zip(short, long_[len(long_) - len(short):]))


def attach_endpoints(inventory: dict, std_root: str, std_endpoints: dict) -> None:
    """
    Endpoints de chaque spécifique : spec["endpoints_client"] (exposés par ses contrôleurs et absents du Standard)
    et spec["endpoints_std"] (endpoints du Cucumber Standard que le spécifique expose, surcharge ou dont le chemin
    nomme son objet métier), au plus 8, les plus utilisés par les steps d'abord.
    """
    std_repos = git_repos(std_root)
    refs = {name: pick_ref(path, STD_REFS) for name, path in std_repos.items()}
    catalogue = sorted(std_endpoints["endpoints"], key=lambda e: -len(e["steps"]))
    for spec in inventory["specifiques"]:
        exposed, standard_paths = [], set()
        for f in spec["fichiers"]:
            if not f["path"].endswith(".java") or "src/main/java/" not in f"/{f['path']}":
                continue
            endpoints = rest_endpoints(_read_text_file(f.get("local", "")))
            cls = f["path"].rsplit("/", 1)[-1][:-5]
            exposed += [(verb, path, f"{cls}#{name}") for verb, path, name in endpoints]
            # Override d'un contrôleur du Standard : ses endpoints existent déjà côté Standard
            if endpoints and f.get("kind") == "override" and ":" in f.get("standard", ""):
                repo, std_path = f["standard"].split(":", 1)
                if repo in std_repos and refs.get(repo):
                    text = _git(std_repos[repo], "show", f"{refs[repo]}:{std_path}").stdout
                    standard_paths |= {(verb, path) for verb, path, _ in rest_endpoints(text)}
        targets = _override_targets(spec, inventory.get("client", ""))
        related = []
        for e in catalogue:
            by_path = any(e["methode"] == verb and _same_path(e["chemin"], path) for verb, path, _ in exposed)
            by_name = any(t in _norm(e["chemin"]) for t in targets)
            if (by_path or by_name) and e not in related:
                related.append(e)
        spec["endpoints_std"] = [{"methode": e["methode"], "chemin": e["chemin"], "java": e["java"][:2],
                                  "steps": e["steps"][:3]} for e in related[:8]]
        spec["endpoints_client"] = [
            {"methode": verb, "chemin": path, "java": java} for verb, path, java in exposed
            if (verb, path) not in standard_paths
            and not any(e["methode"] == verb and _same_path(e["chemin"], path) for e in catalogue)][:10]


def prepare(client_name: str, client_root: str, std_root: str, cucumber_std_dir: str, odatio_version: str,
            cucumber_repo_dir: str, cucumber_ref: str, tickets: list) -> dict:
    """Pré-calculs complets (0 token) ; écrit inventaire.json et renvoie l'inventaire."""
    inventory = build_inventory(client_name, client_root, std_root)
    export_sources(inventory)
    attach_std_tests(inventory, cucumber_std_dir, odatio_version)
    attach_endpoints(inventory, std_root, standard_endpoints(cucumber_std_dir, odatio_version))
    inventory["catalogue_standard"] = standard_catalogue(cucumber_std_dir, odatio_version)
    coverage = client_coverage(client_name, cucumber_repo_dir, cucumber_ref)
    inventory["couverture_existante"] = coverage["fichier"]
    match_coverage(inventory, coverage)
    jira_extracts(inventory, tickets)
    from tools.cucumber_initializers import find_initializers
    inventory["initialiseurs"] = find_initializers(inventory)
    _write_json(inventory_path(client_name), inventory)
    return inventory


def cached_tickets(client_name: str, max_age_s: int = 86400):
    """Tickets Jira mis en cache (évite de les relire à chaque reprise), ou None s'ils sont absents ou périmés."""
    path = os.path.join(cache_dir(client_name), "jira_tickets.json")
    if os.path.exists(path) and datetime.now().timestamp() - os.path.getmtime(path) < max_age_s:
        return _read_json(path)
    return None


def save_tickets(client_name: str, tickets: list) -> None:
    _write_json(os.path.join(cache_dir(client_name), "jira_tickets.json"), tickets)


# ---------------------------------------------------------------------------
# Worktree, commits et patch
# ---------------------------------------------------------------------------

def base_ref(cucumber_repo_dir: str, base_branch: str) -> str:
    """Branche de base du dépôt Cucumber client : origin/<base>, à défaut <base> locale ('' si aucune)."""
    return pick_ref(cucumber_repo_dir, (f"origin/{base_branch}", base_branch))


def prepare_worktree(cucumber_repo_dir: str, client_name: str, base: str, reset: bool) -> str:
    """
    Worktree isolé du dépôt Cucumber client sur la branche cucumber_<client>_coverage. `reset` : repart de
    `base` (nouvelle génération) ; sinon réutilise le worktree existant (reprise, complément), ou à défaut la
    branche et ses commits de lots. Lève RuntimeError.
    """
    return add_worktree(cucumber_repo_dir, worktree_dir(client_name), f"cucumber_{client_name}_coverage", base, reset)


def add_worktree(cucumber_repo_dir: str, path: str, branch: str, base: str, reset: bool) -> str:
    """
    Worktree `path` du dépôt Cucumber client sur `branch`. `reset` : branche recréée depuis `base` ; sinon worktree
    existant réutilisé, ou à défaut la branche existante. Lève RuntimeError.
    """
    if os.path.isdir(path) and not reset:
        return path
    if os.path.isdir(path):
        _git(cucumber_repo_dir, "worktree", "remove", "--force", path)
        shutil.rmtree(path, ignore_errors=True)
    _git(cucumber_repo_dir, "worktree", "prune")
    keep = not reset and _git(cucumber_repo_dir, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0
    result = _git(cucumber_repo_dir, "worktree", "add", path, branch) if keep else _git(
        cucumber_repo_dir, "worktree", "add", "-B", branch, path, base)
    if result.returncode != 0:
        if "already" in result.stderr or "used by worktree" in result.stderr:
            raise RuntimeError(f"La branche {branch} est encore extraite dans un autre worktree (génération "
                               f"précédente) : récupérez son contenu si besoin, puis supprimez-le avec "
                               f"« git -C \"{cucumber_repo_dir}\" worktree remove --force <chemin> ». "
                               f"Détail : {result.stderr.strip()[:300]}")
        raise RuntimeError(f"Création du worktree impossible : {result.stderr.strip()[:500]}")
    return path


def snapshot(repo_dir: str, path: str, ref: str) -> str:
    """
    Copie de lecture du dépôt `repo_dir` à `ref` dans `path` : clone local --shared (objets lus dans le dépôt
    d'origine) extrait en HEAD détaché sur le commit de `ref`. Rien n'est écrit dans le dépôt d'origine (ni branche,
    ni worktree, ni index). Renvoie le commit extrait. Lève RuntimeError.
    """
    commit = _git(repo_dir, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}").stdout.strip()
    if not commit:
        raise RuntimeError(f"Référence {ref} introuvable dans {repo_dir}.")
    if not os.path.isdir(os.path.join(path, ".git")):
        shutil.rmtree(path, ignore_errors=True)
        cloned = subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", repo_dir, path],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        if cloned.returncode != 0:
            raise RuntimeError(f"Copie de lecture de {repo_dir} impossible : {cloned.stderr.strip()[:300]}")
    checkout = _git(path, "checkout", "-q", "--force", "--detach", commit)
    if checkout.returncode != 0:
        raise RuntimeError(f"Extraction de {ref} impossible : {checkout.stderr.strip()[:300]}")
    _git(path, "clean", "-q", "-fdx")
    return commit


def commit_lot(worktree: str, client_name: str, lot_number: int, forced: list = ()) -> bool:
    """
    Commite les fichiers écrits par le lot (hors sorties Maven), plus `forced` : fichiers de test ignorés par le
    .gitignore du client, ajoutés de force pour être livrés. Renvoie True si un commit a été créé.
    """
    _git(worktree, "add", "-A", "--", ".", ":(exclude)target", ":(exclude)**/target/**")
    if forced:
        _git(worktree, "add", "-f", "--", *forced)
    if _git(worktree, "diff", "--cached", "--quiet").returncode == 0:
        return False
    return _git(worktree, "commit", "-m", f"Cucumber coverage of {client_name} specifics - lot {lot_number}"
                ).returncode == 0


def discard_lot(worktree: str) -> None:
    """Lot en échec : fichiers suivis remis au dernier commit et fichiers non suivis supprimés (hors ignorés)."""
    _git(worktree, "reset", "-q", "--hard", "HEAD")
    _git(worktree, "clean", "-fdq")


def strip_agent_tags(text: str, client_name: str, keep: set = frozenset()) -> str:
    """
    Feature sans les tags de vérification @agent_<client>_<lot>[_<n>] (sauf ceux de `keep`, déjà dans la base) ;
    une ligne qui ne portait que ces tags disparaît.
    """
    pattern = re.compile(rf"^@agent_{re.escape(java_package_segment(client_name))}_\d+(?:_\d+)?$")
    lines = []
    for line in text.splitlines(keepends=True):
        tokens = line.split()
        if not tokens or not tokens[0].startswith("@"):
            lines.append(line)
            continue
        kept = [t for t in tokens if t in keep or not pattern.match(t)]
        if len(kept) == len(tokens):
            lines.append(line)
        elif kept:
            indent = line[:len(line) - len(line.lstrip())]
            lines.append(indent + " ".join(kept) + ("\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n")
                                                    else ""))
    return "".join(lines)


def write_patch(worktree: str, base: str, output: str, client_name: str = "") -> bool:
    """
    Livrable de la génération. Par défaut (CUCUMBER_PATCH_MODE=squash) : UN seul commit, construit sur l'état
    actuel de `base` (branche d'intégration), qui ne contient que l'écart net du worktree sur les fichiers que
    l'agent a lui-même modifiés — il s'applique sur la branche telle qu'elle est aujourd'hui, même si elle a avancé
    ou intègre déjà une partie des tests, et ne rétablit jamais une ancienne version d'un autre fichier.
    CUCUMBER_PATCH_MODE=commits : un commit par lot depuis `base` (ancien comportement). Les tags
    @agent_<client>_<lot>[_<n>] sont retirés des .feature du livrable (CUCUMBER_KEEP_AGENT_TAGS=1 : gardés).
    Renvoie False s'il n'y a rien à livrer.
    """
    head = rev_parse(worktree, "HEAD")
    commits = _git(worktree, "rev-list", "--reverse", f"{base}..HEAD").stdout.split()
    if not commits:
        return False
    if os.getenv("CUCUMBER_PATCH_MODE", "squash").strip() != "commits":
        return _squashed_patch(worktree, base, output, client_name)
    tip = head
    if client_name and os.getenv("CUCUMBER_KEEP_AGENT_TAGS", "").strip() != "1":
        tip = _commits_without_agent_tags(worktree, base, commits, client_name) or head
    return format_patch(worktree, f"{base}..{tip}", output)


def agent_paths(worktree: str, base: str) -> list:
    """Fichiers modifiés par les commits de l'agent depuis `base` (hors fusions de synchronisation et sorties Maven)."""
    paths = []
    for commit in _git(worktree, "rev-list", "--no-merges", f"{base}..HEAD").stdout.split():
        out = _git(worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root", commit).stdout
        paths += [p for p in out.split("\0") if p and not p.startswith("target/") and "/target/" not in p]
    return sorted(set(paths))


def _git_env(repo: str, env: dict, *args, data: bytes = None) -> subprocess.CompletedProcess:
    """git avec un environnement complété (index temporaire), entrée et sortie en octets."""
    return subprocess.run(["git", "-C", repo, *args], input=data, capture_output=True, env={**os.environ, **env})


def _squashed_patch(worktree: str, base: str, output: str, client_name: str = "") -> bool:
    """
    Un commit sur `base`, construit sans espace de travail temporaire (index temporaire : robuste sous Windows, où
    un worktree verrouillé faisait retomber sur un patch de tous les commits). Fichiers de l'agent pris dans HEAD
    (tags @agent_ retirés) ; jamais de suppression d'un fichier présent dans `base` (un scénario n'est jamais
    supprimé, et un fichier poussé depuis par l'utilisateur ne doit pas disparaître) ; deux chemins qui ne diffèrent
    que par la casse (même fichier sous Windows) : un seul est livré, celui de `base` à défaut le plus récent.
    """
    paths = agent_paths(worktree, base)
    if not paths:
        return False
    # -z : chemins non ASCII (accents) non échappés, identiques à ceux d'agent_paths
    head_files = set(filter(None, _git(worktree, "ls-tree", "-r", "-z", "--name-only", "HEAD").stdout.split("\0")))
    base_files = set(filter(None, _git(worktree, "ls-tree", "-r", "-z", "--name-only", base).stdout.split("\0")))
    skipped = [p for p in paths if p not in head_files and p in base_files]
    if skipped:
        print(f"[CUCUMBER] Patch : {len(skipped)} suppression(s) de fichiers existants écartée(s) (jamais livrées) : "
              + ", ".join(skipped[:5]) + (" …" if len(skipped) > 5 else ""))
    by_case = {}
    for rel in (p for p in paths if p in head_files):
        by_case.setdefault(rel.lower(), []).append(rel)
    kept = []
    for variants in by_case.values():
        if len(variants) > 1:
            chosen = next((v for v in variants if v in base_files), variants[-1])
            print(f"[CUCUMBER] Patch : chemins identiques à la casse près ({', '.join(variants)}) : seul {chosen} "
                  f"est livré")
            variants = [chosen]
        kept += variants
    if not kept:
        return False
    strip = client_name and os.getenv("CUCUMBER_KEEP_AGENT_TAGS", "").strip() != "1"
    modes = {}
    for line in filter(None, _git(worktree, "ls-tree", "-r", "-z", "HEAD", "--", *kept).stdout.split("\0")):
        meta, _, rel = line.partition("\t")
        modes[rel] = meta.split()[0]
    index = os.path.join(os.path.dirname(worktree), f"patch_index_{os.getpid()}")
    env = {"GIT_INDEX_FILE": index}
    try:
        if _git_env(worktree, env, "read-tree", base).returncode != 0:
            return False
        for rel in kept:
            data = subprocess.run(["git", "-C", worktree, "show", f"HEAD:{rel}"], capture_output=True).stdout
            if strip and rel.endswith(".feature"):
                text = data.decode("utf-8", errors="replace")
                keep = set(re.findall(r"@agent_\w+", _git(worktree, "show", f"{base}:{rel}").stdout))
                data = strip_agent_tags(text, client_name, keep).encode("utf-8")
            blob = _git_env(worktree, env, "hash-object", "-w", "--stdin", data=data).stdout.decode().strip()
            if not blob:
                return False
            _git_env(worktree, env, "update-index", "--add", "--cacheinfo",
                     f"{modes.get(rel, '100644')},{blob},{rel}")
        tree = _git_env(worktree, env, "write-tree").stdout.decode().strip()
        if not tree or tree == _git(worktree, "rev-parse", f"{base}^{{tree}}").stdout.strip():
            return False
        subjects = _git(worktree, "log", "--no-merges", "--reverse", "--format=- %s", f"{base}..HEAD").stdout.strip()
        message = (f"Cucumber : tests des spécifiques {client_name or ''} (agent)".replace("  ", " ")
                   + f"\n\nÉcart net avec {base}, en un commit. Commits de l'agent :\n{subjects}")
        commit = _git_env(worktree, env, "commit-tree", tree, "-p", rev_parse(worktree, base), "-m",
                          message).stdout.decode().strip()
        return bool(commit) and format_patch(worktree, f"{commit}~1..{commit}", output)
    finally:
        if os.path.exists(index):
            os.remove(index)


def format_patch(worktree: str, revisions: str, output: str) -> bool:
    """
    `git format-patch --binary` de `revisions` dans `output`, en octets : fichiers binaires et encodages autres
    qu'UTF-8 (CSV, .xlsm) restent intacts. Renvoie False si le patch est vide ou en erreur.
    """
    result = subprocess.run(["git", "-C", worktree, "format-patch", "--binary", revisions, "--stdout"],
                            capture_output=True)
    if result.returncode != 0 or not result.stdout.strip():
        return False
    with open(output, "wb") as f:
        f.write(result.stdout)
    return True


def _commits_without_agent_tags(worktree: str, base: str, commits: list, client_name: str) -> str:
    """
    Recopie des commits du lot sans les tags @agent_ (chaque commit refait depuis son instantané complet, donc sans
    conflit), dans un worktree temporaire. Renvoie le dernier commit recopié ('' en cas d'erreur).
    """
    export = os.path.join(os.path.dirname(worktree), "patch_export")
    _git(worktree, "worktree", "remove", "--force", export)
    shutil.rmtree(export, ignore_errors=True)
    _git(worktree, "worktree", "prune")
    if _git(worktree, "worktree", "add", "-q", "--detach", export, base).returncode != 0:
        print("[CUCUMBER] ⚠️ Copie sans tags @agent_ impossible : patch produit avec les tags.")
        return commits[-1]
    parent = rev_parse(worktree, base)
    try:
        for commit in commits:
            _git(export, "checkout", "-q", "--force", "--detach", commit)
            changed = _git(export, "diff", "--name-only", base, commit, "--", "*.feature").stdout.split("\n")
            for rel in filter(None, changed):
                path = os.path.join(export, *rel.split("/"))
                if not os.path.exists(path):
                    continue
                keep = set(re.findall(r"@agent_\w+", _git(export, "show", f"{base}:{rel}").stdout))
                with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
                    text = f.read()
                stripped = strip_agent_tags(text, client_name, keep)
                if stripped != text:
                    with open(path, "w", encoding="utf-8", newline="") as f:
                        f.write(stripped)
            _git(export, "add", "-A", "--", ".", ":(exclude)target", ":(exclude)**/target/**")
            tree = _git(export, "write-tree").stdout.strip()
            author = _git(worktree, "log", "-1", "--format=%an%x00%ae%x00%aI", commit).stdout.strip().split("\0")
            message = _git(worktree, "log", "-1", "--format=%B", commit).stdout
            env = {**os.environ, **dict(zip(("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE"), author))}
            created = subprocess.run(["git", "-C", export, "commit-tree", tree, "-p", parent, "-m", message.strip()],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
            if created.returncode != 0 or not created.stdout.strip():
                print(f"[CUCUMBER] ⚠️ Copie sans tags @agent_ impossible ({created.stderr.strip()[:200]}) : patch "
                      f"produit avec les tags.")
                return ""
            parent = created.stdout.strip()
        return parent
    finally:
        _git(worktree, "worktree", "remove", "--force", export)
        shutil.rmtree(export, ignore_errors=True)


# ---------------------------------------------------------------------------
# Compilation et dry-run (Maven), après chaque lot
# ---------------------------------------------------------------------------

DEFAULT_COMPILE_CMD = "mvn -q -o test-compile"
DEFAULT_DRY_RUN_CMD = "mvn -q -o test"
MAX_BUILD_ERRORS = 30
_OFFLINE_FAILURE = re.compile(r"offline mode|has not been downloaded|Could not resolve dependencies|"
                              r"Could not find artifact|Non-resolvable|Plugin .* could not be resolved", re.I)
_MVN_ERROR = re.compile(r"^\[ERROR\]\s+(.+?\.java):\[(\d+),(\d+)\]\s*(.*)$")


def build_dir(client_name: str, worktree: str, cucumber_std_dir: str) -> str:
    """
    Dossier où compiler, à jour du worktree : le worktree lui-même s'il a un pom.xml ; sinon (dépôt client en
    surcouche du Standard) un worktree détaché du Cucumber Standard, .tmp/cucumber/<client>/build, remis à son HEAD
    puis recouvert des fichiers du dépôt client. target/ (ignoré par git) y est conservé. Lève RuntimeError.
    """
    if os.path.exists(os.path.join(worktree, "pom.xml")):
        return worktree
    path = os.path.join(cache_dir(client_name), "build")
    head = rev_parse(cucumber_std_dir, "HEAD")
    # Worktree d'un autre clone du Standard (dépôt changé) : recréé depuis le clone courant
    common_dir = lambda repo: os.path.normcase(os.path.abspath(
        _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip() or repo))
    if os.path.exists(os.path.join(path, ".git")) and common_dir(path) == common_dir(cucumber_std_dir):
        result = _git(path, "checkout", "-q", "--force", "--detach", head)
        _git(path, "clean", "-fdq")
    else:
        shutil.rmtree(path, ignore_errors=True)
        _git(cucumber_std_dir, "worktree", "prune")
        result = _git(cucumber_std_dir, "worktree", "add", "--detach", path, head)
    if result.returncode != 0:
        raise RuntimeError(f"Dossier de compilation {path} non préparé : {result.stderr.strip()[:300]}")
    for rel in _git(worktree, "ls-files", "-co", "--exclude-standard", "-z").stdout.split("\0"):
        if rel:
            target = os.path.join(path, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(worktree, *rel.split("/")), target)
    return path


def run_maven(command: str, cwd: str, timeout: int = 1800) -> tuple:
    """
    Lance une commande Maven : (code retour, sortie). Hors ligne (-o) en échec faute de dépendances en cache :
    relancée une fois en ligne.
    """
    def run(cmd):
        try:
            result = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, f"[ERROR] Délai de {timeout} s dépassé : {cmd}"
        return result.returncode, f"{result.stdout}\n{result.stderr}"
    code, output = run(command)
    offline = re.compile(r"(?<!\S)-o(?!\S)")
    if code != 0 and offline.search(command) and _OFFLINE_FAILURE.search(output):
        code, output = run(" ".join(offline.sub(" ", command).split()))
    return code, output


def _relative(path: str, root: str) -> str:
    path = re.sub(r"^/(?=[A-Za-z]:)", "", path.strip())
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return path.replace("\\", "/")
    return path.replace("\\", "/") if rel.startswith("..") else rel.replace("\\", "/")


def compile_errors(output: str, root: str) -> list:
    """Erreurs de compilation « fichier:ligne:colonne message (symbole) », dédupliquées, au plus 30."""
    errors, lines = [], output.splitlines()
    for i, line in enumerate(lines):
        match = _MVN_ERROR.match(line.strip())
        if not match:
            continue
        message = match.group(4).strip()
        for following in lines[i + 1:i + 3]:
            detail = re.sub(r"^\[ERROR\]\s*", "", following).strip()
            if detail.startswith(("symbol", "location", "symbole", "emplacement")):
                message += f" ({' '.join(detail.split())})"
        error = f"{_relative(match.group(1), root)}:{match.group(2)}:{match.group(3)} {message}"
        if error not in errors:
            errors.append(error)
    return errors[:MAX_BUILD_ERRORS]


def dry_run_errors(output: str) -> list:
    """Steps sans définition, ambigus et features illisibles signalés par un dry-run Cucumber."""
    errors = [f"step non défini : {s}" for s in re.findall(r"The step ['\"](.+?)['\"] is undefined", output)]
    errors += [" ".join(m.split()) for m in re.findall(
        r"((?:AmbiguousStepDefinitionsException|DuplicateStepDefinitionException|ParserException|"
        r"Failed to parse resource)[^\n]*)", output)]
    return list(dict.fromkeys(errors))[:MAX_BUILD_ERRORS]


def _last_errors(output: str, count: int = 8) -> list:
    errors = [line.strip() for line in output.splitlines() if line.strip().startswith("[ERROR]") and line.strip()
              != "[ERROR]"]
    return list(dict.fromkeys(errors))[-count:] or [line for line in output.strip().splitlines()[-count:]]


def verify_build(build: str, compile_cmd: str, features: list = None, dry_run_cmd: str = "") -> dict:
    """
    Compile le dossier de build, puis, si `dry_run_cmd` et des features sont donnés, vérifie en dry-run Cucumber
    que leurs steps ont tous une définition. Renvoie {"ok", "etape", "erreurs", "environnement"} : environnement
    = échec sans erreur exploitable dans le code (dépendances, Maven absent...), à ne pas soumettre à l'agent.
    """
    code, output = run_maven(compile_cmd, build)
    if code != 0:
        errors = compile_errors(output, build)
        return {"ok": False, "etape": "compilation", "erreurs": errors or _last_errors(output),
                "environnement": not errors}
    if dry_run_cmd and features:
        paths = ",".join(features)
        code, output = run_maven(f'{dry_run_cmd} "-Dcucumber.features={paths}" -Dcucumber.execution.dry-run=true',
                                 build)
        errors = dry_run_errors(output)
        if code != 0 or errors:
            return {"ok": False, "etape": "dry-run", "erreurs": errors or _last_errors(output),
                    "environnement": not errors}
    return {"ok": True, "etape": "dry-run" if dry_run_cmd and features else "compilation", "erreurs": [],
            "environnement": False}


# ---------------------------------------------------------------------------
# Lots
# ---------------------------------------------------------------------------

def _lot_domain(spec: dict) -> str:
    """Domaine d'un spécifique pour regrouper les lots : dépôt et deux premiers mots du processus, ou du dossier."""
    if spec.get("nature") == "processus":
        name = re.sub(r"^(spec|ref)-", "", spec.get("dossier", "").rsplit("/", 1)[-1])
        return f"{spec.get('depot', '')}:{'-'.join(name.split('-')[:2])}"
    return f"{spec.get('depot', '')}:{spec.get('dossier', '').split('/')[0]}"


def order_specifics(spec_ids: list, specifics: dict) -> list:
    """Spécifiques regroupés par test Standard principal (tests_std[0]), puis par domaine ; sans test à la fin."""
    def key(item):
        n, i = item
        spec = specifics.get(i) or {}
        tests = spec.get("tests_std") or []
        return (0 if tests else 1, tests[0]["path"] if tests else "", _lot_domain(spec), n)
    return [i for _, i in sorted(enumerate(spec_ids), key=key)]


def make_lots(spec_ids: list, batch_size: int, specifics: dict = None) -> list:
    """
    Lots de `batch_size` spécifiques. Avec `specifics` : regroupés par test Standard principal puis par domaine
    (order_specifics), et les spécifiques de libellés réunis en un seul lot, à la fin.
    """
    size = max(1, batch_size)
    labels = [i for i in spec_ids if (specifics or {}).get(i, {}).get("nature") == "libellés"]
    others = [i for i in spec_ids if i not in labels]
    if specifics:
        others = order_specifics(others, specifics)
    groups = [others[i * size:(i + 1) * size] for i in range(math.ceil(len(others) / size))]
    groups += [labels] if labels else []
    return [{"numero": n + 1, "ids": ids, "statut": "a_faire"} for n, ids in enumerate(groups)]


# ---------------------------------------------------------------------------
# Tri Python avant les lots (0 token)
# ---------------------------------------------------------------------------

_EVENT_ROOT = re.compile(r"<execution-event-(listener-)?definition\b([^>]*)>", re.S)
_EVENT_NAME = re.compile(r"(^|-)(listener|event)(-|$)")
TECHNICAL_REASON = ("technique : ni code Java, ni processus Studio, ni paramètre — aucun comportement observable "
                    "propre à tester (tri automatique, sans appel Claude)")


def studio_event(spec: dict) -> dict:
    """
    Listener ou événement Studio du spécifique (un seul customized.xml, libellés mis à part) :
    {"type": "listener"|"event", "nom", "on_event", "execute", "logique"} ; {} sinon. Un listener a une logique
    propre s'il a une condition (<if>…) ou un mapping autre que `$event.champ` ; un événement, s'il déclare
    autre chose que des variables.
    """
    name = spec.get("dossier", "").rsplit("/", 1)[-1]
    if spec.get("nature") != "processus" or not _EVENT_NAME.search(name):
        return {}
    xml = [f for f in spec.get("fichiers", []) if f["path"].rsplit("/", 1)[-1] == "customized.xml"]
    others = [f for f in spec.get("fichiers", []) if f not in xml and not _is_label_file(f["path"])]
    if len(xml) != 1 or others or spec.get("nb_fichiers", 0) > len(spec.get("fichiers", [])):
        return {}
    text = _read_text_file(xml[0].get("local", ""))
    root = _EVENT_ROOT.search(text)
    if not root:
        return {}
    attributes = dict(re.findall(r'([\w-]+)="([^"]*)"', root.group(2)))
    tags = set(re.findall(r"<([\w-]+)", text[root.end():]))
    if root.group(1):
        inputs = re.findall(r"<input\b[^>]*>(.*?)</input>", text, re.S)
        logic = bool(tags - {"mappings", "input"}) or any(
            not re.fullmatch(r"\$event(\.\w+)*", i.strip()) for i in inputs)
        return {"type": "listener", "nom": name, "on_event": attributes.get("on-event", "").rsplit("/", 1)[-1],
                "execute": attributes.get("execute", "").rsplit("/", 1)[-1], "logique": logic}
    return {"type": "event", "nom": name, "on_event": "", "execute": "",
            "logique": bool(tags - {"variable", "variables", "description"})}


def studio_dependencies(specifics: dict) -> dict:
    """
    Éléments Studio sans logique propre (déclaration d'événement ou de variables seulement, listener sans condition
    ni transformation), classés par analyse du XML, sans appel Claude : {id: {"couvert_par" (id du processus de
    l'inventaire qui l'utilise, '' s'il est du Standard), "processus", "raison", "preuve"}}.
    """
    processes = {s.get("dossier", "").rsplit("/", 1)[-1]: s for s in specifics.values()
                 if s.get("nature") == "processus" and s.get("dossier")}
    events = {i: studio_event(s) for i, s in specifics.items() if s.get("nature") == "processus"}
    listeners = [e for e in events.values() if e.get("type") == "listener"]
    out = {}
    for i, event in events.items():
        if not event or event["logique"]:
            continue
        via = [(event["nom"], event["execute"])] if event["type"] == "listener" else [
            (e["nom"], e["execute"]) for e in listeners if e["on_event"] == event["nom"]]
        via = [(name, process) for name, process in via if process]
        if not via:
            continue
        listener, process_name = next(((n, p) for n, p in via if p in processes and not events.get(
            processes[p]["id"])), via[0])
        process = processes.get(process_name)
        what = ("listener sans logique propre (aucune condition, mappings $event.* seuls)" if event["type"] ==
                "listener" else "événement sans logique propre (déclaration de variables seulement)")
        out[i] = {"couvert_par": process["id"] if process and not events.get(process["id"]) else "",
                  "processus": process_name,
                  "raison": f"{what}, couvert par le processus qui l'utilise : {process_name}"
                            + ("" if event["type"] == "listener" else f" (déclenché par {listener})"),
                  "preuve": f"{specifics[i].get('dossier', '')}/customized.xml : "
                            + ("mappings $event.* seuls" if event["type"] == "listener" else
                               "éléments <variable> seuls, aucune condition, action ni écran")}
    return out


def _is_technical(spec: dict) -> bool:
    """Ni fichier Java, ni processus Studio, ni paramètre (et tous ses fichiers sont listés)."""
    files = spec.get("fichiers", [])
    return bool(files) and spec.get("nb_fichiers", len(files)) <= len(files) and spec.get("nature") != "processus" \
        and not any(f["path"].endswith(".java") or STUDIO_FILES.match(f["path"].rsplit("/", 1)[-1])
                    or f.get("kind") == "parametrage" for f in files)


def presort(spec_ids: list, specifics: dict) -> tuple:
    """
    Tri sans token des spécifiques sélectionnés. Renvoie (identifiants laissés aux lots, résultats déjà conclus) :
    élément Studio sans logique propre (studio_dependencies) -> « couvert_indirectement » par le processus de
    l'inventaire qui l'utilise, quel que soit son état (aucun lot pour lui), ou « sans objet » si ce processus est
    du Standard ; spécifique purement technique -> « non_couvert » (TECHNICAL_REASON).
    """
    dependencies = studio_dependencies(specifics)
    remaining, results = [], []
    for i in spec_ids:
        dependency = dependencies.get(i)
        result = None
        if dependency and dependency["couvert_par"]:
            result = {"id": i, "statut": "couvert_indirectement", "couvert_par": dependency["couvert_par"],
                      "raison": f"{dependency['raison']} (tri automatique, sans appel Claude)",
                      "preuve": dependency["preuve"]}
        elif dependency:
            result = {"id": i, "statut": "non_couvert", "raison": f"{dependency['raison']}, processus du Standard",
                      "sans_objet": {"preuve": dependency["preuve"]}}
        elif _is_technical(specifics[i]):
            result = {"id": i, "statut": "non_couvert", "raison": TECHNICAL_REASON}
        if result:
            results.append({"scenarios": [], "fichiers": [], "classes_java": [], "inspire_de": [], "tri": True,
                            **result})
        else:
            remaining.append(i)
    return remaining, results


def presort_lot(results: list, number: int) -> dict:
    """Lot terminé sans appel Claude, portant les résultats du tri Python."""
    return {"numero": number, "ids": [r["id"] for r in results], "statut": "termine", "tri": True,
            "resultats": results, "commit": False, "fin": datetime.now().isoformat(timespec="seconds")}


def add_sorted_lot(state: dict, results: list) -> dict:
    """
    Complément : lot du tri sans token ajouté à la suite, ses spécifiques retirés de leurs anciens lots (résultats
    gardés dans « historique »). Renvoie le lot ajouté.
    """
    lot = presort_lot(results, max((lot["numero"] for lot in state.get("lots", [])), default=0) + 1)
    _take_from_lots(state, {i: lot["numero"] for i in lot["ids"]})
    state["lots"] = state.get("lots", []) + [lot]
    return lot


# ---------------------------------------------------------------------------
# Contenu injecté dans le prompt (au lieu d'être lu par l'agent)
# ---------------------------------------------------------------------------

DEFAULT_INLINE_MAX_CHARS = 24_000
DEFAULT_INLINE_STUDIO_LINES = 150
STUDIO_CONTEXT_LINES = 3


def studio_excerpt(original: str, customized: str, max_lines: int = DEFAULT_INLINE_STUDIO_LINES) -> str:
    """
    Parties modifiées d'un customized.xml : blocs ajoutés ou modifiés par rapport à la référence, avec
    STUDIO_CONTEXT_LINES lignes de contexte (numéros de ligne du fichier client), dans la limite de `max_lines`.
    Sans référence (processus nouveau) : le début du fichier.
    """
    lines = customized.splitlines()
    if not original:
        kept = [f"{n + 1:>5}  {l}" for n, l in enumerate(lines[:max_lines])]
        return "\n".join(kept + ([f"# … {len(lines) - max_lines} ligne(s) suivante(s) à lire si nécessaire"]
                                 if len(lines) > max_lines else []))
    out, used = [], 0
    matcher = difflib.SequenceMatcher(None, original.splitlines(), lines, autojunk=False)
    for group in matcher.get_grouped_opcodes(STUDIO_CONTEXT_LINES):
        start, end = group[0][3], group[-1][4]
        removed = sum(i2 - i1 for tag, i1, i2, _, _ in group if tag in ("delete", "replace"))
        header = f"# lignes {start + 1}-{end}" + (f" ({removed} ligne(s) du Standard retirée(s) ou remplacée(s))"
                                                   if removed else "")
        block = [header] + [f"{n + 1:>5}  {lines[n]}" for n in range(start, end)]
        if used + len(block) > max_lines:
            out.append(f"# … autres parties modifiées non affichées (borne de {max_lines} lignes) : lire le fichier")
            break
        out += block
        used += len(block)
    return "\n".join(out)


def attach_studio_excerpts(inventory: dict, max_lines: int = DEFAULT_INLINE_STUDIO_LINES) -> int:
    """
    f["extrait_studio"] des customized.xml de chaque spécifique : fichier <client>/studio/<id>/<nom>.txt des
    parties modifiées (studio_excerpt), la référence étant original.xml du dépôt client ou le fichier du Standard ;
    f["comportements_studio"] : différences structurées (cucumber_studio.behaviours, au plus MAX_BEHAVIOURS) et
    f["nb_comportements_studio"] leur nombre par type, même avec `max_lines` à 0.
    Renvoie le nombre de fichiers dont l'extrait est écrit (0 token, recalculé à chaque exécution).
    """
    directory = os.path.join(cache_dir(inventory["client"]), "studio")
    shutil.rmtree(directory, ignore_errors=True)
    client_repos, std_repos = git_repos(inventory["depot_client"]), git_repos(inventory["standard"])
    refs = inventory.get("references", {})
    count = 0
    for spec in inventory["specifiques"]:
        for f in spec["fichiers"]:
            f.pop("extrait_studio", None)
            f.pop("comportements_studio", None)
            f.pop("nb_comportements_studio", None)
            if f["path"].rsplit("/", 1)[-1] != "customized.xml" or not f.get("local"):
                continue
            original = ""
            standard = f.get("standard", "")
            if f.get("kind") == "override" and standard.startswith("original"):
                repo_dir, ref = client_repos.get(f["repo"]), refs.get(f["repo"], {}).get("ref")
                if repo_dir and ref:
                    original = _git(repo_dir, "show", f"{ref}:{f['path'][:-len('customized.xml')]}original.xml").stdout
            elif f.get("kind") == "override" and ":" in standard:
                repo, path = standard.split(":", 1)
                ref = pick_ref(std_repos[repo], STD_REFS) if repo in std_repos else ""
                if ref:
                    original = _git(std_repos[repo], "show", f"{ref}:{path}").stdout
            customized = _read_text_file(f["local"])
            # Chaque différence avec la référence est un comportement à couvrir (vue Couverture, prompt des lots)
            found = cucumber_studio.behaviours(original, customized, limit=None)
            f["comportements_studio"] = found[:cucumber_studio.MAX_BEHAVIOURS]
            f["nb_comportements_studio"] = dict(Counter(b["type"] for b in found))
            if max_lines <= 0:
                continue
            excerpt = studio_excerpt(original, customized, max_lines)
            if not excerpt.strip():
                continue
            target = os.path.join(directory, spec["id"], f["path"].replace("/", "__") + ".txt")
            _write_text(target, f"# Parties modifiées de {f['path']} ("
                                + ("par rapport à " + standard.split(' (')[0] if original else "processus nouveau")
                                + f") ; fichier complet : '{f['local']}'\n{excerpt}\n")
            f["extrait_studio"] = target
            count += 1
    return count


def _inline_documents(documents: list, max_chars: int) -> tuple:
    """
    `documents` : [(chemin, intitulé)], dans l'ordre de priorité. Contenu des documents tant que le total reste
    sous `max_chars` (un document trop gros est sauté, les suivants peuvent entrer). Renvoie (texte, chemins
    injectés, [(chemin, intitulé)] non injectés).
    """
    parts, inlined, skipped, used = [], set(), [], 0
    for path, label in documents:
        if not path or path in inlined:
            continue
        text = _read_text_file(path).strip()
        if text and used + len(text) <= max_chars:
            parts.append(f"--- '{path}' ({label}) ---\n{text}\n--- fin ---")
            inlined.add(path)
            used += len(text)
        elif text:
            skipped.append((path, label))
    return "\n".join(parts), inlined, skipped


def fixed_references(catalogue_std: str, steps_example: str, recipe_examples: list, max_chars: int) -> tuple:
    """
    Références de la partie fixe des lots (prompt système, identique pour tous les lots, donc lue depuis le cache
    à partir du 2e lot) : sommaire du catalogue Standard, Step Definitions exemplaire, exemples des recettes. Au
    plus `max_chars` caractères ; au-delà, le chemin est donné pour lecture. Renvoie (texte, chemins injectés).
    """
    documents = [(catalogue_std, "sommaire du catalogue Cucumber Standard"),
                 (steps_example, "Step Definitions exemplaire du Standard")]
    documents += [(p, "exemple de recette") for p in sorted(set(recipe_examples))]
    text, inlined, skipped = _inline_documents(documents, max_chars)
    lines = ["=== RÉFÉRENCES INJECTÉES (contenu complet, identique pour tous les lots : NE RELIS PAS ces fichiers) ==="]
    lines += [text] if text else ["(aucune)"]
    lines += [f"- Non injecté (taille), à lire une seule fois si nécessaire : '{p}' ({label})" for p, label in skipped]
    lines.append("=== FIN DES RÉFÉRENCES INJECTÉES ===")
    return "\n".join(lines), inlined


def needs_runner(runner_status: str) -> bool:
    """runner_status de client_java_context : faut-il créer un runner ?"""
    return runner_status.startswith("aucun runner")


def runner_to_complete(runner_status: str) -> bool:
    """runner_status de client_java_context : faut-il compléter le runner existant ?"""
    return runner_status.startswith("complète le runner")


def runner_block(runner_example: str, runner_status: str, max_chars: int) -> str:
    """Runner du Standard injecté dans la partie variable du lot, s'il faut créer ou compléter un runner ('' sinon)."""
    if not (runner_example and (needs_runner(runner_status) or runner_to_complete(runner_status))):
        return ""
    purpose = "modèle du runner à créer" if needs_runner(runner_status) else "modèle des options à compléter"
    text, _, _ = _inline_documents([(runner_example, f"runner du Standard, {purpose}")], max_chars)
    return text or f"- Runner du Standard à prendre pour modèle (à lire) : '{runner_example}'"


# Raisons de non-couverture qu'une recette, un nouveau step ou une relecture par endpoint peut lever
COMPLETABLE_REASON = re.compile(r"step|endpoint|interact|v[ée]rifi", re.I)


def latest_results(state: dict) -> dict:
    """Dernier résultat connu de chaque spécifique (lots dans l'ordre : un complément remplace l'ancien lot)."""
    return {r["id"]: r for lot in state.get("lots", []) for r in lot.get("resultats") or [] if r.get("id")}


def completable_specifics(state: dict) -> list:
    """
    Spécifiques non couverts faute de step, d'endpoint, d'interaction ou de vérification possible (raison), hors
    ceux déjà repris par un lot de complément ou en attente dans un lot non terminé.
    """
    completed = {i for lot in state.get("lots", []) if lot.get("complement") for i in lot["ids"]}
    queued = {i for lot in pending_lots(state) for i in lot["ids"]}
    return [i for i, r in latest_results(state).items() if r.get("statut") == "non_couvert"
            and COMPLETABLE_REASON.search(r.get("raison") or "") and i not in completed | queued]


def add_completion_lots(state: dict, spec_ids: list, batch_size: int, specifics: dict = None) -> list:
    """
    Complément d'une génération : nouveaux lots numérotés à la suite pour `spec_ids`, retirés de leurs anciens lots
    (leurs résultats y sont gardés dans « historique », avec le lot qui les reprend). Renvoie les nouveaux lots.
    """
    start = max((lot["numero"] for lot in state.get("lots", [])), default=0)
    new = make_lots(spec_ids, batch_size, specifics)
    for lot in new:
        lot.update(numero=lot["numero"] + start, complement=True)
    _take_from_lots(state, {i: lot["numero"] for lot in new for i in lot["ids"]})
    state["lots"] = state.get("lots", []) + new
    return new


def _take_from_lots(state: dict, taken: dict) -> None:
    """Spécifiques `taken` ({id: lot qui les reprend}) retirés de leurs anciens lots, résultats en « historique »."""
    for lot in state.get("lots", []):
        moved = [i for i in lot["ids"] if i in taken]
        if not moved:
            continue
        lot["ids"] = [i for i in lot["ids"] if i not in taken]
        old = [r for r in lot.get("resultats") or [] if r.get("id") in taken]
        lot.setdefault("historique", []).extend({**r, "repris_par_lot": taken[r["id"]]} for r in old)
        if lot.get("resultats") is not None:
            lot["resultats"] = [r for r in lot["resultats"] if r.get("id") not in taken]
        if not lot["ids"] and lot.get("statut") != "termine":
            lot["statut"] = "exclu"


def requalify_indirect(state: dict, inventory: dict) -> list:
    """
    Statut « couvert_indirectement » (champ couvert_par) : gardé si le spécifique cité est couvert (par cette
    génération, ou déjà couvert à l'inventaire), sinon requalifié « non_couvert » ; réévalué à chaque exécution
    (statut déclaré gardé dans statut_declare). Renvoie les identifiants requalifiés.
    """
    already = {s["id"] for s in inventory.get("specifiques", []) if s.get("couverture") == "deja_couvert"}
    results = latest_results(state)
    requalified = []
    for r in results.values():
        if r.get("statut_declare") == "couvert_indirectement" or r.get("statut") == "couvert_indirectement":
            r["statut_declare"] = "couvert_indirectement"
            target = results.get(r.get("couvert_par") or "", {})
            valid = target.get("statut") == "couvert" or r.get("couvert_par") in already
            r["statut"] = "couvert_indirectement" if valid else "non_couvert"
            r.pop("requalification", None)
            if not valid:
                r["requalification"] = f"couvert_par {r.get('couvert_par') or '(absent)'} non couvert"
                requalified.append(r["id"])
    return requalified


def lot_example(specifics: list) -> str:
    """Extrait de la feature Standard la mieux classée du lot ('' si aucun spécifique n'a de test équivalent)."""
    tests = [t for spec in specifics for t in spec.get("tests_std") or [] if t.get("extrait")]
    return max(tests, key=lambda t: t["score"])["extrait"] if tests else ""


PARTIAL_RULE = ("- TÂCHE PARTIELLE : les tests existants ne vérifient qu'une configuration ou la présence d'une donnée. "
                "Teste l'EFFET du spécifique (document généré, message envoyé avec son contenu, statut atteint, champ "
                "enregistré relu), jamais la configuration seule.")
MAX_LOT_BEHAVIOURS = 15


def lot_block(specifics: list, inline_max: int = 0, inlined: set = frozenset(), step_code: dict = None,
              step_code_max: int = DEFAULT_INLINE_STEP_CODE_CHARS) -> str:
    """
    Partie variable du prompt : recettes applicables (une fois chacune), puis les spécifiques du lot (steps
    candidats compris). `inline_max` > 0 : contenu des extraits Standard et des extraits Jira du lot injecté à la
    fin, dans cette limite de caractères (au-delà, le chemin reste à lire), ainsi que les parties modifiées des
    customized.xml (extrait_studio, déjà bornées en lignes, hors de cette limite). `inlined` : chemins déjà
    injectés dans la partie fixe (exemples de recettes).
    """
    studio = [f["extrait_studio"] for spec in specifics for f in spec["fichiers"] if f.get("extrait_studio")]
    studio_text, studio_here, _ = _inline_documents([(p, "parties modifiées du processus Studio") for p in studio],
                                                    10 ** 9) if inline_max > 0 else ("", set(), [])
    documents = []
    for rank in range(MAX_STD_TESTS):
        documents += [(spec["tests_std"][rank]["extrait"], f"test Standard équivalent de {spec['id']}")
                      for spec in specifics if len(spec.get("tests_std") or []) > rank
                      and spec["tests_std"][rank].get("extrait")]
    documents += [(spec["jira"], f"extraits Jira de {spec['id']}") for spec in specifics if spec.get("jira")]
    injected, here, _ = _inline_documents(documents, inline_max) if inline_max > 0 else ("", set(), [])
    # Code des steps candidats (table de données, clés de contexte, helpers) : step_code de std_step_code
    code_text, with_code = step_code_block(specifics, step_code, step_code_max) if step_code and step_code_max > 0 \
        else ("", set())

    def ref(path: str) -> str:
        return (f"'{path}' (injecté plus bas)" if path in here else
                f"'{path}' (dans les références injectées)" if path in inlined else f"'{path}'")
    lines, recipes = [], {}
    for spec in specifics:
        for recipe in spec.get("recettes") or []:
            recipes.setdefault(recipe["titre"], (recipe, []))[1].append(spec["id"])
    if recipes:
        lines += ["### Recettes applicables (à appliquer avant toute conclusion « non_couvert »)", ""]
        for title, (recipe, ids) in recipes.items():
            lines.append(f"#### {title} — spécifiques : {', '.join(ids)}")
            if recipe.get("steps"):
                lines.append(f"- Steps Standard : {recipe['steps']}")
            if recipe.get("exemple_local"):
                lines.append(f"- Exemple : {ref(recipe['exemple_local'])} (Standard : {recipe['exemple_std']})")
            if recipe.get("statut"):
                lines.append(f"- Statut attendu : {recipe['statut']}")
            if recipe.get("consigne"):
                lines.append(f"- Consigne : {recipe['consigne']}")
            lines.append("")
    for spec in specifics:
        lines += [f"### Spécifique {spec['id']} — {spec['libelle']}",
                  f"- Type : {spec['type']} ({spec['nature']}) ; couverture actuelle : {spec.get('couverture', '?')}",
                  f"- Mots-clés : {', '.join(spec['mots_cles']) or '(aucun)'}"]
        if spec.get("scenarios_existants"):
            lines.append("- Scénarios existants à compléter plutôt que dupliquer : "
                         + " ; ".join(spec["scenarios_existants"]))
        if spec.get("partiel"):
            lines.append(PARTIAL_RULE)
        behaviours = cucumber_studio.spec_behaviours(spec)
        if behaviours:
            total = cucumber_studio.behaviour_counts(spec)
            lines.append(f"- Comportements Studio à couvrir ({cucumber_studio.counts_line(total)} ; chaque scénario "
                         f"vérifie au moins un élément qui n'existe que dans la personnalisation) :")
            lines += [f"  - {cucumber_studio.behaviour_line(b)}" for b in behaviours[:MAX_LOT_BEHAVIOURS]]
        lines.append(f"- Tickets Jira : {ref(spec['jira'])}" if spec.get("jira") else "- Tickets Jira : aucun trouvé")
        if spec.get("tests_std"):
            lines.append("- Tests Standard équivalents (à lire avant d'écrire) :")
            for test in spec["tests_std"]:
                scenarios = " ; ".join(test["scenarios"][:6]) + (" …" if len(test["scenarios"]) > 6 else "")
                steps = ", ".join(f.rsplit("/", 1)[-1][:-5] for f in test.get("step_definitions", [])[:4])
                lines.append(f"  - {ref(test.get('extrait', test['path']))} — {test['feature']} / {scenarios} "
                             f"(score {test['score']}, {test['raison']}"
                             + (f" ; Step Definitions : {steps}" if steps else "") + ")")
        else:
            lines.append("- Tests Standard équivalents : aucun test Standard équivalent")
        if spec.get("steps_candidats"):
            lines.append("- Steps candidats (Standard, les plus proches, à réutiliser tels quels) :")
            lines += [f"  - `{s['expr']}` — {s['classe']}" + (f" — {' ; '.join(s['endpoints'])}" if s["endpoints"]
                                                               else "")
                      + (" (code injecté plus bas)" if s["expr"] in with_code else "") for s in spec["steps_candidats"]]
        if spec.get("endpoints_std"):
            lines.append("- Endpoints Standard liés (déjà appelés par ces steps / classes du Standard : à réutiliser) :")
            lines += [f"  - {e['methode']} {e['chemin']} — {', '.join(e['java'])}"
                      + (f" — steps : {' ; '.join(e['steps'])}" if e["steps"] else "") for e in spec["endpoints_std"]]
        if spec.get("endpoints_client"):
            lines.append("- Endpoints propres au client (aucun appel Cucumber Standard : nouveau code d'appel autorisé, "
                         "sur le modèle des classes *EndPoints du Standard) :")
            lines += [f"  - {e['methode']} {e['chemin']} — {e['java']}" for e in spec["endpoints_client"]]
        lines.append(f"- Fichiers ({spec['nb_fichiers']}, contenu à la version de référence) :")
        for f in spec["fichiers"]:
            detail = f"  - [{f['kind']}] '{f.get('local', f['path'])}'" + (
                " (parties modifiées injectées plus bas)" if f.get("extrait_studio") in studio_here else "")
            if f.get("diff"):
                elements = ", ".join(f["diff"]["elements"]) or "—"
                detail += f" — écart avec {f.get('standard', 'le Standard')} : {f['diff']['stat']} ; modifié : {elements}"
            lines.append(detail)
            if f.get("local_standard"):
                lines.append(f"    Version Standard (avant override, à comparer pour le commentaire « # Standard : … / "
                             f"Client : … » et l'assertion qui échoue sur le Standard) : '{f['local_standard']}'")
            # Classes métier : signatures publiques données ici, le fichier n'est ouvert que si son corps est utile
            if f["path"].endswith(".java") and "src/main/java/" in f"/{f['path']}" and f.get("local"):
                lines += [f"    {line}" for line in business_signatures(f["local"])]
        if spec["nb_fichiers"] > len(spec["fichiers"]):
            lines.append(f"  - … {spec['nb_fichiers'] - len(spec['fichiers'])} autre(s) fichier(s) non listé(s)")
        lines.append("")
    if studio_text:
        lines += ["### Parties modifiées des processus Studio du lot (le reste du fichier : à lire si nécessaire)",
                  studio_text]
    if code_text:
        lines += ["### Code des steps candidats (Standard : colonnes des tables de données, clés de contexte, helpers et "
                  "endpoints appelés — à respecter à l'identique dans les .feature, et modèle des nouveaux steps)",
                  code_text]
    if injected:
        lines += ["### Contenu injecté du lot (extraits Standard et Jira : NE RELIS PAS ces fichiers)", injected]
    return "\n".join(lines).rstrip()


CONSTAT_TYPES = {"donnee_introuvable": "donnée introuvable",
                 "ecart_spec_code": "écart entre spécification et code",
                 "defaut_code": "défaut du code spécifique ou du Studio",
                 "comportement_surprenant": "comportement surprenant du code fourni",
                 "tautologique": "scénario tautologique écarté"}
# Défauts et doutes sur le code fourni (bilan après import) ; leur certitude : « prouvé » exige une preuve citée
DOUBT_TYPES = ("ecart_spec_code", "defaut_code", "comportement_surprenant")
CERTAINTIES = ("prouvé", "probable", "à vérifier")
# Champs facultatifs d'un constat (défaut ou doute sur le code fourni) et leur longueur maximale
CONSTAT_FIELDS = {"ligne": 20, "extrait": 300, "effet": 300, "preuve": 300, "correction": 300}
MAX_CONSTATS = 10


def normalize_constats(result: dict) -> list:
    """
    « constats » d'un résultat de lot ou de correction (champ facultatif) ramenés à [{type, detail, fichier,
    ligne, extrait, effet, preuve, correction}] (champs facultatifs gardés s'ils sont remplis), plus « certitude »
    pour les DOUBT_TYPES (« prouvé » sans preuve ramené à « probable », « à vérifier » par défaut) : entrée sans détail
    ignorée, type hors CONSTAT_TYPES rangé en « autre », au plus MAX_CONSTATS. Le résultat est mis à jour.
    """
    constats = []
    raw = result.get("constats")
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {"detail": item}
        if not isinstance(item, dict) or not str(item.get("detail") or "").strip():
            continue
        kind = str(item.get("type") or "").strip().lower()
        constat = {"type": kind if kind in CONSTAT_TYPES else "autre",
                   "detail": " ".join(str(item["detail"]).split())[:500],
                   "fichier": str(item.get("fichier") or "").strip()}
        constat.update({k: " ".join(str(item[k]).split())[:size] for k, size in CONSTAT_FIELDS.items()
                        if str(item.get(k) or "").strip()})
        if constat["type"] in DOUBT_TYPES:
            certainty = str(item.get("certitude") or "").strip().lower()
            certainty = certainty if certainty in CERTAINTIES else "à vérifier"
            constat["certitude"] = "probable" if certainty == "prouvé" and not constat.get("preuve") else certainty
        if item.get("tag"):
            constat["tag"] = str(item["tag"]).strip()
        constats.append(constat)
    result["constats"] = constats[:MAX_CONSTATS]
    return result["constats"]


def constat_line(constat: dict) -> str:
    where = constat.get("fichier", "") + (f":{constat['ligne']}" if constat.get("ligne") else "")
    return (f"{CONSTAT_TYPES.get(constat['type'], 'constat')} : {constat['detail']}"
            + (f" ({where})" if where else "")
            + (f" — {constat['certitude']}" if constat.get("certitude") else ""))


def parse_lot_result(text: str):
    """
    Bloc JSON final de l'agent (liste d'objets par spécifique, « constats » normalisés), ou None s'il est absent
    ou invalide.
    """
    text = (text or "").strip()
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    candidates = fenced[::-1] + [text]
    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate.strip())
        except ValueError:
            continue
        if isinstance(data, dict):
            data = data.get("specifiques") or data.get("resultats") or [data]
        if isinstance(data, list) and all(isinstance(d, dict) and "id" in d for d in data):
            for result in data:
                normalize_constats(result)
            return data
    return None


def lot_files(worktree: str, results: list) -> list:
    """Fichiers écrits par le lot : modifiés ou nouveaux dans le worktree, plus ceux déclarés dans le JSON final."""
    out = _git(worktree, "ls-files", "-mo", "--exclude-standard").stdout.splitlines()
    declared = [p for r in results or [] for p in (r.get("fichiers") or []) + (r.get("classes_java") or [])
                if isinstance(p, str)]
    paths = (p.replace("\\", "/") for p in out + declared if p.strip())
    return list(dict.fromkeys(p for p in paths if not re.search(r"(^|/)target/", p)))


def lot_cost_average(usage_csv: str, step: str = "cucumber-lot") -> dict:
    """Moyenne des lots déjà exécutés (token_usage.csv) : {'lots', 'tokens', 'cout_usd'} ; lots=0 si inconnue."""
    import csv
    rows = []
    try:
        with open(usage_csv, "r", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f, delimiter=";") if r.get("etape") == step]
    except OSError:
        pass

    def num(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if not rows:
        return {"lots": 0, "tokens": 0, "cout_usd": 0.0}
    tokens = [sum(num(r.get(k)) for k in ("tokens_entree", "cache_ecrit", "cache_lu", "tokens_sortie")) for r in rows]
    return {"lots": len(rows), "tokens": sum(tokens) / len(rows),
            "cout_usd": sum(num(r.get("cout_usd")) for r in rows) / len(rows)}


def lot_indicators(lot: dict) -> dict:
    """Indicateurs d'un lot pour l'IHM et le récapitulatif (vide si le lot n'a pas été contrôlé)."""
    analysis, review = lot.get("indicateurs") or {}, lot.get("revue") or {}
    if not analysis:
        return {}
    return {"scenarios": analysis.get("nb_scenarios", 0), "reutilisation": analysis.get("taux_reutilisation"),
            "steps_std": analysis.get("steps_std", 0), "steps_client": analysis.get("steps_client", 0),
            "steps_crees": analysis.get("steps_crees", 0), "non_definis": analysis.get("steps_non_definis", 0),
            "signales": len(review.get("signales", [])), "supprimes": len(review.get("supprimes", []))}


def _compilation_status(lot: dict) -> str:
    check = lot.get("verification") or {}
    if not check:
        return "non compilé"
    if check.get("environnement"):
        return "non vérifié (environnement Maven)"
    return f"{check.get('etape', 'compilation')} OK" if check.get("ok") else f"en erreur ({check.get('etape')})"


def _selection_line(selection: dict) -> str:
    """Ligne du récapitulatif : nombre attendu de la livraison (deux dry-runs) ou erreurs de sélection."""
    if not selection:
        return "- Nombre attendu de la livraison : non contrôlé"
    if selection.get("erreurs"):
        return (f"- Nombre attendu de la livraison : ❌ {len(selection['erreurs'])} erreur(s) de sélection — "
                f"{selection['erreurs'][0]}")
    if selection.get("nombre_attendu"):
        return (f"- Nombre attendu de la livraison : {selection['nombre_attendu']} scénario(s) (dry-runs sans filtre "
                f"et avec le tag client identiques) ; un run du job qui en exécute un autre nombre ne porte pas sur "
                f"cette version ou ignore la sélection de tags")
    return (f"- Nombre attendu de la livraison : {selection.get('attendu', 0)} scénario(s) actif(s) à la lecture des "
            f"features (dry-runs non lancés)")


def db_usage_lines(state: dict) -> list:
    """
    Section « Base de données consultée » du récapitulatif : par lot, les commandes base réellement lancées par
    l'agent (journal de db_tools, lot["base"]) par étape, avec les tables lues et les erreurs ; rien si aucun lot
    n'a interrogé la base.
    """
    lots = [lot for lot in state.get("lots", []) if lot.get("base")]
    if not lots:
        return []
    lines = ["", "## Base de données consultée", "",
             "Commandes réellement lancées par l'agent (journal de tools/db_tools.py : nature et tables, jamais les "
             "valeurs).", ""]
    for lot in lots:
        by_step = {}
        for entry in lot["base"]:
            by_step.setdefault(entry.get("etape") or "?", []).append(entry)
        for step, entries in by_step.items():
            tables = list(dict.fromkeys(t for e in entries for t in e.get("tables") or []))
            errors = [e.get("erreur") for e in entries if not e.get("ok")]
            lines.append(f"- Lot {lot['numero']}, {step} : {len(entries)} commande(s)"
                         + (f" — tables : {', '.join(tables)}" if tables else "")
                         + (f" — {len(errors)} en erreur ({errors[0]})" if errors else ""))
    return lines


_RUNNER_MARKERS = re.compile(r"@CucumberOptions|Cucumber\.class|@Suite\b|@IncludeEngines|cucumber\.(?:glue|plugin|"
                             r"filter\.tags|features)")


def runner_report(worktree: str, base: str, runner: dict) -> list:
    """
    Section « Runner » du récapitulatif (point 8) : contrôle fait avant le premier lot (consigne donnée) et
    modification du runner livrée dans le patch (fichier, lignes ajoutées / retirées, raison) ; [] sans contrôle.
    """
    if not runner or not base or not os.path.isdir(worktree):
        return []
    lines = ["", "## Runner", "", f"- Contrôle avant le premier lot : {runner.get('consigne', '?')}"]
    changed = []
    for line in _git(worktree, "diff", "--numstat", base, "HEAD", "--", "*.java", "*.properties").stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and _RUNNER_MARKERS.search(_read_text_file(os.path.join(worktree, *parts[2].split("/")))):
            changed.append(f"- Modifié dans le patch : `{parts[2]}` (+{parts[0]} / -{parts[1]} ligne(s)) — raison : "
                           f"{runner.get('consigne', '?')}")
    return lines + (changed or ["- Aucune modification du runner dans le patch."])


def write_coverage_summary(client_name: str, inventory: dict, state: dict) -> str:
    """
    Récapitulatif <client>-cucumber-couverture.md : indicateurs globaux (réutilisation des steps Standard,
    doublons retirés à la revue, scénarios générés / actifs / désactivés par cause), puis par spécifique ses
    scénarios actifs, leur différence avec le Standard, leur source, le test Standard d'inspiration, les steps
    réutilisés / créés, les endpoints et la compilation. Un spécifique sans scénario actif est non couvert.
    """
    specs = {s["id"]: s for s in inventory.get("specifiques", [])}
    lots = state.get("lots", [])
    lot_of = {i: lot for lot in lots for i in lot["ids"]}
    results = {r["id"]: r for lot in lots for r in lot.get("resultats", []) or []}
    selected = [i for lot in lots for i in lot["ids"]]
    analysed = [s for lot in lots for s in (lot.get("indicateurs") or {}).get("scenarios", [])]
    disabled = {(e["fichier"], e["titre"]): e for lot in lots for e in lot.get("a_revoir") or []}
    active = {}
    for s in analysed:
        if (s["fichier"], s["titre"]) not in disabled:
            active[s.get("spec", "")] = active.get(s.get("spec", ""), 0) + 1
    # RÈGLE 3e : un spécifique dont tous les scénarios sont désactivés n'est pas couvert
    emptied = [i for i in selected if results.get(i, {}).get("statut") == "couvert" and not active.get(i)
               and any(s.get("spec") == i for s in analysed)]
    covered = [i for i in selected if results.get(i, {}).get("statut") == "couvert" and i not in emptied]
    indirect = [i for i in selected if results.get(i, {}).get("statut") == "couvert_indirectement"]
    deleted = [d for lot in lots for d in (lot.get("revue") or {}).get("supprimes", [])]
    std_steps = sum(s["steps_std"] for s in analysed)
    all_steps = sum(s["steps_std"] + s["steps_client"] + s["steps_non_definis"] for s in analysed)
    reasons = {}
    for d in deleted:
        for motif in d["motifs"]:
            key = re.sub(r" de \S+ / .*$| \(.*\)$", "", motif)
            reasons[key] = reasons.get(key, 0) + 1
    lines = [f"# Couverture Cucumber des spécifiques {client_name}", "",
             f"{len(specs)} spécifique(s) inventorié(s), {len(selected)} traité(s), {len(covered)} couvert(s), "
             f"{len(indirect)} couvert(s) indirectement. "
             f"Généré le {datetime.now().strftime('%d/%m/%Y %H:%M')}.", ""]
    own_init = state.get("init_corrigee") or {}
    if own_init.get("fichiers"):
        # Compléments d'init corrigés par l'agent : en tête, à relire avant le reste du patch
        last = (own_init.get("tentatives") or [{}])[-1]
        lines += [f"> ⚠️ **Compléments d'initialisation corrigés par l'agent** (à relire en premier) : "
                  f"{', '.join(own_init['fichiers'])}. Cause : {last.get('cause') or '—'}."
                  + (f" Restant : {' ; '.join(last['restantes'])}." if last.get("restantes") else "")
                  + f" Init après correction : {(state.get('init') or {}).get('message', '—')}.", ""]
    lines += [
             f"- Scénarios contrôlés : {len(analysed)} ; réutilisation des steps Standard : "
             + (f"{round(100 * std_steps / all_steps)} % ({std_steps}/{all_steps} steps)" if all_steps else "—")
             + f" ; steps non définis : {sum(s['steps_non_definis'] for s in analysed)}",
             f"- Scénarios générés : {len(analysed)} ; actifs livrés : {len(analysed) - len(disabled)} ; "
             f"désactivés (tag à revoir, jamais supprimés) : {len(disabled)}"
             + (" — " + " ; ".join(f"{k} : {n}" for k, n in sorted(Counter(
                 e["cause"] for e in disabled.values()).items())) if disabled else ""),
             _selection_line(state.get("selection")),
             f"- Doublons retirés à la revue : {len(deleted)}"
             + (" — " + " ; ".join(f"{k} : {n}" for k, n in sorted(reasons.items(), key=lambda kv: -kv[1]))
                if reasons else "")]
    unreviewed = [s for lot in lots for s in (lot.get("revue") or {}).get("sans_appel", [])]
    if unreviewed:
        lines.append(f"- Scénarios signalés sans appel de revue (seuls « source » / « difference_standard » vides "
                     f"dans le JSON) : {len(unreviewed)} — "
                     + " ; ".join(f"{s['fichier']} / {s['titre']}" for s in unreviewed[:20])
                     + (" …" if len(unreviewed) > 20 else ""))
    sorted_out = [r for lot in lots if lot.get("tri") for r in lot.get("resultats") or []]
    if sorted_out:
        lines.append(f"- Tri automatique sans appel Claude : "
                     f"{sum(1 for r in sorted_out if r.get('statut_declare', r['statut']) == 'couvert_indirectement')}"
                     f" listener(s) / événement(s) sans logique propre, "
                     f"{sum(1 for r in sorted_out if r.get('raison') == TECHNICAL_REASON)} spécifique(s) technique(s)")
    from tools.cucumber_verify import verification_indicators  # import local : cucumber_verify importe ce module
    verif = verification_indicators(state)

    def pct(value):
        return "—" if value is None else f"{value} %"
    if verif["hors_test"]:
        lines.append(f"- Échecs hors test, jamais corrigés pour cela : 🌐 environnement {verif['environnement']} (route, "
                     f"champ exigé, version) ; 📭 donnée de référence absente {verif['donnee_reference']} ; 🐞 bug "
                     f"applicatif probable {verif['bug_applicatif']} ; ⛓️ cascade d'une mise en place en échec "
                     f"{verif['cascade']}")
    if verif["scenarios"] and not verif["verifies"] and not verif["hors_test"]:
        lines.append("- ⚪ **Pertinence non vérifiée par exécution** : "
                     + (next((lot["execution"]["message"] for lot in lots if lot.get("execution")), "")
                        or "aucun moyen d'exécution configuré"))
    elif verif["verifies"]:
        lines += [f"- Scénarios pertinents (exécution) : {pct(verif['taux_pertinent'])} ({verif['pertinents']}/"
                  f"{verif['verifies']} vérifiés ; {verif['non_verifies']} non vérifié(s))",
                  f"- Mutations des attendus détectées : {pct(verif['taux_mutation'])} ; overrides en échec sur le "
                  f"Standard : {pct(verif['taux_echec_standard'])}",
                  f"- Scénarios désactivés (A_REVOIR.md) : {verif['desactives']} ; faibles restés actifs : "
                  f"{verif['faibles']}"]
    lines.append(f"- Validation humaine : {pct(verif['taux_validation_humaine'])} ({verif['verdicts']} verdict(s))")
    coverage = {k: v for lot in lots for k, v in ((lot.get("execution") or {}).get("couverture_code") or {}).items()}
    lines += ["", "## Spécifiques couverts", ""]
    for i in covered:
        spec, result, lot = specs.get(i, {"libelle": i, "fichiers": []}), results[i], lot_of.get(i, {})
        mine = [s for s in (lot.get("indicateurs") or {}).get("scenarios", []) if s.get("spec") == i]
        lines += [f"### {spec['libelle']} ({spec.get('type', '?')})",
                  f"- Scénarios actifs livrés : {active.get(i, 0)}"
                  + (f" ; désactivés : {sum(1 for s in mine if (s['fichier'], s['titre']) in disabled)}"
                     if any((s["fichier"], s["titre"]) in disabled for s in mine) else ""),
                  f"- Sources : {', '.join(f['path'].rsplit('/', 1)[-1] for f in spec['fichiers'][:4])}"
                  + (" …" if len(spec["fichiers"]) > 4 else ""),
                  f"- Inspiré du Standard : {' ; '.join(str(x) for x in result.get('inspire_de') or []) or '—'}",
                  f"- Steps : {sum(s['steps_std'] for s in mine)} Standard réutilisé(s), "
                  f"{sum(s['steps_client'] for s in mine)} client, {sum(s['steps_non_definis'] for s in mine)} non "
                  f"défini(s) ; classes Java : {', '.join(c.rsplit('/', 1)[-1] for c in result.get('classes_java') or []) or '—'}",
                  f"- Endpoints : {', '.join(dict.fromkeys(e for s in mine for e in s['endpoints'])) or '—'}",
                  f"- Compilation (lot {lot.get('numero', '?')}) : {_compilation_status(lot)}"]
        executed = [s for s in ((lot.get("execution") or {}).get("scenarios") or {}).values()
                    if s["id"].split("::", 1)[0] in {m["fichier"] for m in mine}]
        if executed:
            statuses = {}
            for s in executed:
                statuses[s["statut"]] = statuses.get(s["statut"], 0) + 1
            lines.append("- Vérification par exécution : " + ", ".join(f"{n} {k}" for k, n in statuses.items()))
        if i in coverage:
            rate = coverage[i]
            lines.append(f"- Code spécifique exécuté (JaCoCo) : {rate['executees']}/{rate['classes']} classe(s)"
                         + (" — ⚠️ non exercé" if not rate["executees"] else ""))
        lines += ["",
                  "| Scénario | Différence avec le Standard | Source |", "|---|---|---|"]
        for declared in result.get("scenarios") or []:
            obj = declared if isinstance(declared, dict) else {"nom": str(declared)}
            lines.append(f"| {obj.get('nom', '—')} | {obj.get('difference_standard') or '—'} | "
                         f"{obj.get('source') or '—'} |")
        lines.append("")
    if indirect:
        lines += ["## Spécifiques couverts indirectement", ""]
        for i in indirect:
            target = results[i].get("couvert_par", "")
            lines.append(f"- {specs.get(i, {}).get('libelle', i)} : par {specs.get(target, {}).get('libelle', target)}"
                         + (f" — {results[i]['raison']}" if results[i].get("raison") else ""))
        lines.append("")
    lines += ["## Spécifiques non couverts", ""]
    not_covered = [i for i in selected if i not in covered and i not in indirect]
    for i in not_covered:
        result = results.get(i)
        reason = result.get("raison", "") if result else "lot non terminé"
        if i in emptied:
            reason = "aucun scénario actif : tous désactivés (voir A_REVOIR.md)"
        if result and result.get("requalification"):
            reason = f"{reason} (requalifié : {result['requalification']})"
        lines.append(f"- {specs.get(i, {}).get('libelle', i)} : {reason or 'raison non précisée'}")
    if not not_covered:
        lines.append("Aucun.")
    if deleted:
        lines += ["", "## Scénarios supprimés à la revue", ""]
        lines += [f"- {d['fichier']} / {d['titre']} : {' ; '.join(d['motifs'])}"
                  + (f" — {d['justification']}" if d.get("justification") else "") for d in deleted]
    noted = [(i, c) for i in selected for c in (results.get(i) or {}).get("constats") or []]
    if noted:
        lines += ["", "## Constats des lots", "",
                  "Signalés par l'agent : donnée absente qu'il n'a pas pu créer, comportement du code différent de la "
                  "spécification (le test suit le code), scénario écarté car il ne vérifiait que sa propre mise en "
                  "place.", ""]
        lines += [f"- {specs.get(i, {}).get('libelle', i)} — {constat_line(c)}" for i, c in noted]
    classes = list(dict.fromkeys(c for i in covered for c in results[i].get("classes_java") or []))
    if classes:
        lines += ["", "## Classes Java écrites", ""] + [f"- {c}" for c in classes]
    unchecked = [lot for lot in state.get("lots", []) if lot.get("statut") == "erreur_compilation"
                 or (lot.get("verification") or {}).get("environnement")]
    if unchecked:
        lines += ["", "## Compilation", ""]
        for lot in unchecked:
            check = lot.get("verification") or {}
            label = ("non vérifié (environnement Maven)" if check.get("environnement")
                     else f"en erreur ({check.get('etape', 'compilation')}), commité tel quel : « Reprendre » relance "
                          f"la correction")
            lines.append(f"- Lot {lot['numero']} : {label}")
            lines += [f"  - `{e}`" for e in (check.get("erreurs") or [])[:10]]
    lines += db_usage_lines(state)
    lines += runner_report(worktree_dir(client_name), state.get("base", ""), state.get("runner") or {})
    already = [s for s in specs.values() if s.get("couverture") == "deja_couvert" and s["id"] not in selected]
    if already:
        lines += ["", "## Déjà couverts (non traités)", ""]
        lines += [f"- {s['libelle']} : {' ; '.join(s.get('scenarios_existants', [])) or '—'}" for s in already]
    path = coverage_path(client_name)
    _write_text(path, "\n".join(lines) + "\n")
    return path
