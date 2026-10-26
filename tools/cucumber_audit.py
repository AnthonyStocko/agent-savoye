"""
Audit des tests existants (point 9i, action « Auditer » de la vue Couverture), 0 token.

Pour chaque scénario lié à une tâche de la vue Couverture (écrit par l'agent ou déjà dans le dépôt client) :
- Détecte-t-il l'ABSENCE du spécifique ? Prouvé par la vérification par exécution (« pertinent » : échoue sur le
  Standard ou avec un attendu faussé ; « faible » : passe aussi sans le spécifique), sinon probable s'il cite un
  élément propre au spécifique (écran, bouton ou message Studio, endpoint du client, mots-clés), sinon inconnu.
- Qualité des assertions : aucune, génériques (statut HTTP, succès), présence d'une donnée seulement, configuration
  seulement, ou valeurs métier.
- Robustesse : attente fixe, date en dur, comptage global dans un contrôle, dépendance à l'état laissé par un autre
  scénario.
- Durée : anormalement longue au dernier run importé (au-delà du seuil ou de 3 fois la médiane).
Verdict : « partiel » (ne vérifie qu'une configuration ou la présence d'une donnée, ou ne détecte pas l'absence du
spécifique), « fragile », « pertinent » ou « à vérifier ». Les verdicts « partiel » font passer la tâche à l'état
partiel et sortent ses scénarios de la couverture réelle. Stockage : .tmp/cucumber/<client>/audit.json.
"""
import os
import re
from datetime import datetime

from tools import cucumber_checks, cucumber_import, cucumber_studio
from tools.cucumber_tools import _read_json, _read_text_file, _write_json, cache_dir, worktree_dir
from tools.cucumber_verify import (_GENERIC_THEN, check_scenario_dependencies, parse_scenarios, scenario_id)

VERDICTS = {"pertinent": "pertinent", "partiel": "partiel", "fragile": "fragile", "a_verifier": "à vérifier"}
ASSERTIONS = {"aucune": "aucune assertion (Then)", "generique": "assertions génériques (statut HTTP, succès)",
              "presence": "vérifie seulement la présence d'une donnée",
              "configuration": "vérifie seulement une configuration", "metier": "assertions sur des valeurs métier"}
WEAK_ASSERTIONS = ("aucune", "generique", "presence", "configuration")
_PRESENCE = re.compile(r"(?i)\b(existe(?:nt)?|exists?|est (?:bien )?(?:présent|créé)e?s?|is (?:present|created)|"
                       r"n'est pas vide|is not empty|not empty|trouvée?s?)\b")
_CONFIGURATION = re.compile(r"(?i)param[eèé]tr|configur|setting|propriét[ée] syst")
_FIXED_WAIT = re.compile(r"(?i)\b(attend(?:re|s)?|patiente[rz]?|wait|pause|sleep)\b\D{0,20}\d+\s*"
                         r"(ms|s|sec|secondes?|seconds?|minutes?)\b")
_HARD_DATE = re.compile(r"\b20\d\d-[01]\d-[0-3]\d\b|\b[0-3]\d/[01]\d/20\d\d\b")
_GLOBAL_COUNT = re.compile(r"(?i)\b(nombre (?:de|d')|count|au moins|at least|total)\b")
_QUOTED = re.compile(r"\"[^\"]+\"")


def audit_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "audit.json")


def load_audit(client_name: str) -> dict:
    """Dernier audit : {"date", "scenarios": {id: résultat}} ({} sans audit)."""
    return _read_json(audit_path(client_name), {}) or {}


def source_dir(client_name: str) -> str:
    """Worktree de génération, sinon celui du bilan de santé (dépôt client à sa base) ; '' sans l'un ni l'autre."""
    return next((d for d in (worktree_dir(client_name), cucumber_checks.health_worktree_dir(client_name))
                if os.path.isdir(d)), "")


