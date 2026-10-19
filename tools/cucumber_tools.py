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
from datetime import datetime

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
    """Copie, à la version de référence, le contenu des fichiers spécifiques dans .tmp/cucumber/<client>/sources/."""
    root = os.path.join(cache_dir(inventory["client"]), "sources")
    repos = git_repos(inventory["depot_client"])
    missing = {}
    for spec in inventory["specifiques"]:
        for f in spec["fichiers"]:
            f["local"] = os.path.join(root, f["repo"], *f["path"].split("/"))
            if not os.path.exists(f["local"]):
                missing.setdefault(f["repo"], []).append(f)
    for repo, entries in missing.items():
        texts = blob_texts(repos[repo], [f["blob"] for f in entries])
        for f in entries:
            _write_text(f["local"], texts.get(f["blob"], ""))


# ---------------------------------------------------------------------------
# Catalogues Cucumber (Standard et client)
# ---------------------------------------------------------------------------

def extract_cucumber(repo_dir: str, ref: str) -> dict:
    """Steps (expression, fichier:ligne) et features (titre, scénarios, tags) de `ref`, par git grep."""
    steps = []
    out = _git(repo_dir, "grep", "-nE", STEP_PATTERN, ref, "--", "*.java", "*.kt").stdout
    for line in out.splitlines():
        line = line[len(ref) + 1:] if line.startswith(f"{ref}:") else line
        parts = line.split(":", 2)
        if len(parts) == 3:
            expr = re.search(r'\(\s*"((?:[^"\\]|\\.)*)"', parts[2])
            if expr:
                steps.append({"expr": expr.group(1), "file": parts[0], "line": int(parts[1])})
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
    return {"ref": ref, "commit": rev_parse(repo_dir, ref), "steps": steps, "features": list(features.values())}


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
    if meta.get("head") == head and all(os.path.exists(p) for p in paths.values()):
        return meta
    catalogue = extract_cucumber(cucumber_std_dir, "HEAD")
    steps, features = catalogue["steps"], catalogue["features"]
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
        "Cherche-y les steps par préfixe ou mot-clé (Grep), avant d'en créer un nouveau.", "",
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
    meta = {"head": head, "version": odatio_version, "examples": examples, "nb_steps": len(steps),
            "nb_features": len(features), **paths}
    _write_json(meta_file, meta)
    return meta


def client_coverage(client_name: str, cucumber_repo_dir: str, ref: str) -> dict:
    """Catalogue du dépôt Cucumber client (couverture_existante.md)."""
    catalogue = extract_cucumber(cucumber_repo_dir, ref)
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


