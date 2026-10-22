"""
Profil client Cucumber (0 token) : conventions d'un dépôt <client>-cucumber, détectées puis modifiables.

Le profil réunit ce que la génération et les contrôles doivent savoir du client : racine et tag de ses features,
features et tags de son initialisation, clear de son environnement, fichiers d'intégration de son module
spécifique, runner, codes de référence créés par l'init, version de la bibliothèque de steps. Rien n'est propre à
un client dans le code : tout vient de la détection dans le dépôt, de la saisie dans l'IHM ou d'un fichier versionné
dans le dépôt client. Priorité : dépôt (.agent-savoye.json) > saisie (cache) > détection ; un champ non détecté
reste vide, avec un avertissement.

Fichier .tmp/cucumber/<client>/profil.json : {"detecte", "depot", "saisi", "suggestions", "avertissements_detection",
"date"} ; effective_profile() calcule le profil appliqué et la provenance de chaque champ.
"""
import json
import os
import re
from datetime import datetime

from tools import cucumber_checks, cucumber_init_data
from tools.cucumber_tools import (_git, _read_json, _read_text_file, _runners, _test_java_files, _write_json,
                                  add_worktree, cache_dir, depends_on_std_test_jar, java_classes,
                                  java_package_segment)

REPO_PROFILE = ".agent-savoye.json"
REFERENCE_CATEGORIES = ("sites", "donneurs_ordres", "secteurs", "partenaires")
FIELDS = ("features_root", "tag_client", "init_features", "init_tags", "clear_name", "spec_files", "runner",
          "reference_codes", "library_version")
LABELS = {"features_root": "racine des features", "tag_client": "tag client", "init_features": "features d'init",
          "init_tags": "tags d'init", "clear_name": "clear de l'environnement",
          "spec_files": "fichiers du module spécifique", "runner": "runner",
          "reference_codes": "codes de référence", "library_version": "version de la bibliothèque de steps"}
RUNNER_KEYS = ("classe", "chemin", "features", "glue", "tags", "plugin_json", "surcouche")
_PURGE_ARGUMENT = re.compile(r"^INIT- Purge\b.*\"([^\"]+)\"\s*$")
_GLOBAL_INIT = "Initialisation globale par fichiers"


def profile_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "profil.json")


def base_worktree_dir(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "worktree_profil")


def empty_profile() -> dict:
    return {"features_root": "", "tag_client": "", "init_features": [], "init_tags": "", "clear_name": "",
            "spec_files": [], "runner": {k: ([] if k in ("features", "glue", "tags") else False if k == "surcouche"
                                             else "") for k in RUNNER_KEYS},
            "reference_codes": {k: [] for k in REFERENCE_CATEGORIES}, "library_version": ""}


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


