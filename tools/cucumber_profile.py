"""
Profil client Cucumber (0 token) : conventions d'un dépôt <client>-cucumber, détectées puis modifiables.

Le profil réunit ce que la génération et les contrôles doivent savoir du client : racine et tag de ses features,
features et tags de son initialisation, clear de son environnement, fichiers d'intégration de son module
spécifique, fichiers protégés de l'init globale, runner, codes de référence créés par l'init, version de la
bibliothèque de steps et version du serveur (celle du Standard applicatif), données créées par les initializers
serveur et leur sort après un clear, contournements TODO-VERSION du dépôt, branche lue par le job de vérification
(verify_branch) et paramètre du job qui la porte, correspondance « paramètre du job → rôle » (job_roles :
détectée sur les paramètres du job par cucumber_job, enregistrée par record_job_roles). Rien n'est propre à un client dans le code : tout vient de la détection dans le dépôt, de la saisie dans l'IHM ou d'un fichier versionné
dans le dépôt client. Priorité : dépôt (.agent-savoye.json) > saisie (cache) > détection ; un champ non détecté
reste vide, avec un avertissement.

Fichier .tmp/cucumber/<client>/profil.json : {"detecte", "depot", "saisi", "suggestions", "avertissements_detection",
"date"} ; effective_profile() calcule le profil appliqué et la provenance de chaque champ.
"""
import json
import os
import re
from datetime import datetime

from tools import cucumber_checks, cucumber_init_data, cucumber_initializers, cucumber_modules, cucumber_versions
from tools.cucumber_tools import (client_ref, CLIENT_REFS, _git, _read_json, _read_text_file, _runners, _test_java_files,
                                  _write_json,
                                  cache_dir, depends_on_std_test_jar, git_repos, java_classes, java_package_segment,
                                  load_inventory, pick_ref, snapshot)

REPO_PROFILE = ".agent-savoye.json"
REFERENCE_CATEGORIES = ("sites", "donneurs_ordres", "secteurs", "partenaires")
FIELDS = ("features_root", "tag_client", "init_features", "init_tags", "clear_name", "spec_files", "protected_files",
          "runner", "reference_codes", "library_version", "server_version", "server_initialized_data",
          "initializers_survive_clear", "workarounds", "verify_branch", "verify_branch_param", "run_job",
          "environment_checks", "reponses", "job_roles", "job_params", "run_tags_target", "modules",
          "modules_exclus", "cucumber_repo_url", "livraison_autonome", "branche_cible",
          "maj_bibliotheque", "adresse_test", "logs_namespace", "logs_conteneur", "config_copiee", "schema_copie")
# Champs dont l'absence est normale : vides sans avertissement
OPTIONAL_FIELDS = ("server_initialized_data", "workarounds", "verify_branch", "verify_branch_param", "run_job",
                   "environment_checks", "reponses", "job_roles", "job_params", "run_tags_target", "modules",
                   "modules_exclus", "cucumber_repo_url", "livraison_autonome", "branche_cible",
          "maj_bibliotheque", "adresse_test", "logs_namespace", "logs_conteneur", "config_copiee", "schema_copie")
