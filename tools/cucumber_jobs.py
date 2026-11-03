"""
Jobs KIS de l'initialisation d'un client (0 token) : chaque job désactivé, activé, modifié ou lancé doit exister
au moment du step — créé AVANT par l'init, ou prouvé présent chez CE client — et la classe de chaque job créé doit
être prouvée.

L'initialisation copiée du Standard cite des jobs qu'un client ne déploie pas, ou qu'elle ne crée que plus loin
(ex. désactivation initiale d'un job créé dans « Initialisation du paramétrage ») : le step échoue sur « Pas
d'élément correspondant à l'attendu {} pour KisJob » et arrête toute l'init.

Règles, dans l'ordre (déterministes) :
  1. ordre : un job référencé avant sa création par l'init (« KIS- Création du job » plus bas dans la feature)
     n'existe pas encore : la référence est retirée (commentée) ; référencé après : prouvé par cette création ;
  2. définition du job chez le client, source citée : nom cité littéralement dans le code ou la configuration de
     son application (dépôts hors Cucumber, référence distante), dans le Standard à sa version (références de
     version, tools/standard_version), ou dans le socle KIS (recherche de code Bitbucket, projet KIS) ;
  3. classe d'un job créé : présente dans l'application du client ou dans le Standard à sa version.
La base du client (ex. cansond) n'est PAS une preuve : ce n'est pas la base de l'environnement de test ; elle
n'est citée qu'à titre d'information. Un job non prouvé n'est jamais deviné : sa ligne est commentée sous
« # TODO-JOB: <raison> » (le step entier si plus aucune ligne ne reste), et listé avec sa raison.
"""
import re

from tools.cucumber_tools import client_ref, CLIENT_REFS, _git, git_repos, pick_ref

TODO_MARKER = "TODO-JOB:"
# Step -> ce qui doit être prouvé : le nom du job (il doit exister) ou sa classe (job créé)
STEPS = {"KIS- Désactivation du job": "nom", "KIS- Activation du job": "nom", "KIS- Modification du job": "nom",
         "KIS- Lancement du job": "nom", "KIS- Création du job": "classe"}
ONE_OFF, ONE_ON = "KIS- Désactivation du job", "KIS- Activation du job"
ALL_OFF, ALL_ON = "KIS- Désactivation de tous les jobs", "KIS- Activation de tous les jobs"
_STEP = re.compile(r"^(\s*)(Given|When|Then|And|But|\*)\s+(.+?)\s*$")
JOB_TABLE = "kis_qrtz_job_details"


def _cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def step_key(step: str) -> str:
    """Step de STEPS correspondant : « <CLIENT>- Désactivation du job » (step du projet) contrôlé comme celui du KIS."""
    if step in STEPS:
        return step
    other = re.sub(r"^[A-Z][A-Z0-9_]*- ", "KIS- ", step)
    return other if other in (ONE_OFF, ONE_ON) else step


def usages(text: str) -> list:
    """
    Jobs cités par les steps de STEPS (lignes actives) : [{step, preuve, ligne, nom, classe}] ; `ligne` : numéro
    (1-based) de la ligne de table.
    """
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        match = _STEP.match(lines[i])
        i += 1
        if not match or lines[i - 1].lstrip().startswith("#") or step_key(match.group(3)) not in STEPS:
            continue
        step = step_key(match.group(3))
        header = None
        while i < len(lines) and lines[i].strip().startswith(("|", "#")):
            if lines[i].strip().startswith("|"):
                cells = _cells(lines[i])
                if header is None:
                    header = cells
                else:
                    row = dict(zip(header, cells))
                    out.append({"step": step, "preuve": STEPS[step], "ligne": i + 1, "nom": row.get("name", ""),
                                "classe": row.get("type", "")})
            i += 1
    return out


# ---------------------------------------------------------------------------
# Jobs connus du client
# ---------------------------------------------------------------------------

def from_database(schemas: list) -> dict:
    """
    {"noms": {nom: source}, "classes": {classe: source}, "source": libellé, "erreur": ''} lus dans la base du client
    (premier schéma de `schemas` qui a la table des jobs, connexion DB_* puis CUCUMBER_DB_*), lecture seule.
    """
    from tools import db_tools
    errors = []
    for profile in ("", "cucumber"):
        if not db_tools.is_db_configured(profile):
            continue
        previous = db_tools.set_profile(profile)
        try:
            for schema in [s for s in dict.fromkeys(schemas) if s]:
                try:
                    out = db_tools.run_query(f"SELECT job_name, job_class_name FROM {JOB_TABLE}", schema,
                                             limit=db_tools.MAX_LIMIT)
                except Exception as e:  # noqa: BLE001 - schéma absent de cette base : suivante
                    errors.append(f"{schema} ({db_tools.describe_db_target()}) : {str(e)[:120]}")
                    continue
                rows = [line.split(" | ") for line in out.splitlines()[1:-1]]
                where = f"base {schema} ({db_tools.describe_db_target()})"
                return {"noms": {r[0].strip(): where for r in rows if r and r[0].strip()},
                        "classes": {r[1].strip(): where for r in rows if len(r) > 1 and r[1].strip()},
                        "source": where, "erreur": ""}
        finally:
            db_tools.set_profile(previous)
    return {"noms": {}, "classes": {}, "source": "", "erreur": "; ".join(errors[:3]) or "aucune base configurée"}


def from_code(client_root: str, names: list, classes: list) -> dict:
    """Noms cités littéralement et classes présentes dans l'application du client (dépôts hors Cucumber)."""
    found = {"noms": {}, "classes": {}}
    for repo_name, repo in git_repos(client_root).items():
        if "cucumber" in repo_name.lower():
            continue
        ref = client_ref(repo)
        for name in [n for n in names if n and n not in found["noms"]]:
            grep = _git(repo, "grep", "-I", "-l", "-F", f'"{name}"', ref, "--")
            if grep.stdout.strip():
                found["noms"][name] = f"{repo_name}/{grep.stdout.splitlines()[0].split(':', 1)[-1]}"
        files = _git(repo, "ls-tree", "-r", "--name-only", ref).stdout.splitlines()
        for cls in [c for c in classes if c and c not in found["classes"]]:
            suffix = "/" + cls.replace(".", "/") + ".java"
            hit = next((f for f in files if f.endswith(suffix)), "")
            if hit:
                found["classes"][cls] = f"{repo_name}/{hit}"
    return found


