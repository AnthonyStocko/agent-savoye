"""
Résultat obligatoire de chaque génération d'une tâche Cucumber (0 token).

Chaque génération d'une tâche se termine par UN résultat enregistré dans etat.json (state["generations"][id]) et
affiché sur sa carte :
- test_ecrit : scénarios, features et fichiers écrits, lot et commit ;
- couverte_par : pas de test, la tâche est couverte par une autre (lien vers elle) ;
- sans_logique : pas de test, aucune logique propre (déclaration d'événement, de variables…), avec sa preuve ;
- blocage : pas de test, blocage prouvé (cause et preuve) ;
- a_confirmer : pas de test, informations à confirmer (résumé de l'agent) ;
- interrompue : génération interrompue (cause technique en une phrase et action à faire) ; l'état de la tâche ne
  change pas, ses résultats de lot précédents sont gardés.
Le diff et le patch d'une tâche se lisent dans le worktree (task_files, task_diff).
"""
import os
from datetime import datetime

from tools import cucumber_testability
from tools.cucumber_tools import TECHNICAL_REASON, _git, load_state, save_state

KINDS = {"test_ecrit": "Test écrit", "couverte_par": "Pas de test : couverte par une autre fonctionnalité",
         "sans_logique": "Pas de test : sans logique propre", "blocage": "Pas de test : blocage prouvé",
         "a_confirmer": "Pas de test : réponse attendue", "interrompue": "Interrompu"}
