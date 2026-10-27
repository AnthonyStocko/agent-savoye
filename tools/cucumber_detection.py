"""
Détection automatique du projet Cucumber d'un client (onglet Cucumber, main.py --detect-only), 0 token.

Dès que le projet est choisi et ses dépôts prêts, puis à chaque changement de commit des dépôts (client,
<client>-cucumber à sa base, Cucumber Standard), une tâche de fond enchaîne l'inventaire, le profil client et le
relevé des faits de l'initialisation des données. Le résultat est mis en cache par empreinte des commits :
.tmp/cucumber/<client>/detection.json {"empreinte", "depots", "date", "verifie_le", "statut": ok|erreur, "cause",
"faits"}. La détection ne modifie jamais rien dans les dépôts : lecture dans un worktree détaché du dépôt client
(aucune branche créée), dans les copies des fichiers de l'inventaire et par `git rev-parse`.

L'état de l'init (init_status) se calcule à l'affichage, sans relire les dépôts, à partir des faits relevés, du
profil appliqué et du dernier run importé :
- absente : aucune feature d'init (tag @INIT_* ou dossier d'init, step de purge, intégration globale) ;
- incomplete : au moins un constat (clear non conforme, donnée d'initializer effacée et non recréée, déclaration de
  champ personnalisé absente, contrôle d'environnement absent, step commenté TODO-VERSION, init en échec au dernier
  run importé) ;
- complete : rien à signaler.
Chaque constat porte sa preuve : fichier, ligne et extrait, ou résultat de run.
"""
import hashlib
import json
import os
import re
import subprocess
import time
import zipfile
from datetime import datetime

from tools import cucumber_environment, cucumber_init_data, cucumber_profile, cucumber_recreation, cucumber_versions
from tools.cucumber_tools import _git, _read_json, _read_text_file, _write_json, cache_dir, git_repos

INIT_STATES = {"absente": "init absente", "incomplete": "init incomplète", "complete": "init complète"}
FINDINGS = {"absente": "aucune initialisation des données",
            "clear": "clear non conforme à l'environnement",
            "initializer": "donnée d'initializer effacée par le clear et non recréée",
            "champ_personnalise": "déclaration de champ personnalisé absente",
            "controle_environnement": "contrôle d'environnement absent",
            "todo_version": "step d'init commenté TODO-VERSION",
            "init_en_echec": "init en échec au dernier run importé"}
# Constats que le lot « Compléments d'initialisation » livre dans une nouvelle feature (fichiers protégés intacts) ;
# les autres y figurent avec leur preuve, à traiter à la main
DELIVERABLE = ("initializer", "champ_personnalise", "controle_environnement")
# Constats qui rendent tous les tests dépendants de l'init
GLOBAL_FINDINGS = ("absente", "clear", "init_en_echec")
PROJECT_FILE = "projet.json"
DEFAULT_INTERVAL = 300
_PURGE_STEP = re.compile(r"^INIT- Purge\b")
_PURGE_ARGUMENT = re.compile(r"^INIT- Purge\b.*\"([^\"]+)\"\s*$")
_STEP = re.compile(r"^\s*(Given|When|Then|And|But|\*|Soit|Quand|Alors|Et|Mais)\s+(.+?)\s*$")
_GLOBAL_INIT = "Initialisation globale par fichiers"
# Attributs de champs personnalisés des entités (customString1, customDate2…), à déclarer dans l'environnement
CUSTOM_FIELD = re.compile(r"\bcustom(?:String|Integer|Int|Long|Decimal|Double|Date|DateTime|Boolean)\d+\b")
_DECLARATION_FILE = re.compile(r"custom.?field", re.I)
_PROCESSES = {}


def detection_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "detection.json")


def log_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "detection.log")


def load_detection(client_name: str) -> dict:
    return _read_json(detection_path(client_name), {}) or {}


def interval() -> int:
    """Délai entre deux contrôles de commits (CUCUMBER_DETECTION_INTERVAL, secondes)."""
    try:
        return max(30, int(os.getenv("CUCUMBER_DETECTION_INTERVAL", "") or DEFAULT_INTERVAL))
    except ValueError:
        return DEFAULT_INTERVAL


# ---------------------------------------------------------------------------
# Projets connus
# ---------------------------------------------------------------------------

