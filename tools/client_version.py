"""
Version du client choisie dans « Données du projet » (onglet Cucumber) ; tout le reste en est déduit (0 token).

1. Versions proposées, par client, sans valeur en dur (list_versions) :
   - dépôt de l'application : celui du projet client dont le pom déclare des artefacts du Standard (groupIds du
     Standard, standard_version) ;
   - sa branche d'intégration (version en cours, lue dans son pom), ses branches de release et ses tags (Bitbucket,
     ou clone local), et les versions publiées de l'artefact du client dans le dépôt Maven (rôles « composant_* » du
     job : propriétaire / nom, ex. canson / odatio-app) ;
   - chaque entrée : version du client, référence Git, date ; par défaut la branche d'intégration.
2. Déduction pour la version choisie (deduce) :
   a. version du Standard lue dans le pom de l'application À CETTE référence ;
   b. référence du code du client (même tag ou branche ; à défaut intégration, avec un avertissement) ;
   c. référence du Standard et du Cucumber Standard pour cette version (release/<v>, v<v>, <v>, tags) ; absente du
      Cucumber Standard : version existante la plus proche INFÉRIEURE, avertissement explicite, jamais master ;
   d. bibliothèque Cucumber du dépôt <client>-cucumber : cible = version du Standard ;
   e. paramètres Jenkins des runs de l'agent : version de l'artefact = version du client, branche du Cucumber
      Standard = référence retenue, version KIS = celle enregistrée pour cette version du Standard (dernier build qui
      a démarré une application de cette version, tous clients confondus : résultat SUCCESS ou UNSTABLE), sinon
      question à l'utilisateur ; jamais une valeur recopiée d'un build d'une autre version.
3. Ligne de « Données du projet » (line) et avertissements ; choix et déduction enregistrés dans le profil
   (« version_client ») : ODATIO_VERSION du .env n'est plus utilisé.
"""
import os
import re
import subprocess
from datetime import datetime, timezone

from tools import standard_version as sv
from tools.cucumber_tools import STD_CACHE_DIR, _git, _read_json, _write_json, git_repos

FIELD = "version_client"
INTEGRATION = "integration"
KIS_REGISTRY = os.path.join(STD_CACHE_DIR, "kis_versions.json")
# Builds dont l'application a démarré : tests exécutés (verts ou en échec)
STARTED_RESULTS = ("SUCCESS", "UNSTABLE")
_RELEASE = re.compile(r"^(?:release|releases|rel)[/_-]", re.I)
_CLIENT_VERSION = re.compile(r"\d+(?:\.\d+)+(?:-[\w.]+)?")
# Rôles des paramètres Jenkins (détectés sur leurs noms, jamais en dur) : voir cucumber_job
JENKINS_ROLES = {"composant_version": r"(COMPONENT|ARTIFACT).*VERSION",
                 "standard_branche": r"STANDARD.*BRANCH",
                 "kis_version": r"KIS.*VERSION",
                 "composant_proprietaire": r"(COMPONENT|ARTIFACT).*(OWNER|GROUP)",
                 "composant_nom": r"(COMPONENT|ARTIFACT).*NAME|ARTIFACT_?ID"}


# ---------------------------------------------------------------------------
# Accès aux dépôts : API Bitbucket ou clone local
# ---------------------------------------------------------------------------

class LocalRepo:
    """Dépôt lu dans un clone local (références distantes origin/* et tags)."""

    def __init__(self, path: str, name: str = ""):
        self.path, self.name = path, name or os.path.basename(path)

    def refs(self) -> list:
        out = _git(self.path, "for-each-ref", "--format=%(refname)|%(committerdate:iso-strict)",
                   "refs/remotes/origin", "refs/tags").stdout
        found = []
        for line in out.splitlines():
            ref, _, date = line.partition("|")
            if ref.startswith("refs/tags/"):
                found.append({"type": "tag", "nom": ref[len("refs/tags/"):], "git": ref, "date": date})
            elif not ref.endswith("/HEAD"):
                name = ref[len("refs/remotes/origin/"):]
                found.append({"type": "branche", "nom": name, "git": f"origin/{name}", "date": date})
        return found

    def file(self, ref: dict, path: str) -> str:
        shown = _git(self.path, "show", f"{ref['git']}:{path}")
        return shown.stdout if shown.returncode == 0 else ""


