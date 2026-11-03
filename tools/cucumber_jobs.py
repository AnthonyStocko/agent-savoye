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

from tools.cucumber_tools import CLIENT_REFS, _git, git_repos, pick_ref

TODO_MARKER = "TODO-JOB:"
# Step -> ce qui doit être prouvé : le nom du job (il doit exister) ou sa classe (job créé)
STEPS = {"KIS- Désactivation du job": "nom", "KIS- Activation du job": "nom", "KIS- Modification du job": "nom",
         "KIS- Lancement du job": "nom", "KIS- Création du job": "classe"}
_STEP = re.compile(r"^(\s*)(Given|When|Then|And|But|\*)\s+(.+?)\s*$")
JOB_TABLE = "kis_qrtz_job_details"


def _cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def usages(text: str) -> list:
    """
    Jobs cités par les steps de STEPS (lignes actives) : [{step, preuve, ligne, nom, classe}] ; `ligne` : numéro
    (1-based) de la ligne de table.
    """
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        match = _STEP.match(lines[i])
        i += 1
        if not match or lines[i - 1].lstrip().startswith("#") or match.group(3) not in STEPS:
            continue
        step = match.group(3)
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
        ref = pick_ref(repo, CLIENT_REFS)
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


def from_standard(std_root: str, version: str, classes: list) -> dict:
    """Classes présentes dans les dépôts du Standard à la référence de la version du client : {classe: source}."""
    from tools.cucumber_tools import STD_REFS
    from tools.standard_version import std_refs
    found, wanted = {}, [c for c in classes if c]
    if not wanted:
        return found
    for repo_name, repo in git_repos(std_root).items():
        ref = pick_ref(repo, std_refs(version, STD_REFS))
        if not ref:
            continue
        files = _git(repo, "ls-tree", "-r", "--name-only", ref).stdout.splitlines()
        for cls in [c for c in wanted if c not in found]:
            suffix = "/" + cls.replace(".", "/") + ".java"
            hit = next((f for f in files if f.endswith(suffix)), "")
            if hit:
                found[cls] = f"Standard {repo_name}@{ref.split('/')[-1]}"
        if len(found) == len(wanted):
            break
    return found


_BUNDLE = re.compile(r"^\s*(runtime|import)\s*:\s*[\w.-]+/([\w.-]+)@", re.M)


def deployed_modules(client_root: str, std_root: str, version: str) -> set:
    """
    Modules du Standard déployés chez le client : artefacts « runtime: » des bundle.list de son application
    (référence distante), plus ceux des contribs qu'ils importent (« import: »), lus dans le Standard à sa version,
    récursivement. Vide si rien n'est lisible.
    """
    from tools.cucumber_tools import STD_REFS
    from tools.standard_version import std_refs
    lists = []
    for repo_name, repo in git_repos(client_root).items():
        if "cucumber" in repo_name.lower():
            continue
        ref = pick_ref(repo, CLIENT_REFS)
        for path in _git(repo, "ls-tree", "-r", "--name-only", ref).stdout.splitlines():
            if path.endswith("bundle.list") and "/test/" not in path:
                lists.append(_git(repo, "show", f"{ref}:{path}").stdout)
    index = {}
    for repo in git_repos(std_root).values():
        ref = pick_ref(repo, std_refs(version, STD_REFS))
        if not ref:
            continue
        for path in _git(repo, "ls-tree", "-r", "--name-only", ref).stdout.splitlines():
            # Module Maven = dossier de premier niveau ou suivant (artefact du même nom)
            if path.endswith("bundle.list") and "/src/main/" in path:
                for part in path.split("/src/main/")[0].split("/"):
                    index.setdefault(part, []).append((repo, ref, path))
    modules, seen, pending = set(), set(), list(lists)
    while pending:
        for kind, artifact in _BUNDLE.findall(pending.pop()):
            if kind == "runtime":
                modules.add(artifact)
            elif artifact not in seen:
                seen.add(artifact)
                for repo, ref, path in index.get(artifact, [])[:1]:
                    pending.append(_git(repo, "show", f"{ref}:{path}").stdout)
    return modules


def _module_deployed(module: str, deployed: set) -> bool:
    """Module Maven déployé : un bundle déployé porte son nom, ou son nom suivi d'un suffixe (un module produit
    plusieurs bundles, déclarés dans son kis.descriptor.yaml)."""
    return module in deployed or any(d.startswith(module + "-") for d in deployed)