def save_project(client_name: str, settings: dict) -> None:
    """Réglages du projet (dépôts, schéma, application) : rechargés quand le projet est choisi dans la liste."""
    _write_json(os.path.join(cache_dir(client_name), PROJECT_FILE), settings)


def known_projects(cache_root: str) -> dict:
    """{client: réglages} des projets déjà travaillés (dossier de cache avec réglages, profil ou génération)."""
    out = {}
    if not os.path.isdir(cache_root):
        return out
    for name in sorted(os.listdir(cache_root)):
        folder = os.path.join(cache_root, name)
        if name.startswith(("_", ".")) or not os.path.isdir(folder):
            continue
        if not any(os.path.exists(os.path.join(folder, f)) for f in (PROJECT_FILE, "profil.json", "etat.json",
                                                                      "inventaire.json")):
            continue
        settings = _read_json(os.path.join(folder, PROJECT_FILE), {}) or {}
        if not settings.get("client_repo"):
            settings["client_repo"] = (_read_json(os.path.join(folder, "inventaire.json"), {}) or {}).get(
                "depot_client", "")
        out[name] = settings
    return out


# ---------------------------------------------------------------------------
# Empreinte des commits
# ---------------------------------------------------------------------------

def repo_commits(client_root: str, cucumber_repo_dir: str, base: str, cucumber_std_dir: str) -> dict:
    """Commits des dépôts lus par la détection : dépôts du client, base du dépôt Cucumber client, Standard."""
    commits = {f"client/{name}": _git(path, "rev-parse", "HEAD").stdout.strip()
               for name, path in git_repos(client_root).items()}
    commits["cucumber"] = _git(cucumber_repo_dir, "rev-parse", base).stdout.strip() if base else ""
    commits["standard"] = _git(cucumber_std_dir, "rev-parse", "HEAD").stdout.strip() if cucumber_std_dir else ""
    return commits


