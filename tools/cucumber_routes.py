"""
Aucune route HTTP sans preuve (règle valable pour tous les clients).

1. Réutilisation d'abord : un step du projet lit ou écrit une entité par les fonctions de la bibliothèque Cucumber de
   la version du pom (classes *EndPoints). Un appel HTTP écrit à la main (sendGetRest, sendPostRest…, request.get…)
   n'est permis que si aucune fonction de la bibliothèque ne couvre le besoin, justifié en commentaire.
2. Preuve : toute route écrite à la main existe, à la version testée, dans les ressources REST du code (annotations
   Restx @GET("/…") ou Spring @GetMapping… des classes du Standard et du client) ou dans une fonction de la
   bibliothèque de même version ; la preuve (fichier, ligne, méthode HTTP) est citée en commentaire :
       // Route : GET /reportdocs — <dépôt>/<chemin>/ReportDocRest.java:95
       // — hors bibliothèque : <raison> (si une fonction de la bibliothèque couvre déjà la route)
3. Contrôle déterministe avant livraison (0 token) : routes des steps du projet extraites et cherchées dans l'index
   des routes REST de la version testée ; route absente = erreur de lot, avec les routes proches existantes.
   L'index d'un dépôt est construit une fois par commit de sa référence de version et mis en cache.
4. Version : le Standard est lu à la référence de sa version (release/<v>, v<v>), le client à la référence de la
   version choisie ; un dépôt sans cette référence est déclaré indisponible, jamais lu sur integration ou dev.
"""
import difflib
import fnmatch
import json
import os
import re

from tools import cucumber_tools as ct
from tools.cucumber_tools import _git, git_repos, rev_parse

INDEX_FORMAT = 1
CACHE_DIR = os.path.join(ct.STD_CACHE_DIR, "routes")
NATURE = "route_sans_preuve"
_REST_FILES = r"@(GET|POST|PUT|DELETE|PATCH)\(|(Get|Post|Put|Delete|Patch|Request)Mapping"
# Dépôts du client sans ressource REST du serveur : tests, écrans
_CLIENT_SKIP = ("*cucumber*", "*-frontend", "*-front", "*-ui", "*-ui-*", "odatio-ui-*")
_PROOF = re.compile(r"Route\s*:\s*(GET|POST|PUT|DELETE|PATCH)\s+(\S+)", re.I)
_CITED_FILE = re.compile(r"([\w$-]+\.java):(\d+)")


# ---------------------------------------------------------------------------
# Index des routes REST (ressources du code) et de la bibliothèque
# ---------------------------------------------------------------------------

def rest_routes(text: str) -> list:
    """Routes exposées par une classe REST : [(MÉTHODE, chemin, ligne)] (Restx @GET("/…"), Spring @GetMapping…)."""
    masked, found = ct._mask_java(text), []
    head = re.search(r'@RequestMapping\s*\(\s*(?:(?:value|path)\s*=\s*)?\{?\s*"([^"]*)"[^)]*\)\s*(?:public\s+)?'
                     r'(?:abstract\s+)?class', text)
    prefix = head.group(1) if head else ""
    for regex, spring in ((ct._REST_ANNOTATION, False), (ct._SPRING_MAPPING, True)):
        for match in regex.finditer(text):
            if masked[match.start()] != "@" or (spring and head and match.start() == head.start()):
                continue
            verb = match.group(1).upper()
            verb = "GET" if verb == "REQUEST" else verb
            found.append((verb, ct._http_path((prefix if spring else "") + "/" + match.group(2)),
                          text.count("\n", 0, match.start()) + 1))
    return found


def _read_cache(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if data.get("format") == INDEX_FORMAT else None
    except (OSError, ValueError):
        return None


def _write_cache(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"format": INDEX_FORMAT, **data}, f, ensure_ascii=False)


