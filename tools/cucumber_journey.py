"""
Parcours de l'onglet Cucumber en 7 étapes (Projet, Couverture, Sélection, Génération, Exécution, Correction,
Bilan), 0 token : état de chaque étape, résumé en une ligne et action suivante proposée, en libellés métier.
"""
import os
import re

from tools import cucumber_bilan, cucumber_checks, cucumber_coverage, cucumber_decisions, cucumber_import
from tools.cucumber_tools import (_read_json, completable_specifics, init_patch_path, patch_path, pending_lots,
                                  worktree_dir)

STEPS = (("projet", "Projet"), ("couverture", "Couverture"), ("selection", "Sélection"),
         ("generation", "Génération"), ("execution", "Exécution"), ("correction", "Correction"),
         ("bilan", "Bilan"))
# Clé : (pastille, signification)
STATUSES = {"fait": ("✅", "fait"), "a_faire": ("⏳", "à faire"), "attention": ("⚠️", "à regarder"),
            "vide": ("▫️", "rien pour l'instant")}
HEALTH_VERDICT = re.compile(r"^- Verdict : (.+)$", re.M)


def _step(key: str, status: str, summary: str, action: str = "") -> dict:
    return {"cle": key, "titre": dict(STEPS)[key], "etat": status, "resume": summary, "action": action}


def health_verdict(client_name: str) -> str:
    """Verdict du dernier bilan de santé (« sain », « 3 défaut(s) ») ; '' s'il n'a jamais été lancé."""
    path = cucumber_checks.health_path(client_name)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        match = HEALTH_VERDICT.search(f.read())
    return match.group(1).strip() if match else "lancé"


def project_step(client_name: str, inventory: dict, profile_doc: dict, state: dict) -> dict:
    """Profil client, bilan de santé, initialisation et dernier résultat connu de l'init."""
    health = health_verdict(client_name)
    init = state.get("init") or {}
    has_init = os.path.exists(init_patch_path(client_name))
    parts = ["profil " + ("détecté" if profile_doc else "absent"),
             "bilan de santé : " + (health or "non lancé"),
             "initialisation " + ("générée" if has_init else "non générée")]
    if init:
        parts.append("dernier lancement de l'init : " + ("réussi" if init.get("ok") else
                                                          f"en échec ({init.get('message', '')[:120]})"))
    action = ("Lancer l'inventaire des spécifiques" if not inventory.get("specifiques") else
              "Détecter le profil client" if not profile_doc else
              "Lancer le bilan de santé" if not health else
              "Générer l'initialisation du client" if not has_init else "")
    trouble = (health not in ("", "sain", "lancé")) or (init and not init.get("ok"))
    status = "a_faire" if action else "attention" if trouble else "fait"
    return _step("projet", status, " · ".join(parts), action)


def coverage_step(rows: list) -> dict:
    """Taux réel et écrit, partielles ; action : choisir les tâches à couvrir, sinon exécuter les tests écrits."""
    if not rows:
        return _step("couverture", "vide", "aucune tâche inventoriée", "Lancer l'inventaire (étape Projet)")
    count = cucumber_coverage.rates(rows)["par_etat"]
    to_cover = count["non_couvert"] + count["partiel"]
    summary = cucumber_coverage.summary_line(rows) + "".join(
        f", {count[key]} {label}" for key, label in (("echec", "en échec"), ("desactive", "désactivée(s)"),
                                                     ("non_execute", "jamais exécutée(s)"),
                                                     ("non_testable", "non testable(s) aujourd'hui")) if count[key])
    if to_cover:
        return _step("couverture", "a_faire", summary, f"Choisir parmi les {to_cover} tâche(s) non couvertes ou "
                                                       f"partielles")
    if count["echec"] or count["non_execute"] or count["desactive"]:
        return _step("couverture", "attention", summary, "Exécuter les tests écrits et importer le run (étape "
                                                         "Exécution)" if count["non_execute"] else "")
    return _step("couverture", "fait", summary)


def selection_step(selected: int, lots: int) -> dict:
    """Tâches cochées et nombre de lots qu'elles donneront."""
    if not selected:
        return _step("selection", "vide", "aucune tâche sélectionnée")
    return _step("selection", "a_faire", f"{selected} tâche(s) sélectionnée(s) → {lots} lot(s)",
                 "Générer les tests")