def _std_candidates(version: str) -> tuple:
    from tools.cucumber_tools import STD_REFS
    from tools.standard_version import std_refs
    return std_refs(version, STD_REFS)


def from_standard(std_root, version: str, classes: list, errors: list = None) -> dict:
    """
    Classes présentes dans le Standard à la référence de la version du client : {classe: source}. `std_root` :
    dossier de clones ou source Bitbucket (tools/bitbucket_std). Classe trouvée seulement hors version : `errors`.
    """
    from tools.bitbucket_std import ref_label, standard_source
    std = standard_source(std_root)
    found, candidates = {}, _std_candidates(version)
    for cls in [c for c in classes if c]:
        hit, unconfirmed = std.find_file("/" + cls.replace(".", "/") + ".java", candidates)
        if hit:
            found[cls] = f"Standard {hit[0]}@{ref_label(std, hit[0], hit[1])}"
        elif unconfirmed and errors is not None:
            errors.append({"nom": cls, "erreur": "classe non confirmée à la version : " + "; ".join(unconfirmed)[:200]})
    return found


_BUNDLE = re.compile(r"^\s*(runtime|import)\s*:\s*[\w.-]+/([\w.-]+)@", re.M)


def client_bundle_lists(client_root: str) -> list:
    """Textes des bundle.list de l'application du client (dépôts hors Cucumber, référence distante)."""
    lists = []
    for repo_name, repo in git_repos(client_root).items():
        if "cucumber" in repo_name.lower():
            continue
        ref = client_ref(repo)
        for path in _git(repo, "ls-tree", "-r", "--name-only", ref).stdout.splitlines():
            if path.endswith("bundle.list") and "/test/" not in path:
                lists.append(_git(repo, "show", f"{ref}:{path}").stdout)
    return lists


def _contrib_bundle(std, artifact: str, candidates) -> str:
    """bundle.list du module `artifact` (contrib importée) dans le Standard à la version, '' s'il est introuvable."""
    # Bitbucket : dépôts qui déclarent l'artefact (recherche) ; clones : tous les dépôts
    repos = (dict.fromkeys(r for r, _ in std.search(f"<artifactId>{artifact}</artifactId>"))
             if hasattr(std, "search") else std.repos())
    for repo in repos:
        ref = std.ref(repo, candidates)
        if not ref:
            continue
        for path in std.files(repo, ref):
            if (path.endswith("bundle.list") and "/src/main/" in path
                    and artifact in path.split("/src/main/")[0].split("/")):
                return std.show(repo, ref, path)
    return ""


def deployed_modules(client_root: str, std_root, version: str) -> set:
    """
    Modules du Standard déployés chez le client : artefacts « runtime: » des bundle.list de son application
    (référence distante), plus ceux des contribs qu'ils importent (« import: »), lus dans le Standard à sa version,
    récursivement. Vide si rien n'est lisible.
    """
    from tools.bitbucket_std import standard_source
    std = standard_source(std_root)
    candidates = _std_candidates(version)
    modules, seen, pending = set(), set(), client_bundle_lists(client_root)
    while pending:
        for kind, artifact in _BUNDLE.findall(pending.pop()):
            if kind == "runtime":
                modules.add(artifact)
            elif artifact not in seen:
                seen.add(artifact)
                text = _contrib_bundle(std, artifact, candidates)
                if text:
                    pending.append(text)
    return modules


def _module_deployed(module: str, deployed: set) -> bool:
    """Module Maven déployé : un bundle déployé porte son nom, ou son nom suivi d'un suffixe (un module produit
    plusieurs bundles, déclarés dans son kis.descriptor.yaml)."""
    return module in deployed or any(d.startswith(module + "-") for d in deployed)


def _embedded(std, repo: str, ref: str, module: str, deployed: set) -> bool:
    """
    Module bibliothèque (ex. *-event) embarqué par un module déployé du même dépôt : son pom.xml en dépend
    (<artifactId>module</artifactId>).
    """
    for pom in [p for p in std.files(repo, ref) if p.endswith("/pom.xml")]:
        owner = pom.rsplit("/", 1)[0].split("/")[-1]
        if (owner != module and _module_deployed(owner, deployed)
                and f"<artifactId>{module}</artifactId>" in std.show(repo, ref, pom)):
            return True
    return False


def names_in_standard(std_root, version: str, names: list, deployed: set = None, errors: list = None) -> tuple:
    """
    Noms de jobs cités littéralement (entre guillemets) dans le Standard à la version du client, dans un module
    déployé chez lui (`deployed`, deployed_modules ; None : non vérifié). ({nom: source}, {nom: module non déployé}).
    Source Bitbucket : candidat trouvé sur la branche par défaut mais absent à la version → `errors` (indéterminé).
    """
    from tools.bitbucket_std import ref_label, standard_source
    std = standard_source(std_root)
    found, elsewhere, candidates = {}, {}, _std_candidates(version)
    for name in [n for n in names if n]:
        hits, unconfirmed = std.find_quoted(name, candidates)
        for repo, ref, hit in hits:
            module = hit.split("/", 1)[0]
            if deployed is None or _module_deployed(module, deployed) or _embedded(std, repo, ref, module, deployed):
                found[name] = f"Standard {repo}@{ref_label(std, repo, ref)} : {hit}"
                break
            elsewhere.setdefault(name, f"{repo}/{hit}")
        if name not in found and name not in elsewhere and unconfirmed and errors is not None:
            errors.append({"nom": name, "erreur": "non confirmé à la version : " + "; ".join(unconfirmed)[:200]})
    return found, {n: m for n, m in elsewhere.items() if n not in found}


KIS_PROJECT = "KIS"


class SearchUnavailable(RuntimeError):
    """Recherche de code Bitbucket en erreur (réseau, droits, adresse) : ni preuve, ni absence."""


def names_in_kis(names: list, search=None, errors: list = None) -> dict:
    """
    Noms de jobs définis dans le socle KIS (recherche de code Bitbucket du projet KIS, branche indexée par
    Bitbucket) : {nom: source}. `search(requête) -> [(dépôt, fichier)]` remplaçable (tests). Une recherche en erreur
    ne vaut jamais absence : le nom est ajouté à `errors` ({nom, erreur}), à traiter comme indéterminé.
    """
    if search is None:
        search = _bitbucket_code_search
    found = {}
    for name in [n for n in names if n]:
        try:
            hits = search(f'"{name}" project:{KIS_PROJECT}')
        except Exception as exc:  # noqa: BLE001 - recherche indisponible : ni preuve ni absence
            if errors is not None:
                errors.append({"nom": name, "erreur": " ".join(str(exc).split())[:200]})
            continue
        java = [h for h in hits if h[1].endswith(".java")] or hits
        if java:
            found[name] = f"socle KIS {java[0][0]} : {java[0][1]}"
    return found


