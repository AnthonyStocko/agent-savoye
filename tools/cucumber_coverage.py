"""
Vue « Couverture » de l'onglet Cucumber (parcours, étapes 2 et 3), 0 token.

Une ligne par spécifique de l'inventaire (tickets Jira en colonne), croisée avec :
- les scénarios écrits par l'agent (résultats des lots) et les features existantes du dépôt client rapprochées à
  l'inventaire ;
- le dernier résultat connu de chaque scénario : imports de rapports d'exécution (le plus récent l'emporte), à
  défaut la vérification par exécution des lots ;
- les scénarios désactivés du worktree (tag à revoir et commentaire de cause).

ÉTATS (STATES) : couvert vert, couvert en échec, couvert désactivé (cause), couvert jamais exécuté, partiel
(vérifie seulement une configuration ou la présence d'une donnée : rapprochement partiel ou scénarios « faibles »),
non couvert, non testable aujourd'hui (raison). Un spécifique couvert indirectement prend l'état de celui qui le
couvre.
TESTABILITÉ : code spécifique déployé, personnalisation Studio déployée, données recréables, steps disponibles ;
« inconnu » par défaut, « non » sur preuve d'exécution, « oui » si un scénario lié est vert. Le risque métier est
proposé (nature et type du spécifique), le risque et la testabilité sont modifiables (surcharges enregistrées dans
.tmp/cucumber/<client>/couverture.json).
"""
import os
import re
from datetime import datetime

from tools import cucumber_import, cucumber_studio
from tools.cucumber_tools import (_read_json, _write_json, a_revoir_tag, cache_dir, latest_results, load_state,
                                  worktree_dir)
from tools.cucumber_verify import NOT_TEST_STATUSES, all_verified, failure_category, scenario_id
from tools.cucumber_a_revoir import DISABLED_COMMENT

# Clé : (libellé, pastille, couleur Streamlit)
STATES = {
    "vert": ("couvert, vert", "🟢", "green"),
    "echec": ("couvert, en échec", "🔴", "red"),
    "desactive": ("couvert, désactivé", "🟠", "orange"),
    "non_execute": ("couvert, jamais exécuté", "🔵", "blue"),
    "partiel": ("partiel", "🟣", "violet"),
    "non_couvert": ("non couvert", "⚪", "gray"),
    "non_testable": ("non testable aujourd'hui", "⚫", "gray"),
}
WRITTEN_STATES = ("vert", "echec", "desactive", "non_execute")
SELECTABLE_STATES = ("non_couvert", "partiel", "non_testable")
RISKS = ("élevé", "moyen", "faible")
CRITERIA = {"code": "code spécifique déployé", "studio": "personnalisation Studio déployée",
            "donnees": "données recréables par l'init", "steps": "steps disponibles dans la bibliothèque"}
ANSWERS = ("oui", "non", "inconnu")
TESTABILITY = ("oui", "à vérifier", "non")
# Statuts de vérification des lots : vert, en échec, jamais exécuté
_GREEN = ("pertinent", "faible")
_NOT_RUN = ("non_verifie",)
_TICKET = re.compile(r"^##\s+([A-Z][A-Z0-9]+-\d+)\b", re.M)


def overrides_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "couverture.json")


def load_overrides(client_name: str) -> dict:
    """Surcharges de l'utilisateur : {id: {"risque", "testabilite": {critère: réponse}, "raison"}}."""
    return (_read_json(overrides_path(client_name), {}) or {}).get("surcharges", {})


def save_override(client_name: str, spec_id: str, values: dict) -> None:
    """Enregistre (ou retire, valeurs vides) la surcharge d'un spécifique."""
    data = _read_json(overrides_path(client_name), {}) or {}
    overrides = data.setdefault("surcharges", {})
    clean = {k: v for k, v in values.items() if v not in ("", None, {})}
    if clean:
        overrides[spec_id] = {**clean, "date": datetime.now().isoformat(timespec="seconds")}
    else:
        overrides.pop(spec_id, None)
    _write_json(overrides_path(client_name), data)