ICONS = {"test_ecrit": "✍", "couverte_par": "🔗", "sans_logique": "—", "blocage": "⛔", "a_confirmer": "🟡",
         "interrompue": "⛔"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def classify(result: dict, lot: dict) -> dict:
    """Résultat d'une génération à partir du résultat de lot d'une tâche : {type, message, lot, …}."""
    base = {"lot": lot.get("numero"), "date": lot.get("fin") or _now()}
    scenarios = [s for s in result.get("scenarios") or [] if isinstance(s, dict)]
    declared = result.get("statut_declare") or result.get("statut")
    if result.get("tri") and result.get("couvert_par"):
        # Élément Studio sans logique propre (analyse du XML) : pas de lot Claude, couvert par le process qui l'utilise
        return {**base, "type": "sans_logique", "couvert_par": result["couvert_par"],
                "message": cucumber_testability.summary(result.get("raison") or "", 1)}
    if result.get("statut") == "couvert" and scenarios:
        features = sorted({s.get("fichier", "") for s in scenarios if s.get("fichier")})
        files = list(dict.fromkeys(features + list(result.get("fichiers") or []) + list(result.get("classes_java") or [])))
        return {**base, "type": "test_ecrit", "scenarios": len(scenarios), "features": features, "fichiers": files,
                "commit": bool(lot.get("commit")),
                "message": f"{len(scenarios)} scénario(s) dans {len(features)} feature(s), {len(files)} fichier(s)."}
    if declared == "couvert_indirectement" and result.get("couvert_par"):
        return {**base, "type": "couverte_par", "couvert_par": result["couvert_par"],
                "message": cucumber_testability.summary(result.get("resume") or result.get("raison") or "", 1)}
    if result.get("tri") or result.get("raison") == TECHNICAL_REASON or (result.get("sans_objet") or {}).get("preuve"):
        return {**base, "type": "sans_logique", "message": (result.get("sans_objet") or {}).get("preuve")
                or cucumber_testability.summary(result.get("raison") or "", 1)}
    evaluation = cucumber_testability.assess(result, {}, {})
    if evaluation["etat"] == "non_testable":
        return {**base, "type": "blocage", "message": cucumber_testability.blocker_line(evaluation)}
    return {**base, "type": "a_confirmer",
            "message": result.get("resume") or cucumber_testability.summary(result.get("raison") or "", 2)}


def record_lot(state: dict, lot: dict) -> list:
    """Enregistre le résultat de chaque tâche d'un lot terminé ; renvoie les identifiants enregistrés."""
    done = []
    for result in lot.get("resultats") or []:
        if result.get("id"):
            state.setdefault("generations", {})[result["id"]] = classify(result, lot)
            done.append(result["id"])
    return done


def record_interruption(client_name: str, ids: list, cause: str, action: str = "", since: str = "") -> list:
    """
    Génération interrompue : résultat « interrompue » (cause en une phrase, action) pour chaque tâche de `ids` qui
    n'a pas de résultat plus récent que `since` ; ses résultats de lot et donc son état ne changent pas. Renvoie
    les identifiants enregistrés.
    """
    state = load_state(client_name)
    generations = state.setdefault("generations", {})
    recorded = []
    for spec_id in ids:
        previous = generations.get(spec_id) or {}
        if since and previous.get("date", "") >= since and previous.get("type") != "interrompue":
            continue
        # Une cause qui donne déjà son action (« … Action : … ») n'en reçoit pas une seconde, générique
        generations[spec_id] = {"type": "interrompue", "date": _now(), "message": cause.strip(),
                                "action": "" if "Action :" in cause else action.strip(), "precedent": previous if previous.get("type") !=
                                "interrompue" else previous.get("precedent")}
        recorded.append(spec_id)
    if recorded:
        save_state(client_name, state)
    return recorded


def record_preparation(client_name: str, ok: bool, message: str, files: list = (), patch: str = "",
                       remaining: list = ()) -> dict:
    """
    Résultat de la préparation des données de test (lot d'initialisation) : réussite, phrase, fichiers du patch,
    chemin du patch et points restants (non faits, à traiter à la main). Remplace le précédent.
    """
    state = load_state(client_name)
    state["preparation"] = {"date": _now(), "ok": ok, "message": message.strip(), "fichiers": list(files),
                            "patch": patch, "restantes": [str(r).strip() for r in remaining if str(r).strip()][:30]}
    save_state(client_name, state)
    share_preparation(client_name, state["preparation"])
    return state["preparation"]


def share_preparation(client_name: str, preparation: dict) -> dict:
    """Résultat de la préparation des données de test déposé dans les connaissances partagées (jamais bloquant)."""
    from tools import knowledge_tools
    if not knowledge_tools.enabled():
        return {}
    patch = preparation.get("patch") or ""
    body = (f"# Préparation des données de test de {client_name}\n\n"
            f"- Résultat : {'réussie' if preparation.get('ok') else 'en échec'} — {preparation.get('message', '')}\n"
            + "".join(f"- Fichier : {f}\n" for f in preparation.get("fichiers") or [])
            + ("\n## Points non faits, à traiter à la main\n\n" + "".join(
                f"- {r}\n" for r in preparation["restantes"]) if preparation.get("restantes") else "")
            + (f"\n## Patch\n\n```diff\n{open(patch, encoding='utf-8', errors='replace').read()[:20000]}\n```\n"
               if patch and os.path.exists(patch) else ""))
    result = knowledge_tools.publish_knowledge(body, client_name.upper(), "cucumber-initialisation",
                                               f"Préparation des données de test {client_name}", client=client_name,
                                               tags=["cucumber"], resume=preparation.get("message", ""))
    print(knowledge_tools.publish_line(result))
    return result


def missing_results(state: dict, ids: list, since: str) -> list:
    """Tâches demandées sans résultat enregistré depuis `since` : aucune génération ne finit sans résultat."""
    generations = state.get("generations") or {}
    return [i for i in ids if (generations.get(i) or {}).get("date", "") < since]


def task_files(state: dict, spec_id: str) -> tuple:
    """(fichiers de la tâche, lot) d'après son dernier résultat « test écrit » ; à défaut ceux de son lot."""
    generation = (state.get("generations") or {}).get(spec_id) or {}
    if generation.get("type") == "interrompue":
        generation = generation.get("precedent") or {}
    lot = next((l for l in state.get("lots") or [] if l.get("numero") == generation.get("lot")), {})
    return list(generation.get("fichiers") or []), lot


def modifications(state: dict, spec_id: str, worktree: str) -> dict:
    """
    Modifications d'une fonctionnalité (dernier « test écrit ») : {"diff", "fichiers", "commits", "partage"} ; si les
    fichiers de la fonctionnalité ne donnent rien, ceux écrits en même temps qu'elle (« partage » vrai).
    """
    base = state.get("base", "")
    files, lot = task_files(state, spec_id)
    diff, shared = task_diff(worktree, base, files), False
    if not diff and lot.get("fichiers"):
        files, shared = list(lot["fichiers"]), True
        diff = task_diff(worktree, base, files)
    return {"diff": diff, "fichiers": files, "commits": task_commits(worktree, base, files), "partage": shared}


def task_diff(worktree: str, base: str, files: list) -> str:
    """Diff lisible des fichiers de la tâche entre la base et le worktree ('' sans fichier ni écart)."""
    if not files or not os.path.isdir(worktree):
        return ""
    return _git(worktree, "diff", "--binary", f"{base}..HEAD", "--", *files).stdout


def task_commits(worktree: str, base: str, files: list) -> list:
    """Commits du worktree qui modifient les fichiers de la tâche : [« abc1234 message »]."""
    if not files or not os.path.isdir(worktree):
        return []
    return _git(worktree, "log", "--format=%h %s", f"{base}..HEAD", "--", *files).stdout.splitlines()