# Option « configuration copiée » et son schéma : réglés à l'étape 1 de l'onglet Cucumber, gardés par toute saisie
COPIED_CONFIG_FIELDS = ("config_copiee", "schema_copie")
# Paramètre du job de vérification qui porte la branche, à défaut de saisie
DEFAULT_VERIFY_BRANCH_PARAM = "CUCUMBER_SPEC_BRANCH"
SURVIVE_CLEAR = {"": "inconnu", "vrai": "vrai", "faux": "faux"}
LABELS = {"config_copiee": "configuration copiée, sans purge : tests dans la configuration réelle du client, copiée "
                           "dans le schéma de test des runs et restaurée avant chaque run (désactivée par défaut)",
          "schema_copie": "schéma de test des runs visé par la configuration copiée (vide : CUCUMBER_DB_SCHEMA)",
          "adresse_test": "adresse de livraison de test (remplace l'adresse par défaut de l'agent, ex. client hors "
                          "de France)",
          "cucumber_repo_url": "dépôt Cucumber du client (seul dépôt où l'agent peut écrire)",
          "logs_namespace": "namespace Kubernetes de l'application de test (logs Groundcover ; vide : celui du pod "
                            "du build, lu dans la console)",
          "logs_conteneur": "conteneur de l'application de test dans le pod du build (logs Groundcover)",
          "livraison_autonome": "livraison autonome : l'agent pousse et promeut lui-même ses corrections",
          "branche_cible": "branche lue par le job, cible de la promotion (ex. integration)",
          "maj_bibliotheque": "mise à jour automatique de la bibliothèque Cucumber (vide ou auto : activée, non : "
                              "désactivée)",
          "modules": "modules du client (tag @<X> d'une feature sous un dossier <X>, tâches de l'inventaire)",
          "modules_exclus": "modules exclus du périmètre testé (décochés dans « Données du projet »)",
          "features_root": "racine des features", "tag_client": "tag client", "init_features": "features d'init",
          "init_tags": "tags d'init", "clear_name": "clear de l'environnement",
          "spec_files": "fichiers du module spécifique", "protected_files": "fichiers protégés de l'init globale",
          "runner": "runner",
          "reference_codes": "codes de référence", "library_version": "version de la bibliothèque de steps",
          "server_version": "version du serveur",
          "server_initialized_data": "données créées par les initializers serveur",
          "initializers_survive_clear": "données des initializers conservées après un clear (vrai / faux / inconnu)",
          "workarounds": "contournements TODO-VERSION",
          "verify_branch": "branche lue par le job de vérification (l'agent y commite et pousse avant chaque vérification)",
          "verify_branch_param": "paramètre du job de vérification qui porte la branche",
          "run_job": "job Jenkins qui exécute le dépôt client (lancement automatique d'un run)",
          "environment_checks": "dépendances d'environnement contrôlées par l'init (champs personnalisés, "
                                "documents, énumérations, paramétrages)",
          "reponses": "réponses de l'utilisateur aux questions des tâches à confirmer",
          "job_roles": "correspondance paramètre du job → rôle (branche, dépôt, arguments, attente, artefact monté)",
          "run_tags_target": "où envoyer le filtre de tags d'un run (arguments, parametre, les_deux ; vide : "
                             "arguments)",
          "job_params": "paramètres du job imposés à chaque run de l'agent (NOM=valeur ; ex. artefact du client, "
                        "suites Standard décochées)"}
RUNNER_KEYS = ("classe", "chemin", "features", "glue", "tags", "plugin_json", "surcouche")
_PURGE_ARGUMENT = re.compile(r"^INIT- Purge\b.*\"([^\"]+)\"\s*$")
_GLOBAL_INIT = "Initialisation globale par fichiers"
# Ressource REST de clear déclarée par l'application du client (bundle.list, pom, configuration)
_APP_CLEAR = re.compile(r"init-data-rest-clear-[A-Za-z0-9_-]*[A-Za-z0-9]")
_APP_CONFIG_GLOBS = ("*.list", "*.xml", "*.properties", "*.yml", "*.yaml", "*.json", "*.java")


def profile_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "profil.json")


def base_worktree_dir(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "copie_profil")


def empty_profile() -> dict:
    return {"features_root": "", "tag_client": "", "init_features": [], "init_tags": "", "clear_name": "",
            "spec_files": [], "protected_files": [],
            "runner": {k: ([] if k in ("features", "glue", "tags") else False if k == "surcouche" else "")
                       for k in RUNNER_KEYS},
            "reference_codes": {k: [] for k in REFERENCE_CATEGORIES}, "library_version": "", "server_version": "",
            "server_initialized_data": [], "initializers_survive_clear": "", "workarounds": [], "verify_branch": "",
            "verify_branch_param": "", "run_job": "", "environment_checks": [], "reponses": [], "job_roles": {},
            "job_params": [], "run_tags_target": "", "modules": [], "modules_exclus": [],
            "cucumber_repo_url": "", "livraison_autonome": False, "branche_cible": "",
            "maj_bibliotheque": "", "adresse_test": {}, "logs_namespace": "", "logs_conteneur": "",
            "config_copiee": False, "schema_copie": ""}


def is_empty(value) -> bool:
    """Champ vide : chaîne ou liste vide ; runner sans classe ; codes sans aucune valeur."""
    if isinstance(value, dict):
        return not value.get("classe") if "classe" in value else not any(value.values())
    return not value