def _bitbucket_code_search(query: str) -> list:
    import os

    import requests
    base = os.getenv("BITBUCKET_URL", "").strip().rstrip("/")
    token = os.getenv("BITBUCKET_HTTP_TOKEN", "").strip()
    if not base:
        raise SearchUnavailable("BITBUCKET_URL non renseignée")
    try:
        response = requests.post(f"{base}/rest/search/latest/search", timeout=30,
                                 headers={"Authorization": f"Bearer {token}"} if token else {},
                                 json={"query": query, "entities": {"code": {"start": 0, "limit": 10}}})
    except requests.RequestException as exc:
        raise SearchUnavailable(f"recherche de code Bitbucket injoignable : {exc}") from exc
    if response.status_code != 200:
        raise SearchUnavailable(f"recherche de code Bitbucket : HTTP {response.status_code}")
    return [(v["repository"]["slug"], v["file"]) for v in (response.json().get("code") or {}).get("values") or []]


def proven(items: list, known: dict) -> tuple:
    """
    (prouvés [{..., source}], non prouvés [{..., raison}]). Ordre d'abord : un job référencé avant sa création par
    l'init (même feature) est retiré, même s'il est défini ailleurs ; référencé après une création prouvée, il est
    prouvé par elle.
    """
    ok, missing = [], []
    creations = {}
    for item in items:
        if item["preuve"] == "classe":
            creations.setdefault((item.get("fichier", ""), item["nom"]), item["ligne"])
    for item in items:
        created_at = creations.get((item.get("fichier", ""), item["nom"]))
        if item["preuve"] == "nom" and created_at and created_at > item["ligne"]:
            missing.append({**item, "raison": f"job « {item['nom']} » référencé l. {item['ligne']} avant sa création "
                                              f"par l'init (l. {created_at}) : il n'existe pas encore"})
            continue
        if item["preuve"] == "nom" and created_at and created_at < item["ligne"]:
            ok.append({**item, "source": f"créé par l'init (l. {created_at})"})
            continue
        key, pool = (item["nom"], "noms") if item["preuve"] == "nom" else (item["classe"], "classes")
        source = (known.get(pool) or {}).get(key, "")
        if source:
            ok.append({**item, "source": source})
        else:
            what = f"job « {item['nom']} »" if item["preuve"] == "nom" else \
                f"classe {item['classe'] or '(vide)'} du job créé « {item['nom']} »"
            missing.append({**item, "raison": f"{what} : aucune définition chez le client (ni création plus haut par "
                                              f"l'init, ni code ou configuration de son application, ni Standard à "
                                              f"sa version, ni socle KIS)"})
    return ok, missing


def comment_unproven(text: str, missing: list) -> str:
    """
    Lignes de table des jobs non prouvés commentées sous « # TODO-JOB: <raison> » ; le step et son en-tête aussi
    quand il ne reste aucune ligne. Rien n'est supprimé.
    """
    if not missing:
        return text
    lines = text.splitlines(keepends=True)
    by_line = {m["ligne"]: m for m in missing}
    out, i = [], 0
    while i < len(lines):
        match = _STEP.match(lines[i])
        if not match or lines[i].lstrip().startswith("#") or step_key(match.group(3)) not in STEPS:
            out.append(lines[i])
            i += 1
            continue
        block = [lines[i]]
        j = i + 1
        while j < len(lines) and lines[j].strip().startswith(("|", "#")):
            block.append(lines[j])
            j += 1
        rows = [k for k in range(i + 2, j) if lines[k].strip().startswith("|")]  # hors en-tête
        removed = [k for k in rows if (k + 1) in by_line]
        indent = match.group(1)
        if removed and len(removed) == len(rows):
            reasons = "; ".join(by_line[k + 1]["raison"] for k in removed)[:300]
            out.append(f"{indent}# {TODO_MARKER} {reasons}\n")
            out += [f"{indent}# {line.lstrip()}" if line.strip() and not line.lstrip().startswith("#") else line
                    for line in block]
        else:
            out.append(block[0])
            for offset, line in enumerate(block[1:], start=i + 1):
                if offset in removed:
                    pad = line[:len(line) - len(line.lstrip())]
                    out.append(f"{pad}# {TODO_MARKER} {by_line[offset + 1]['raison'][:200]}\n")
                    out.append(f"{pad}# {line.lstrip()}")
                else:
                    out.append(line)
        i = j
    return "".join(out)


def check_init(texts: dict, client_root: str, schemas: list, std_root="", version: str = "",
               kis_search=None) -> dict:
    """
    Contrôle des features d'init {chemin: texte} : {"prouves", "non_prouves", "base", "erreur_base"} ; chaque
    usage porte son chemin (« fichier »). Preuves par définition seulement (application, Standard à la version,
    socle KIS) ; la base du client est lue pour information (« en_base » : jobs présents dans cette base).
    """
    items = [{**u, "fichier": path} for path, text in texts.items() for u in usages(text)]
    database = from_database(schemas) if schemas else {"noms": {}, "classes": {}, "source": "", "erreur": ""}
    names = sorted({i["nom"] for i in items if i["preuve"] == "nom"})
    classes = sorted({i["classe"] for i in items if i["preuve"] == "classe"})
    code = from_code(client_root, names, classes) if client_root and (names or classes) else {"noms": {}, "classes": {}}
    left = [n for n in names if n not in code["noms"]]
    search_errors = []
    wanted_classes = [c for c in classes if c not in code["classes"]]
    from tools.bitbucket_std import BitbucketUnavailable
    try:
        deployed = deployed_modules(client_root, std_root, version) if std_root and client_root and left else set()
        standard_names, not_deployed = (names_in_standard(std_root, version, left, deployed or None, search_errors)
                                        if std_root and left else ({}, {}))
        standard = from_standard(std_root, version, wanted_classes, search_errors) if std_root else {}
    except BitbucketUnavailable as exc:
        # Standard illisible (API Bitbucket) : tout ce qui restait à prouver est indéterminé, jamais absent
        deployed, standard_names, not_deployed, standard = set(), {}, {}, {}
        search_errors += [{"nom": n, "erreur": str(exc)} for n in left + wanted_classes]
    left = [n for n in left if n not in standard_names]
    kis = names_in_kis([n for n in left if n not in {e["nom"] for e in search_errors}], kis_search,
                       search_errors) if left else {}
    known = {"noms": {**kis, **standard_names, **code["noms"]}, "classes": {**standard, **code["classes"]}}
    ok, missing = proven(items, known)
    # Recherche en erreur : le job n'est ni prouvé ni absent — jamais commenté, la correction doit s'arrêter
    failed = {e["nom"]: e["erreur"] for e in search_errors}

    def failure(m: dict) -> str:
        key = m.get("classe") if m["preuve"] == "classe" else m["nom"]
        return "" if "avant sa création" in m["raison"] else failed.get(key, "")
    undetermined = [{**m, "raison": f"job « {m['nom']} » indéterminé : {failure(m)}"} for m in missing if failure(m)]
    missing = [m for m in missing if not failure(m)]
    for item in ok + missing:
        item["en_base"] = item["nom"] in database["noms"]
    for item in missing:
        if item["nom"] in not_deployed and "avant sa création" not in item["raison"]:
            item["raison"] += f" ; défini seulement dans {not_deployed[item['nom']]}, module non déployé chez le client"
    return {"prouves": ok, "non_prouves": missing, "indetermines": undetermined, "base": database["source"],
            "erreur_base": database["erreur"], "modules_deployes": len(deployed)}


