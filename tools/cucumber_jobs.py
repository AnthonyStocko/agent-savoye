"""
Jobs KIS de l'initialisation d'un client (0 token) : chaque job désactivé, activé, modifié ou lancé doit être
prouvé présent chez CE client, et la classe de chaque job créé aussi.

L'initialisation copiée du Standard cite des jobs du Standard (ex. integrate-cdc-messages) qu'un client ne
déploie pas : le step échoue sur « Pas d'élément correspondant à l'attendu {} pour KisJob » et arrête toute l'init.

Preuves, dans l'ordre :
  1. base du client (schéma du projet, ex. cansond) : table kis_qrtz_job_details (job_name, job_class_name) ;
  2. code ou configuration de son application (dépôts hors Cucumber, référence distante) : nom du job cité
     littéralement, ou classe du job présente (fichier <Classe>.java) ;
  3. classe d'un job créé : présente dans le code du Standard à la version du client (références de version,
     tools/standard_version) ;
  4. job désactivé, activé, modifié ou lancé après avoir été créé plus haut par l'init (création prouvée).
Un job non prouvé n'est jamais deviné : sa ligne est commentée sous « # TODO-JOB: <raison> » (le step entier si
plus aucune ligne ne reste), et listé avec sa raison.
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


def proven(items: list, known: dict) -> tuple:
    """
    (prouvés [{..., source}], non prouvés [{..., raison}]) ; un job dont la création est prouvée plus haut dans la
    même feature compte comme présent pour la suite.
    """
    ok, missing, created = [], [], {}
    for item in items:
        key, pool = (item["nom"], "noms") if item["preuve"] == "nom" else (item["classe"], "classes")
        source = (known.get(pool) or {}).get(key, "")
        if not source and item["preuve"] == "nom":
            source = created.get((item.get("fichier", ""), item["nom"]), "")
        if source and item["preuve"] == "classe":
            created[(item.get("fichier", ""), item["nom"])] = f"créé par l'init (l. {item['ligne']})"
        if source:
            ok.append({**item, "source": source})
        else:
            what = f"job « {item['nom']} »" if item["preuve"] == "nom" else \
                f"classe {item['classe'] or '(vide)'} du job créé « {item['nom']} »"
            missing.append({**item, "raison": f"{what} absent chez le client (ni base, ni code ou configuration de "
                                              f"son application)"})
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


def check_init(texts: dict, client_root: str, schemas: list, std_root: str = "", version: str = "") -> dict:
    """
    Contrôle des features d'init {chemin: texte} : {"prouves", "non_prouves", "base", "erreur_base"} ; chaque
    usage porte son chemin (« fichier »).
    """
    items = [{**u, "fichier": path} for path, text in texts.items() for u in usages(text)]
    database = from_database(schemas)
    names = sorted({i["nom"] for i in items if i["preuve"] == "nom" and i["nom"] not in database["noms"]})
    classes = sorted({i["classe"] for i in items if i["preuve"] == "classe" and i["classe"] not in database["classes"]})
    code = from_code(client_root, names, classes) if client_root and (names or classes) else {"noms": {}, "classes": {}}
    standard = from_standard(std_root, version, [c for c in classes if c not in code["classes"]]) if std_root else {}
    known = {"noms": {**code["noms"], **database["noms"]},
             "classes": {**standard, **code["classes"], **database["classes"]}}
    ok, missing = proven(items, known)
    return {"prouves": ok, "non_prouves": missing, "base": database["source"], "erreur_base": database["erreur"]}


def apply(worktree: str, paths: list, client_root: str, schemas: list, std_root: str = "", version: str = "") -> dict:
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
    result = check_init(texts, client_root, schemas, std_root, version)
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
    lines = [f"{len(result['prouves'])} job(s) prouvé(s)" + (f" (base : {result['base']})" if result.get("base") else
                                                             f" (base indisponible : {result.get('erreur_base')})")]
    lines += [f"{m['fichier'].rsplit('/', 2)[-2]}/{m['fichier'].rsplit('/', 1)[-1]} l. {m['ligne']} — {m['step']} : "
              f"{m['raison']}" for m in result["non_prouves"]]
    return lines
