"""
Version du Standard propre à un client (0 token), détectée dès que son projet est renseigné.

Sources, dans l'ordre :
  a. code de son application : pom.xml de chaque dépôt du projet client, lu sur origin/integration (parent, BOM
     importé dans dependencyManagement, dépendances, propriétés référencées) ; seuls comptent les artefacts du
     Standard, reconnus à leurs groupIds, déduits des pom des dépôts du Standard configuré (jamais en dur) ;
  b. derniers builds Jenkins du job de vérification dont le dépôt Cucumber est celui du client : paramètres
     CUCUMBER_STANDARD_BRANCH, KIS_VERSION, COMPONENT_VERSION ;
  c. pom.xml de son dépôt Cucumber : version de la bibliothèque Cucumber (repli seulement : elle peut être plus
     ancienne que le serveur sans que ce soit un conflit, voir cucumber_versions).
Normalisation : 26.22, 26.22.3, 26.22-SNAPSHOT, v26.22, release/26.22 → « 26.22 », version complète gardée.
Résultat : « version_standard » du profil du client (server_version détecté) ; ODATIO_VERSION du .env n'est plus
qu'un repli. Sources a et b divergentes : conflit, l'utilisateur choisit (saisie du profil) ; aucune : saisie.
"""
import re
import xml.etree.ElementTree as ET

from tools.cucumber_tools import _git, git_repos

JENKINS_PARAMS = ("CUCUMBER_STANDARD_BRANCH", "KIS_VERSION", "COMPONENT_VERSION")
CLIENT_REF = "origin/integration"
_VERSION = re.compile(r"(?<![\d.])(\d{2})\.(\d{1,3})((?:\.\d+)*)")
_PROPERTY = re.compile(r"\$\{([^}]+)\}")


def normalize(raw: str) -> tuple:
    """(« majeure.mineure », version complète) ; ('', '') si `raw` n'est pas une version (ex. development)."""
    text = (raw or "").strip()
    match = _VERSION.search(text)
    if not match:
        return "", ""
    full = match.group(0) + ("-SNAPSHOT" if "SNAPSHOT" in text.upper() else "")
    return f"{match.group(1)}.{match.group(2)}", full


# ---------------------------------------------------------------------------
# pom.xml
# ---------------------------------------------------------------------------

def _tag(node) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _child(node, name: str):
    return next((n for n in node if _tag(n) == name), None) if node is not None else None


def _text(node, name: str) -> str:
    found = _child(node, name)
    return (found.text or "").strip() if found is not None else ""


def _parse(text: str):
    try:
        return ET.fromstring(text or "")
    except ET.ParseError:
        return None


def pom_group_id(text: str) -> str:
    """groupId propre d'un pom (à défaut celui de son parent)."""
    root = _parse(text)
    if root is None:
        return ""
    return _text(root, "groupId") or _text(_child(root, "parent"), "groupId")


def std_group_ids(std_root: str, ref_candidates=("origin/development", "origin/dev", "origin/HEAD", "HEAD")) -> set:
    """groupIds du Standard : ceux des pom racine des dépôts du projet Standard configuré."""
    out = set()
    for repo in git_repos(std_root).values():
        for ref in ref_candidates:
            shown = _git(repo, "show", f"{ref}:pom.xml")
            if shown.returncode == 0:
                group = pom_group_id(shown.stdout)
                if group:
                    out.add(group)
                break
    return out


def _is_std(group: str, group_ids: set) -> bool:
    return any(group == g or group.startswith(g + ".") for g in group_ids if g)


def pom_versions(text: str, group_ids: set, where: str = "pom.xml") -> list:
    """
    Versions d'artefacts du Standard citées par un pom : [{artefact, version, normalisee, emplacement, source}] où
    emplacement vaut parent, bom, dependance ; une version ${propriété} est résolue par les propriétés du pom
    (la propriété est citée dans la source).
    """
    root = _parse(text)
    if root is None:
        return []
    properties = _child(root, "properties")
    props = {_tag(p): (p.text or "").strip() for p in (properties if properties is not None else [])}

    def resolve(value: str) -> tuple:
        names = _PROPERTY.findall(value or "")
        for _ in range(5):
            if not _PROPERTY.search(value or ""):
                break
            value = _PROPERTY.sub(lambda m: props.get(m.group(1), m.group(0)), value)
        return value, names

    found = []

    def add(node, kind: str) -> None:
        group, artifact = _text(node, "groupId"), _text(node, "artifactId")
        if not _is_std(group, group_ids):
            return
        version, names = resolve(_text(node, "version"))
        short, full = normalize(version)
        if short:
            found.append({"artefact": f"{group}:{artifact}", "version": full, "normalisee": short,
                          "emplacement": kind,
                          "source": where + (f" (propriété {names[0]})" if names else "")})
    parent = _child(root, "parent")
    if parent is not None:
        add(parent, "parent")
    # Racine du pom puis profils Maven (une application déclare souvent ses composants Standard dans un profil)
    profiles = _child(root, "profiles")
    for scope in [root] + list(profiles if profiles is not None else []):
        management = _child(_child(scope, "dependencyManagement"), "dependencies")
        for dep in management if management is not None else []:
            add(dep, "bom" if _text(dep, "scope") == "import" else "dependance")
        deps = _child(scope, "dependencies")
        for dep in deps if deps is not None else []:
            add(dep, "dependance")
    return found


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