def scenario_index(root: str, wanted: set) -> dict:
    """{id: (feature, scénario, texte de la feature)} des scénarios `wanted` trouvés dans les features de `root`."""
    index = {}
    for rel in sorted({i.split("::", 1)[0] for i in wanted}):
        path = os.path.join(root, *rel.split("/"))
        if not os.path.exists(path):
            continue
        text = _read_text_file(path)
        for scenario in parse_scenarios(text):
            identifier = scenario_id(rel, scenario["titre"])
            if identifier in wanted:
                index[identifier] = (rel, scenario, text)
    return index


def assertion_quality(scenario: dict) -> str:
    """Clé de ASSERTIONS : la plus forte des assertions (Then) du scénario."""
    thens = [s for s in scenario["steps"] if s["phase"] == "then"]
    if not thens:
        return "aucune"
    if all(_GENERIC_THEN.search(s["texte"]) for s in thens):
        return "generique"
    business = [s for s in thens if not _GENERIC_THEN.search(s["texte"])]
    valued = [s for s in business if len(s.get("table") or []) > 1 or _QUOTED.search(s["texte"])]
    if valued and all(_CONFIGURATION.search(s["texte"]) for s in valued):
        return "configuration"
    if not valued or all(_PRESENCE.search(s["texte"]) and len(s.get("table") or []) <= 1 for s in valued):
        return "presence"
    return "metier"


def spec_markers(spec: dict) -> tuple:
    """
    (éléments propres au spécifique : libellés Studio — écrans, boutons, messages, champs — et endpoints du client ;
    mots-clés du spécifique).
    """
    strong = {b["nom"].split(" : ", 1)[-1].lower() for b in cucumber_studio.spec_behaviours(spec)
              if b["type"] in ("écran", "bouton", "message", "champ") and len(b["nom"]) > 2}
    strong |= {str(e.get("chemin", "")).lower() for e in spec.get("endpoints_client") or [] if e.get("chemin")}
    keywords = {k.lower() for k in spec.get("mots_cles") or [] if len(k) > 3}
    return strong - {""}, keywords - strong


def absence_detection(scenario: dict, spec: dict, verified: str) -> tuple:
    """
    (« oui » | « non » | « probable » | « inconnu », motif) : le scénario échouerait-il sans le spécifique ? Probable
    s'il cite un élément propre au spécifique, ou au moins deux de ses mots-clés.
    """
    if verified == "pertinent":
        return "oui", "prouvé par l'exécution (échoue sur le Standard ou avec un attendu faussé)"
    if verified == "faible":
        return "non", "passe aussi sans le spécifique (exécution sur le Standard ou attendu faussé)"
    text = " ".join([scenario["titre"]] + [s["texte"] + " " + " ".join(s.get("table") or [])
                                           for s in scenario["steps"]]).lower()
    strong, keywords = spec_markers(spec)
    cited, words = sorted(m for m in strong if m in text), sorted(k for k in keywords if k in text)
    if cited or len(words) >= 2:
        return "probable", "cite " + ", ".join((cited + words)[:3])
    return "inconnu", "aucun élément propre au spécifique cité, pas de vérification par exécution"


def robustness(rel: str, scenario: dict, dependencies: list) -> list:
    """Fragilités : attente fixe, date en dur, comptage global dans un Then, dépendance à un autre scénario."""
    issues = []
    for step in scenario["steps"]:
        line = step["texte"] + " " + " ".join(step.get("table") or [])
        if _FIXED_WAIT.search(step["texte"]):
            issues.append(f"attente fixe : « {step['texte'][:80]} »")
        if _HARD_DATE.search(line):
            issues.append(f"date en dur : « {step['texte'][:80]} »")
        if step["phase"] == "then" and _GLOBAL_COUNT.search(step["texte"]):
            issues.append(f"comptage global dans un contrôle : « {step['texte'][:80]} »")
    issues += [e.split(" ", 1)[1] for e in dependencies if e.startswith(f"{rel}:{scenario['ligne']} ")]
    return list(dict.fromkeys(issues))