def apply(worktree: str, paths: list, client_root: str, schemas: list, std_root: str = "", version: str = "",
          kis_search=None) -> dict:
    """
    Contrôle des features d'init `paths` du worktree et commentaire des jobs non prouvés (fichiers réécrits).
    Renvoie check_init(...) plus "fichiers" (features modifiées).
    """
    import os
    texts = {}
    for path in paths:
        full = os.path.join(worktree, *path.split("/"))
        if os.path.exists(full):
            with open(full, encoding="utf-8", newline="") as f:
                texts[path] = f.read()
    result = check_init(texts, client_root, schemas, std_root, version, kis_search)
    result["fichiers"] = []
    if result["indetermines"]:
        # Preuve indisponible (recherche en erreur) : aucun fichier réécrit, la correction ne se fait pas à moitié
        return result
    for path, text in texts.items():
        missing = [m for m in result["non_prouves"] if m["fichier"] == path]
        if missing:
            with open(os.path.join(worktree, *path.split("/")), "w", encoding="utf-8", newline="") as f:
                f.write(comment_unproven(text, missing))
            result["fichiers"].append(path)
    return result


def summary(result: dict) -> list:
    """Lignes lisibles : source des preuves, puis chaque job non prouvé (fichier, ligne, raison)."""
    sources = {}
    for item in result["prouves"]:
        sources.setdefault(item["source"].split(" : ")[0].split(" (l.")[0], 0)
        sources[item["source"].split(" : ")[0].split(" (l.")[0]] += 1
    lines = [f"{len(result['prouves'])} job(s) prouvé(s) — " + ", ".join(f"{k} : {v}" for k, v in sorted(sources.items()))]
    lines += [f"{m['fichier'].rsplit('/', 2)[-2]}/{m['fichier'].rsplit('/', 1)[-1]} l. {m['ligne']} — {m['step']} : "
              f"{m['nom']} — prouvé par {m['source']}" for m in result["prouves"] if m["preuve"] == "nom"]
    lines += [f"{m['fichier'].rsplit('/', 2)[-2]}/{m['fichier'].rsplit('/', 1)[-1]} l. {m['ligne']} — {m['step']} : "
              f"{m['raison']}" for m in result["non_prouves"]]
    lines += [f"{m['fichier'].rsplit('/', 2)[-2]}/{m['fichier'].rsplit('/', 1)[-1]} l. {m['ligne']} — {m['step']} : "
              f"{m['raison']}" for m in result.get("indetermines") or []]
    return lines


# ---------------------------------------------------------------------------
# Diagnostic temporaire : jobs présents sur l'environnement avant la désactivation
# ---------------------------------------------------------------------------

DIAGNOSTIC_STEP = "TEMPORAIRE- Liste des jobs de l'environnement"
DIAGNOSTIC_PREFIX = "[DIAGNOSTIC JOBS]"


def diagnostic_java_path(client_name: str) -> str:
    from tools.cucumber_tools import java_class_suffix, java_package_segment
    return (f"src/test/java/cucumber/features/{java_package_segment(client_name)}/"
            f"Diagnostic{java_class_suffix(client_name)}JobsSteps.java")


def diagnostic_java(client_name: str) -> str:
    """
    Step temporaire, lecture seule : liste des jobs de l'environnement (nom, type, actif) dans la console, avec
    l'API de la bibliothèque 26.16 (EndPoints.sendGetRestToDisplayList, KisJob, Definition, Trigger).
    """
    from tools.cucumber_tools import java_class_suffix, java_package_segment
    return f"""package cucumber.features.{java_package_segment(client_name)};

import cucumber.endPoints.EndPoints;
import cucumber.features.BaseStep;
import cucumber.pojo.response.KIS.kisJob.KisJob;
import cucumber.util.context.TestContext;
import io.cucumber.java.en.Given;

import java.util.List;

/** TEMPORAIRE (diagnostic de l'init, à retirer) : jobs présents sur l'environnement, lecture seule. */
public class Diagnostic{java_class_suffix(client_name)}JobsSteps extends BaseStep {{

    public Diagnostic{java_class_suffix(client_name)}JobsSteps(TestContext testContext) {{
        super(testContext);
    }}

    @Given("{DIAGNOSTIC_STEP}")
    public void listeDesJobs() throws Exception {{
        List<KisJob> jobs = EndPoints.sendGetRestToDisplayList("jobs", KisJob.class);
        System.out.println("{DIAGNOSTIC_PREFIX} " + jobs.size() + " job(s)");
        for (KisJob job : jobs) {{
            String type = job.getDefinition() == null ? "?" : job.getDefinition().getType();
            String active = job.getTriggers() == null || job.getTriggers().isEmpty() ? "sans trigger"
                    : String.valueOf(job.getTriggers().get(0).getActive());
            System.out.println("{DIAGNOSTIC_PREFIX} " + job.getName() + " | " + type + " | actif=" + active);
        }}
    }}
}}
"""