_ORDER = {"parent": 0, "bom": 1, "dependance": 2}


def code_source(client_root: str, group_ids: set, ref: str = CLIENT_REF) -> dict:
    """
    Source a : versions du Standard des pom de l'application du client (dépôts hors Cucumber), lus à `ref`.
    {version, complete, detail, candidats} ; version la plus citée (parent, puis BOM, puis dépendance) ; {} sinon.
    """
    if not group_ids:
        return {}
    found = []
    for name, repo in git_repos(client_root).items():
        if "cucumber" in name.lower():
            continue
        shown = _git(repo, "show", f"{ref}:pom.xml")
        if shown.returncode == 0:
            found += pom_versions(shown.stdout, group_ids, f"{name}/pom.xml")
    if not found:
        return {}
    counts = {}
    for item in found:
        counts[item["normalisee"]] = counts.get(item["normalisee"], 0) + 1
    best = max(counts, key=lambda v: (counts[v], v))
    item = min((f for f in found if f["normalisee"] == best), key=lambda f: _ORDER[f["emplacement"]])
    return {"source": "code", "version": best, "complete": item["version"],
            "detail": f"{item['source']}, {item['artefact'].split(':', 1)[-1]} {item['version']}",
            "candidats": sorted(counts)}


def jenkins_source(builds: list, cucumber_repo: str) -> dict:
    """
    Source b : premier build (du plus récent au plus ancien) dont un paramètre désigne le dépôt Cucumber du client
    et dont CUCUMBER_STANDARD_BRANCH, KIS_VERSION ou COMPONENT_VERSION est une version. `builds` :
    [{numero, url, parametres: {nom: valeur}}]. {} sinon.
    """
    repo = (cucumber_repo or "").lower()
    for build in builds or []:
        params = build.get("parametres") or {}
        if repo and not any(repo in str(v).lower() for v in params.values()):
            continue
        for name in JENKINS_PARAMS:
            short, full = normalize(str(params.get(name) or ""))
            if short:
                return {"source": "jenkins", "version": short, "complete": str(params[name]).strip(),
                        "detail": f"build Jenkins n° {build.get('numero')}, {name} {params[name]}"}
    return {}


def library_source(cucumber_repo_dir: str, version: str) -> dict:
    """Source c : version de la bibliothèque Cucumber du pom du dépôt Cucumber du client."""
    short, full = normalize(version)
    return {"source": "bibliotheque", "version": short, "complete": full,
            "detail": f"{cucumber_repo_dir.replace(chr(92), '/').rstrip('/').rsplit('/', 1)[-1]}/pom.xml, "
                      f"bibliothèque Cucumber {version}"} if short else {}


_TEST_JAR = re.compile(r"<dependency>(?:(?!</dependency>).)*?<type>test-jar</type>(?:(?!</dependency>).)*?"
                       r"</dependency>", re.S)


def cucumber_library_version(cucumber_repo_dir: str, ref: str = CLIENT_REF) -> str:
    """Version de la bibliothèque Cucumber (dépendance test-jar) du pom du dépôt Cucumber du client, à `ref`."""
    shown = _git(cucumber_repo_dir, "show", f"{ref}:pom.xml") if cucumber_repo_dir else None
    dependency = _TEST_JAR.search(shown.stdout) if shown is not None and shown.returncode == 0 else None
    version = re.search(r"<version>\s*([^<\s]+)\s*</version>", dependency.group(0)) if dependency else None
    return version.group(1) if version else ""


def detect(client_root: str, std_root: str, cucumber_repo_dir: str, cucumber_repo: str, builds: list) -> dict:
    """Détection complète (sources a, b, c) pour un client ; voir resolve."""
    code = code_source(client_root, std_group_ids(std_root))
    jenkins = jenkins_source(builds, cucumber_repo)
    library = library_source(cucumber_repo_dir, cucumber_library_version(cucumber_repo_dir))
    return resolve(code, jenkins, library)