def spec_tickets(spec: dict) -> list:
    """Clés des tickets Jira rattachés au spécifique (titres « ## CLÉ — … » de son extrait Jira)."""
    path = spec.get("jira") or ""
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return list(dict.fromkeys(_TICKET.findall(f.read())))


def studio_files(spec: dict) -> list:
    """customized.xml du spécifique (personnalisations Studio)."""
    return [f for f in spec.get("fichiers") or [] if f["path"].rsplit("/", 1)[-1] == "customized.xml"]


def studio_pending(spec: dict) -> bool:
    """Comportements Studio pas encore calculés (inventaire antérieur à leur calcul) : relancer l'inventaire."""
    return any("nb_comportements_studio" not in f for f in studio_files(spec))


def module_of(spec: dict) -> str:
    """Module affiché : dépôt et premier dossier (processus : nom sans préfixe technique)."""
    folder = spec.get("dossier", "")
    if spec.get("nature") == "processus":
        return re.sub(r"^(spec|ref)-", "", folder.rsplit("/", 1)[-1])
    return folder.split("/")[0] or spec.get("depot", "")


def entry_point(spec: dict) -> str:
    """Point d'entrée : écrans Studio, endpoints, sinon nature du spécifique."""
    counts = cucumber_studio.behaviour_counts(spec)
    if counts.get("écran"):
        return f"processus interactif ({counts['écran']} écran(s))"
    endpoints = (spec.get("endpoints_client") or []) + (spec.get("endpoints_std") or [])
    if endpoints:
        first = endpoints[0]
        return f"{first.get('methode', '')} {first.get('chemin', '')}".strip() + (
            f" (+{len(endpoints) - 1})" if len(endpoints) > 1 else "")
    if counts:
        return "processus Studio"
    return spec.get("nature", "")


def proposed_risk(spec: dict) -> str:
    """Risque métier proposé : libellés et paramétrage faibles, override et processus élevés, sinon moyen."""
    if spec.get("nature") in ("libellés", "scripts SQL") or spec.get("type") == "parametrage":
        return "faible"
    if spec.get("type") == "override" or spec.get("nature") == "processus":
        return "élevé"
    return "moyen"