class BitbucketRepo:
    """Dépôt lu par l'API REST Bitbucket (aucun clone)."""

    def __init__(self, base: str, key: str, slug: str, dates: int = 30):
        self.base, self.key, self.name, self.dates = base, key, slug, dates

    def _url(self, tail: str) -> str:
        return f"{self.base}/rest/api/1.0/projects/{self.key}/repos/{self.name}/{tail}"

    def _pages(self, tail: str, params: dict) -> list:
        from tools.standard_version_task import _get
        values, start = [], 0
        while True:
            response = _get(self._url(tail), {**params, "start": start, "limit": 100}, f"{self.name} {tail}")
            if response.status_code != 200:
                return values
            page = response.json()
            values += page.get("values") or []
            if page.get("isLastPage", True) or len(values) >= 500:
                return values
            start = page.get("nextPageStart", start + 100)

    def _date(self, commit: str) -> str:
        from tools.standard_version_task import _get
        response = _get(self._url(f"commits/{commit}"), {}, f"{self.name} commit")
        stamp = response.json().get("committerTimestamp") if response.status_code == 200 else None
        return datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat(timespec="seconds") if stamp else ""

    def refs(self) -> list:
        found = [{"type": "branche", "nom": b["displayId"], "git": f"refs/heads/{b['displayId']}",
                  "commit": b.get("latestCommit", "")}
                 for b in self._pages("branches", {"orderBy": "MODIFICATION"})]
        found += [{"type": "tag", "nom": t["displayId"], "git": f"refs/tags/{t['displayId']}",
                   "commit": t.get("latestCommit", "")}
                  for t in self._pages("tags", {"orderBy": "MODIFICATION"})]
        return found

    def dated(self, refs: list) -> list:
        """Date de commit des `dates` premières références (les autres restent sans date)."""
        for n, ref in enumerate(refs):
            if n < self.dates and ref.get("commit") and not ref.get("date"):
                ref["date"] = self._date(ref["commit"])
        return refs

    def file(self, ref: dict, path: str) -> str:
        from tools.standard_version_task import _get
        response = _get(self._url(f"raw/{path}"), {"at": ref["git"]}, f"{self.name}/{path}")
        return response.text if response.status_code == 200 else ""


def repositories(client_repo: str) -> list:
    """Dépôts du projet client (hors <client>-cucumber) : Bitbucket (URL de projet) ou clones locaux."""
    from tools.standard_version_task import list_repos, project_of
    base, key = project_of(client_repo)
    if base:
        return [BitbucketRepo(base, key, slug) for slug in list_repos(base, key) if "cucumber" not in slug.lower()]
    return [LocalRepo(path, name) for name, path in sorted(git_repos(client_repo).items())
            if "cucumber" not in name.lower()]


def cucumber_repository(client_repo: str, client_name: str):
    """Dépôt <client>-cucumber du projet client (Bitbucket ou clone local) ; None s'il est introuvable."""
    from tools.standard_version_task import list_repos, project_of
    base, key = project_of(client_repo)
    if base:
        names = [s for s in list_repos(base, key) if "cucumber" in s.lower()]
        make = lambda n: BitbucketRepo(base, key, n)
    else:
        found = {n: p for n, p in git_repos(client_repo).items() if "cucumber" in n.lower()}
        names = sorted(found)
        make = lambda n: LocalRepo(found[n], n)
    wanted = f"{client_name}-cucumber".lower()
    picked = next((n for n in names if n.lower() == wanted), names[0] if len(names) == 1 else None)
    return make(picked) if picked else None


def cucumber_library_ref(entry: dict, names: list) -> str:
    """
    Branche (ou tag) du dépôt <client>-cucumber qui correspond à la version client choisie : intégration pour la
    branche d'intégration ; sinon même nom de référence, v<version>, <version>, release/<version>, puis la ligne
    majeure.mineure (v1.3, 1.3, release/1.3) ; à défaut intégration. '' si aucune.
    """
    names = set(names or [])
    version = str(entry.get("version") or "").strip()
    line_ = re.match(r"(\d+\.\d+)", version)
    line_ = line_.group(1) if line_ else ""
    candidates = [] if entry.get("type") == INTEGRATION else [
        entry.get("ref"), f"v{version}", version, f"release/{version}", f"v{line_}", line_, f"release/{line_}"]
    return next((c for c in candidates + [INTEGRATION] if c and c in names), "")