def prepare(client_name: str, client_root: str, std_root: str, cucumber_std_dir: str, odatio_version: str,
            cucumber_repo_dir: str, cucumber_ref: str, tickets: list) -> dict:
    """Pré-calculs complets (0 token) ; écrit inventaire.json et renvoie l'inventaire."""
    inventory = build_inventory(client_name, client_root, std_root)
    export_sources(inventory)
    inventory["catalogue_standard"] = standard_catalogue(cucumber_std_dir, odatio_version)
    coverage = client_coverage(client_name, cucumber_repo_dir, cucumber_ref)
    inventory["couverture_existante"] = coverage["fichier"]
    match_coverage(inventory, coverage)
    jira_extracts(inventory, tickets)
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
    `base` (nouvelle génération) ; sinon réutilise le worktree existant (reprise). Lève RuntimeError.
    """
    path, branch = worktree_dir(client_name), f"cucumber_{client_name}_coverage"
    if os.path.isdir(path) and not reset:
        return path
    if os.path.isdir(path):
        _git(cucumber_repo_dir, "worktree", "remove", "--force", path)
        shutil.rmtree(path, ignore_errors=True)
    _git(cucumber_repo_dir, "worktree", "prune")
    result = _git(cucumber_repo_dir, "worktree", "add", "-B", branch, path, base)
    if result.returncode != 0:
        if "already" in result.stderr or "used by worktree" in result.stderr:
            raise RuntimeError(f"La branche {branch} est encore extraite dans un autre worktree (génération "
                               f"précédente) : récupérez son contenu si besoin, puis supprimez-le avec "
                               f"« git -C \"{cucumber_repo_dir}\" worktree remove --force <chemin> ». "
                               f"Détail : {result.stderr.strip()[:300]}")
        raise RuntimeError(f"Création du worktree impossible : {result.stderr.strip()[:500]}")
    return path


def commit_lot(worktree: str, client_name: str, lot_number: int) -> bool:
    """Commite les fichiers écrits par le lot. Renvoie True si un commit a été créé."""
    _git(worktree, "add", "-A")
    if _git(worktree, "diff", "--cached", "--quiet").returncode == 0:
        return False
    return _git(worktree, "commit", "-m", f"Cucumber coverage of {client_name} specifics - lot {lot_number}"
                ).returncode == 0


def write_patch(worktree: str, base: str, output: str) -> bool:
    """Patch de tous les commits de la branche depuis `base`. Renvoie False s'il n'y en a aucun."""
    result = _git(worktree, "format-patch", f"{base}..HEAD", "--stdout")
    if result.returncode != 0 or not result.stdout.strip():
        return False
    with open(output, "w", encoding="utf-8", newline="\n") as f:
        f.write(result.stdout)
    return True


# ---------------------------------------------------------------------------
# Lots
# ---------------------------------------------------------------------------

def make_lots(spec_ids: list, batch_size: int) -> list:
    size = max(1, batch_size)
    return [{"numero": i + 1, "ids": spec_ids[i * size:(i + 1) * size], "statut": "a_faire"}
            for i in range(math.ceil(len(spec_ids) / size))]


def lot_block(specifics: list) -> str:
    """Partie variable du prompt : les spécifiques du lot."""
    lines = []
    for spec in specifics:
        lines += [f"### Spécifique {spec['id']} — {spec['libelle']}",
                  f"- Type : {spec['type']} ({spec['nature']}) ; couverture actuelle : {spec.get('couverture', '?')}",
                  f"- Mots-clés : {', '.join(spec['mots_cles']) or '(aucun)'}"]
        if spec.get("scenarios_existants"):
            lines.append("- Scénarios existants à compléter plutôt que dupliquer : "
                         + " ; ".join(spec["scenarios_existants"]))
        lines.append(f"- Tickets Jira : '{spec['jira']}'" if spec.get("jira") else "- Tickets Jira : aucun trouvé")
        lines.append(f"- Fichiers ({spec['nb_fichiers']}, contenu à la version de référence) :")
        for f in spec["fichiers"]:
            detail = f"  - [{f['kind']}] '{f.get('local', f['path'])}'"
            if f.get("diff"):
                elements = ", ".join(f["diff"]["elements"]) or "—"
                detail += f" — écart avec {f.get('standard', 'le Standard')} : {f['diff']['stat']} ; modifié : {elements}"
            lines.append(detail)
        if spec["nb_fichiers"] > len(spec["fichiers"]):
            lines.append(f"  - … {spec['nb_fichiers'] - len(spec['fichiers'])} autre(s) fichier(s) non listé(s)")
        lines.append("")
    return "\n".join(lines).rstrip()


def parse_lot_result(text: str):
    """Bloc JSON final de l'agent (liste d'objets par spécifique), ou None s'il est absent ou invalide."""
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
            return data
    return None


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


def write_coverage_summary(client_name: str, inventory: dict, state: dict) -> str:
    """Récapitulatif <client>-cucumber-couverture.md, produit à partir de l'inventaire et des résultats des lots."""
    specs = {s["id"]: s for s in inventory.get("specifiques", [])}
    results = {r["id"]: r for lot in state.get("lots", []) for r in lot.get("resultats", []) or []}
    selected = [i for lot in state.get("lots", []) for i in lot["ids"]]
    covered = [i for i in selected if results.get(i, {}).get("statut") == "couvert"]
    lines = [f"# Couverture Cucumber des spécifiques {client_name}", "",
             f"{len(specs)} spécifique(s) inventorié(s), {len(selected)} traité(s), {len(covered)} couvert(s). "
             f"Généré le {datetime.now().strftime('%d/%m/%Y %H:%M')}.", "",
             "## Spécifiques couverts", "", "| Spécifique | Fichier(s) source | Scénario(s) | Nouveau/Adapté |",
             "|---|---|---|---|"]
    for i in covered:
        spec, result = specs.get(i, {"libelle": i, "fichiers": [], "couverture": ""}), results[i]
        sources = ", ".join(f["path"].rsplit("/", 1)[-1] for f in spec["fichiers"][:4])
        sources += " …" if len(spec["fichiers"]) > 4 else ""
        scenarios = "<br>".join(result.get("scenarios") or []) or "—"
        lines.append(f"| {spec['libelle']} | {sources} | {scenarios} | "
                     f"{'Adapté' if spec.get('couverture') == 'partiel' else 'Nouveau'} |")
    lines += ["", "## Spécifiques non couverts", ""]
    not_covered = [i for i in selected if i not in covered]
    for i in not_covered:
        result = results.get(i)
        reason = result.get("raison", "") if result else "lot non terminé"
        lines.append(f"- {specs.get(i, {}).get('libelle', i)} : {reason or 'raison non précisée'}")
    if not not_covered:
        lines.append("Aucun.")
    already = [s for s in specs.values() if s.get("couverture") == "deja_couvert" and s["id"] not in selected]
    if already:
        lines += ["", "## Déjà couverts (non traités)", ""]
        lines += [f"- {s['libelle']} : {' ; '.join(s.get('scenarios_existants', [])) or '—'}" for s in already]
    path = coverage_path(client_name)
    _write_text(path, "\n".join(lines) + "\n")
    return path