# ---------------------------------------------------------------------------
# Détection dans le dépôt client
# ---------------------------------------------------------------------------

def init_clear_name(texts: list) -> str:
    """Argument du step de purge des features d'init (ex. « INIT- Purge … par le clear "<nom>" »)."""
    for text in texts:
        for step in cucumber_init_data.step_lines(text):
            match = _PURGE_ARGUMENT.match(step)
            if match:
                return match.group(1)
    return ""


def application_clears(client_root: str) -> dict:
    """
    Clears déclarés par le code ou la configuration de l'application du client (dépôts de `client_root`, hors
    dépôts Cucumber, dont l'init a pu être générée avec un mauvais clear) : {nom du clear: [dépôt/fichier]}.
    """
    found = {}
    for name, repo in git_repos(client_root).items():
        if "cucumber" in name.lower():
            continue
        # Lu sur la référence distante (copies mises à jour par fetch, jamais par pull)
        ref = client_ref(repo)
        grep = _git(repo, "grep", "-I", "-o", "-E", _APP_CLEAR.pattern, ref, "--", *_APP_CONFIG_GLOBS)
        for line in grep.stdout.splitlines():
            path, _, value = line[len(ref) + 1:].partition(":") if line.startswith(ref + ":") else                 line.partition(":")
            if value and "/test/" not in f"/{path}":
                places = found.setdefault(value, [])
                if f"{name}/{path}" not in places:
                    places.append(f"{name}/{path}")
    return found


def choose_clear(init_clear: str, app_clears: dict, analysed: bool, proven: dict = None) -> tuple:
    """
    (clear retenu, avertissement) : le clear vient de l'application du client, ou du socle (routes init-data de KIS
    et des contribs du Standard) s'il est prouvé par un run vert de CE client (`proven`, record_proven_clear) ; celui
    de l'init n'est retenu que s'il figure dans l'un ou l'autre. Sans application analysée (`analysed` faux), le
    clear de l'init est retenu tel quel.
    """
    proven = proven or {}
    if init_clear and init_clear in proven:
        return init_clear, ""
    if not analysed:
        return init_clear, ""
    if init_clear and init_clear in app_clears:
        return init_clear, ""
    if not init_clear and not app_clears and len(proven) == 1:
        return next(iter(proven)), ""
    names = sorted(app_clears)
    if len(names) == 1:
        return names[0], (f"clear de l'init « {init_clear} » absent de l'application du client : clear de "
                          f"l'application retenu, {names[0]} ({app_clears[names[0]][0]})" if init_clear else "")
    if names:
        return "", (f"plusieurs clears dans l'application du client ({', '.join(names)}) : choisissez celui de "
                    f"l'environnement dans le profil client")
    return "", (CLEAR_QUESTION + (f" (l'init cite « {init_clear} », absent de l'application : il peut venir d'un "
                                  f"autre client)" if init_clear else ""))


# Clears prouvés par un run vert du client (route du socle KIS / init-data comprise) : {route: {build, preuves, …}}
PROVEN_CLEARS = "clears_prouves"


def proven_clears(client_name: str) -> dict:
    return dict((_read_json(profile_path(client_name), {}) or {}).get(PROVEN_CLEARS) or {}) if client_name else {}


def record_proven_clear(client_name: str, routes: list, build: str, proofs: list = None, reason: str = "") -> dict:
    """
    Clear(s) fourni(s) par le socle (KIS / init-data) ou l'application et prouvé(s) par un run vert de l'init de ce
    client : enregistrés dans le profil (valides), l'avertissement « aucun clear trouvé » retiré. Renvoie le document.
    """
    document = load_profile(client_name)
    proven = dict(document.get(PROVEN_CLEARS) or {})
    for route in (r.strip() for r in routes or [] if r and r.strip()):
        proven[route] = {"build": build or "", "preuves": list(proofs or [])[:10], "motif": reason or "",
                         "date": datetime.now().isoformat(timespec="seconds")}
    document[PROVEN_CLEARS] = proven
    if proven:
        # Avertissements de la détection sur le clear (aucun clear, clear de l'init absent de l'application) levés
        document["avertissements_detection"] = [w for w in document.get("avertissements_detection") or []
                                                if not (w.startswith(CLEAR_QUESTION) or w.startswith("clear de l'init"))]
        detected = document.setdefault("detecte", {})
        if not detected.get("clear_name") or detected["clear_name"] not in proven:
            routes_ok = [r for r in routes or [] if r in proven]
            if len(routes_ok) == 1:
                detected["clear_name"] = routes_ok[0]
    _write_json(profile_path(client_name), document)
    return document