def library_at_version(client_repo: str, client_name: str, entry: dict) -> dict:
    """
    Bibliothèque Cucumber (dépendance test-jar) du pom.xml de <client>-cucumber À LA BRANCHE de la version client
    choisie (v1.3 pour 1.3.1), lue en direct : {"version", "depot", "ref", "repli"} ; {} si illisible.
    """
    from tools.standard_version_task import SourceError
    try:
        repo = cucumber_repository(client_repo, client_name)
        refs = repo.refs() if repo else []
    except (SourceError, OSError):
        return {}
    by_name = {}
    for ref in refs:                      # branche prioritaire sur un tag du même nom
        by_name.setdefault(ref["nom"], ref) if ref["type"] == "tag" else by_name.__setitem__(ref["nom"], ref)
    name = cucumber_library_ref(entry, list(by_name))
    if not name:
        return {}
    try:
        version = sv.library_from_pom(repo.file(by_name[name], "pom.xml"))
    except (SourceError, OSError):
        return {}
    fallback = name == INTEGRATION and entry.get("type") != INTEGRATION
    return {"version": version, "depot": repo.name, "ref": name, "repli": fallback} if version else {}


def library_where(library: dict) -> str:
    """« canson-cucumber, branche v1.3 » (repli sur intégration signalé)."""
    if not library:
        return ""
    return (f"{library['depot']}, branche {library['ref']}"
            + (" — aucune branche du dépôt Cucumber pour cette version" if library.get("repli") else ""))


def _integration(repo) -> dict:
    return next((r for r in repo.refs() if r["type"] == "branche" and r["nom"] == INTEGRATION), {})


def app_repository(repos: list, groups: set):
    """Dépôt de l'application : son pom (intégration) déclare des artefacts du Standard ; le plus cité. None sinon."""
    best, score = None, 0
    for repo in repos:
        ref = _integration(repo)
        text = repo.file(ref, "pom.xml") if ref else ""
        found = sv.pom_versions(text, groups, f"{repo.name}/pom.xml") if text and groups else []
        if len(found) > score:
            best, score = repo, len(found)
    return best


def pom_project_version(text: str) -> str:
    """Version propre du projet (balise <version> racine) d'un pom ; '' sinon."""
    root = sv._parse(text or "")
    return sv._text(root, "version") if root is not None else ""


# ---------------------------------------------------------------------------
# 1. Versions proposées
# ---------------------------------------------------------------------------

def git_versions(repo) -> list:
    """Branche d'intégration (version de son pom), branches de release et tags de l'application, datés."""
    refs = repo.refs()
    integration = next((r for r in refs if r["type"] == "branche" and r["nom"] == INTEGRATION), None)
    kept = [r for r in refs if r["type"] == "tag" or (r["type"] == "branche" and _RELEASE.match(r["nom"]))]
    if hasattr(repo, "dated"):
        repo.dated(([integration] if integration else []) + kept)
    entries = []
    if integration:
        version = pom_project_version(repo.file(integration, "pom.xml")) or INTEGRATION
        entries.append({"type": INTEGRATION, "version": version, "ref": INTEGRATION, "git": integration["git"],
                        "date": integration.get("date", ""), "depot": repo.name})
    for ref in kept:
        name = ref["nom"].split("/", 1)[-1] if ref["type"] == "branche" else ref["nom"]
        match = _CLIENT_VERSION.search(name)
        entries.append({"type": "release" if ref["type"] == "branche" else "tag",
                        "version": match.group(0) if match else name, "ref": ref["nom"], "git": ref["git"],
                        "date": ref.get("date", ""), "depot": repo.name})
    return entries


def maven_versions(owner: str, name: str, fetch=None, dated: int = 10) -> dict:
    """{"versions": [{version, date}], "erreur"} : maven-metadata.xml de l'artefact du client (dépôt Maven)."""
    from tools import cucumber_job
    from tools.cucumber_library_upgrade import metadata_versions
    if not (owner and name):
        return {"versions": [], "erreur": "artefact du client inconnu (propriétaire / nom du composant)"}
    repository, auth = cucumber_job.maven_repository()
    group = "/".join(p for p in owner.split(".") if p)
    url = f"{repository}/{group}/{name}/maven-metadata.xml"
    if fetch is None:
        if not repository:
            return {"versions": [], "erreur": "MAVEN_REPOSITORY_URL non configuré (onglet Configuration)"}
        import requests
        try:
            response = requests.get(url, auth=auth, timeout=20)
        except requests.RequestException as e:
            return {"versions": [], "erreur": f"dépôt Maven injoignable ({type(e).__name__})"}
        if response.status_code != 200:
            return {"versions": [], "erreur": f"{url} : HTTP {response.status_code}"}
        text = response.text
    else:
        text = fetch(url)
    versions = list(dict.fromkeys(metadata_versions(text)))
    out = []
    for n, version in enumerate(reversed(versions)):
        date = ""
        if n < dated and fetch is None:
            date = cucumber_job.publication({"proprietaire": owner, "nom": name, "version": version}).get("date", "")
        out.append({"version": version, "date": date})
    return {"versions": out, "erreur": ""}