def resolve(code: dict, jenkins: dict, library: dict) -> dict:
    """
    Version retenue : code, sinon Jenkins, sinon bibliothèque. Code et Jenkins divergents : statut « conflit »,
    aucune version retenue, les deux affichées avec leurs sources. Aucune source : « introuvable ».
    """
    sources = [s for s in (code, jenkins, library) if s]
    main = [s for s in (code, jenkins) if s]
    if len({s["version"] for s in main}) > 1:
        return {"statut": "conflit", "version": "", "complete": "", "detail": "", "sources": sources}
    chosen = (main or sources or [None])[0]
    if not chosen:
        return {"statut": "introuvable", "version": "", "complete": "", "detail": "", "sources": []}
    return {"statut": "ok", "version": chosen["version"], "complete": chosen["complete"],
            "detail": chosen["detail"], "source": chosen["source"], "sources": sources}


def line(result: dict, chosen: str = "") -> str:
    """Ligne de « Données du projet »."""
    if chosen:
        return f"Standard {chosen} — choisie dans le profil client"
    if (result or {}).get("statut") == "ok":
        return f"Standard {result['version']} — source : {result['detail']}"
    if (result or {}).get("statut") == "conflit":
        return "Versions du Standard divergentes : " + " ; ".join(
            f"{s['version']} ({s['detail']})" for s in result["sources"]) + " — laquelle retenir ?"
    return "Version du Standard introuvable (code, builds Jenkins, bibliothèque Cucumber) : à saisir"


# ---------------------------------------------------------------------------
# Profil du client
# ---------------------------------------------------------------------------

def save(client_name: str, result: dict) -> dict:
    """Résultat enregistré dans le profil du client (document « version_standard », server_version détecté)."""
    from tools import cucumber_profile
    from tools.cucumber_tools import _write_json
    document = cucumber_profile.load_profile(client_name)
    document["version_standard"] = result
    detected = document.setdefault("detecte", {})
    detected["server_version"] = result.get("version", "") if result.get("statut") == "ok" else ""
    _write_json(cucumber_profile.profile_path(client_name), document)
    return document


def client_version(client_name: str, fallback: str = "") -> tuple:
    """
    (version, origine) à utiliser pour le client : choix saisi dans son profil, sinon version détectée, sinon
    `fallback` (ODATIO_VERSION du .env ou version du pom du Standard).
    """
    from tools import cucumber_profile
    document = cucumber_profile.load_profile(client_name)
    chosen = ((document.get("saisi") or {}).get("server_version") or "").strip()
    if chosen:
        return normalize(chosen)[0] or chosen, "profil client (choix)"
    result = document.get("version_standard") or {}
    if result.get("statut") == "ok":
        return result["version"], f"profil client ({result['detail']})"
    return fallback, "repli global"


def choose(client_name: str, version: str) -> dict:
    """Choix de l'utilisateur (conflit ou saisie) : server_version saisi du profil, les autres saisies gardées."""
    from tools import cucumber_profile
    from tools.cucumber_tools import _write_json
    document = cucumber_profile.load_profile(client_name)
    saisi = document.setdefault("saisi", {})
    if version.strip():
        saisi["server_version"] = normalize(version)[0] or version.strip()
    else:
        saisi.pop("server_version", None)
    _write_json(cucumber_profile.profile_path(client_name), document)
    return document


def display(client_name: str) -> dict:
    """{ligne, statut, choix, versions} pour « Données du projet » ; ligne vide avant la première détection."""
    from tools import cucumber_profile
    document = cucumber_profile.load_profile(client_name)
    result = document.get("version_standard") or {}
    chosen = ((document.get("saisi") or {}).get("server_version") or "").strip()
    if not result and not chosen:
        return {"ligne": "", "statut": "", "choix": "", "versions": []}
    return {"ligne": line(result, chosen), "statut": "choisie" if chosen else result.get("statut", ""),
            "choix": chosen, "versions": list(dict.fromkeys(s["version"] for s in result.get("sources") or []))}


def std_refs(version: str, defaults: tuple) -> tuple:
    """Références d'un dépôt du Standard pour `version` (branches et tags de version), puis `defaults`."""
    if not version or not normalize(version)[0]:
        return tuple(defaults)
    v = normalize(version)[0]
    return (f"origin/release/{v}", f"origin/v{v}", f"origin/{v}", f"v{v}", v, *defaults)