def with_diagnostic(text: str) -> str:
    """Step de diagnostic ajouté en tête du premier scénario (avant la première désactivation), en « Given »."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("Scenario"):
            j = i + 1
            while j < len(lines) and not _STEP.match(lines[j]):
                j += 1
            if j >= len(lines):
                return text
            match = _STEP.match(lines[j])
            eol = "\r\n" if lines[j].endswith("\r\n") else "\n"
            lines[j] = lines[j].replace(match.group(2), "And", 1) if match.group(2) == "Given" else lines[j]
            lines.insert(j, f"{match.group(1)}Given {DIAGNOSTIC_STEP}{eol}")
            return "".join(lines)
    return text


def diagnostic_lines(console: str) -> list:
    """Lignes du diagnostic lues dans la console Jenkins (horodatage retiré)."""
    return [line.split(DIAGNOSTIC_PREFIX, 1)[1].strip() for line in (console or "").splitlines()
            if DIAGNOSTIC_PREFIX in line]


# ---------------------------------------------------------------------------
# Désactivation des jobs : steps du projet quand la bibliothèque du client est défaillante
# ---------------------------------------------------------------------------
# Bibliothèque ≤ 26.16 : « KIS- Désactivation du job » envoie POST jobs/{nom}/inactive puis attend
# triggers[0].active == false avec une assertion DANS la condition d'attente (KISEndPoints.jobActivityIs) : si le
# premier contrôle voit encore le job actif, l'assertion « Pas d'élément correspondant à l'attendu {} pour KisJob »
# interrompt l'attente au lieu de la prolonger (identique en 26.22). Le Standard aligné sur le serveur 26.22
# (commit f5fd6da1b « Pb Jobs et clear full ») suspend et réactive tous les jobs (POST jobs/suspend, /unsuspend)
# autour de l'intégration ; la bibliothèque 26.16 n'a pas ces steps. Steps du projet, de nom unique, sans toucher
# à la bibliothèque partagée ; marqués TODO-VERSION, à remplacer par ceux du Standard à la montée de bibliothèque.

SUSPEND_COMMIT = "f5fd6da1b"
JOBS_NOTE = "[JOBS]"


def library_has_all_jobs(cucumber_std_dir: str, ref: str) -> bool:
    """La bibliothèque à `ref` du dépôt Standard définit « KIS- Désactivation de tous les jobs »."""
    shown = _git(cucumber_std_dir, "show", f"{ref}:src/test/java/cucumber/features/kis/KISJobStep.java")
    return shown.returncode == 0 and f'"{ALL_OFF}"' in shown.stdout


def project_job_steps(client_name: str) -> dict:
    """{step de la bibliothèque : step du projet} — préfixe du client (ex. « CANSON- »), nom unique."""
    from tools.cucumber_tools import java_class_suffix
    prefix = re.sub(r"[^A-Z0-9]", "", java_class_suffix(client_name).upper())
    return {k: k.replace("KIS- ", f"{prefix}- ", 1) for k in (ALL_OFF, ALL_ON, ONE_OFF, ONE_ON)}


def jobs_java_path(client_name: str) -> str:
    from tools.cucumber_tools import java_class_suffix, java_package_segment
    return (f"src/test/java/cucumber/features/{java_package_segment(client_name)}/"
            f"Jobs{java_class_suffix(client_name)}Steps.java")


def jobs_java(client_name: str, library_version: str) -> str:
    """
    Steps du projet (API de la bibliothèque 26.16 : EndPoints.sendPostRest, sendGetRestToDisplayList, kisJobs,
    ConstantConfig.TIME_MAX_JOB) : suspension / réactivation de tous les jobs comme le Standard f5fd6da1b, et
    (dés)activation par job dont l'attente relit l'état sans assertion avant la fin du délai, puis échoue avec
    l'état réellement lu de chaque job.
    """
    from tools.cucumber_tools import java_class_suffix, java_package_segment
    steps = project_job_steps(client_name)
    name = f"Jobs{java_class_suffix(client_name)}Steps"
    return f"""package cucumber.features.{java_package_segment(client_name)};

import cucumber.endPoints.EndPoints;
import cucumber.features.BaseStep;
import cucumber.pojo.response.KIS.kisJob.KisJob;
import cucumber.util.constant.config.ConstantConfig;
import cucumber.util.context.TestContext;
import io.cucumber.datatable.DataTable;
import io.cucumber.java.en.Given;
import org.junit.Assert;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * TODO-VERSION : bibliothèque {library_version} sans « {ALL_OFF} » / « {ALL_ON} » (Standard
 * {SUSPEND_COMMIT}, aligné sur le serveur 26.22) et dont « {ONE_OFF} » échoue au premier contrôle de son attente
 * (assertion dans la condition). Steps du projet, à remplacer par ceux du Standard à la montée de bibliothèque.
 */
public class {name} extends BaseStep {{

    private static final String URL_JOBS = "jobs";

    public {name}(TestContext testContext) {{
        super(testContext);
    }}

    @Given("{steps[ALL_OFF]}")
    public void desactivationDeTousLesJobs() {{
        EndPoints.sendPostRest(URL_JOBS + "/suspend");
        EndPoints.kisJobs = null;
        System.out.println("{JOBS_NOTE} Tous les jobs suspendus (POST jobs/suspend)");
    }}

    @Given("{steps[ALL_ON]}")
    public void activationDeTousLesJobs() {{
        EndPoints.sendPostRest(URL_JOBS + "/unsuspend");
        EndPoints.kisJobs = null;
        System.out.println("{JOBS_NOTE} Tous les jobs réactivés (POST jobs/unsuspend)");
    }}

    @Given("{steps[ONE_OFF]}")
    public void desactivationDuJob(DataTable jobList) throws Exception {{
        changeActivity(jobList, false);
    }}

    @Given("{steps[ONE_ON]}")
    public void activationDuJob(DataTable jobList) throws Exception {{
        changeActivity(jobList, true);
    }}