def _same(a: str, b: str) -> bool:
    return (a or "").lstrip("vV").strip().lower() == (b or "").lstrip("vV").strip().lower()


def merge_versions(git_entries: list, maven: list, owner: str = "", name: str = "") -> list:
    """
    Liste proposée : branche d'intégration d'abord, puis le reste par date décroissante ; une version publiée dans
    Maven qui a son tag ou sa branche de release y est rattachée (« publiee »), les autres restent des entrées
    « maven » (code lu sur intégration, avec avertissement).
    """
    entries = [dict(e) for e in git_entries]
    for item in maven or []:
        twin = next((e for e in entries if e["type"] in ("tag", "release") and _same(e["version"], item["version"])),
                    None)
        if twin:
            twin["publiee"] = True
            twin["date"] = twin.get("date") or item.get("date", "")
        elif not any(e["type"] == INTEGRATION and _same(e["version"], item["version"]) for e in entries):
            entries.append({"type": "maven", "version": item["version"], "ref": "", "git": "",
                            "date": item.get("date", ""), "artefact": f"{owner}/{name}"})
    head = [e for e in entries if e["type"] == INTEGRATION]
    rest = sorted((e for e in entries if e["type"] != INTEGRATION), key=lambda e: e.get("date") or "", reverse=True)
    for e in head + rest:
        e["id"] = f"{e['type']}:{e['ref'] or e['version']}"
        e["libelle"] = label(e)
    return head + rest


def label(entry: dict) -> str:
    """« 1.4-SNAPSHOT (integration) — 2026-11-03 »."""
    where = {INTEGRATION: INTEGRATION, "release": f"branche {entry.get('ref')}", "tag": f"tag {entry.get('ref')}",
             "maven": "publiée dans Maven"}.get(entry.get("type"), entry.get("type"))
    if entry.get("publiee"):
        where += ", publiée"
    date = (entry.get("date") or "")[:10]
    return f"{entry.get('version')} ({where})" + (f" — {date}" if date else "")


# ---------------------------------------------------------------------------
# 2. Déduction
# ---------------------------------------------------------------------------

def _version_key(text: str) -> tuple:
    short = sv.normalize(text)[0]
    return tuple(int(p) for p in short.split(".")) if short else ()


def cucumber_std_ref(version: str, refs: list) -> dict:
    """
    Référence du Cucumber Standard pour la version du Standard `version`, parmi ses branches et tags `refs`
    (noms) : release/<v>, v<v>, <v> ; à défaut la version existante la plus proche INFÉRIEURE, avec avertissement ;
    aucune : {"ref": ""} et avertissement (jamais de repli sur master). {"ref", "version", "avertissement"}.
    """
    wanted = _version_key(version)
    if not wanted:
        return {"ref": "", "version": "", "avertissement": f"version du Standard « {version or '?'} » illisible : "
                                                            f"référence du Cucumber Standard à choisir"}
    short = sv.normalize(version)[0]
    names = list(dict.fromkeys(r for r in refs or [] if r))
    for candidate in (f"release/{short}", f"v{short}", short):
        if candidate in names:
            return {"ref": candidate, "version": short, "avertissement": ""}
    versioned = [(k, n) for n in names if (k := _version_key(n)) and re.fullmatch(r"(?:release/|v)?[\d.]+", n)]
    lower = [(k, n) for k, n in versioned if k[:2] < wanted[:2]]
    if not lower:
        return {"ref": "", "version": "", "avertissement": f"aucune référence du Cucumber Standard pour {short} ni "
                                                            f"pour une version inférieure : à choisir (pas de repli "
                                                            f"sur master)"}
    key, name = max(lower, key=lambda kn: (kn[0], name_rank(kn[1])))
    found = ".".join(map(str, key[:2]))
    return {"ref": name, "version": found,
            "avertissement": f"Cucumber Standard {short} absent (aucune branche ni tag release/{short}, v{short}, "
                             f"{short}) : version existante la plus proche inférieure retenue, {name}"}


def name_rank(name: str) -> int:
    return 2 if name.startswith("release/") else 1 if name.startswith("v") else 0