def _ref_table(repo_dir: str) -> dict:
    """{nom complet de référence: objet} lu dans .git (packed-refs et références libres), sans lancer git."""
    git_dir = os.path.join(repo_dir, ".git")
    table = {}
    try:
        with open(os.path.join(git_dir, "packed-refs"), encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(" ", 1)
                if len(parts) == 2 and not line.startswith(("#", "^")):
                    table[parts[1]] = parts[0]
    except OSError:
        pass
    refs = os.path.join(git_dir, "refs")
    for folder, _, names in os.walk(refs):
        for name in names:
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8") as f:
                    value = f.read().strip()
            except OSError:
                continue
            if re.fullmatch(r"[0-9a-f]{40}", value):
                table["refs/" + os.path.relpath(path, refs).replace("\\", "/")] = value
    return table


def resolve_ref(repo_dir: str, candidates) -> tuple:
    """(référence, objet) de la première candidate présente (« origin/<b> », « refs/tags/<t> », « <tag> ») ; ('', '')."""
    table = _ref_table(repo_dir)
    for candidate in candidates:
        if candidate.startswith("refs/"):
            names = [candidate]
        elif candidate.startswith("origin/"):
            names = [f"refs/remotes/{candidate}"]
        else:
            names = [f"refs/tags/{candidate}", f"refs/heads/{candidate}"]
        for name in names:
            if name in table:
                return candidate, table[name]
    return "", ""


def repo_routes(repo_dir: str, ref: str, name: str = "", commit: str = "") -> list:
    """Routes REST d'un dépôt à `ref` : [{methode, chemin, preuve}] ; en cache par objet de la référence (immuable)."""
    name = name or os.path.basename(repo_dir.rstrip("\\/"))
    commit = commit or rev_parse(repo_dir, ref)
    if not commit:
        return []
    cache = os.path.join(CACHE_DIR, f"{name}@{commit}.json")
    cached = _read_cache(cache)
    if cached is not None:
        return cached["routes"]
    listed = _git(repo_dir, "grep", "-l", "-E", _REST_FILES, commit, "--", "*.java").stdout.splitlines()
    paths = [line.split(":", 1)[1] for line in listed if ":" in line]
    paths = [p for p in paths if "src/test/" not in f"/{p}"]
    tree = ct.ls_tree(repo_dir, commit) if paths else {}
    texts = ct.blob_texts(repo_dir, [tree[p] for p in paths if p in tree])
    routes = []
    for path in paths:
        for verb, route, line in rest_routes(texts.get(tree.get(path, ""), "")):
            routes.append({"methode": verb, "chemin": route, "preuve": f"{name}/{path}:{line}"})
    _write_cache(cache, {"depot": name, "commit": commit, "routes": routes})
    return routes


def build_index(std_root: str, client_root: str, std_version: str, client_ref: str) -> dict:
    """
    Index des routes REST de la version testée : Standard à la référence de `std_version`, dépôts du client à
    `client_ref` (version choisie). {"routes": [...], "indisponibles": [...], "sources": [...]} ; un dépôt sans la
    référence de la version est déclaré indisponible, jamais lu sur une autre branche.
    """
    from tools.standard_version import std_refs
    routes, missing, sources = [], [], []
    std_candidates = std_refs(std_version, ()) if std_version else ()
    std_repos = git_repos(std_root) if std_root and os.path.isdir(std_root) else {}
    if std_repos and not std_candidates:
        missing.append("Standard : version inconnue, code du Standard non lu")
    for name, path in sorted(std_repos.items()) if std_candidates else ():
        ref, commit = resolve_ref(path, std_candidates)
        if not ref:
            missing.append(f"Standard {name} : aucune référence de la version {std_version}")
            continue
        routes += repo_routes(path, ref, name, commit)
        sources.append(f"{name}@{ref.split('/')[-1]}")
    client_repos = {n: p for n, p in (git_repos(client_root) if client_root and os.path.isdir(client_root)
                                      else {}).items() if not any(fnmatch.fnmatch(n, g) for g in _CLIENT_SKIP)}
    if client_repos and not client_ref:
        missing.append("client : version du client non choisie (« Données du projet ») — code du client non lu, "
                       "jamais lu sur integration")
    for name, path in sorted(client_repos.items()) if client_ref else ():
        ref, commit = resolve_ref(path, (client_ref,))
        if not ref:
            missing.append(f"client {name} : pas de {client_ref} — code de cette version indisponible (integration "
                           f"jamais lue)")
            continue
        routes += repo_routes(path, ref, name, commit)
        sources.append(f"{name}@{client_ref.split('/')[-1]}")
    return {"routes": routes, "indisponibles": missing, "sources": sources,
            "version_standard": std_version, "ref_client": client_ref}


def library_index(cucumber_std_dir: str, library_version: str) -> dict:
    """
    Routes appelées par les fonctions de la bibliothèque de la version du pom (sources du Cucumber Standard à
    v<version>) : {"routes": [{methode, chemin, fonctions}], "ref", "indisponible"} ; en cache par commit.
    """
    from tools.cucumber_versions import library_ref
    ref = library_ref(cucumber_std_dir, library_version) if cucumber_std_dir and os.path.isdir(cucumber_std_dir) else ""
    if not ref:
        return {"routes": [], "ref": "", "version": library_version,
                "indisponible": f"sources de la bibliothèque {library_version or '(version inconnue)'} introuvables"}
    commit = rev_parse(cucumber_std_dir, ref)
    cache = os.path.join(CACHE_DIR, f"bibliotheque@{commit}.json")
    cached = _read_cache(cache)
    if cached is None:
        endpoints, _ = ct.extract_std_endpoints(ct._test_java_files(cucumber_std_dir, commit))
        cached = {"routes": [{"methode": e["methode"], "chemin": e["chemin"], "fonctions": e["java"]}
                             for e in endpoints]}
        _write_cache(cache, cached)
    return {"routes": cached["routes"], "ref": ref, "version": library_version, "indisponible": ""}


def prepare(client: str, std_root: str, client_root: str, std_version: str, client_ref: str,
            cucumber_std_dir: str, library_version: str, log=print) -> dict:
    """Index de la génération (une fois avant les lots) ; écrit .tmp/cucumber/<client>/routes_rest.json."""
    index = build_index(std_root, client_root, std_version, client_ref)
    library = library_index(cucumber_std_dir, library_version)
    context = {"index": index, "bibliotheque": library}
    try:
        _write_cache(os.path.join(ct.cache_dir(client), "routes_rest.json"), context)
    except OSError:
        pass
    log(f"[ROUTES] Index des routes REST de la version testée : {len(index['routes'])} route(s) "
        f"({len(index['sources'])} dépôt(s), Standard {std_version or '?'}"
        + (f", client {client_ref.split('/')[-1]}" if client_ref else "") + f") ; bibliothèque "
        f"{library_version or '?'} : {len(library['routes'])} route(s) dans ses fonctions.")
    for line in index["indisponibles"][:8] + ([library["indisponible"]] if library["indisponible"] else []):
        log(f"[ROUTES] ⚠️ {line}")
    if len(index["indisponibles"]) > 8:
        log(f"[ROUTES] ⚠️ … {len(index['indisponibles']) - 8} autre(s) dépôt(s) indisponible(s)")
    return context


# ---------------------------------------------------------------------------
# Appels HTTP écrits à la main dans les steps du projet
# ---------------------------------------------------------------------------

def _comment_blocks(text: str) -> list:
    """Blocs de lignes de commentaire consécutives : [(première ligne, texte)]."""
    blocks, current, start = [], [], 0
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(("//", "/*", "*")):
            if not current:
                start = number
            current.append(stripped.lstrip("/*").strip())
        elif current:
            blocks.append((start, " ".join(current)))
            current = []
    if current:
        blocks.append((start, " ".join(current)))
    return blocks


def project_calls(worktree: str, java_files: list) -> list:
    """
    Appels HTTP écrits à la main dans les classes du projet : [{fichier, ligne, methode, chemin, preuves}] où
    `preuves` sont les commentaires « Route : MÉTHODE /chemin … » du fichier. Les URL entièrement variables
    (paramètre d'un helper générique) ne sont pas des routes écrites à la main.
    """
    calls = []
    for rel in java_files:
        if not rel.endswith(".java"):
            continue
        text = ct._read_text_file(os.path.join(worktree, *rel.split("/")))
        if not text:
            continue
        masked, constants = ct._mask_java(text), ct._string_constants(text)
        proofs = []
        for line, block in _comment_blocks(text):
            for match in _PROOF.finditer(block):
                proofs.append({"methode": match.group(1).upper(), "chemin": ct._http_path(match.group(2)),
                               "texte": block, "ligne": line})
        for call in ct._HTTP_CALL.finditer(masked):
            before = masked[max(0, call.start() - 120):call.start()]
            if re.search(r"\b(?:public|private|protected|static)\b[\w<>\[\],.?\s]*$", before):
                continue  # déclaration d'une méthode du projet, pas un appel
            verb = (call.group(1) or call.group(2)).upper()
            path = ct._resolve(ct._first_argument(masked, text, call.end() - 1), constants, {})
            if not path:
                continue
            calls.append({"fichier": rel, "ligne": text.count("\n", 0, call.start()) + 1,
                          "methode": "GET" if verb == "COUNT" else verb, "chemin": ct._http_path(path),
                          "preuves": proofs})
    return calls


# ---------------------------------------------------------------------------
# Contrôle
# ---------------------------------------------------------------------------

def same_route(verb: str, path: str, route: dict) -> bool:
    """Même route : même méthode HTTP, mêmes segments (paramètre {…}, :id, {id} = n'importe quel segment)."""
    if verb != route["methode"]:
        return False
    a, b = ct._path_key(path), ct._path_key(route["chemin"])
    if len(a) == len(b) and all(x == y or "*" in (x, y) for x, y in zip(a, b)):
        return True
    return ct._same_path(path, route["chemin"])


def _near(verb: str, path: str, routes: list, count: int = 3) -> list:
    labels = {}
    for route in routes:
        labels.setdefault(f"{route['methode']} {route['chemin']}", route)
    wanted = f"{verb} {path}"
    near = difflib.get_close_matches(wanted, list(labels), n=count * 2, cutoff=0.45)
    near.sort(key=lambda label: (not label.startswith(verb + " "), -difflib.SequenceMatcher(None, wanted, label).ratio()))
    return [f"{label} ({labels[label].get('preuve') or ', '.join(labels[label].get('fonctions', [])[:1])})"
            for label in near[:count]]


def _cites(proof: dict, server: list, functions: list) -> bool:
    """Le commentaire cite une preuve trouvée : fichier:ligne d'une ressource REST, ou fonction de la bibliothèque."""
    for name, line in _CITED_FILE.findall(proof["texte"]):
        if any(r["preuve"].endswith(f"/{name}:{line}") or r["preuve"].endswith(f"{name}:{line}") for r in server):
            return True
    return any(f.split("#")[-1] in proof["texte"] and f.split("#")[0] in proof["texte"] for f in functions)


def check_calls(calls: list, context: dict) -> list:
    """Erreurs de lot des appels écrits à la main (route absente, preuve non citée, bibliothèque non réutilisée)."""
    if not context:
        return []
    index, library = context.get("index") or {}, context.get("bibliotheque") or {}
    server_routes, library_routes = index.get("routes") or [], library.get("routes") or []
    versions = (f"Standard {index.get('version_standard') or '?'}"
                + (f", client {index['ref_client'].split('/')[-1]}" if index.get("ref_client") else ""))
    errors = []
    for call in calls:
        where, route = f"{call['fichier']}:{call['ligne']}", f"{call['methode']} {call['chemin']}"
        server = [r for r in server_routes if same_route(call["methode"], call["chemin"], r)]
        covering = [r for r in library_routes if same_route(call["methode"], call["chemin"], r)]
        functions = sorted({f for r in covering for f in r.get("fonctions") or []})
        if not server and not covering:
            near = _near(call["methode"], call["chemin"], server_routes + library_routes)
            missing = index.get("indisponibles") or []
            errors.append(
                f"{where} route {route} sans preuve : absente des ressources REST de la version testée ({versions}) "
                f"et des fonctions de la bibliothèque {library.get('version') or ''} (« 404 - No route found » au "
                f"run) — utilise une fonction de la bibliothèque, ou une route prouvée"
                + (f" ; routes existantes proches : {' ; '.join(near)}" if near else "")
                + (f" ; code non lu à cette version : {' ; '.join(missing[:3])}" if missing else ""))
            continue
        proofs = [p for p in call["preuves"] if same_route(call["methode"], call["chemin"], p)]
        if functions and not any("biblioth" in p["texte"].lower() for p in proofs):
            errors.append(
                f"{where} route {route} écrite à la main alors que la bibliothèque {library.get('version') or ''} la "
                f"couvre : {', '.join(functions[:3])} — appelle cette fonction, ou justifie l'appel direct dans le "
                f"commentaire de preuve (« // Route : {route} — <fichier>:<ligne> — hors bibliothèque : <raison> »)")
            continue
        if not any(_cites(p, server, functions) for p in proofs):
            cite = server[0]["preuve"] if server else functions[0] if functions else "?"
            errors.append(
                f"{where} route {route} : preuve à citer en commentaire au-dessus de l'appel ou de sa constante "
                f"(fichier, ligne, méthode HTTP) — « // Route : {call['methode']} "
                f"{(server or covering)[0]['chemin']} — {cite} »")
    return errors


def check_lot(worktree: str, files: list, context: dict) -> list:
    """Contrôle des routes des classes Java du projet écrites ou modifiées par le lot."""
    java = [f for f in files if f.endswith(".java") and "src/test/java/" in f"/{f}"
            and os.path.exists(os.path.join(worktree, *f.split("/")))]
    return check_calls(project_calls(worktree, java), context) if java and context else []


# ---------------------------------------------------------------------------
# Échec au run : « 404 - No route found » sur un step du projet
# ---------------------------------------------------------------------------

_NO_ROUTE = re.compile(r"No route found", re.I)
_FRAME = re.compile(r"\bat\s+cucumber\.features\.(\w+)\.")


def project_step_without_route(step: str, text: str, client: str = "") -> bool:
    """
    « 404 - No route found » levé par un step du projet : step préfixé du code client (« CANSON- … ») dont la pile
    passe par une classe du package du client (cucumber.features.<client>), ou client donné par le contexte.
    """
    if not _NO_ROUTE.search(text or ""):
        return False
    step = step or (re.search(r"\bat\s+✽\.(.+?)\(", text or "") or [None, ""])[1]
    prefix = (re.match(r"\s*([A-Za-z][\w]*)\s*-\s", step or "") or [None, ""])[1].lower()
    packages = {p.lower() for p in _FRAME.findall(text or "")}
    if client:
        return ct.java_package_segment(client).lower() in packages or prefix == client.lower()
    return bool(prefix) and prefix in packages


class ProjectRouteFailure:
    """Motif « cherchable » (pattern.search) des échecs connus de la vérification : 404 d'un step du projet."""

    def search(self, text: str) -> bool:
        return project_step_without_route("", text)


FAILURE_HINT = ("route sans preuve : le step du projet appelle une route absente du serveur (404 - No route found) — "
                "appelle la fonction de la bibliothèque de la version du pom qui couvre le besoin ; sinon une route "
                "prouvée dans les ressources REST de la version testée, preuve citée en commentaire (« // Route : "
                "MÉTHODE /chemin — Fichier.java:ligne »). Jamais une erreur HTTP prise pour une absence de donnée")