CLEAR_QUESTION = ("aucun clear trouvé dans le code ou la configuration de l'application du client : quel est le nom "
                  "du clear de ce client ? À renseigner dans le profil client (« clear de l'environnement »)")


def init_spec_files(rel: str, text: str) -> list:
    """Fichiers intégrés par une feature d'init APRÈS son initialisation globale, dans l'ordre (module spécifique)."""
    lines = text.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if _GLOBAL_INIT in line), 0)
    if not start:
        return []
    return list(dict.fromkeys(cucumber_checks.resolved_path(rel, ref) for ref in cucumber_checks.data_references(text)
                              if ref["ligne"] > start and not ref["regle"]))


def runner_profile(worktree: str, ref: str, std_runners: list) -> dict:
    """Runner du client (premier runner Cucumber), à défaut celui du Standard en surcouche (test-jar sans runner)."""
    files = _test_java_files(worktree, ref)
    runners = _runners(worktree, ref, java_classes(files), files)
    overlay = not runners and (depends_on_std_test_jar(worktree)
                               or not os.path.exists(os.path.join(worktree, "pom.xml")))
    runner = runners[0] if runners else (std_runners[0] if overlay and std_runners else None)
    if not runner:
        return empty_profile()["runner"]
    return {"classe": runner["fqcn"], "chemin": runner["path"], "features": runner.get("features") or [],
            "glue": runner.get("glue") or [], "tags": runner.get("tags") or [],
            "plugin_json": next((p for p in runner.get("plugins") or [] if p.startswith("json:")), ""),
            "surcouche": bool(overlay)}


def spec_file_suggestions(worktree: str, cucumber_std_dir: str, chosen: list) -> list:
    """Fichiers de données du dépôt client hors initialisation Standard (même nom), candidats au module spécifique."""
    from tools.cucumber_init import std_init_files
    std_names = {p.rsplit("/", 1)[-1] for paths in (std_init_files(cucumber_std_dir) if cucumber_std_dir else {})
                 .values() for p in paths}
    data = re.compile(rf"\.({'|'.join(cucumber_checks.DATA_EXTENSIONS)})$", re.I)
    return sorted(p for p in cucumber_checks.repo_files(worktree) if p.startswith(cucumber_checks.FILES_ROOT)
                  and data.search(p) and p.rsplit("/", 1)[-1] not in std_names and p not in chosen)[:300]