def kis_registry(builds: list, saved: dict = None) -> dict:
    """
    {version du Standard: {"kis", "build", "source"}} : par version du Standard (branche du Cucumber Standard du
    build), le DERNIER build dont l'application a démarré (SUCCESS / UNSTABLE), tous clients confondus ; les
    entrées déjà enregistrées (`saved`, saisies comprises) sont gardées si aucun build plus récent ne les remplace.
    """
    registry = dict(saved or {})
    seen = set()
    for build in builds or []:
        if str(build.get("resultat") or "").upper() not in STARTED_RESULTS:
            continue
        params = build.get("parametres") or {}
        branch = next((str(v) for k, v in params.items() if re.search(JENKINS_ROLES["standard_branche"], k, re.I)),
                      "")
        kis = next((str(v) for k, v in params.items() if re.search(JENKINS_ROLES["kis_version"], k, re.I)), "")
        short = sv.normalize(branch)[0]
        if not short or not kis or short in seen:
            continue
        seen.add(short)
        previous = registry.get(short) or {}
        # Saisie de l'utilisateur ou build plus ancien : remplacés par ce build réel de la même version
        if previous.get("source") != "build" or int(build.get("numero") or 0) >= int(previous.get("numero") or 0):
            registry[short] = {"kis": kis, "build": build.get("url") or "", "numero": build.get("numero"),
                               "source": "build"}
    return registry


def load_kis_registry() -> dict:
    return _read_json(KIS_REGISTRY, {}) or {}


def record_kis(version: str, kis: str) -> dict:
    """Réponse de l'utilisateur : version KIS d'une version du Standard (gardée jusqu'à un build réel)."""
    registry = load_kis_registry()
    short = sv.normalize(version)[0] or version
    registry[short] = {"kis": kis.strip(), "build": "", "numero": 0, "source": "saisie"}
    _write_json(KIS_REGISTRY, registry)
    return registry


def deduce(entry: dict, app_pom: str, groups: set, cucumber_std_refs: list, library_current: str,
           registry: dict, client_ref_found: bool = True, library_source: str = "", kis_pom: dict = None) -> dict:
    """
    Déduction pour la version choisie `entry` : `app_pom` (pom de l'application à sa référence), `groups`
    (groupIds du Standard), `cucumber_std_refs` (branches et tags du Cucumber Standard), `library_current`
    (bibliothèque du pom <client>-cucumber), `registry` (kis_registry). Renvoie
    {version_client, ref_client, standard, cucumber_standard, bibliotheque, kis, jenkins, avertissements}.
    """
    warnings = []
    code = sv.code_from_poms({entry.get("depot") or "application": app_pom}, groups) if app_pom and groups else {}
    standard = code.get("version", "")
    if not standard:
        warnings.append(f"version du Standard introuvable dans le pom de l'application à "
                        f"{entry.get('ref') or INTEGRATION} : à saisir")
    ref_client = entry.get("ref") or INTEGRATION
    if entry.get("type") == "maven" or not client_ref_found:
        warnings.append(f"version {entry.get('version')} sans tag ni branche dans les dépôts du client : code lu sur "
                        f"{INTEGRATION}")
        ref_client = INTEGRATION
    cucumber = cucumber_std_ref(standard, cucumber_std_refs) if standard else {"ref": "", "version": "",
                                                                                "avertissement": ""}
    if cucumber["avertissement"]:
        warnings.append(cucumber["avertissement"])
    current_short = sv.normalize(library_current)[0]
    if standard and current_short and current_short != standard:
        where = f" ({library_source})" if library_source else ""
        warnings.append(f"bibliothèque Cucumber {library_current} du pom.xml du dépôt Cucumber du client{where}, "
                        f"Standard de l'application {standard} : montée proposée")
    kis = (registry or {}).get(standard) or {}
    if standard and not kis.get("kis") and (kis_pom or {}).get("kis"):
        kis = dict(kis_pom)                  # build réel ou saisie d'abord ; à défaut, poms de l'application
    if standard and not kis.get("kis"):
        warnings.append(f"version KIS inconnue pour le Standard {standard} (aucun build qui a démarré une application "
                        f"de cette version) : à renseigner")
    jenkins = {"composant_version": entry.get("version", ""), "standard_branche": cucumber["ref"],
               "kis_version": kis.get("kis", "")}
    return {"version_client": entry.get("version", ""), "type": entry.get("type", ""), "ref_client": ref_client,
            "standard": {"version": standard, "complete": code.get("complete", ""), "detail": code.get("detail", "")},
            "cucumber_standard": cucumber, "bibliotheque": {"cible": standard, "actuelle": library_current, "source": library_source},
            "kis": kis, "jenkins": jenkins, "avertissements": warnings}


