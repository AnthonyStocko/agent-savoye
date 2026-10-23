"""
Initializers serveur du client (0 token) : classes exécutées au démarrage de la plateforme (interface d'initialisation
avec initialize()) qui créent des données hors de toute feature d'init. Un clear peut les effacer sans qu'elles
soient recréées : un test qui s'y fie échoue alors sur une « donnée de référence absente ».

Détection limitée aux dépôts qui portent au moins un spécifique de l'inventaire (liste d'exclusion
CUCUMBER_INITIALIZER_EXCLUDE_REPOS), sources src/main/java, classes identiques au Standard écartées. Motif générique
(DEFAULT_INITIALIZER_PATTERN), surchargeable par CUCUMBER_INITIALIZER_PATTERN.

Extraction : créations reconnues dans la classe (initByNaturalId, existOrCreate…, new Entité(CODE), valeurs
d'énumération dynamiques), code résolu par les constantes du dépôt client ou du Standard. Les appels de service
susceptibles de créer des données sont listés en « partiel », jamais devinés.
"""
import os
import re

from tools.cucumber_tools import (CLIENT_REFS, STD_REFS, _git, _globs, _java_methods, _mask_java, _matches,
                                  _resolve, _string_constants, blob_texts, git_repos, ls_tree, pick_ref)

DEFAULT_INITIALIZER_PATTERN = (r"\bclass\s+\w*Initiali[sz]er\w*|"
                               r"\bimplements\s+[^{]*?\b\w*Initiali[sz]er\b")
STATUSES = {"recreee": "recréée après un clear", "survit": "survit au clear", "inconnu": "inconnu"}
_INIT_BY_ID = re.compile(r"\b(\w+)\s*\.\s*initByNaturalId\s*\(")
_EXIST_OR_CREATE = re.compile(r"\b(\w+)\s*\.\s*(existOrCreate\w*)\s*\(")
_NEW = re.compile(r"\bnew\s+([A-Z]\w*)\s*\(")
_CALL = re.compile(r"\b([a-z]\w*)\s*\.\s*(\w+)\s*\(")
_TYPED_NAME = re.compile(r"\b([A-Z]\w*)(?:<[^<>()]*>)?\s+([a-z]\w*)\s*[;,)=]")
_CODE_ARGUMENT = re.compile(r'"(?:[^"\\]|\\.)*"|(?:[A-Z]\w*\.)?[A-Z][A-Z0-9_]+')
# Classes outillage, jamais une donnée créée
_NOT_ENTITY = re.compile(r"^(String\w*|Array\w*|\w*List|\w*Map|\w*Set|Optional|Big\w+|Date\w*|Local\w+|Object|"
                         r"Integer|Long|Boolean|Double|Float|Thread|File\w*|Path|URI|URL|\w*Exception|\w*Error|"
                         r"\w*Builder)$")
# Services d'infrastructure dont les appels ne créent aucune donnée par eux-mêmes
_PLUMBING_SERVICE = re.compile(r"^(Transaction|Log\w*|Cache|Scheduler|Executor)\w*Service$")
MAX_INITIALIZERS = 200


def initializer_pattern():
    return re.compile(os.getenv("CUCUMBER_INITIALIZER_PATTERN", "").strip() or DEFAULT_INITIALIZER_PATTERN)


def _arguments(masked: str, text: str, open_index: int) -> list:
    """Arguments (texte d'origine) de l'appel dont la parenthèse ouvrante est en `open_index`."""
    args, level, start = [], 0, open_index + 1
    for i in range(open_index, len(masked)):
        if masked[i] in "([{":
            level += 1
        elif masked[i] in ")]}":
            level -= 1
            if level == 0:
                args.append(text[start:i].strip())
                return [a for a in args if a]
        elif masked[i] == "," and level == 1:
            args.append(text[start:i].strip())
            start = i + 1
    return [a for a in args if a]


def _code_argument(args: list) -> str:
    """Premier argument littéral ou constante (le code de la donnée créée), '' sinon."""
    return next((a for a in args if _CODE_ARGUMENT.fullmatch(a)), "")


def _entity_of(type_name: str) -> str:
    return re.sub(r"(Repository|Repo|Dao)$", "", type_name)


def initialize_scope(text: str) -> list:
    """
    (texte, masque) des méthodes exécutées par initialize() : elle-même et les méthodes de la classe qu'elle appelle,
    de proche en proche (appel direct, this.m(), this::m). Sans méthode initialize, la classe entière.
    """
    methods = {}
    for method in _java_methods(text):
        methods.setdefault(method["name"], []).append(method)
    if "initialize" not in methods:
        return [(text, _mask_java(text))]
    reached, pending = set(), ["initialize"]
    while pending:
        name = pending.pop()
        if name in reached or name not in methods:
            continue
        reached.add(name)
        for method in methods[name]:
            pending += [a or b for a, b in re.findall(r"(?:(?<![.\w])|\bthis\s*\.\s*)(\w+)\s*\(|\bthis\s*::\s*(\w+)",
                                                       method["masked"])]
    return [(m["body"], m["masked"]) for name in sorted(reached) for m in methods[name]]


