"""
Tests existants du dépôt <client>-cucumber et leur rattachement aux tâches de l'inventaire (0 token, lecture seule).

Tous les scénarios du dépôt client sont reconnus, y compris ceux écrits hors de l'agent. Ils sont lus sur la
branche configurée dans le profil (verify_branch, celle que lit le job Jenkins), à défaut la branche de base, par
`git ls-tree` / `git show` sur le clone : rien n'est écrit dans le dépôt.

Rattachement d'une feature aux tâches, par ordre de force :
- « certain » : clé d'un ticket (tag @SPEC-123, nom de fichier, titre) liée de façon vérifiée à la tâche
  (tickets_verifies.json), ou référence explicite à un fichier de la tâche (commentaire « Source », nom de classe
  Java, nom du processus Studio) ;
- « proposé » : clé d'un ticket seulement rapproché de la tâche par mots-clés (extrait Jira), steps Standard
  candidats de la tâche utilisés par la feature, ou mots-clés de la tâche dans la feature. À confirmer.
Une feature sans aucun rattachement certain est « non rattachée », avec sa meilleure proposition.
Confirmations de l'utilisateur : .tmp/cucumber/<client>/rattachements.json {feature: id de tâche | ""}.
"""
import os
import re
import unicodedata

from tools.cucumber_tools import _git, _read_json, _write_json, cache_dir, a_revoir_tag
from tools.cucumber_verify import _STEP_LINE, scenario_units

_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_CLASS = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+){1,}\b")
_PROCESS_NAME = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+){2,}\b")
_COMMENT = re.compile(r"^\s*#\s*(.*)$")
_WORD = re.compile(r"[a-zà-ÿ0-9]{4,}")
# Au-delà, un indice partagé par plusieurs tâches est ambigu
MAX_SHARED = 2


def confirmations_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "rattachements.json")


def load_confirmations(client_name: str) -> dict:
    return _read_json(confirmations_path(client_name), {}) or {}


def confirm(client_name: str, feature: str, spec_id: str) -> None:
    """Rattachement confirmé (ou refusé : spec_id vide) par l'utilisateur."""
    data = load_confirmations(client_name)
    data[feature] = spec_id
    _write_json(confirmations_path(client_name), data)


def read_ref(cucumber_repo_dir: str, profile: dict, base_branch: str = "integration") -> str:
    """Branche lue : celle du job Jenkins (verify_branch du profil), à défaut la branche de base (origin/<base>)."""
    for name in [profile.get("verify_branch") or "", base_branch]:
        for ref in (f"origin/{name}", name) if name else ():
            if _git(cucumber_repo_dir, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}").returncode == 0:
                return ref
    return ""


def existing_features(cucumber_repo_dir: str, ref: str, features_root: str, init_paths=()) -> list:
    """
    Features du client à `ref` (hors init) : [{chemin, titre, tags, scenarios (actifs), desactives, commentaires,
    steps, texte}], lues par git sans rien écrire.
    """
    if not ref:
        return []
    paths = [p for p in _git(cucumber_repo_dir, "ls-tree", "-r", "--name-only", ref, "--",
                             features_root).stdout.splitlines() if p.endswith(".feature") and p not in set(init_paths)]
    disabled = a_revoir_tag()
    out = []
    for path in sorted(paths):
        text = _git(cucumber_repo_dir, "show", f"{ref}:{path}").stdout
        units = scenario_units(text)
        title = next((line.split(":", 1)[1].strip() for line in text.splitlines()
                      if re.match(r"^\s*(Feature|Fonctionnalité)\s*:", line)), "")
        out.append({"chemin": path, "titre": title,
                    "titres": [u["titre"] for u in units if disabled not in u["tags"]],
                    "tags": sorted({t for u in units for t in u["tags"]}),
                    "scenarios": sum(u["nombre"] for u in units if disabled not in u["tags"]),
                    "desactives": sum(u["nombre"] for u in units if disabled in u["tags"]),
                    "commentaires": [m.group(1) for m in map(_COMMENT.match, text.splitlines()) if m],
                    "steps": [m.group(2) for m in map(_STEP_LINE.match, text.splitlines()) if m],
                    "texte": text})
    return out