def generation_step(client_name: str, state: dict) -> dict:
    """Lots terminés, en attente, patch ; action : reprendre ou compléter."""
    lots = state.get("lots") or []
    if not lots:
        return _step("generation", "vide", "aucune génération")
    done = sum(1 for lot in lots if lot.get("statut") == "termine")
    pending = pending_lots(state)
    completable = completable_specifics(state)
    summary = f"{done}/{len(lots)} lot(s) terminé(s)" + (f", {len(pending)} en attente" if pending else "")
    summary += ", patch " + ("généré" if os.path.exists(patch_path(client_name)) else "non généré")
    stopped = sum(1 for lot in lots if lot.get("statut") == "decision_humaine")
    if stopped:
        summary += f", {stopped} lot(s) arrêté(s) : décision humaine attendue"
    action = ("Reprendre la génération" if pending else
              f"Compléter la génération ({len(completable)} tâche(s))" if completable else "")
    return _step("generation", "a_faire" if pending else "attention" if stopped else "fait",
                 summary, action)


def last_analysis(client_name: str) -> dict:
    """Analyse du dernier import ({} sans import)."""
    index = cucumber_import.load_index(client_name)
    if not index.get("imports"):
        return {}
    numero = max(e["numero"] for e in index["imports"])
    return _read_json(os.path.join(cucumber_import.imports_dir(client_name), str(numero), "analyse.json"), {}) or {}


def run_counts(analysis: dict) -> tuple:
    """(verts, échecs) d'une analyse d'import."""
    results = (analysis.get("resultats") or {}).values()
    green = sum(1 for r in results if r.get("statut") == "passe")
    return green, len(results) - green


def execution_step(client_name: str, analysis: dict) -> dict:
    """Dernier run importé ; action : importer les logs du run suivant le dernier patch."""
    patch = patch_path(client_name)
    if not analysis:
        return _step("execution", "a_faire" if os.path.exists(patch) else "vide", "aucun run importé",
                     "Lancer le job Jenkins puis importer sa console et son rapport" if os.path.exists(patch) else "")
    if analysis.get("non_analyse"):
        return _step("execution", "attention", f"dernier import (n° {analysis['numero']}) non analysé : "
                                               f"{analysis['non_analyse']}", "Importer le bon run")
    init = analysis.get("init_en_echec") or {}
    if init:
        return _step("execution", "attention", f"dernier run (import n° {analysis['numero']}) : initialisation en "
                                               f"échec, aucun scénario jugé ({init['message'][:100]})",
                     "Corriger l'initialisation du client puis relancer le run")
    green, failed = run_counts(analysis)
    comparison = analysis.get("comparaison") or {}
    slow = (analysis.get("durees") or {}).get("lents") or []
    summary = (f"dernier run : {green} vert(s) / {failed} échec(s) (import n° {analysis['numero']})"
               + (f", {len(comparison['regressions'])} régression(s)" if comparison.get("regressions") else "")
               + (f", {len(slow)} scénario(s) anormalement long(s)" if slow else ""))
    return _step("execution", "attention" if failed or slow else "fait", summary)


def correction_step(client_name: str, rows: list, analysis: dict, state: dict) -> dict:
    """
    Décisions en attente, lots de correction proposés au dernier import, causes à prouver, diagnostics en attente
    du prochain run, tâches en échec pour une cause « test ».
    """
    decisions = len(cucumber_decisions.pending(client_name)) if client_name else 0
    proposals = analysis.get("propositions") or []
    to_prove = analysis.get("a_prouver") or []
    diagnostics = sum(len(lot.get("diagnostics_en_attente") or []) for lot in state.get("lots") or [])
    failing = cucumber_coverage.failing_test_causes(rows)
    parts = [f"{decisions} décision(s) en attente"] if decisions else []
    parts += [f"{len(proposals)} lot(s) de correction proposé(s)"] if proposals else []
    parts += [f"{len(to_prove)} cause(s) à prouver"] if to_prove else []
    parts += [f"{diagnostics} diagnostic(s) en attente du prochain run"] if diagnostics else []
    parts += [f"{failing} tâche(s) en échec pour une cause « test »"] if failing else []
    if not parts:
        return _step("correction", "vide", "aucune correction en attente")
    action = ("Trancher les décisions en attente" if decisions else "Lancer la correction ciblée" if proposals else
              "Apporter une preuve pour les causes non résolues" if to_prove else
              "Lancer le run suivant puis l'importer" if diagnostics else "Importer le dernier run pour le classer")
    return _step("correction", "attention" if to_prove and not (decisions or proposals) else "a_faire",
                 ", ".join(parts), action)