def effort(spec: dict) -> int:
    """Effort estimé (plus petit = plus simple) : fichiers, sans test Standard ni step candidat, comportements."""
    behaviours = sum(cucumber_studio.behaviour_counts(spec).values())
    return (min(spec.get("nb_fichiers", 0), 20) + (0 if spec.get("tests_std") else 5)
            + (0 if spec.get("steps_candidats") else 3) + min(behaviours // 10, 5))


def disabled_scenarios(worktree: str) -> dict:
    """Scénarios désactivés des features du worktree : {id: cause} (commentaire « # À revoir : » au-dessus)."""
    tag, found = a_revoir_tag(), {}
    if not worktree or not os.path.isdir(worktree):
        return found
    for root, dirs, files in os.walk(worktree):
        dirs[:] = [d for d in dirs if d not in (".git", "target", "node_modules")]
        for name in files:
            if not name.endswith(".feature"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            if tag in text:
                rel = os.path.relpath(path, worktree).replace("\\", "/")
                found.update(_disabled_in(text, rel, tag))
    return found


def _disabled_in(text: str, rel: str, tag: str) -> dict:
    """Scénarios d'une feature précédés du tag à revoir : {id: cause}."""
    found, cause, tagged = {}, "", False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(DISABLED_COMMENT):
            cause = stripped[len(DISABLED_COMMENT):].strip()
        elif stripped.startswith("@"):
            tagged = tagged or tag in stripped.split()
        elif re.match(r"^(Scenario|Scénario|Scenario Outline|Plan du [Ss]cénario|Example|Exemple)\s*:", stripped):
            if tagged:
                found[scenario_id(rel, stripped.split(":", 1)[1])] = cause or "cause non précisée"
            cause, tagged = "", False
        elif stripped and not stripped.startswith("#"):
            cause, tagged = "", False
    return found


def import_results(client_name: str) -> dict:
    """Dernier résultat de chaque scénario dans les imports analysés : {id: {statut, nature, motif, import, date}}."""
    results = {}
    index = cucumber_import.load_index(client_name)
    for entry in sorted(index.get("imports", []), key=lambda e: e["numero"]):
        analysis = _read_json(os.path.join(cucumber_import.imports_dir(client_name), str(entry["numero"]),
                                           "analyse.json"), {}) or {}
        # Run non analysé ou initialisation en échec : aucun scénario n'y est jugé
        if analysis.get("non_analyse") or analysis.get("init_en_echec"):
            continue
        for identifier, r in (analysis.get("resultats") or {}).items():
            results[identifier] = {"statut": r.get("statut", ""), "nature": r.get("nature", ""),
                                   "motif": r.get("message", ""), "import": entry["numero"],
                                   "date": entry.get("date", "")}
    return results


def verified_results(state: dict) -> dict:
    """Statut de vérification des scénarios des lots : {id: {statut, motif, lot}}."""
    return {item["id"]: {"statut": item.get("statut", ""), "motif": item.get("motif", ""), "lot": item["lot"]}
            for item in all_verified(state) if item.get("id")}


def scenario_status(identifier: str, sources: dict) -> dict:
    """
    État d'un scénario : {id, etat (vert | echec | desactive | non_execute), faible, cause, nature, derniere}.
    Ordre : désactivé dans le worktree, dernier import, vérification du lot.
    """
    out = {"id": identifier, "etat": "non_execute", "faible": False, "cause": "", "nature": "", "derniere": ""}
    if identifier in sources["desactives"]:
        return {**out, "etat": "desactive", "cause": sources["desactives"][identifier]}
    imported = sources["imports"].get(identifier)
    if imported:
        passed = imported["statut"] == "passe"
        return {**out, "etat": "vert" if passed else "echec", "cause": "" if passed else imported["motif"],
                "nature": imported["nature"], "derniere": f"import n° {imported['import']} du "
                                                          f"{imported['date'][:10]}"}
    verified = sources["verifies"].get(identifier)
    if verified and verified["statut"] not in _NOT_RUN:
        green = verified["statut"] in _GREEN
        return {**out, "etat": "vert" if green else "echec", "faible": verified["statut"] == "faible",
                "cause": "" if green else verified["motif"], "nature": verified["statut"],
                "derniere": f"vérification du lot {verified['lot']}"}
    return out


def linked_scenarios(spec: dict, result: dict, sources: dict) -> list:
    """Scénarios liés au spécifique : écrits par l'agent, puis scénarios importés des features existantes liées."""
    ids = [scenario_id(s.get("fichier", ""), str(s.get("nom", "")).rsplit(" / ", 1)[-1])
           for s in result.get("scenarios") or [] if isinstance(s, dict) and s.get("fichier")]
    features = [entry.rsplit(" (", 1)[0] for entry in spec.get("scenarios_existants") or []]
    for identifier in list(sources["imports"]) + list(sources["desactives"]):
        if identifier.split("::", 1)[0] in features and identifier not in ids:
            ids.append(identifier)
    return [scenario_status(i, sources) for i in ids]


def testability(spec: dict, result: dict, scenarios: list, override: dict = None) -> dict:
    """
    {critère: (réponse, raison)} pour les critères applicables, puis la synthèse « testabilite » (oui, à vérifier,
    non) et « raisons ». Les réponses saisies (override) l'emportent.
    """
    green = any(s["etat"] == "vert" for s in scenarios)
    natures = {s["nature"] for s in scenarios}
    has_studio = bool(studio_files(spec))
    has_code = any(f["path"].endswith(".java") for f in spec.get("fichiers") or [])
    criteria = {}
    for key, applies in (("code", has_code), ("studio", has_studio)):
        if applies:
            criteria[key] = (("non", "échec « déploiement » d'un scénario lié") if "deploiement" in natures else
                             ("oui", "scénario lié vert") if green else ("inconnu", ""))
    criteria["donnees"] = (("non", "donnée de référence absente à l'exécution") if "donnee_reference" in natures
                           else ("oui", "scénario lié vert") if green else ("inconnu", ""))
    reason = result.get("raison", "") if result.get("statut") == "non_couvert" else ""
    criteria["steps"] = (("non", reason) if reason else ("oui", "scénario lié vert") if green else
                         ("oui", "steps Standard candidats") if spec.get("steps_candidats") or spec.get("tests_std")
                         else ("inconnu", ""))
    for key, answer in ((override or {}).get("testabilite") or {}).items():
        if key in criteria and answer in ANSWERS:
            criteria[key] = (answer, "saisi")
    answers = [a for a, _ in criteria.values()]
    summary = "non" if "non" in answers else "oui" if all(a == "oui" for a in answers) else "à vérifier"
    reasons = [f"{CRITERIA[k]} : {a}" + (f" ({r})" if r else "") for k, (a, r) in criteria.items() if a != "oui"]
    return {"criteres": criteria, "testabilite": summary, "raisons": reasons}


def spec_state(spec: dict, result: dict, scenarios: list, testable: dict) -> tuple:
    """(état, cause) d'un spécifique à partir de ses scénarios, du résultat de son lot et de sa testabilité."""
    active = [s for s in scenarios if s["etat"] != "desactive"]
    if scenarios and not active:
        return "desactive", " ; ".join(dict.fromkeys(s["cause"] for s in scenarios))
    if active:
        failing = [s for s in active if s["etat"] == "echec"]
        if failing:
            return "echec", failing[0]["cause"]
        green = [s for s in active if s["etat"] == "vert"]
        if green and all(s["faible"] for s in green):
            return "partiel", "scénarios « faibles » : ils passent aussi sans le spécifique"
        return ("vert", "") if green else ("non_execute", "")
    if result.get("statut") == "non_couvert" and result.get("raison"):
        return "non_testable", result["raison"]
    if testable["testabilite"] == "non":
        return "non_testable", " ; ".join(testable["raisons"])
    if spec.get("couverture") == "partiel":
        return "partiel", "features existantes rapprochées en partie seulement"
    if spec.get("couverture") == "deja_couvert":
        return "non_execute", "features existantes du dépôt client, aucun résultat d'exécution connu"
    return "non_couvert", ""


def last_execution(scenarios: list) -> str:
    """« import n° 3 du 2026-10-26 : 4 vert(s), 1 échec(s) » ; '' si aucun scénario exécuté."""
    run = [s for s in scenarios if s["derniere"]]
    if not run:
        return ""
    latest = max(s["derniere"] for s in run)
    green = sum(1 for s in run if s["etat"] == "vert")
    return f"{latest} : {green} vert(s), {len(run) - green} échec(s)"


def coverage_sources(client_name: str, state: dict) -> dict:
    """Résultats croisés une fois pour toutes les lignes : imports, vérifications, scénarios désactivés."""
    return {"imports": import_results(client_name), "verifies": verified_results(state),
            "desactives": disabled_scenarios(worktree_dir(client_name))}


def coverage_rows(client_name: str, inventory: dict, state: dict = None, sources: dict = None) -> list:
    """Lignes de la vue Couverture, triées dans l'ordre de sélection proposé (selection_order)."""
    state = load_state(client_name) if state is None else state
    sources = coverage_sources(client_name, state) if sources is None else sources
    overrides, results = load_overrides(client_name), latest_results(state)
    rows = {}
    for spec in inventory.get("specifiques") or []:
        result, override = results.get(spec["id"], {}), overrides.get(spec["id"], {})
        scenarios = linked_scenarios(spec, result, sources)
        testable = testability(spec, result, scenarios, override)
        state_key, cause = spec_state(spec, result, scenarios, testable)
        rows[spec["id"]] = {
            "id": spec["id"], "tache": spec["libelle"], "module": module_of(spec), "point_entree": entry_point(spec),
            "tickets": spec_tickets(spec), "risque": override.get("risque") or proposed_risk(spec),
            "risque_propose": proposed_risk(spec), "features": sorted({s["id"].split("::", 1)[0] for s in scenarios}),
            "etat": state_key, "cause": cause, "testabilite": testable["testabilite"],
            "criteres": testable["criteres"], "raisons": testable["raisons"], "derniere": last_execution(scenarios),
            "scenarios": scenarios, "effort": effort(spec), "constats": result.get("constats") or [],
            "studio": cucumber_studio.spec_behaviours(spec), "studio_total": cucumber_studio.behaviour_counts(spec),
            "studio_a_calculer": studio_pending(spec),
            "couvert_par": result.get("couvert_par", "") if result.get("statut") == "couvert_indirectement" else ""}
    for row in rows.values():
        covering = rows.get(row["couvert_par"])
        if covering and not row["scenarios"]:
            row["etat"], row["cause"] = covering["etat"], f"couvert indirectement par {covering['tache']}"
    return selection_order(list(rows.values()))


def selection_order(rows: list) -> list:
    """Ordre proposé : risque métier, puis testabilité, puis effort."""
    return sorted(rows, key=lambda r: (RISKS.index(r["risque"]) if r["risque"] in RISKS else 1,
                                       TESTABILITY.index(r["testabilite"]), r["effort"], r["tache"]))


def rates(rows: list) -> dict:
    """Couverture RÉELLE (actifs et verts) et ÉCRITE (actifs et désactivés), en % des tâches, et partielles."""
    total = len(rows)
    count = {key: sum(1 for r in rows if r["etat"] == key) for key in STATES}

    def pct(n):
        return round(100 * n / total) if total else 0
    return {"total": total, "par_etat": count, "reelle": pct(count["vert"]),
            "ecrite": pct(sum(count[k] for k in WRITTEN_STATES)), "partielles": count["partiel"],
            "couvertes": count["vert"]}


def state_label(row: dict) -> str:
    """Libellé affiché de l'état, avec sa pastille et sa cause (désactivé, non testable)."""
    label, badge, _ = STATES[row["etat"]]
    cause = row["cause"] if row["etat"] in ("desactive", "non_testable") and row["cause"] else ""
    return f"{badge} {label}" + (f" ({cause[:80]})" if cause else "")


def legend() -> str:
    """Légende des états (pastilles)."""
    return " · ".join(f"{badge} {label}" for label, badge, _ in STATES.values())


def partial_ids(rows: list) -> set:
    """Tâches « partielles » : leur lot doit tester l'EFFET, pas la configuration."""
    return {r["id"] for r in rows if r["etat"] == "partiel"}


def selection_warnings(rows: list, selected: list) -> list:
    """Avertissements d'une sélection : tâches non testables aujourd'hui, avec leur raison."""
    by_id = {r["id"]: r for r in rows}
    return [f"{by_id[i]['tache']} : non testable aujourd'hui ({by_id[i]['cause'] or 'raison inconnue'})"
            for i in selected if i in by_id and by_id[i]["etat"] == "non_testable"]


def summary_line(rows: list) -> str:
    """« 12 tâches couvertes sur 67 (réelle 18 %, écrite 25 %), 4 partielle(s) »."""
    r = rates(rows)
    if not r["total"]:
        return "aucune tâche inventoriée"
    return (f"{r['couvertes']} tâche(s) couverte(s) et vertes sur {r['total']} (réelle {r['reelle']} %, écrite "
            f"{r['ecrite']} %), {r['partielles']} partielle(s)")


def failing_test_causes(rows: list) -> int:
    """Nombre de tâches en échec pour une cause « test » (à corriger par l'agent)."""
    def is_test(scenario):
        nature = scenario["nature"]
        return nature not in NOT_TEST_STATUSES and failure_category(nature) == "test"
    return sum(1 for r in rows if r["etat"] == "echec"
               and any(s["etat"] == "echec" and is_test(s) for s in r["scenarios"]))