def _plain(text: str) -> str:
    """Minuscules sans accents (« Étiquette » = « etiquette »)."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def _spec_signatures(spec: dict) -> dict:
    """Ce qui désigne explicitement une tâche : fichiers, classes Java, processus Studio."""
    files = [f.get("path", "") for f in spec.get("fichiers") or []]
    classes = {os.path.splitext(os.path.basename(p))[0] for p in files if p.endswith(".java")}
    processes = {p.split("/")[0] for p in files if p.endswith(("customized.xml", "customized.json"))}
    if spec.get("nature") == "processus" and spec.get("dossier"):
        processes.add(spec["dossier"].rsplit("/", 1)[-1])
    return {"fichiers": set(files), "classes": {c for c in classes if len(c) > 6}, "processus": processes}


def link_feature(feature: dict, specifics: list, verified: dict, candidates: dict) -> list:
    """
    Rattachements d'une feature : [{"spec", "force": certain|proposé, "raisons", "score"}], du plus fort au plus
    faible. `verified` : {id: [clés de tickets vérifiées]} ; `candidates` : {id: [clés rapprochées par mots-clés]}.
    """
    text = feature["texte"]
    keys = set(_KEY.findall(" ".join([feature["chemin"], feature["titre"], " ".join(feature["tags"]),
                                      " ".join(feature["commentaires"])])))
    comments = " ".join(feature["commentaires"])
    classes_in = set(_CLASS.findall(comments))
    processes_in = set(_PROCESS_NAME.findall(text))
    words = set(_WORD.findall(_plain(text)))
    steps = {" ".join(s.lower().split()) for s in feature["steps"]}
    found = []
    for spec in specifics:
        signature = _spec_signatures(spec)
        strong, weak = [], []
        for key in sorted(keys & set(verified.get(spec["id"]) or [])):
            strong.append(f"ticket {key} lié à la tâche (lien vérifié)")
        for key in sorted(keys & set(candidates.get(spec["id"]) or []) - set(verified.get(spec["id"]) or [])):
            weak.append(f"ticket {key} rapproché de la tâche par mots-clés")
        strong += [f"commentaire « Source » : {path}" for path in signature["fichiers"] if path and path in comments]
        strong += [f"classe {c} citée" for c in sorted(signature["classes"] & classes_in)]
        strong += [f"processus Studio {p} cité" for p in sorted(signature["processus"] & processes_in)]
        expressions = {" ".join(str(s.get("expr", "")).lower().split()) for s in spec.get("steps_candidats") or []}
        shared_steps = [e for e in expressions if e and any(_step_matches(e, s) for s in steps)]
        if len(shared_steps) >= 2:
            weak.append(f"{len(shared_steps)} step(s) Standard candidat(s) de la tâche utilisé(s)")
        keywords = {_plain(k) for k in spec.get("mots_cles") or [] if len(k) >= 5}
        shared_words = sorted(keywords & words)
        if len(shared_words) >= 3:
            weak.append(f"mots-clés communs : {', '.join(shared_words[:5])}")
        if strong or weak:
            found.append({"spec": spec["id"], "strong": strong, "weak": weak, "mots": len(shared_words)})
    # Un indice fort partagé par plus de MAX_SHARED tâches (ticket transverse, classe utilitaire) est ambigu : il ne
    # désigne pas une tâche, il ne vaut qu'une proposition
    shared = {}
    for item in found:
        for reason in item["strong"]:
            shared[reason] = shared.get(reason, 0) + 1
    out = []
    for item in found:
        strong = [r for r in item["strong"] if shared[r] <= MAX_SHARED]
        weak = [f"{r} (commun à {shared[r]} tâches)" for r in item["strong"] if shared[r] > MAX_SHARED] + item["weak"]
        out.append({"spec": item["spec"], "force": "certain" if strong else "proposé", "raisons": strong + weak,
                    "score": 10 * len(strong) + len(weak) + item["mots"] / 10})
    return sorted(out, key=lambda f: (f["force"] != "certain", -f["score"]))


def _step_matches(expression: str, step: str) -> bool:
    """Step de la feature conforme à l'expression Cucumber d'un step Standard ({string}, {int}… = valeur)."""
    pattern = re.escape(expression)
    pattern = re.sub(r"\\\{[a-z]*\\\}", ".+?", pattern)
    return re.fullmatch(pattern, step) is not None


def link_all(features: list, specifics: list, verified: dict, candidates: dict, confirmed: dict = None) -> dict:
    """
    Rattachement de toutes les features : {"rattachees": {chemin: [rattachements certains]}, "non_rattachees":
    [{feature, titre, scenarios, proposition}], "par_tache": {id: [chemins]}}. Une confirmation l'emporte.
    """
    return resolve([{**f, "liens": link_feature(f, specifics, verified, candidates)} for f in features], confirmed)


def resolve(features: list, confirmed: dict = None) -> dict:
    """Rattachements à partir des liens calculés de chaque feature (« liens ») et des confirmations de l'utilisateur."""
    confirmed = confirmed or {}
    linked, unlinked, by_task = {}, [], {}
    for feature in features:
        path, links = feature["chemin"], feature.get("liens") or []
        certain = [l for l in links if l["force"] == "certain"]
        if path in confirmed:
            certain = [{"spec": confirmed[path], "force": "certain", "raisons": ["confirmé par l'utilisateur"],
                        "score": 100}] if confirmed[path] else []
        if certain:
            linked[path] = certain
            for link in certain:
                by_task.setdefault(link["spec"], []).append(path)
        elif path not in confirmed:
            unlinked.append({"feature": path, "titre": feature["titre"], "scenarios": feature["scenarios"],
                             "propositions": links[:3], "proposition": links[0] if links else None})
    return {"rattachees": linked, "non_rattachees": unlinked, "par_tache": by_task}