    private static void changeActivity(DataTable jobList, boolean active) throws Exception {{
        List<String> names = jobList.asMaps(String.class, String.class).stream().map(m -> m.get("name"))
                .collect(Collectors.toList());
        List<KisJob> existing = EndPoints.sendGetRestToDisplayList(URL_JOBS, KisJob.class);
        for (String name : names) {{
            if (existing.stream().noneMatch(j -> name.equals(j.getName()))) {{
                Assert.fail("Job [" + name + "] absent de l'environnement");
            }}
            EndPoints.sendPostRest(URL_JOBS + "/" + name + (active ? "/active" : "/inactive"));
        }}
        long deadline = System.currentTimeMillis() + ConstantConfig.TIME_UNIT.toMillis(ConstantConfig.TIME_MAX_JOB);
        List<String> pending = new ArrayList<>(names);
        while (true) {{
            List<KisJob> jobs = EndPoints.sendGetRestToDisplayList(URL_JOBS, KisJob.class);
            pending.removeIf(name -> jobs.stream().anyMatch(j -> name.equals(j.getName())
                    && (j.getTriggers() == null || j.getTriggers().isEmpty()
                    || Boolean.valueOf(active).equals(j.getTriggers().get(0).getActive()))));
            if (pending.isEmpty()) {{
                break;
            }}
            if (System.currentTimeMillis() > deadline) {{
                String states = jobs.stream().filter(j -> pending.contains(j.getName()))
                        .map(j -> j.getName() + " : active=" + j.getTriggers().get(0).getActive())
                        .collect(Collectors.joining(", "));
                Assert.fail("Jobs non " + (active ? "activés" : "désactivés") + " après "
                        + ConstantConfig.TIME_MAX_JOB + " " + ConstantConfig.TIME_UNIT + " : " + states);
            }}
            Thread.sleep(1000);
        }}
        EndPoints.kisJobs = null;
        System.out.println("{JOBS_NOTE} " + names.size() + " job(s) " + (active ? "activé(s)" : "désactivé(s)")
                + " : " + String.join(", ", names));
    }}
}}
"""


def _blocks(lines: list) -> list:
    """Steps actifs ou commentés d'une feature : [(début, fin exclue, scénario, step, commenté)], table comprise."""
    out, scenario, i = [], -1, 0
    while i < len(lines):
        raw = lines[i].lstrip()
        if raw.startswith(("Scenario", "Background")):
            scenario += 1
        body = raw[1:].lstrip() if raw.startswith("#") else raw
        match = _STEP.match("  " + body.rstrip("\r\n"))
        if match and not body.startswith("|"):
            j = i + 1
            while j < len(lines) and (lines[j].strip().lstrip("#").strip().startswith("|")
                                      or lines[j].strip().startswith(f"# {TODO_MARKER}")):
                j += 1
            out.append((i, j, scenario, match.group(3), raw.startswith("#")))
            i = j
            continue
        i += 1
    return out


def use_project_job_steps(text: str, client_name: str, library_version: str) -> tuple:
    """
    Feature d'init : steps de jobs remplacés par ceux du projet (project_job_steps), table conservée.
      - « tous les jobs » commenté TODO-VERSION (absent de la bibliothèque) ou actif : rétabli en step du projet ;
      - désactivation par job suivie, dans le même scénario, d'une activation par job (désactivation autour de la
        purge) : suspension puis réactivation de tous les jobs, tables gardées en commentaire ;
      - autre (dés)activation par job : step du projet à attente tolérante, même table.
    Renvoie (texte, [notes]). Rien n'est supprimé.
    """
    steps = project_job_steps(client_name)
    lines = text.splitlines(keepends=True)
    blocks = _blocks(lines)
    by_start = {b[0]: b for b in blocks}
    paired = {}
    for n, (start, _, scenario, step, commented) in enumerate(blocks):
        if commented or step != ONE_OFF:
            continue
        on = next((b for b in blocks[n + 1:] if b[2] == scenario and not b[4] and b[3] in (ONE_OFF, ONE_ON)), None)
        if on and on[3] == ONE_ON:
            paired[start], paired[on[0]] = ALL_OFF, ALL_ON
    out, notes, i = [], [], 0
    while i < len(lines):
        block = by_start.get(i)
        if not block or block[3] not in (ALL_OFF, ALL_ON, ONE_OFF, ONE_ON) or (block[4] and block[3] in (ONE_OFF,
                                                                                                          ONE_ON)):
            out.append(lines[i])
            i += 1
            continue
        start, end, _, step, commented = block
        line = lines[start]
        eol = "\r\n" if line.endswith("\r\n") else "\n"
        indent = line[:len(line) - len(line.lstrip())]
        keyword = _STEP.match("  " + line.lstrip().lstrip("#").strip()).group(2)
        if out and cucumber_todo_version(out[-1]) and step in (ALL_OFF, ALL_ON):
            out.pop()
        if step in (ALL_OFF, ALL_ON):
            target = steps[step]
            api = "jobs/suspend" if step == ALL_OFF else "jobs/unsuspend"
            out.append(f"{indent}# TODO-VERSION: « {step} » absent de la bibliothèque {library_version} : step du "
                       f"projet (POST {api}, Standard {SUSPEND_COMMIT}), à remplacer à la montée de bibliothèque{eol}")
            out.append(f"{indent}{keyword} {target}{eol}")
            notes.append(f"l. {start + 1} : « {step} » → « {target} »")
            i = end
        elif start in paired:
            target = steps[paired[start]]
            out.append(f"{indent}# TODO-VERSION: « {step} » inopérant avec la bibliothèque {library_version} (attente "
                       f"interrompue au premier contrôle) : {'suspension' if step == ONE_OFF else 'réactivation'} de "
                       f"tous les jobs (Standard {SUSPEND_COMMIT}), liste d'origine en commentaire{eol}")
            out.append(f"{indent}{keyword} {target}{eol}")
            for row in lines[start + 1:end]:
                row_indent = row[:len(row) - len(row.lstrip())]
                out.append(row if row.lstrip().startswith("#") else f"{row_indent}# {row.lstrip()}")
            notes.append(f"l. {start + 1} : « {step} » (par job, autour de la purge) → « {target} »")
            i = end
        else:
            target = steps[step]
            out.append(f"{indent}# TODO-VERSION: « {step} » inopérant avec la bibliothèque {library_version} (attente "
                       f"interrompue au premier contrôle) : step du projet, attente sans assertion avant le délai{eol}")
            out.append(f"{indent}{keyword} {target}{eol}")
            out.extend(lines[start + 1:end])
            notes.append(f"l. {start + 1} : « {step} » → « {target} »")
            i = end
    return "".join(out), notes