def line(deduction: dict) -> str:
    """« Version client 1.4-SNAPSHOT (integration) → Standard 26.24 → Cucumber Standard v26.23 → bibliothèque
    Cucumber 26.24 (actuelle : 26.16) → KIS 3.12 »."""
    if not deduction:
        return ""
    library = deduction.get("bibliotheque") or {}
    current = library.get("actuelle") or "?"
    target = library.get("cible") or "?"
    return (f"Version client {deduction.get('version_client') or '?'} ({deduction.get('ref_client') or '?'}) → "
            f"Standard {(deduction.get('standard') or {}).get('version') or '?'} → Cucumber Standard "
            f"{(deduction.get('cucumber_standard') or {}).get('ref') or '?'} → bibliothèque Cucumber {target}"
            + (f" (actuelle : {current})" if sv.normalize(current)[0] != sv.normalize(target)[0] else "")
            + f" → KIS {(deduction.get('kis') or {}).get('kis') or '?'}"
            + (" (pom de l'application)" if (deduction.get("kis") or {}).get("source") == "pom" else ""))


# ---------------------------------------------------------------------------
# Profil du client
# ---------------------------------------------------------------------------

def saved(client_name: str) -> dict:
    from tools import cucumber_profile
    return cucumber_profile.load_profile(client_name).get(FIELD) or {}


def save(client_name: str, entry: dict, deduction: dict, versions: list = None) -> dict:
    """Choix et déduction enregistrés dans le profil (« version_client ») ; versions proposées gardées en cache."""
    from tools import cucumber_profile
    document = cucumber_profile.load_profile(client_name)
    current = document.get(FIELD) or {}
    document[FIELD] = {"choix": entry, "deduction": deduction, "date": datetime.now().isoformat(timespec="seconds"),
                       "versions": versions if versions is not None else current.get("versions") or []}
    _write_json(cucumber_profile.profile_path(client_name), document)
    return document


def save_versions(client_name: str, versions: list, error: str = "") -> dict:
    from tools import cucumber_profile
    document = cucumber_profile.load_profile(client_name)
    current = document.setdefault(FIELD, {})
    current["versions"], current["erreur_versions"] = versions, error
    current["versions_date"] = datetime.now().isoformat(timespec="seconds")
    _write_json(cucumber_profile.profile_path(client_name), document)
    return document


def chosen(document: dict) -> dict:
    """Déduction de la version choisie ({} sans choix)."""
    return ((document or {}).get(FIELD) or {}).get("deduction") or {}


def standard_of(document: dict) -> str:
    return (chosen(document).get("standard") or {}).get("version", "")


def client_git_ref(document: dict) -> str:
    """Référence Git du code du client pour la version choisie (origin/<branche> ou refs/tags/<tag>) ; '' sinon."""
    entry = ((document or {}).get(FIELD) or {}).get("choix") or {}
    deduction = chosen(document)
    if not deduction or deduction.get("ref_client") == INTEGRATION or entry.get("type") not in ("tag", "release"):
        return ""
    return f"refs/tags/{entry['ref']}" if entry["type"] == "tag" else f"origin/{entry['ref']}"


# ---------------------------------------------------------------------------
# Lecture réelle (Bitbucket / clones, Maven, Jenkins)
# ---------------------------------------------------------------------------

def _no_task():
    class Quiet:
        state = {}

        def step(self, *a, **k):
            return None
    return Quiet()


def std_groups(std_repo: str, client_name: str) -> set:
    from tools.standard_version_task import std_group_ids_cached
    return std_group_ids_cached(std_repo, _no_task(), client_name) if std_repo else set()


def component(profile: dict, builds: list, cucumber_repo: str) -> tuple:
    """(propriétaire, nom) de l'artefact du client : paramètres imposés du profil, sinon dernier build du client."""
    from tools import cucumber_job
    fixed, _ = cucumber_job.fixed_parameters({**(profile or {}), FIELD: {}})
    sources = [fixed] + [b.get("parametres") or {} for b in builds or []
                         if any(cucumber_repo and cucumber_repo == str(v).strip().lower()
                                for v in (b.get("parametres") or {}).values())]
    for params in sources:
        owner = next((str(v) for k, v in params.items() if re.search(JENKINS_ROLES["composant_proprietaire"], k, re.I)
                      and v), "")
        name = next((str(v) for k, v in params.items() if re.search(JENKINS_ROLES["composant_nom"], k, re.I) and v),
                    "")
        if owner and name:
            return owner, name
    return "", ""