def tests_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "tests_existants.json")


def refresh(client_name: str, cucumber_repo_dir: str, profile: dict, inventory: dict, verified: dict,
            base_branch: str = "integration") -> dict:
    """
    Tests existants du dépôt client et leurs liens, recalculés (détection automatique) et mis en cache :
    {"branche", "commit", "date", "features": [{chemin, titre, tags, scenarios, desactives, titres, liens}]}.
    """
    ref = read_ref(cucumber_repo_dir, profile, base_branch)
    specifics = inventory.get("specifiques") or []
    candidates = candidate_keys(specifics)
    features = existing_features(cucumber_repo_dir, ref, profile.get("features_root") or "",
                                 profile.get("init_features") or [])
    document = {"branche": ref, "commit": _git(cucumber_repo_dir, "rev-parse", "--short", ref).stdout.strip() if ref
                else "", "date": _git(cucumber_repo_dir, "log", "-1", "--format=%cI", ref).stdout.strip() if ref
                else "", "branche_profil": profile.get("verify_branch") or "",
                "features": [{**{k: f[k] for k in ("chemin", "titre", "tags", "scenarios", "desactives", "titres")},
                              "liens": link_feature(f, specifics, verified, candidates)} for f in features]}
    _write_json(tests_path(client_name), document)
    return document


def load_tests(client_name: str) -> dict:
    return _read_json(tests_path(client_name), {}) or {}


def current_links(client_name: str) -> dict:
    """Rattachements actuels (cache des tests existants et confirmations) ; {} sans cache."""
    document = load_tests(client_name)
    if not document.get("features") and not document.get("branche"):
        return {}
    return {**resolve(document.get("features") or [], load_confirmations(client_name)), "document": document}


def candidate_keys(specifics: list) -> dict:
    """{id: [clés de tickets]} de l'extrait Jira de chaque tâche (rapprochement par mots-clés, non vérifié)."""
    out = {}
    for spec in specifics:
        path = spec.get("jira") or ""
        if path and os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                out[spec["id"]] = re.findall(r"^##\s+([A-Z][A-Z0-9]+-\d+)", f.read(), re.M)
    return out