def detect_profile(client_name: str, worktree: str, ref: str, cucumber_std_dir: str, std_runners: list = (),
                   server_version: str = "", client_root: str = "") -> tuple:
    """
    Profil détecté dans le worktree du dépôt client (à `ref`) : (profil, suggestions, avertissements). Un champ non
    détecté reste vide ; seules exceptions, racine et tag déduits du nom du client, signalés comme tels.
    `server_version` : version lue dans le pom du Standard applicatif, retenue si elle est numérique. Données des
    initializers serveur : celles de l'inventaire (statut inconnu), non analysées sans inventaire. Clear : celui de
    l'application du client (dépôts de `client_root`, application_clears), jamais repris d'un autre client ;
    introuvable, il reste vide et la question est posée (CLEAR_QUESTION).
    """
    profile, warnings = empty_profile(), []
    root = cucumber_checks.client_features_root(worktree, ref, client_name, cucumber_std_dir, fallback=False)
    tag = cucumber_checks.client_tag(worktree, ref, client_name, fallback=False)
    segment = java_package_segment(client_name)
    if not root:
        root = f"{cucumber_checks.FEATURES_ROOT}{segment}/"
        warnings.append(f"racine des features non détectée (aucune feature client) : déduite du nom, {root}")
    if not tag:
        tag = f"@{segment.upper()}"
        warnings.append(f"tag client non détecté (aucune feature client taguée) : déduit du nom, {tag}")
    init = cucumber_init_data.client_init(worktree, client_name, os.getenv("CUCUMBER_INIT_TAGS", ""))
    warnings += [f"initialisation : {w}" for w in init["avertissements"]]
    paths = cucumber_init_data.init_paths(init)
    texts = {p: _read_text_file(os.path.join(worktree, *p.split("/"))) for p in paths}
    init_tags = sorted({t for f in init["features"] for t in f["tags"] if t.upper().startswith("@INIT_")})
    init_clear = init_clear_name(list(texts.values()))
    analysed = bool(client_root and git_repos(client_root))
    app_clears = application_clears(client_root) if analysed else {}
    clear, clear_warning = choose_clear(init_clear, app_clears, analysed, proven_clears(client_name))
    if clear_warning:
        warnings.append(clear_warning)
    profile.update(features_root=root, tag_client=tag, init_features=paths, init_tags=" or ".join(init_tags),
                   clear_name=clear,
                   spec_files=[s for p in paths for s in init_spec_files(p, texts[p])],
                   protected_files=cucumber_init_data.protected_files(init),
                   runner=runner_profile(worktree, ref, list(std_runners)),
                   reference_codes={k: list((init.get("codes") or {}).get(k) or []) for k in REFERENCE_CATEGORIES},
                   library_version=cucumber_checks.library_version(worktree),
                   server_version=server_version if re.match(r"^\d{2}\.\d+", server_version or "") else "")
    profile["workarounds"] = cucumber_versions.todo_markers(worktree, ref)
    # Modules du client (aucune liste en dur : tags des features rangées sous un dossier du même nom, inventaire)
    profile["modules"] = cucumber_modules.detect_in_worktree(worktree, client_name, cucumber_std_dir, tag)
    inventory = load_inventory(client_name)
    if "initialiseurs" in inventory:
        profile["server_initialized_data"] = cucumber_initializers.initialized_data(inventory["initialiseurs"])
    else:
        warnings.append("initializers serveur non analysés (inventaire absent ou antérieur) : lancez l'inventaire")
    suggestions = {"spec_files": spec_file_suggestions(worktree, cucumber_std_dir, profile["spec_files"]),
                   "clear_init": init_clear, "clears_application": app_clears if analysed else None}
    return profile, suggestions, warnings


def repo_profile(worktree: str, ref: str) -> dict:
    """Surcharge versionnée .agent-savoye.json du dépôt client à `ref` ({} si absente ou illisible)."""
    text = _git(worktree, "show", f"{ref}:{REPO_PROFILE}").stdout
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return {"_erreur": f"{REPO_PROFILE} illisible (JSON invalide)"}
    return {k: v for k, v in data.items() if k in FIELDS} if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Profil enregistré et profil appliqué
# ---------------------------------------------------------------------------

def load_profile(client_name: str) -> dict:
    return _read_json(profile_path(client_name), {}) or {}


def refresh_profile(client_name: str, worktree: str, ref: str, cucumber_std_dir: str, std_runners: list = (),
                    server_version: str = "", client_root: str = "") -> dict:
    """Détection et surcharge du dépôt recalculées, saisie conservée ; profil.json réécrit. Renvoie le document."""
    detected, suggestions, warnings = detect_profile(client_name, worktree, ref, cucumber_std_dir, std_runners,
                                                     server_version, client_root)
    document = load_profile(client_name)
    # Rôles des paramètres du job : détectés sur Jenkins (record_job_roles), pas dans le dépôt
    detected["job_roles"] = (document.get("detecte") or {}).get("job_roles") or {}
    # Adresse autorisée du dépôt Cucumber (garde d'écriture, relevée sur le clone) : conservée d'une détection à l'autre
    if (document.get("detecte") or {}).get("cucumber_repo_url") and not detected.get("cucumber_repo_url"):
        detected["cucumber_repo_url"] = document["detecte"]["cucumber_repo_url"]
    # Version du Standard propre au client (tools/standard_version), jamais la version globale à sa place
    version = document.get("version_standard") or {}
    if version:
        detected["server_version"] = version.get("version", "") if version.get("statut") == "ok" else ""
    depot = repo_profile(worktree, ref)
    if depot.pop("_erreur", ""):
        warnings.append(f"{REPO_PROFILE} du dépôt client illisible : ignoré")
    document.update(detecte=detected, depot=depot, suggestions=suggestions, avertissements_detection=warnings,
                    date=datetime.now().isoformat(timespec="seconds"))
    document.setdefault("saisi", {})
    _write_json(profile_path(client_name), document)
    return document