def list_versions(client_name: str, client_repo: str, std_repo: str, builds: list = None, maven_fetch=None) -> dict:
    """{"versions", "depot", "erreur"} : versions proposées (merge_versions), enregistrées dans le profil."""
    from tools import cucumber_profile
    from tools.cucumber_tools import java_package_segment
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client_name))
    groups = std_groups(std_repo, client_name)
    repo = app_repository(repositories(client_repo), groups)
    if repo is None:
        error = ("aucun dépôt du projet client dont le pom déclare des artefacts du Standard"
                 if groups else "groupIds du Standard inconnus (dépôt du Standard non configuré ou illisible)")
        save_versions(client_name, [], error)
        return {"versions": [], "depot": "", "erreur": error}
    owner, name = component(profile, builds or [], f"{java_package_segment(client_name)}-cucumber")
    maven = maven_versions(owner, name, maven_fetch)
    versions = merge_versions(git_versions(repo), maven["versions"], owner, name)
    save_versions(client_name, versions, maven["erreur"])
    return {"versions": versions, "depot": repo.name, "erreur": maven["erreur"]}


def cucumber_std_names(cucumber_std_dir: str) -> list:
    """Branches distantes et tags du clone du Cucumber Standard (noms sans origin/)."""
    if not cucumber_std_dir:
        return []
    out = _git(cucumber_std_dir, "for-each-ref", "--format=%(refname)", "refs/remotes/origin", "refs/tags").stdout
    names = []
    for ref in out.splitlines():
        if ref.startswith("refs/tags/"):
            names.append(ref[len("refs/tags/"):])
        elif not ref.endswith("/HEAD"):
            names.append(ref[len("refs/remotes/origin/"):])
    return names