def extract_creations(text: str) -> list:
    """
    Créations d'une classe d'initializer, dans les méthodes exécutées par initialize() : [{entite, expression,
    mode, certitude}] ; expression = argument code (littéral ou constante, à résoudre), à défaut premier argument
    (certitude « partiel ») ; '' pour un appel de service (« partiel »).
    """
    types = {name: kind for kind, name in _TYPED_NAME.findall(_mask_java(text))}
    creations, seen = [], set()

    def add(entity, expression, mode, certainty):
        key = (entity, expression, mode)
        if key not in seen:
            seen.add(key)
            creations.append({"entite": entity, "expression": expression, "mode": mode, "certitude": certainty})

    for body, masked in initialize_scope(text):
        for regex, mode in ((_INIT_BY_ID, "initByNaturalId"), (_EXIST_OR_CREATE, "")):
            for match in regex.finditer(masked):
                args = _arguments(masked, body, match.end() - 1)
                code = _code_argument(args)
                add(_entity_of(types.get(match.group(1), "")), code or (args[0] if args else ""),
                    mode or match.group(2), "certain" if code else "partiel")
        for match in _NEW.finditer(masked):
            if _NOT_ENTITY.match(match.group(1)):
                continue
            code = _code_argument(_arguments(masked, body, match.end() - 1))
            if code:
                add(match.group(1), code, "new", "certain")
        for match in _CALL.finditer(masked):
            kind = types.get(match.group(1), "")
            if kind.endswith("Service") and not _PLUMBING_SERVICE.match(kind):
                add("", "", f"appel de service {kind}.{match.group(2)}", "partiel")
    return creations


class _ConstantResolver:
    """
    Valeur des constantes String (NOM ou Classe.NOM) : classe lue dans l'expression ou les imports statiques de
    l'initializer, trouvée par nom de fichier dans le dépôt client puis les dépôts Standard ; à défaut, recherche du
    nom dans le dépôt client seul. Mis en cache.
    """

    def __init__(self, repos: list):
        self.repos, self.indexes, self.cache = repos, {}, {}

    def _index(self, position: int) -> dict:
        """{Classe: [chemins]} des fichiers .java d'un dépôt (calculé à la première demande)."""
        if position not in self.indexes:
            repo_dir, ref = self.repos[position]
            index = {}
            for path in _git(repo_dir, "ls-tree", "-r", "--name-only", ref).stdout.splitlines():
                if path.endswith(".java"):
                    index.setdefault(path.rsplit("/", 1)[-1][:-5], []).append(path)
            self.indexes[position] = index
        return self.indexes[position]

    @staticmethod
    def _read(repo_dir: str, ref: str, path: str, name: str) -> str:
        constants = _string_constants(_git(repo_dir, "show", f"{ref}:{path}").stdout)
        return (_resolve(constants[name], constants, {}) or "") if name in constants else ""

    def _lookup(self, cls: str, name: str) -> str:
        if cls:
            for position, (repo_dir, ref) in enumerate(self.repos):
                for path in self._index(position).get(cls, [])[:3]:
                    value = self._read(repo_dir, ref, path, name)
                    if value:
                        return value
        repo_dir, ref = self.repos[0]
        out = _git(repo_dir, "grep", "-l", "-E", f"String[[:space:]]+{name}[[:space:]]*=", ref, "--",
                   "*.java").stdout.split()
        for path in [p[len(ref) + 1:] if p.startswith(f"{ref}:") else p for p in out][:3]:
            value = self._read(repo_dir, ref, path, name)
            if value:
                return value
        return ""

    def value(self, expression: str, static_imports: dict = None) -> str:
        """Valeur d'un littéral ou d'une constante ; `static_imports` : {NOM: Classe} de l'initializer."""
        if expression.startswith('"'):
            return expression[1:-1]
        cls, _, name = expression.rpartition(".")
        cls = cls.rsplit(".", 1)[-1] or (static_imports or {}).get(name, "")
        if (cls, name) not in self.cache:
            self.cache[(cls, name)] = self._lookup(cls, name)
        return self.cache[(cls, name)]


_STATIC_IMPORT = re.compile(r"^\s*import\s+static\s+[\w.]*?\.([A-Z]\w*)\.([A-Z][A-Z0-9_]*)\s*;", re.M)


def static_imports(text: str) -> dict:
    """{CONSTANTE: Classe} des imports statiques d'un fichier Java."""
    return {name: cls for cls, name in _STATIC_IMPORT.findall(text)}