def record_job_roles(client_name: str, roles: dict) -> dict:
    """Rôles détectés sur les paramètres du job (cucumber_job.detect_roles), enregistrés comme détection."""
    document = load_profile(client_name)
    document.setdefault("detecte", {})["job_roles"] = {k: v for k, v in (roles or {}).items() if v}
    _write_json(profile_path(client_name), document)
    return document


def effective_profile(document: dict) -> dict:
    """
    Profil appliqué : dépôt > saisie > détection, champ par champ. Ajoute "sources" {champ: depot|saisi|detecte}
    et "avertissements" (détection, puis champs vides).
    """
    profile, sources = empty_profile(), {}
    for source in ("detecte", "saisi", "depot"):
        for key, value in ((document or {}).get(source) or {}).items():
            if key in FIELDS and not is_empty(value):
                if key == "server_initialized_data" and source != "detecte":
                    # Statuts choisis appliqués aux données détectées, sans les remplacer
                    value = cucumber_initializers.merge_statuses(profile[key], value)
                elif key == "job_roles":
                    # Rôles choisis appliqués par-dessus les rôles détectés
                    value = {**profile[key], **{k: v for k, v in value.items() if v}}
                profile[key], sources[key] = value, source
    warnings = list((document or {}).get("avertissements_detection") or [])
    warnings += [f"{LABELS[k]} vide : à renseigner dans le profil client" for k in FIELDS
                 if is_empty(profile[k]) and k not in OPTIONAL_FIELDS]
    # Clears déclarés par l'application du client (None : application non analysée)
    clears = ((document or {}).get("suggestions") or {}).get("clears_application")
    # Modules exclus (saisie) : leurs features et tags d'init retirés du profil appliqué, partout
    detected_tags = (((document or {}).get("detecte") or {}).get("init_tags") or "")
    # Version du client choisie (tools/client_version) : paramètres Jenkins des runs de l'agent qui en sont déduits
    version = (document or {}).get("version_client") or {}
    proven = dict((document or {}).get(PROVEN_CLEARS) or {})
    # Copie de référence du mode « configuration copiée » (cucumber_copied_config)
    copied_reference = dict((document or {}).get("reference_copie") or {})
    return cucumber_modules.scope_profile({**profile, "sources": sources, "avertissements": warnings,
                                           "clears_application": clears, "init_tags_detectes": detected_tags,
                                           "version_client": version, PROVEN_CLEARS: proven,
                                           "reference_copie": copied_reference})


def save_user_fields(client_name: str, fields: dict) -> dict:
    """Enregistre la saisie de l'IHM : seuls les champs différents de la détection sont gardés. Renvoie le document."""
    document = load_profile(client_name)
    detected = document.get("detecte") or empty_profile()
    kept = {k: v for k, v in (document.get("saisi") or {}).items() if k in COPIED_CONFIG_FIELDS and k not in fields}
    document["saisi"] = {**kept, **{k: v for k, v in fields.items() if k in FIELDS and v != detected.get(k)}}
    _write_json(profile_path(client_name), document)
    return document


def client_profile(client_name: str, worktree: str, ref: str, cucumber_std_dir: str, std_runners: list = (),
                   server_version: str = "", client_root: str = "") -> dict:
    """Profil appliqué, après détection dans `worktree` à `ref` (profil.json mis à jour)."""
    return effective_profile(refresh_profile(client_name, worktree, ref, cucumber_std_dir, std_runners,
                                             server_version, client_root))


def detect_in_base(client_name: str, cucumber_repo_dir: str, base: str, cucumber_std_dir: str,
                   std_runners: list = (), server_version: str = "", client_root: str = "") -> dict:
    """
    Profil détecté dans une copie de lecture du dépôt client à `base` (avant génération) : rien n'est écrit dans le
    dépôt client. Lève RuntimeError.
    """
    commit = snapshot(cucumber_repo_dir, base_worktree_dir(client_name), base)
    return client_profile(client_name, base_worktree_dir(client_name), commit, cucumber_std_dir, std_runners,
                          server_version, client_root)