def remote_ref_names(url: str) -> list:
    """Branches et tags d'un dépôt distant (git ls-remote) ; [] si injoignable."""
    token = os.getenv("BITBUCKET_HTTP_TOKEN", "").strip()
    extra = ["-c", f"http.extraHeader=Authorization: Bearer {token}"] if token else []
    try:
        out = subprocess.run(["git", *extra, "ls-remote", "--heads", "--tags", url], capture_output=True, text=True,
                             timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = []
    for line in out.splitlines():
        ref = line.split("\t", 1)[-1]
        if ref.endswith("^{}"):
            continue
        names.append(ref.split("/", 2)[-1])
    return names


_KIS_ARTIFACT = re.compile(r"<artifactId>\s*(kis-[\w.-]+)\s*</artifactId>")
_VERSION_TAG = re.compile(r"<version>\s*([^<\s]+)\s*</version>")
_PROPERTY = re.compile(r"\$\{([\w.-]+)\}")
_MODULE = re.compile(r"<module>\s*([^<\s]+)\s*</module>")


def kis_in_pom(text: str, properties: dict = None) -> str:
    """Version KIS déclarée par un pom (dépendance ou parent a-sis:kis-*, propriété résolue) ; '' sinon."""
    props = dict(properties or {})
    for name, value in re.findall(r"<([\w.-]+)>\s*([^<\s]+)\s*</\1>",
                                  (re.search(r"<properties>(.*?)</properties>", text or "", re.S) or [None, ""])[1]
                                  if re.search(r"<properties>", text or "") else ""):
        props.setdefault(name, value)
    for block in re.findall(r"<(?:dependency|parent)>(.*?)</(?:dependency|parent)>", text or "", re.S):
        if not _KIS_ARTIFACT.search(block) or "a-sis" not in block:
            continue
        version = _VERSION_TAG.search(block)
        if not version:
            continue
        value = _PROPERTY.sub(lambda m: props.get(m.group(1), m.group(0)), version.group(1))
        if "${" not in value and re.match(r"\d+\.\d+", value):
            return value
    return ""


def kis_from_poms(repo, ref: dict, limit: int = 40) -> dict:
    """
    Version KIS lue dans les poms de l'application à la référence de la version choisie (racine puis modules, en
    largeur) : {"kis": « 3.12 », "detail": « <dépôt>/<chemin>/pom.xml : kis-core 3.12-SNAPSHOT », "source": "pom"} ;
    {} si aucun pom ne la déclare.
    """
    if not (repo and ref):
        return {}
    root = repo.file(ref, "pom.xml")
    root_props = dict(re.findall(r"<([\w.-]+)>\s*([^<\s]+)\s*</\1>",
                                 (re.search(r"<properties>(.*?)</properties>", root or "", re.S) or [None, ""])[1]
                                 if "<properties>" in (root or "") else ""))
    queue, seen = [""], set()
    while queue and len(seen) < limit:
        folder = queue.pop(0)
        if folder in seen:
            continue
        seen.add(folder)
        path = f"{folder}/pom.xml" if folder else "pom.xml"
        text = root if not folder else repo.file(ref, path)
        if not text:
            continue
        found = kis_in_pom(text, root_props)
        if found:
            artifact = next((m for block in re.findall(r"<(?:dependency|parent)>(.*?)</(?:dependency|parent)>", text,
                                                       re.S) for m in _KIS_ARTIFACT.findall(block)), "kis")
            return {"kis": sv.normalize(found)[0] or found.replace("-SNAPSHOT", ""), "source": "pom",
                    "detail": f"{repo.name}/{path} : {artifact} {found}", "build": "", "numero": 0}
        queue += [f"{folder}/{m}".strip("/") for m in _MODULE.findall(text)]
    return {}


def compute(client_name: str, entry: dict, client_repo: str, std_repo: str, cucumber_std_refs: list,
            library_current: str, builds: list = None, library_source: str = "") -> dict:
    """Déduction réelle pour `entry` : pom de l'application à sa référence, registre KIS mis à jour."""
    groups = std_groups(std_repo, client_name)
    repos = repositories(client_repo)
    repo = next((r for r in repos if r.name == entry.get("depot")), None) or app_repository(repos, groups)
    ref = {"git": entry["git"]} if entry.get("git") else _integration(repo) if repo else {}
    pom = repo.file(ref, "pom.xml") if repo and ref else ""
    registry = kis_registry(builds or [], load_kis_registry())
    _write_json(KIS_REGISTRY, registry)
    # Aucun build ni saisie pour ce Standard : version KIS déclarée par les poms de l'application à cette version
    return deduce(entry, pom, groups, cucumber_std_refs, library_current, registry, library_source=library_source,
                  kis_pom=kis_from_poms(repo, ref) if repo and ref else {})


def cucumber_std_refs_for(std_input: str) -> list:
    """Branches et tags du Cucumber Standard : clone de l'agent (.tmp/cucumber_std) s'il existe, sinon ls-remote."""
    std_input = (std_input or os.getenv("BITBUCKET_CUCUMBER_STD_REPO", "")).strip()
    if not std_input:
        return []
    if not std_input.startswith(("http://", "https://", "git@", "file://")):
        return cucumber_std_names(std_input) if os.path.isdir(os.path.join(std_input, ".git")) else []
    url = std_input.split("/browse")[0].rstrip("/")
    web = re.match(r"^(https?://[^/]+)/projects/([^/]+)/repos/([^/]+)$", url)
    if web:
        url = f"{web.group(1)}/scm/{web.group(2).lower()}/{web.group(3)}.git"
    slug = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp", "cucumber_std")
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        clone = os.path.join(root, name)
        origin = _git(clone, "remote", "get-url", "origin").stdout.strip().lower()
        if origin and origin.rstrip("/").removesuffix(".git").endswith(slug):
            return cucumber_std_names(clone)
    return remote_ref_names(url)


def choose(client_name: str, entry: dict, client_repo: str, std_repo: str, cucumber_std_input: str,
           builds: list = None) -> dict:
    """Version choisie : déduction calculée et enregistrée dans le profil. Renvoie la déduction."""
    from tools import cucumber_profile
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client_name))
    # Bibliothèque lue en direct à la branche de la version choisie ; à défaut, la dernière détection du profil
    library = library_at_version(client_repo, client_name, entry)
    source = library_where(library) if library else (
        "dernière analyse du profil" if profile.get("library_version") else "")
    deduction = compute(client_name, entry, client_repo, std_repo, cucumber_std_refs_for(cucumber_std_input),
                        library.get("version") or profile.get("library_version", ""), builds, source)
    save(client_name, entry, deduction)
    return deduction


def answer_kis(client_name: str, kis: str) -> dict:
    """Réponse à la question KIS : enregistrée pour la version du Standard, déduction du client mise à jour."""
    from tools import cucumber_profile
    document = cucumber_profile.load_profile(client_name)
    current = document.get(FIELD) or {}
    deduction = dict(current.get("deduction") or {})
    standard = (deduction.get("standard") or {}).get("version", "")
    if not (standard and kis.strip()):
        return deduction
    registry = record_kis(standard, kis)
    deduction["kis"] = registry[standard]
    deduction["jenkins"] = {**(deduction.get("jenkins") or {}), "kis_version": kis.strip()}
    deduction["avertissements"] = [w for w in deduction.get("avertissements") or [] if "version KIS" not in w]
    save(client_name, current.get("choix") or {}, deduction)
    return deduction