def find_initializers(inventory: dict) -> list:
    """
    Initializers des dépôts du client qui portent au moins un spécifique de l'inventaire :
    [{depot, chemin, classe, creations: [{entite, code, expression, mode, certitude}]}]. Code non résolu : vide,
    l'expression reste et la certitude passe à « partiel ».
    """
    spec_repos = {s.get("depot") for s in inventory.get("specifiques") or []}
    excluded = _globs("CUCUMBER_INITIALIZER_EXCLUDE_REPOS", "")
    client_repos = git_repos(inventory.get("depot_client", ""))
    std_repos = git_repos(inventory.get("standard", ""))
    std_refs = {name: pick_ref(path, STD_REFS) for name, path in std_repos.items()}
    pattern = initializer_pattern()
    found = []
    for repo in sorted(r for r in spec_repos if r in client_repos and not _matches(r, excluded)):
        repo_dir = client_repos[repo]
        ref = (inventory.get("references") or {}).get(repo, {}).get("ref") or pick_ref(repo_dir, CLIENT_REFS)
        if not ref:
            continue
        out = _git(repo_dir, "grep", "-l", "-E", "Initiali[sz]er", ref, "--", "*.java").stdout.splitlines()
        paths = [p[len(ref) + 1:] if p.startswith(f"{ref}:") else p for p in out]
        paths = [p for p in paths if "src/main/java/" in f"/{p}"]
        if not paths:
            continue
        tree = ls_tree(repo_dir, ref)
        std_tree = ls_tree(std_repos[repo], std_refs[repo]) if std_refs.get(repo) else {}
        paths = [p for p in paths if p in tree and std_tree.get(p) != tree[p]]
        texts = blob_texts(repo_dir, [tree[p] for p in paths])
        resolver = _ConstantResolver([(repo_dir, ref)] + [(std_repos[n], r) for n, r in std_refs.items() if r])
        for path in paths:
            text = texts.get(tree[path], "")
            if not pattern.search(_mask_java(text)):
                continue
            creations, imports = [], static_imports(text)
            for creation in extract_creations(text):
                resolvable = _CODE_ARGUMENT.fullmatch(creation["expression"] or "")
                code = resolver.value(creation["expression"], imports) if resolvable else ""
                certainty = creation["certitude"] if code or not creation["expression"] else "partiel"
                creations.append({**creation, "code": code, "certitude": certainty})
            found.append({"depot": repo, "chemin": path, "classe": path.rsplit("/", 1)[-1][:-5],
                          "creations": creations})
            if len(found) >= MAX_INITIALIZERS:
                return found
    return found


def data_key(item: dict) -> str:
    return "|".join((item.get("classe", ""), item.get("entite", ""), item.get("code") or item.get("expression", ""),
                     item.get("mode", "")))


def initialized_data(initializers: list) -> list:
    """Données créées par les initializers, à plat, statut « inconnu » : champ server_initialized_data du profil."""
    return [{"depot": i["depot"], "classe": i["classe"], "entite": c["entite"], "code": c["code"],
             "expression": c["expression"], "mode": c["mode"], "certitude": c["certitude"], "statut": "inconnu"}
            for i in initializers for c in i["creations"]]


def merge_statuses(detected: list, chosen: list) -> list:
    """
    Données détectées avec le statut choisi (saisie ou dépôt) de même clé ; données ajoutées à la main conservées.
    """
    by_key = {data_key(c): c for c in chosen or [] if isinstance(c, dict)}
    merged = [{**d, "statut": by_key[data_key(d)].get("statut", d["statut"])} if data_key(d) in by_key else d
              for d in detected or []]
    known = {data_key(d) for d in detected or []}
    return merged + [c for k, c in by_key.items() if k not in known]


def usable(item: dict, survive_clear: str) -> bool:
    """Donnée présente après un clear : recréée, conservée, ou statut inconnu avec initializers déclarés survivants."""
    return item.get("statut") in ("recreee", "survit") or (item.get("statut") == "inconnu" and survive_clear == "oui")


_REFERENCE_CODE = re.compile(r"avec\s+(?:le\s+)?code\s*:?\s*[\"'«]?\s*([\w.-]+)", re.I)


def reference_hint(message: str, data: list) -> str:
    """
    Indice pour un échec « donnée de référence absente » dont le code est créé par un initializer : il ne survit
    sans doute pas au clear. '' si le code n'en vient pas.
    """
    match = _REFERENCE_CODE.search(message or "")
    if not match:
        return ""
    item = next((d for d in data or [] if d.get("code") and d["code"] == match.group(1)), None)
    if not item:
        return ""
    return (f"code {item['code']} créé par l'initializer {item['classe']} ({item['depot']}) : il ne survit sans doute "
            f"pas au clear — statut à revoir dans le profil client (ni recréé ni conservé : le créer dans le test)")