def fingerprint(commits: dict) -> str:
    return hashlib.sha1(json.dumps(commits, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def up_to_date(client_name: str, print_ok: str) -> bool:
    """Détection en cache, réussie, pour cette empreinte : seule la date du contrôle est mise à jour."""
    document = load_detection(client_name)
    if document.get("statut") != "ok" or document.get("empreinte") != print_ok:
        return False
    document["verifie_le"] = datetime.now().isoformat(timespec="seconds")
    _write_json(detection_path(client_name), document)
    return True


def save_result(client_name: str, commits: dict, facts: dict) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    document = {"empreinte": fingerprint(commits), "depots": commits, "date": now, "verifie_le": now,
                "statut": "ok", "cause": "", "faits": facts}
    _write_json(detection_path(client_name), document)
    return document


def save_error(client_name: str, cause: str) -> dict:
    """Échec de la détection : cause gardée, derniers faits conservés (le reste de l'onglet reste utilisable)."""
    document = load_detection(client_name)
    document.update(statut="erreur", cause=" ".join((cause or "cause inconnue").split())[:300],
                    verifie_le=datetime.now().isoformat(timespec="seconds"))
    _write_json(detection_path(client_name), document)
    return document


# ---------------------------------------------------------------------------
# Faits de l'initialisation (lecture seule)
# ---------------------------------------------------------------------------

def _active_lines(text: str) -> list:
    """[(numéro, ligne)] non commentées."""
    return [(n, line) for n, line in enumerate(text.splitlines(), 1) if line.strip() and
            not line.strip().startswith("#")]


def feature_facts(rel: str, text: str) -> dict:
    """Faits d'une feature d'init : purges, intégration globale, tokens actifs, lignes TODO, contrôle d'environnement."""
    purges, steps, todos = [], [], []
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if any(m in stripped for m in (cucumber_versions.TODO_MARKER, cucumber_recreation.TODO_MARKER,
                                       cucumber_environment.TODO_MARKER)):
            todos.append({"fichier": rel, "ligne": n, "extrait": stripped[:200]})
        match = _STEP.match(line)
        if not match or stripped.startswith("#"):
            continue
        steps.append(match.group(2))
        if _PURGE_STEP.match(match.group(2)):
            argument = _PURGE_ARGUMENT.match(match.group(2))
            purges.append({"fichier": rel, "ligne": n, "extrait": stripped[:200],
                           "clear": argument.group(1) if argument else ""})
    active = " ".join(line for _, line in _active_lines(text))
    return {"chemin": rel, "purges": purges, "integration_globale": any(_GLOBAL_INIT in s for s in steps),
            "steps": steps, "tokens": sorted(set(re.findall(r"[\w.\-]+", active))), "todo": todos}


def _data_text(path: str) -> str:
    """Texte d'un fichier de données (xlsx / xlsm : chaînes partagées et feuilles ; sinon texte brut)."""
    if path.lower().endswith((".xlsx", ".xlsm")):
        try:
            with zipfile.ZipFile(path) as archive:
                return " ".join(archive.read(n).decode("utf-8", "replace") for n in archive.namelist()
                                if n.startswith("xl/") and n.endswith(".xml"))
        except (OSError, zipfile.BadZipFile, KeyError):
            return ""
    return _read_text_file(path)


def declared_fields(worktree: str, init: dict) -> dict:
    """Champs personnalisés déclarés par l'init : fichiers de déclaration (nom « CustomField ») qu'elle intègre."""
    out = {}
    for feature in (init or {}).get("features") or []:
        for rel in feature.get("fichiers") or []:
            if _DECLARATION_FILE.search(rel.rsplit("/", 1)[-1]):
                for code in CUSTOM_FIELD.findall(_data_text(os.path.join(worktree, *rel.split("/")))):
                    out.setdefault(code, rel)
    return out


def _proof(rel: str, lines: list, token: str) -> dict:
    """Première ligne [(numéro, ligne)] qui contient `token`."""
    for n, line in lines:
        if token in line:
            return {"fichier": rel, "ligne": n, "extrait": line.strip()[:200]}
    return {"fichier": rel, "ligne": 0, "extrait": ""}


def usages(worktree: str, features_root: str, init_paths: list, inventory: dict, codes: set) -> dict:
    """
    Usages, lignes commentées exclues : champs personnalisés utilisés par les tests (features du client hors init)
    et par les processus Studio des spécifiques (customized.xml) ; `codes` des données d'initializers dans ces
    features et dans tous les fichiers des spécifiques (copies de l'inventaire). {code: [preuves]}, chaque preuve
    avec la tâche concernée ("spec") quand elle vient d'un spécifique.
    """
    found = {}

    def scan(rel: str, text: str, spec: str = "", fields: bool = True) -> None:
        lines = (_active_lines(text) if rel.endswith(".feature") else
                 list(enumerate(text.splitlines(), 1)))
        active = "\n".join(line for _, line in lines)
        hits = (set(CUSTOM_FIELD.findall(active)) if fields else set()) | {
            c for c in codes if c and re.search(rf"\b{re.escape(c)}\b", active)}
        for code in sorted(hits):
            proofs = found.setdefault(code, [])
            if len(proofs) < 20:
                proofs.append({**_proof(rel, lines, code), "spec": spec})

    root = os.path.join(worktree, *features_root.strip("/").split("/")) if features_root else ""
    skip = set(init_paths)
    if root and os.path.isdir(root):
        for folder, _, names in os.walk(root):
            for name in sorted(n for n in names if n.endswith(".feature")):
                rel = os.path.relpath(os.path.join(folder, name), worktree).replace(os.sep, "/")
                if rel not in skip:
                    scan(rel, _read_text_file(os.path.join(folder, name)))
    for spec in (inventory or {}).get("specifiques") or []:
        for item in spec.get("fichiers") or []:
            local = item.get("local") or ""
            if local and os.path.isfile(local):
                scan(f"{item.get('repo', '')}/{item.get('path', '')}", _read_text_file(local), spec["id"],
                     fields=(item.get("path") or "").endswith("customized.xml"))
    return found


def init_facts(worktree: str, client_name: str, profile: dict, inventory: dict) -> dict:
    """
    Faits relevés dans le worktree du dépôt client à sa base (lecture seule) : features d'init, champs déclarés,
    usages des champs personnalisés et des données d'initializers par les tests et les processus.
    """
    init = cucumber_init_data.client_init(worktree, client_name, os.getenv("CUCUMBER_INIT_TAGS", ""))
    paths = cucumber_init_data.init_paths(init)
    features = [feature_facts(p, _read_text_file(os.path.join(worktree, *p.split("/")))) for p in paths]
    # Feature hors init qui purge ou intègre globalement : trace d'une init existante mal rangée
    stray = []
    root = os.path.join(worktree, *cucumber_init_data.FEATURES_ROOT.strip("/").split("/"))
    for folder, _, names in os.walk(root) if os.path.isdir(root) else []:
        for name in (n for n in names if n.endswith(".feature")):
            rel = os.path.relpath(os.path.join(folder, name), worktree).replace(os.sep, "/")
            if rel not in paths:
                facts = feature_facts(rel, _read_text_file(os.path.join(folder, name)))
                if facts["integration_globale"] or facts["purges"]:
                    stray.append(rel)
    codes = {d.get("code") for d in (profile or {}).get("server_initialized_data") or [] if d.get("code")}
    return {"features": features, "hors_init": sorted(stray), "fichiers": sorted({f for x in init["features"]
                                                                                  for f in x["fichiers"]}),
            "champs_declares": declared_fields(worktree, init),
            "usages": usages(worktree, (profile or {}).get("features_root", ""), paths, inventory, codes)}


# ---------------------------------------------------------------------------
# État de l'init (calculé à l'affichage, sans relire les dépôts)
# ---------------------------------------------------------------------------

def expected_clear(profile: dict) -> str:
    """
    Clear propre à l'environnement : saisi ou fixé par le dépôt dans le profil, à défaut CUCUMBER_CLEAR_NAME. Le
    clear détecté dans l'init n'est pas une référence (il serait comparé à lui-même).
    """
    if (profile.get("sources") or {}).get("clear_name") in ("saisi", "depot"):
        return profile.get("clear_name", "")
    return os.getenv("CUCUMBER_CLEAR_NAME", "").strip()


def _finding(kind: str, message: str, proofs: list, codes: list = ()) -> dict:
    return {"type": kind, "libelle": FINDINGS[kind], "message": message, "preuves": list(proofs)[:10],
            "codes": list(codes), "livrable": kind in DELIVERABLE}


def proof_line(proof: dict) -> str:
    """« fichier:ligne — extrait » ou « run importé n° N : scénario, step : message »."""
    if proof.get("run"):
        return (f"run importé n° {proof['run']} : {proof.get('scenario', '?')}, premier step en échec "
                f"« {proof.get('step') or '—'} » : {proof.get('message', '')[:160]}")
    where = f"{proof.get('fichier', '?')}" + (f":{proof['ligne']}" if proof.get("ligne") else "")
    return where + (f" — {proof['extrait']}" if proof.get("extrait") else "")


def init_status(facts: dict, profile: dict, client_name: str, analysis: dict = None, imports_dir: str = "") -> dict:
    """
    État de l'initialisation : {"etat": absente|incomplete|complete, "constats": [{type, libelle, message, preuves,
    codes, livrable}]}. Sans faits (détection jamais réussie) : {"etat": ""}.
    """
    if not facts:
        return {"etat": "", "constats": []}
    features = facts.get("features") or []
    if not features:
        proof = {"fichier": profile.get("features_root") or cucumber_init_data.FEATURES_ROOT, "ligne": 0,
                 "extrait": "aucune feature taguée @INIT_*, sous un dossier d'init (0x-Contexte), avec un step de "
                            "purge ou une intégration globale"
                            + (f" ; purge ou intégration globale hors init : {', '.join(facts['hors_init'][:3])}"
                               if facts.get("hors_init") else "")}
        return {"etat": "absente", "constats": [_finding("absente", "aucune feature d'initialisation des données "
                                                                     "dans le dépôt client", [proof])]}
    findings = []
    tokens = {t for f in features for t in f["tokens"]}
    # Clear : chaque feature d'init purge avec le clear de l'environnement
    clear = expected_clear(profile)
    purges = [p for f in features for p in f["purges"]]
    if not purges:
        findings.append(_finding("clear", "aucun step de purge dans l'init : l'environnement n'est pas remis à zéro",
                                 [{"fichier": features[0]["chemin"], "ligne": 0, "extrait": "aucun step « INIT- "
                                                                                             "Purge »"}]))
    else:
        wrong = [p for p in purges if not p["clear"] or (clear and p["clear"] != clear)]
        if wrong:
            findings.append(_finding("clear", (f"clear attendu « {clear} »" if clear else
                                               "purge Standard sans clear nommé")
                                     + f" : {len(wrong)} purge(s) non conforme(s)", wrong))
    # Données d'initializers effacées par le clear : recréées par une ligne active de l'init
    todo_lines = [t for f in features for t in f["todo"]]
    missing = [d for d in cucumber_recreation.to_recreate(profile) if d["code"] not in tokens]
    if missing:
        proofs = []
        for d in missing:
            commented = next((t for t in todo_lines if d["code"] in t["extrait"]), None)
            proofs.append(commented or {"fichier": d.get("depot", "?") + f" ({d.get('classe', '?')})", "ligne": 0,
                                        "extrait": f"{d.get('entite', '?')} {d['code']} créé par l'initializer, "
                                                   f"absent des features d'init"})
        findings.append(_finding("initializer", f"{len(missing)} donnée(s) effacée(s) par le clear non recréée(s)",
                                 proofs, [d["code"] for d in missing]))
    # Contrôles d'environnement : dépendances du profil et des runs importés contrôlées par l'init
    dependencies = cucumber_environment.dependencies(profile, imports_dir)
    controlled = cucumber_environment.check_expression(client_name) in {s for f in features for s in f["steps"]}
    unchecked = [d for d in dependencies if not controlled or d["code"] not in tokens]
    if unchecked:
        findings.append(_finding("controle_environnement", f"{len(unchecked)} dépendance(s) d'environnement non "
                                                            f"contrôlée(s) par l'init",
                                 [{"fichier": d.get("source", "profil"), "ligne": 0,
                                   "extrait": f"{cucumber_environment.TYPES.get(d['type'], d['type'])} "
                                              f"{(d.get('entite') + ' ') if d.get('entite') else ''}{d['code']}"}
                                  for d in unchecked], [d["code"] for d in unchecked]))
    # Champs personnalisés utilisés par les tests ou les processus, ni déclarés ni contrôlés par l'init
    known = set(facts.get("champs_declares") or {}) | tokens | {d["code"] for d in dependencies}
    fields = sorted(c for c in (facts.get("usages") or {}) if CUSTOM_FIELD.fullmatch(c) and c not in known)
    if fields:
        findings.append(_finding("champ_personnalise", f"{len(fields)} champ(s) personnalisé(s) utilisé(s) sans "
                                                       f"déclaration dans l'init : {', '.join(fields[:8])}",
                                 [facts["usages"][c][0] for c in fields], fields))
    # Steps d'init commentés TODO-VERSION (fichiers protégés : à traiter à la main)
    versions = [t for t in todo_lines if cucumber_versions.TODO_MARKER in t["extrait"]]
    if versions:
        findings.append(_finding("todo_version", f"{len(versions)} step(s) d'init commenté(s) "
                                                 f"{cucumber_versions.TODO_MARKER.rstrip(':')}", versions))
    # Init en échec au dernier run importé : premier step en échec
    failure = (analysis or {}).get("init_en_echec") or {}
    if failure:
        findings.append(_finding("init_en_echec", f"premier step en échec : {failure.get('step') or '—'}",
                                 [{"run": analysis.get("numero", "?"), "scenario": failure.get("id", ""),
                                   "step": failure.get("step", ""), "message": failure.get("message", "")}]))
    return {"etat": "incomplete" if findings else "complete", "constats": findings}


def element_count(status: dict) -> int:
    """Nombre d'éléments manquants (codes, sinon preuves) des constats."""
    return sum(len(f["codes"]) or len(f["preuves"]) or 1 for f in status.get("constats") or [])


def state_label(status: dict) -> str:
    """« init incomplète (3 éléments) », « init absente », « init complète », « init non détectée »."""
    state = status.get("etat")
    if not state:
        return "init non détectée"
    count = element_count(status) if state == "incomplete" else 0
    return INIT_STATES[state] + (f" ({count} élément{'s' if count > 1 else ''})" if count else "")


# ---------------------------------------------------------------------------
# Lot d'initialisation et dépendances des lots de tests
# ---------------------------------------------------------------------------

def init_lot(status: dict) -> dict:
    """
    Lot proposé avant les lots de tests : {"titre", "mode": init|complements, "constats"} ; {} si l'init est
    complète ou non détectée.
    """
    state = status.get("etat")
    if state == "absente":
        return {"titre": "Initialisation des données", "mode": "init", "constats": status["constats"]}
    if state == "incomplete":
        return {"titre": "Compléments d'initialisation", "mode": "complements", "constats": status["constats"]}
    return {}


def init_dependencies(status: dict, facts: dict) -> dict:
    """
    {"global": raison} si tous les tests dépendent de l'init (absente, clear non conforme, init en échec), sinon
    {"specs": {id du spécifique: [codes manquants qu'il utilise]}}.
    """
    findings = status.get("constats") or []
    blocking = [f for f in findings if f["type"] in GLOBAL_FINDINGS]
    if blocking:
        return {"global": blocking[0]["libelle"], "specs": {}}
    specs = {}
    for finding in findings:
        for code in finding["codes"]:
            for proof in ((facts or {}).get("usages") or {}).get(code) or []:
                if proof.get("spec"):
                    specs.setdefault(proof["spec"], [])
                    if code not in specs[proof["spec"]]:
                        specs[proof["spec"]].append(code)
    return {"global": "", "specs": specs}


def lot_dependency(ids: list, dependencies: dict) -> str:
    """« dépend du lot d'initialisation (…) » pour un lot de tests concerné, '' sinon."""
    if dependencies.get("global"):
        return f"dépend du lot d'initialisation ({dependencies['global']})"
    codes = list(dict.fromkeys(c for i in ids for c in (dependencies.get("specs") or {}).get(i, [])))
    return f"dépend du lot d'initialisation ({', '.join(codes[:5])})" if codes else ""


# ---------------------------------------------------------------------------
# Indicateur du profil
# ---------------------------------------------------------------------------

def profile_indicator(detection: dict, profile: dict) -> dict:
    """
    {"couleur": vert|orange|rouge|attente, "manquants": [champs], "cause"} : rouge si la détection est impossible
    (cause), orange si des champs obligatoires du profil sont vides (liste), vert si le profil est complet, attente
    tant qu'aucune détection n'a abouti.
    """
    if detection.get("statut") == "erreur":
        return {"couleur": "rouge", "manquants": [], "cause": detection.get("cause") or "cause inconnue"}
    if not detection.get("faits"):
        return {"couleur": "attente", "manquants": [], "cause": ""}
    missing = [k for k in cucumber_profile.FIELDS if k not in cucumber_profile.OPTIONAL_FIELDS
               and cucumber_profile.is_empty(profile.get(k))]
    return {"couleur": "orange" if missing else "vert", "manquants": missing, "cause": ""}


# ---------------------------------------------------------------------------
# Tâche de fond
# ---------------------------------------------------------------------------

def running(client_name: str) -> bool:
    process = _PROCESSES.get(client_name)
    return bool(process) and process.poll() is None


def due(client_name: str) -> bool:
    """Contrôle des commits à faire : jamais fait, ou plus ancien que interval()."""
    document = load_detection(client_name)
    checked = document.get("verifie_le") or ""
    try:
        age = time.time() - datetime.fromisoformat(checked).timestamp()
    except ValueError:
        return True
    return age >= interval()


def start(client_name: str, cmd: list, force: bool = False) -> bool:
    """
    Lance la détection en arrière-plan (main.py --detect-only, journal detection.log), sauf si elle tourne déjà
    ou si le dernier contrôle est récent (`force` : sans attendre). Renvoie True si elle a été lancée.
    """
    finish(client_name)
    if running(client_name) or not (force or due(client_name)):
        return False
    log = open(log_path(client_name), "w", encoding="utf-8")
    try:
        _PROCESSES[client_name] = subprocess.Popen(cmd + ["--detect-only"] + (["--detect-force"] if force else []),
                                                   stdout=log, stderr=subprocess.STDOUT, text=True,
                                                   encoding="utf-8", errors="replace",
                                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    finally:
        log.close()
    _PROCESSES[client_name].started = time.time()
    return True


def finish(client_name: str) -> None:
    """
    Tâche terminée : un code retour non nul sans résultat écrit depuis son lancement (dépôt inaccessible avant la
    détection) devient une erreur, avec la dernière ligne « ❌ » du journal pour cause.
    """
    process = _PROCESSES.get(client_name)
    if not process or process.poll() is None:
        return
    _PROCESSES.pop(client_name, None)
    if process.returncode == 0:
        return
    document = load_detection(client_name)
    try:
        written = datetime.fromisoformat(document.get("verifie_le") or "").timestamp() >= process.started
    except ValueError:
        written = False
    if not (written and document.get("statut") == "erreur"):
        lines = [line.strip() for line in _read_text_file(log_path(client_name)).splitlines() if line.strip()]
        cause = next((line for line in reversed(lines) if "❌" in line), lines[-1] if lines else "")
        save_error(client_name, cause.replace("❌", "").strip() or f"code retour {process.returncode}")