def init_view(profile: dict) -> dict:
    """Vue « init » attendue par cucumber_init_data (chemins des features d'init et codes de référence)."""
    return {"features": [{"chemin": p} for p in profile.get("init_features") or []],
            "codes": {k: v for k, v in (profile.get("reference_codes") or {}).items() if v}}


def profile_block(profile: dict, max_codes: int = cucumber_init_data.MAX_PROMPT_CODES, gap_file: str = "",
                  known: dict = None) -> str:
    """
    Bloc « PROFIL CLIENT » de la partie variable du prompt de lot : seuls emplacement, tag et données autorisés,
    interdiction d'écrire purge, clear ou init et de modifier les fichiers de l'init globale, steps du projet
    marqués TODO-VERSION quand la bibliothèque du client est plus ancienne que le serveur (`gap_file` : écarts du
    catalogue, cucumber_versions.library_gap_file), données des initializers serveur (`known` : index des valeurs
    connues, cucumber_testability ; une donnée retrouvée dans l'init ou le dépôt client est utilisable). Tout vient
    du profil ; un champ vide est signalé comme tel.
    """
    root = profile.get("features_root") or "(non renseignée : signale-le dans « raison »)"
    tag = profile.get("tag_client") or "(non renseigné : n'ajoute aucun tag client)"
    clear = profile.get("clear_name") or "non renseigné"
    return "\n".join([
        "=== PROFIL CLIENT (seuls emplacement, tag et données autorisés) ===",
        f"- Racine des features (seul emplacement autorisé) : '{root}'",
        f"- Tag du client (casse exacte) : {tag}",
        # Configuration copiée : codes de référence lus dans la copie, jamais ceux de l'init du Standard
        _copied_block(profile) or cucumber_init_data.init_block(init_view(profile), clear, max_codes),
        protected_block(profile.get("protected_files") or []),
        *filter(None, [excluded_modules_block(profile),
                       version_block(profile.get("library_version", ""), profile.get("server_version", ""),
                                     gap_file),
                       initializers_block(profile.get("server_initialized_data") or [],
                                          profile.get("initializers_survive_clear", ""),
                                          present_codes={v for v, places in (known or {}).items()
                                                         if any(p["source"] in ("init", "depot") for p in places)})]),
        "- N'utilise QUE des steps existants (catalogue du Standard, steps du client) ou définis par ce lot, avec la "
        "syntaxe de colonnes du Standard actuel ; préfère un Background à un scénario « Contexte » séparé."])


def _copied_block(profile: dict) -> str:
    from tools import cucumber_copied_config
    return cucumber_copied_config.prompt_block(profile)


def excluded_modules_block(profile: dict) -> str:
    """
    Ligne du bloc profil : modules exclus du périmètre testé (« Données du projet ») ; une fonctionnalité qui en
    dépend n'est pas générée mais classée « non testable : dépend du module exclu ». '' sans module exclu.
    """
    from tools.cucumber_modules import excluded
    out = sorted(excluded(profile))
    if not out:
        return ""
    return (f"- Modules exclus du périmètre testé : {', '.join(out)} (absents de l'environnement de test). Une "
            f"fonctionnalité qui en dépend (step, endpoint, process, job ou donnée du module ; ex. affectation "
            f"transport → TMS) n'est PAS générée : réponds « non_couvert » avec « blocage » {{\"type\": "
            f"\"module_exclu\", \"module\": \"<code>\", \"preuve\": \"<step, endpoint ou fichier du module>\"}}.")


def version_block(library: str, server: str, gap_file: str = "") -> str:
    """
    Ligne du bloc profil quand la bibliothèque de steps du client diffère du serveur (Standard aligné) : step dont
    l'implémentation de la bibliothèque est plus ancienne (`gap_file`, à interroger par Grep) remplacé par un step du
    projet de nom unique sur le modèle récent, marqué TODO-VERSION ; bibliothèque partagée jamais modifiée. '' si les
    versions sont inconnues ou identiques.
    """
    if not library or not server or not cucumber_checks.compare_versions(library, server):
        return ""
    marker = f"`# {cucumber_versions.TODO_MARKER} <step, colonne ou champ> — à vérifier en {library}`"
    return (f"- Versions : le client exécute la bibliothèque de steps {library}, le catalogue du Standard est aligné "
            f"sur le serveur {server}. "
            + (f"Avant d'utiliser un step du Standard, cherche-le par Grep dans '{gap_file}' (steps absents de la "
               f"bibliothèque, signature, endpoint ou champs POJO modifiés). " if gap_file else "")
            + f"Un step absent ou dont l'implémentation de la bibliothèque est plus ancienne : écris un step du projet "
              f"de nom unique (préfixe du client) sur le modèle du Standard aligné, et précède son usage du "
              f"commentaire Gherkin {marker} ; une colonne ou un champ récent : même commentaire. Ne modifie JAMAIS "
              f"la bibliothèque partagée ; n'invente aucun autre contournement.")