def apply_project_steps(worktree: str, paths: list, client_name: str, cucumber_std_dir: str) -> dict:
    """
    Bibliothèque du client (pom.xml du worktree) sans « tous les jobs » : features d'init `paths` passées aux steps
    du projet (use_project_job_steps) et classe Java écrite (jobs_java_path). Bibliothèque qui les a, ou
    introuvable dans le dépôt Standard : rien n'est changé (raison donnée). Renvoie {fichiers, notes, raison}.
    """
    import os

    from tools import cucumber_checks, cucumber_versions
    version = cucumber_checks.library_version(worktree)
    ref = cucumber_versions.library_ref(cucumber_std_dir, version)
    if not ref:
        return {"fichiers": [], "notes": [], "raison": f"bibliothèque {version or '?'} introuvable dans le Standard"}
    if library_has_all_jobs(cucumber_std_dir, ref):
        return {"fichiers": [], "notes": [], "raison": f"bibliothèque {version} : « {ALL_OFF} » disponible"}
    changed, notes = [], []
    for rel in paths:
        full = os.path.join(worktree, *rel.split("/"))
        if not os.path.exists(full):
            continue
        with open(full, encoding="utf-8", newline="") as f:
            text = f.read()
        new, found = use_project_job_steps(text, client_name, version)
        if new != text:
            with open(full, "w", encoding="utf-8", newline="") as f:
                f.write(new)
            changed.append(rel)
            notes += [f"{rel.rsplit('/', 2)[-2]}/{rel.rsplit('/', 1)[-1]} {n}" for n in found]
    if changed:
        java = jobs_java_path(client_name)
        os.makedirs(os.path.join(worktree, *java.split("/")[:-1]), exist_ok=True)
        with open(os.path.join(worktree, *java.split("/")), "w", encoding="utf-8", newline="\n") as f:
            f.write(jobs_java(client_name, version))
        changed.append(java)
    return {"fichiers": changed, "notes": notes, "raison": f"bibliothèque {version} sans « {ALL_OFF} »"}


# ---------------------------------------------------------------------------
# Jobs reliés à un service externe : connexion fournie par le job Jenkins (ADDITIONAL_ARGLINE)
# ---------------------------------------------------------------------------
# Job -> (propriété système qui porte l'adresse du service, step d'init qui configure la connexion, service).
# « INIT- Configuration du colisage » (QAConfigurationStep) appelle RefParameterEndPoints.updateCubingConfiguration :
# configuration SaaS « CUBING » mise à jour avec urlCubing / userCubing / passwordCubing du job Jenkins.
CONNECTED_JOBS = {"Schedule preparation orders precubing": ("urlCubing", "INIT- Configuration du colisage", "cubing")}


def arg_properties(argline: str) -> dict:
    """Propriétés système -Dnom=valeur (valeur entre quotes ou non) d'une ligne d'arguments Maven."""
    return {m.group(1): next((g for g in m.group(2, 3, 4) if g is not None), "")
            for m in re.finditer(r"-D([\w.]+)=(?:'([^']*)'|\"([^\"]*)\"|(\S*))", argline or "")}


def keep_connected_jobs_active(text: str, properties: dict) -> tuple:
    """
    Désactivation (par job) d'un job de CONNECTED_JOBS alors que le job Jenkins fournit la connexion au service
    (propriété non vide dans `properties`) et que l'init la configure plus haut (step actif) : la ligne est
    commentée sous TODO-JOB, le job reste actif. Renvoie (texte, [raisons]).
    """
    lines = text.splitlines()
    missing = []
    for item in usages(text):
        if item["step"] != ONE_OFF or item["nom"] not in CONNECTED_JOBS:
            continue
        prop, config, service = CONNECTED_JOBS[item["nom"]]
        configured = next((n for n, line in enumerate(lines[:item["ligne"]], 1) if not line.lstrip().startswith("#")
                           and (_STEP.match(line) or [None] * 4)[3] == config), 0)
        if not (properties or {}).get(prop) or not configured:
            continue
        missing.append({**item, "raison": f"job « {item['nom']} » laissé actif : connexion {service} fournie par le "
                                          f"job Jenkins (-D{prop}, ADDITIONAL_ARGLINE), configurée par « {config} » "
                                          f"l. {configured}"})
    return comment_unproven(text, missing), [m["raison"] for m in missing]


# ---------------------------------------------------------------------------
# Règles importées avant l'init
# ---------------------------------------------------------------------------
RULES_STEP = "INIT- Initialisation globale des règles"
FEATURES_ROOT, RULES_ROOT = "src/test/resources/cucumber/features/", "src/test/resources/cucumber/rules/"
INIT_MARKER = "TODO-INIT:"


def rules_folder(feature_rel: str) -> str:
    """Dossier lu par « INIT- Initialisation globale des règles » (QARuleStep : RULE_PATH + dossier du scénario)."""
    directory = feature_rel.split(FEATURES_ROOT, 1)[-1].rsplit("/", 1)[0]
    return RULES_ROOT + directory + "/"


def drop_rules_without_folder(text: str, feature_rel: str, worktree: str) -> tuple:
    """
    « INIT- Initialisation globale des règles » sans dossier de règles dans le dépôt (règles importées avant l'init) :
    le step est commenté sous « # TODO-INIT: » (sans dossier, listFiles() renvoie null : NullPointerException).
    Renvoie (texte, [raisons]).
    """
    import os
    folder = rules_folder(feature_rel)
    if os.path.isdir(os.path.join(worktree, *folder.strip("/").split("/"))):
        return text, []
    out, notes = [], []
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        match = _STEP.match(line.rstrip("\r\n"))
        if match and not line.lstrip().startswith("#") and match.group(3) == RULES_STEP:
            eol = "\r\n" if line.endswith("\r\n") else "\n"
            reason = (f"règles importées avant l'init, aucun dossier {folder} dans le dépôt : step sans objet "
                      f"(NullPointerException sinon)")
            out += [f"{match.group(1)}# {INIT_MARKER} {reason}{eol}", f"{match.group(1)}# {line.lstrip()}"]
            notes.append(f"l. {number} : « {RULES_STEP} » commenté — {reason}")
            continue
        out.append(line)
    return "".join(out), notes


def client_imports(client_root: str) -> set:
    """Contribs importées (lignes « import: » actives) par les bundle.list de l'application du client, lus à la
    référence distante ; une ligne commentée (« ### import: ») ne compte pas."""
    found = set()
    for repo_name, repo in git_repos(client_root).items():
        if "cucumber" in repo_name.lower():
            continue
        ref = client_ref(repo)
        for path in _git(repo, "ls-tree", "-r", "--name-only", ref).stdout.splitlines():
            if path.endswith("bundle.list") and "/test/" not in path:
                found |= {a for k, a in _BUNDLE.findall(_git(repo, "show", f"{ref}:{path}").stdout) if k == "import"}
    return found


