"""
État des tests en un coup d'œil (écran principal de l'onglet Cucumber), à partir des lignes de couverture (0 token) :
- failing : scénarios en échec, avec la raison du dernier échec et d'où elle vient (run importé, vérification) ;
- disabled : scénarios désactivés (mis de côté par l'utilisateur ou par l'agent), avec leur cause ;
- covered : fonctionnalités couvertes — par leur propre test OU par une autre fonctionnalité (comptées couvertes) ;
- untested : fonctionnalités sans test, avec la raison (jamais traitée, réponse attendue, blocage prouvé…) ;
- out_of_scope : non testables aujourd'hui ou sans objet, avec la raison.
"""
from tools import cucumber_coverage
from tools.cucumber_results import KINDS

STATE_WORDS = {"vert": "vert", "echec": "en échec", "desactive": "désactivé", "non_execute": "pas encore exécuté"}


def _titles(rows: list) -> dict:
    return {r["id"]: r["titre"] for r in rows}


def _scenario_title(identifier: str) -> str:
    return identifier.split("::", 1)[-1]


def failing(rows: list) -> list:
    """Scénarios en échec (une ligne par scénario, ses fonctionnalités jointes), le plus récent d'abord."""
    found = {}
    for row in rows:
        for scenario in row.get("scenarios") or []:
            if scenario["etat"] != "echec":
                continue
            item = found.setdefault(scenario["id"], {"id": scenario["id"], "scenario": _scenario_title(scenario["id"]),
                                                     "feature": scenario["id"].split("::", 1)[0].rsplit("/", 1)[-1],
                                                     "fonctionnalites": [], "raison": scenario.get("cause") or
                                                     "échec sans message", "constate": scenario.get("derniere", "")})
            if row["titre"] not in item["fonctionnalites"]:
                item["fonctionnalites"].append(row["titre"])
    return sorted(found.values(), key=lambda i: (i["constate"], i["id"]), reverse=True)


def disabled(rows: list, set_aside: list) -> list:
    """Scénarios désactivés : cause, et qui l'a décidé (toi : mis de côté ; l'agent : cause constatée)."""
    mine = {e.get("id") for e in set_aside or []}
    found = {}
    for row in rows:
        for scenario in row.get("scenarios") or []:
            if scenario["etat"] != "desactive":
                continue
            item = found.setdefault(scenario["id"], {
                "id": scenario["id"], "scenario": _scenario_title(scenario["id"]),
                "feature": scenario["id"].split("::", 1)[0].rsplit("/", 1)[-1], "fonctionnalites": [],
                "raison": scenario.get("cause") or "cause non renseignée",
                "par": "toi (mis de côté)" if scenario["id"] in mine else "l'agent"})
            if row["titre"] not in item["fonctionnalites"]:
                item["fonctionnalites"].append(row["titre"])
    for entry in set_aside or []:   # mis de côté mais rattaché à aucune fonctionnalité
        found.setdefault(entry["id"], {"id": entry["id"], "scenario": entry.get("titre", ""), "feature":
                                       entry.get("fichier", "").rsplit("/", 1)[-1], "fonctionnalites": [],
                                       "raison": entry.get("preuve", ""), "par": "toi (mis de côté)"})
    return sorted(found.values(), key=lambda i: i["id"])


def covered(rows: list) -> list:
    """Fonctionnalités couvertes : par leur propre test (avec l'état de ses scénarios) ou par une autre."""
    titles = _titles(rows)
    out = []
    for row in rows:
        group = cucumber_coverage.group_of(row)
        if group == "test":
            states = {}
            for scenario in row.get("scenarios") or []:
                states[scenario["etat"]] = states.get(scenario["etat"], 0) + 1
            detail = ", ".join(f"{n} {STATE_WORDS.get(k, k)}" for k, n in sorted(states.items()))
            out.append({"id": row["id"], "fonctionnalite": row["titre"], "module": row["module"],
                        "couverture": f"par son test ({detail})", "etat": row["etat"]})
        elif group == "indirect":
            by = titles.get(row["couvert_par"], row["couvert_par"])
            out.append({"id": row["id"], "fonctionnalite": row["titre"], "module": row["module"],
                        "couverture": f"par « {by} »" + (f" ({row['chaine_titre']})" if row.get("chaine_titre")
                                                         else ""), "etat": "indirect"})
    return out


def untested_reason(row: dict) -> str:
    """Pourquoi une fonctionnalité n'a pas (encore) de test, en une phrase."""
    generation = row.get("generation") or {}
    kind = generation.get("type", "")
    if row["etat"] == "partiel":
        return f"partielle : {row.get('cause') or 'le test existant ne vérifie pas tout'}"
    if row["etat"] == "a_confirmer":
        return f"réponse attendue : {row.get('cause') or 'information à confirmer'}"
    if row["etat"] == "en_attente":
        return "couverte par une autre fonctionnalité, en attente de la génération de son test"
    if kind and kind != "test_ecrit":
        message = generation.get("message", "")
        return KINDS.get(kind, kind) + (f" — {message}" if message else "") + (
            f" Que faire : {generation['action']}." if kind == "interrompue" and generation.get("action") else "")
    if row.get("relance_inutile"):
        return "la dernière génération n'a rien écrit et ses entrées n'ont pas changé : relance inutile"
    if row["etat"] in ("non_testable", "sans_objet"):
        return row.get("cause") or row.get("phrase", "")
    return "pas encore traitée par l'agent : à écrire (bouton « Écrire les tests »)"


def untested(rows: list) -> list:
    """Fonctionnalités restant à tester (à couvrir ou partielles), avec leur raison."""
    return [{"id": r["id"], "fonctionnalite": r["titre"], "module": r["module"], "raison": untested_reason(r)}
            for r in rows if cucumber_coverage.group_of(r) in ("a_couvrir", "partiel")]


def out_of_scope(rows: list) -> list:
    """Fonctionnalités non testables aujourd'hui ou sans objet, avec la raison."""
    return [{"id": r["id"], "fonctionnalite": r["titre"], "module": r["module"],
             "etat": "non testable" if r["etat"] == "non_testable" else "sans objet",
             "raison": r.get("cause") or r.get("phrase", "")}
            for r in rows if cucumber_coverage.group_of(r) in ("non_testable", "sans_objet")]