def _embedded(repo: str, ref: str, module: str, deployed: set) -> bool:
    """
    Module bibliothèque (ex. *-event) embarqué par un module déployé du même dépôt : son pom.xml en dépend
    (<artifactId>module</artifactId>).
    """
    poms = [p for p in _git(repo, "ls-tree", "-r", "--name-only", ref).stdout.splitlines()
            if p.endswith("/pom.xml")]
    for pom in poms:
        owner = pom.rsplit("/", 1)[0].split("/")[-1]
        if owner != module and _module_deployed(owner, deployed) and                 f"<artifactId>{module}</artifactId>" in _git(repo, "show", f"{ref}:{pom}").stdout:
            return True
    return False


def names_in_standard(std_root: str, version: str, names: list, deployed: set = None) -> tuple:
    """
    Noms de jobs cités littéralement (entre guillemets) dans le Standard à la version du client, dans un module
    déployé chez lui (`deployed`, deployed_modules ; None : non vérifié). ({nom: source}, {nom: module non déployé}).
    """
    from tools.cucumber_tools import STD_REFS
    from tools.standard_version import std_refs
    found, elsewhere, wanted = {}, {}, [n for n in names if n]
    for repo_name, repo in git_repos(std_root).items():
        if len(found) == len(wanted):
            break
        ref = pick_ref(repo, std_refs(version, STD_REFS))
        if not ref:
            continue
        for name in [n for n in wanted if n not in found]:
            grep = _git(repo, "grep", "-I", "-l", "-F", f'"{name}"', ref, "--", "*.java")
            for hit in (line.split(":", 1)[-1] for line in grep.stdout.splitlines()):
                module = hit.split("/", 1)[0]
                if deployed is None or _module_deployed(module, deployed) or _embedded(repo, ref, module, deployed):
                    found[name] = f"Standard {repo_name}@{ref.split('/')[-1]} : {hit}"
                    break
                elsewhere.setdefault(name, f"{repo_name}/{hit}")
    return found, {n: m for n, m in elsewhere.items() if n not in found}


KIS_PROJECT = "KIS"


def names_in_kis(names: list, search=None) -> dict:
    """
    Noms de jobs définis dans le socle KIS (recherche de code Bitbucket du projet KIS, branche indexée par
    Bitbucket) : {nom: source}. `search(requête) -> [(dépôt, fichier)]` remplaçable (tests).
    """
    if search is None:
        search = _bitbucket_code_search
    found = {}
    for name in [n for n in names if n]:
        try:
            hits = search(f'"{name}" project:{KIS_PROJECT}')
        except Exception:  # noqa: BLE001 - recherche indisponible : aucune preuve, jamais devinée
            hits = []
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
        return []
    response = requests.post(f"{base}/rest/search/latest/search", timeout=30,
                             headers={"Authorization": f"Bearer {token}"} if token else {},
                             json={"query": query, "entities": {"code": {"start": 0, "limit": 10}}})
    if response.status_code != 200:
        return []
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
        if not match or lines[i].lstrip().startswith("#") or match.group(3) not in STEPS:
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


def check_init(texts: dict, client_root: str, schemas: list, std_root: str = "", version: str = "",
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
    deployed = deployed_modules(client_root, std_root, version) if std_root and client_root and left else set()
    standard_names, not_deployed = names_in_standard(std_root, version, left, deployed or None)         if std_root and left else ({}, {})
    left = [n for n in left if n not in standard_names]
    kis = names_in_kis(left, kis_search) if left else {}
    standard = from_standard(std_root, version, [c for c in classes if c not in code["classes"]]) if std_root else {}
    known = {"noms": {**kis, **standard_names, **code["noms"]}, "classes": {**standard, **code["classes"]}}
    ok, missing = proven(items, known)
    for item in ok + missing:
        item["en_base"] = item["nom"] in database["noms"]
    for item in missing:
        if item["nom"] in not_deployed and "avant sa création" not in item["raison"]:
            item["raison"] += f" ; défini seulement dans {not_deployed[item['nom']]}, module non déployé chez le client"
    return {"prouves": ok, "non_prouves": missing, "base": database["source"], "erreur_base": database["erreur"],
            "modules_deployes": len(deployed)}


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