# Steps propres à un module (produit) : son clear, sa configuration (« INIT- Configuration TMS : "…" »)
_MODULE_STEPS = (
    (re.compile(r"INIT- Purge de l'environnement par le clear \"([a-z]+)/"), "clear « {m}/… »"),
    (re.compile(r"INIT- Configuration ([A-Z]{2,6})\s*:"), "configuration {M}"),
)


def _module_of(line: str) -> tuple:
    for pattern, label in _MODULE_STEPS:
        match = pattern.search(line)
        if match:
            module = match.group(1).lower()
            return module, label.format(m=module, M=module.upper())
    return "", ""


def drop_steps_of_absent_modules(text: str, imports: set, excluded=()) -> tuple:
    """
    Steps d'un module que l'application du client n'importe plus (aucune contrib « <module>-contrib-* » dans ses
    bundle.list, ex. TMS désactivé) — son clear, sa configuration — : routes absentes (404 - No route found), step
    commenté sous TODO-INIT, table comprise. `imports` vide (bundle.list illisible) : rien n'est changé pour ce
    motif. `excluded` : modules exclus du périmètre testé (profil client, cucumber_modules), commentés de même.
    Renvoie (texte, [raisons]).
    """
    excluded = {m.lower() for m in excluded or ()}
    if not imports and not excluded:
        return text, []
    lines = text.splitlines(keepends=True)
    out, notes, i = [], [], 0
    while i < len(lines):
        line = lines[i]
        module, label = _module_of(line) if not line.lstrip().startswith("#") and _STEP.match(line.rstrip("\r\n")) \
            else ("", "")
        absent = bool(imports) and not any(a.startswith(f"{module}-contrib") for a in imports)
        if not module or not (module in excluded or absent):
            out.append(line)
            i += 1
            continue
        indent = line[:len(line) - len(line.lstrip())]
        eol = "\r\n" if line.endswith("\r\n") else "\n"
        reason = (f"module {module.upper()} exclu du périmètre testé (profil client, « Modules testés »)"
                  if module in excluded else
                  f"module {module.upper()} non déployé chez le client (aucune contrib {module}-contrib importée "
                  f"par ses bundle.list) : route absente (404 - No route found)")
        out += [f"{indent}# {INIT_MARKER} {reason}{eol}", f"{indent}# {line.lstrip()}"]
        notes.append(f"l. {i + 1} : {label} commenté — {reason}")
        i += 1
        while i < len(lines) and lines[i].strip().startswith("|"):
            row = lines[i]
            out.append(f"{row[:len(row) - len(row.lstrip())]}# {row.lstrip()}")
            i += 1
    return "".join(out), notes


drop_clears_of_absent_modules = drop_steps_of_absent_modules


def apply_environment_rules(worktree: str, paths: list, properties: dict, imports: set = frozenset(),
                            excluded=(), profile: dict = None) -> dict:
    """Règles sans dossier, clears de modules non déployés et jobs reliés à un service fourni par le job Jenkins,
    sur les features `paths`. `profile` : steps des modules décochés retirés (cucumber_modules), sans trace."""
    import os
    changed, notes = [], []
    for rel in paths:
        full = os.path.join(worktree, *rel.split("/"))
        if not os.path.exists(full):
            continue
        with open(full, encoding="utf-8", newline="") as f:
            text = f.read()
        new, found = drop_rules_without_folder(text, rel, worktree)
        new, cleared = drop_steps_of_absent_modules(new, set(imports), excluded)
        found += cleared
        if profile:
            from tools import cucumber_modules
            new, removed = cucumber_modules.strip_excluded_steps(new, profile)
            found += [f"l. {r['ligne']} retirée (module {r['module']} décoché) : {r['lignes'][0].strip()[:100]}"
                      for r in removed]
        new, kept = keep_connected_jobs_active(new, properties)
        if new != text:
            with open(full, "w", encoding="utf-8", newline="") as f:
                f.write(new)
            changed.append(rel)
            name = f"{rel.rsplit('/', 2)[-2]}/{rel.rsplit('/', 1)[-1]}"
            notes += [f"{name} {n}" for n in found] + [f"{name} {k}" for k in kept]
    return {"fichiers": changed, "notes": notes}


def cucumber_todo_version(line: str) -> bool:
    return line.lstrip().startswith("# TODO-VERSION:")


# ---------------------------------------------------------------------------
# Règle générale d'ordre : partenaire, secteur, règle référencés par l'init
# ---------------------------------------------------------------------------

_REFERENCE = re.compile(r"\b(partenaire|secteur|règles?|site)\s+\"([^\"]+)\"", re.I)


def reference_order(texts: dict, data_texts: dict = None, known: dict = None) -> list:
    """
    Références de l'init (partenaire, secteur, règle, site cités entre guillemets dans un step) : chacune doit être
    créée AVANT par l'init (citée plus haut dans la feature, ou dans un fichier de données qu'elle intègre avant :
    `data_texts` {chemin: texte}, intégrés par l'initialisation globale) ou prouvée présente (`known` {code:
    source}). Contrôle déterministe ; renvoie [{fichier, ligne, type, code, raison}] des références non prouvées.
    """
    out = []
    data = "\n".join((data_texts or {}).values())
    for path, text in texts.items():
        lines = text.splitlines()
        global_init = next((n for n, line in enumerate(lines, 1) if "Initialisation globale par fichiers" in line
                            and not line.lstrip().startswith("#")), 0)
        for number, line in enumerate(lines, 1):
            if line.lstrip().startswith("#") or not _STEP.match(line):
                continue
            for kind, code in _REFERENCE.findall(line):
                before = "\n".join(lines[:number - 1])
                if re.search(rf"(?<![\w-]){re.escape(code)}(?![\w-])", before):
                    continue
                if global_init and global_init < number and re.search(rf"(?<![\w-]){re.escape(code)}(?![\w-])", data):
                    continue
                if (known or {}).get(code):
                    continue
                out.append({"fichier": path, "ligne": number, "type": kind.lower(), "code": code,
                            "raison": f"{kind.lower()} « {code} » référencé l. {number} sans être créé avant par "
                                      f"l'init (step plus haut ou fichier intégré avant) ni prouvé présent"})
    return out