def detect_profile(client_name: str, worktree: str, ref: str, cucumber_std_dir: str, std_runners: list = ()) -> tuple:
    """
    Profil détecté dans le worktree du dépôt client (à `ref`) : (profil, suggestions, avertissements). Un champ non
    détecté reste vide ; seules exceptions, racine et tag déduits du nom du client, signalés comme tels.
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
    profile.update(features_root=root, tag_client=tag, init_features=paths, init_tags=" or ".join(init_tags),
                   clear_name=init_clear_name(list(texts.values())),
                   spec_files=[s for p in paths for s in init_spec_files(p, texts[p])],
                   runner=runner_profile(worktree, ref, list(std_runners)),
                   reference_codes={k: list((init.get("codes") or {}).get(k) or []) for k in REFERENCE_CATEGORIES},
                   library_version=cucumber_checks.library_version(worktree))
    suggestions = {"spec_files": spec_file_suggestions(worktree, cucumber_std_dir, profile["spec_files"])}
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


def refresh_profile(client_name: str, worktree: str, ref: str, cucumber_std_dir: str, std_runners: list = ()) -> dict:
    """Détection et surcharge du dépôt recalculées, saisie conservée ; profil.json réécrit. Renvoie le document."""
    detected, suggestions, warnings = detect_profile(client_name, worktree, ref, cucumber_std_dir, std_runners)
    document = load_profile(client_name)
    depot = repo_profile(worktree, ref)
    if depot.pop("_erreur", ""):
        warnings.append(f"{REPO_PROFILE} du dépôt client illisible : ignoré")
    document.update(detecte=detected, depot=depot, suggestions=suggestions, avertissements_detection=warnings,
                    date=datetime.now().isoformat(timespec="seconds"))
    document.setdefault("saisi", {})
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
                profile[key], sources[key] = value, source
    warnings = list((document or {}).get("avertissements_detection") or [])
    warnings += [f"{LABELS[k]} vide : à renseigner dans le profil client" for k in FIELDS if is_empty(profile[k])]
    return {**profile, "sources": sources, "avertissements": warnings}


def save_user_fields(client_name: str, fields: dict) -> dict:
    """Enregistre la saisie de l'IHM : seuls les champs différents de la détection sont gardés. Renvoie le document."""
    document = load_profile(client_name)
    detected = document.get("detecte") or empty_profile()
    document["saisi"] = {k: v for k, v in fields.items() if k in FIELDS and v != detected.get(k)}
    _write_json(profile_path(client_name), document)
    return document


def client_profile(client_name: str, worktree: str, ref: str, cucumber_std_dir: str, std_runners: list = ()) -> dict:
    """Profil appliqué, après détection dans `worktree` à `ref` (profil.json mis à jour)."""
    return effective_profile(refresh_profile(client_name, worktree, ref, cucumber_std_dir, std_runners))


def detect_in_base(client_name: str, cucumber_repo_dir: str, base: str, cucumber_std_dir: str,
                   std_runners: list = ()) -> dict:
    """Profil détecté dans un worktree du dépôt client à `base` (avant génération). Lève RuntimeError."""
    worktree = add_worktree(cucumber_repo_dir, base_worktree_dir(client_name), f"cucumber_{client_name}_profil", base,
                            reset=True)
    return client_profile(client_name, worktree, base, cucumber_std_dir, std_runners)


def init_view(profile: dict) -> dict:
    """Vue « init » attendue par cucumber_init_data (chemins des features d'init et codes de référence)."""
    return {"features": [{"chemin": p} for p in profile.get("init_features") or []],
            "codes": {k: v for k, v in (profile.get("reference_codes") or {}).items() if v}}


def profile_block(profile: dict, max_codes: int = cucumber_init_data.MAX_PROMPT_CODES) -> str:
    """
    Bloc « PROFIL CLIENT » de la partie variable du prompt de lot : seuls emplacement, tag et données autorisés,
    interdiction d'écrire purge, clear ou init. Tout vient du profil ; un champ vide est signalé comme tel.
    """
    root = profile.get("features_root") or "(non renseignée : signale-le dans « raison »)"
    tag = profile.get("tag_client") or "(non renseigné : n'ajoute aucun tag client)"
    clear = profile.get("clear_name") or "non renseigné"
    return "\n".join([
        "=== PROFIL CLIENT (seuls emplacement, tag et données autorisés) ===",
        f"- Racine des features (seul emplacement autorisé) : '{root}'",
        f"- Tag du client (casse exacte) : {tag}",
        cucumber_init_data.init_block(init_view(profile), clear, max_codes),
        "- N'utilise QUE des steps existants (catalogue du Standard, steps du client) ou définis par ce lot, avec la "
        "syntaxe de colonnes du Standard actuel ; préfère un Background à un scénario « Contexte » séparé."])


def conventions(profile: dict) -> dict:
    """Conventions des contrôles et du runner : racine, tag, init et runner du client."""
    return {"features_root": profile.get("features_root", ""), "client_tag": profile.get("tag_client", ""),
            "init": list(profile.get("init_features") or []), "runner": profile.get("runner") or {}}