def durations(client_name: str) -> tuple:
    """({id: secondes} des runs importés, le plus récent l'emporte ; {id: motif} des scénarios anormalement longs)."""
    found, slow = {}, {}
    index = cucumber_import.load_index(client_name)
    for entry in sorted(index.get("imports", []), key=lambda e: e["numero"]):
        analysis = _read_json(os.path.join(cucumber_import.imports_dir(client_name), str(entry["numero"]),
                                           "analyse.json"), {}) or {}
        found.update((analysis.get("durees") or {}).get("par_scenario") or {})
        slow.update({e["id"]: e["motif"] for e in (analysis.get("durees") or {}).get("lents") or []})
    return found, slow


def verdict(result: dict) -> str:
    """Verdict d'un scénario audité (voir docstring du module)."""
    if result["assertions"] in WEAK_ASSERTIONS or result["absence"] == "non":
        return "partiel"
    if result["robustesse"] or result["duree_anormale"]:
        return "fragile"
    return "pertinent" if result["absence"] in ("oui", "probable") else "a_verifier"


def audit_scenario(identifier: str, found: tuple, spec: dict, verified: str, dependencies: list,
                   timing: tuple) -> dict:
    """Audit d'un scénario : critères, motifs et verdict."""
    rel, scenario, _ = found
    absence, why = absence_detection(scenario, spec, verified)
    result = {"spec": spec.get("id", ""), "absence": absence, "absence_motif": why,
              "assertions": assertion_quality(scenario), "robustesse": robustness(rel, scenario, dependencies),
              "duree": timing[0].get(identifier), "duree_anormale": timing[1].get(identifier, "")}
    result["verdict"] = verdict(result)
    result["motifs"] = ([ASSERTIONS[result["assertions"]]] if result["assertions"] != "metier" else []) + (
        [f"détection de l'absence du spécifique : {absence} ({why})"]) + result["robustesse"] + (
        [f"durée : {result['duree_anormale']}"] if result["duree_anormale"] else [])
    return result


def run_audit(client_name: str, inventory: dict, rows: list, verified: dict) -> dict:
    """
    Audite les scénarios liés aux tâches `rows` (vue Couverture) ; `verified` : {id: statut de vérification des
    lots}. Écrit et renvoie {"date", "source", "scenarios": {id: résultat}, "introuvables": [ids]}.
    """
    specs = {s["id"]: s for s in inventory.get("specifiques") or []}
    wanted = {s["id"]: r["id"] for r in rows for s in r["scenarios"]}
    root = source_dir(client_name)
    found = scenario_index(root, set(wanted)) if root else {}
    features = sorted({rel for rel, _, _ in found.values()})
    dependencies = check_scenario_dependencies(root, features) if root else []
    timing = durations(client_name)
    audit = {"date": datetime.now().isoformat(timespec="seconds"), "source": root, "scenarios": {},
             "introuvables": sorted(set(wanted) - set(found))}
    for identifier, item in found.items():
        audit["scenarios"][identifier] = audit_scenario(identifier, item, specs.get(wanted[identifier], {}),
                                                        verified.get(identifier, ""), dependencies, timing)
    _write_json(audit_path(client_name), audit)
    return audit


def summary_line(audit: dict) -> str:
    """« audit du 2026-10-26 : 12 scénario(s), 3 partiel(s), 1 fragile(s)… » ; '' sans audit."""
    if not audit.get("scenarios"):
        return ""
    counts = {}
    for result in audit["scenarios"].values():
        counts[result["verdict"]] = counts.get(result["verdict"], 0) + 1
    return (f"audit du {audit['date'][:10]} : {len(audit['scenarios'])} scénario(s), "
            + ", ".join(f"{n} {VERDICTS[k]}" for k, n in sorted(counts.items())))