def initializers_block(data: list, survive_clear: str, max_codes: int = 20, present_codes: set = frozenset()) -> str:
    """
    Ligne du bloc profil : codes créés par les initializers serveur, utilisables (présents après un clear, ou
    retrouvés dans l'init ou le dépôt client : `present_codes`) ou à créer dans le Background. '' sans donnée codée.
    """
    coded = [d for d in data if d.get("code")]
    if not coded:
        return ""

    def codes(items):
        shown = [f"{d['code']} ({d.get('entite') or '?'}, {d.get('classe') or '?'})" for d in items[:max_codes]]
        return ", ".join(shown) + (f", … (+{len(items) - max_codes})" if len(items) > max_codes else "")
    present = [d for d in coded if cucumber_initializers.usable(d, survive_clear) or d["code"] in present_codes]
    missing = [d for d in coded if d not in present]
    parts = [f"- Données créées au démarrage par les initializers serveur ({len(coded)}, hors features d'init)"]
    if present:
        parts.append(f"présentes après le clear (ou recréées par l'init du projet), utilisables : {codes(present)}")
    if missing:
        parts.append(("effacées par le clear" if survive_clear == "faux" else "présence après le clear incertaine")
                     + f" : {codes(missing)} — ne t'y fie pas, crée la donnée dans le Background ou signale un "
                       f"constat « donnee_introuvable »")
    parts.append("aucun test de leur seule existence (tautologique) ; toute assertion sur leur état initial reprend "
                 "les valeurs écrites par l'initializer cité (lis sa classe), un écart avec la spécification est un "
                 "constat « ecart_spec_code », pas un test")
    return " ; ".join(parts) + "."


def protected_block(protected: list, max_folders: int = 6) -> str:
    """Ligne du bloc profil : fichiers de l'init globale à ne jamais modifier, résumés par dossier (borné)."""
    if not protected:
        return "- Fichiers de l'init globale : aucun détecté ; ne modifie de toute façon aucun fichier d'initialisation."
    folders = sorted({p.rsplit("/", 1)[0] + "/" for p in protected})
    shown = ", ".join(folders[:max_folders]) + (f", … ({len(folders) - max_folders} autres)"
                                                if len(folders) > max_folders else "")
    return (f"- Fichiers protégés de l'init globale ({len(protected)} : features d'init et fichiers qu'elles intègrent, "
            f"dossiers {shown}) : partagés par tous les tests, ne les modifie JAMAIS (un fichier rejeté arrête "
            f"l'intégration et fait échouer les autres tests) ; une donnée manquante se crée dans le Background de ta "
            f"feature.")


def conventions(profile: dict) -> dict:
    """Conventions des contrôles et du runner : racine, tag, init, fichiers protégés, runner et versions du client."""
    return {"features_root": profile.get("features_root", ""), "client_tag": profile.get("tag_client", ""),
            "init": list(profile.get("init_features") or []), "runner": profile.get("runner") or {},
            "protected_files": list(profile.get("protected_files") or []),
            "library_version": profile.get("library_version", ""), "server_version": profile.get("server_version", ""),
            "server_initialized_data": list(profile.get("server_initialized_data") or []),
            "initializers_survive_clear": profile.get("initializers_survive_clear", ""),
            "workarounds": list(profile.get("workarounds") or []),
            "verify_branch": profile.get("verify_branch", ""),
            "verify_branch_param": profile.get("verify_branch_param", "") or DEFAULT_VERIFY_BRANCH_PARAM,
            "job_roles": dict(profile.get("job_roles") or {})}