def review_counts(state: dict) -> dict:
    """Défauts détectés, scénarios désactivés et faibles, lots arrêtés (mêmes sources qu'A_REVOIR.md)."""
    lots = state.get("lots") or []
    disabled = [e for lot in lots for e in lot.get("a_revoir") or []]
    return {"defauts": sum(1 for e in disabled if e.get("cause") == "bug_applicatif")
            + sum(1 for lot in lots for r in lot.get("resultats") or [] for c in r.get("constats") or []
                  if c.get("type") == "ecart_spec_code"),
            "desactives": len(disabled), "faibles": sum(len(lot.get("faibles") or []) for lot in lots),
            "arrets": sum(1 for lot in lots if lot.get("statut") == "decision_humaine")}


def review_step(client_name: str, state: dict) -> dict:
    """
    Bilan du dernier run importé (prochaine action), à défaut A_REVOIR.md : défauts détectés dans le code
    spécifique, scénarios désactivés, arrêts pour décision humaine.
    """
    bilan = cucumber_bilan.latest_bilan(client_name)
    if bilan:
        summary = (f"run n° {bilan['import']} : {len(bilan['defauts'])} défaut(s) détecté(s), "
                   f"{len(bilan['corriges'])} corrigé(s), {len(bilan['desactives'])} désactivé(s), "
                   f"{len(bilan['doutes'])} doute(s), {len(bilan['manuelles'])} correction(s) manuelle(s)")
        done = bilan["prochaine_action"].startswith("Livrer")
        return _step("bilan", "fait" if done else "attention" if bilan["defauts"] or bilan["manuelles"]
                     else "a_faire", summary, bilan["prochaine_action"])
    if not os.path.exists(os.path.join(worktree_dir(client_name), "A_REVOIR.md")):
        return _step("bilan", "vide", "aucun bilan")
    counts = review_counts(state)
    summary = (f"{counts['defauts']} défaut(s) détecté(s), {counts['desactives']} scénario(s) désactivé(s), "
               f"{counts['faibles']} faible(s)" + (f", {counts['arrets']} lot(s) arrêté(s) pour décision humaine"
                                                   if counts["arrets"] else ""))
    return _step("bilan", "attention" if counts["defauts"] or counts["arrets"] else "fait", summary,
                 "Lire les défauts détectés" if counts["defauts"] else "")


def journey(client_name: str, inventory: dict, profile_doc: dict, state: dict, rows: list,
            selected: int = 0, lots: int = 0) -> list:
    """Les 7 étapes du parcours, dans l'ordre."""
    analysis = last_analysis(client_name) if client_name else {}
    return [project_step(client_name, inventory, profile_doc, state), coverage_step(rows),
            selection_step(selected, lots), generation_step(client_name, state),
            execution_step(client_name, analysis), correction_step(client_name, rows, analysis, state),
            review_step(client_name, state)]


def next_step(steps: list) -> str:
    """Clé de la première étape à faire (à défaut, à regarder), sinon la couverture."""
    for wanted in ("a_faire", "attention"):
        found = next((s["cle"] for s in steps if s["etat"] == wanted and s["cle"] != "selection"), "")
        if found:
            return found
    return "couverture"


def step_line(step: dict) -> str:
    """« ✅ Projet — résumé · action suivante » pour la vue d'ensemble."""
    badge = STATUSES[step["etat"]][0]
    return f"{badge} **{step['titre']}** — {step['resume']}" + (f" · :blue[{step['action']}]" if step["action"] else "")
