"""RÈGLE 3 : un scénario n'est jamais supprimé ; corrections contrôlées, désactivation prouvée, seuils, A_REVOIR.md."""
import importlib
import io
import os
import sys

import pytest

from tests.conftest import CLIENT, CLIENT_TAG, git, write
from tools import cucumber_a_revoir as ar
from tools import cucumber_verify as cv

RES = "src/test/resources/cucumber"
FEATURE = (f"@WMS {CLIENT_TAG}\n"
           "Feature: Stock\n\n"
           "  Background:\n    Given STOCK- contexte\n\n"
           "  @agent_clientx_1 @agent_clientx_1_1\n"
           "  Scenario: Statut du contenant\n"
           "    When STOCK- je crée le contenant \"C1\"\n"
           "    Then STOCK- le contenant a les valeurs\n"
           "      | code | statut |\n"
           "      | C1   | LIBRE  |\n\n"
           "  @agent_clientx_1 @agent_clientx_1_2\n"
           "  Scenario: Motif de blocage\n"
           "    When STOCK- je bloque \"C1\"\n"
           "    Then STOCK- le motif est \"DECANTING\"\n")
REL = f"{RES}/features/{CLIENT}/Stock.feature"


@pytest.fixture
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


def _result(status, motif="", spec_id="sp-1", cause=""):
    return {"id": f"{REL}::{spec_id}", "statut": status, "motif": motif, "cause": cause}


# --- RÈGLE 3a / 3c : corrections contrôlées --------------------------------------------------------------------

def test_forbidden_changes():
    assert ar.forbidden_changes(FEATURE, FEATURE, REL) == []
    deleted = FEATURE.split("  @agent_clientx_1 @agent_clientx_1_2")[0]
    assert "scénario supprimé" in ar.forbidden_changes(FEATURE, deleted, REL)[0]
    no_then = FEATURE.replace('    Then STOCK- le motif est "DECANTING"\n', "")
    assert "assertion(s) (Then) retirée(s)" in ar.forbidden_changes(FEATURE, no_then, REL)[0]
    changed = FEATURE.replace('"DECANTING"', '"AUTRE"')
    assert "valeur attendue modifiée sans justification" in ar.forbidden_changes(FEATURE, changed, REL)[0]
    realigned = changed.replace("  @agent_clientx_1 @agent_clientx_1_2",
                                "  # Réaligné sur le code : app/Init.java\n  @agent_clientx_1 @agent_clientx_1_2")
    assert ar.forbidden_changes(FEATURE, realigned, REL) == []
    # Renforcer (colonne ajoutée) est permis ; retirer une valeur ne l'est pas
    stronger = FEATURE.replace("| code | statut |", "| code | statut | site |").replace(
        "| C1   | LIBRE  |", "| C1   | LIBRE  | S1   |")
    assert ar.forbidden_changes(FEATURE, stronger, REL) == []
    weaker = FEATURE.replace("| code | statut |", "| code |").replace("| C1   | LIBRE  |", "| C1   |")
    assert ar.forbidden_changes(FEATURE, weaker, REL)
    ignored = FEATURE.replace("@agent_clientx_1 @agent_clientx_1_2", "@agent_clientx_1 @agent_clientx_1_2 @ignore")
    assert "@ignore" in ar.forbidden_changes(FEATURE, ignored, REL)[0]
    # Seul un doublon autorisé peut disparaître
    assert ar.forbidden_changes(FEATURE, deleted, REL, {cv._title_key("Motif de blocage")}) == []


def test_check_corrections_restores_the_feature(tmp_path):
    worktree = str(tmp_path)
    write(os.path.join(worktree, REL), FEATURE)
    before = ar.snapshot(worktree, [REL, "A.java"])
    write(os.path.join(worktree, REL), FEATURE.split("  @agent_clientx_1 @agent_clientx_1_2")[0])
    errors = ar.check_corrections(worktree, before)
    assert errors and "correction annulée" in errors[0]
    with open(os.path.join(worktree, REL), encoding="utf-8") as f:
        assert f.read() == FEATURE


def test_review_may_only_remove_near_exact_duplicates():
    assert cv.exact_duplicate("doublon de a.feature / T (similarité 0.97)")
    assert not cv.exact_duplicate("doublon de a.feature / T (similarité 0.86)")
    assert not cv.exact_duplicate("aucune assertion (pas de Then)")


# --- RÈGLE 3b / 3d : décision et seuils ------------------------------------------------------------------------

def test_out_of_test_cause_with_proof_is_disabled():
    scenarios = {f"@t{i}": _result("pertinent", spec_id=f"sp-{i}") for i in range(9)}
    scenarios["@t9"] = _result("environnement", "non vérifié — environnement : No route found", "sp-9b")
    spec_of = {v["id"]: v["id"].rsplit("::", 1)[1] for v in scenarios.values()}
    spec_of[scenarios["@t9"]["id"]] = "sp-0"  # le spécifique garde un scénario actif
    decision = ar.decide(scenarios, spec_of, max_pct=30)
    assert decision["desactiver"] == {"@t9": "environnement"} and not decision["arret"]
    # Sans preuve (message vide) : jamais désactivé
    scenarios["@t9"]["motif"] = ""
    assert ar.decide(scenarios, spec_of, max_pct=30)["desactiver"] == {}


def test_lot_that_would_disable_too_much_stops_instead_of_emptying():
    scenarios = {f"@t{i}": _result("pertinent", spec_id="sp-a") for i in range(6)}
    for i in range(6, 10):
        scenarios[f"@t{i}"] = _result("environnement", "non vérifié — environnement : 503", "sp-a")
    spec_of = {v["id"]: "sp-a" for v in scenarios.values()}
    decision = ar.decide(scenarios, spec_of, max_pct=30)
    assert decision["arret"] and decision["desactiver"] == {} and "40 %" in decision["motifs"][0]
    assert not ar.decide(scenarios, spec_of, max_pct=50)["arret"]


def test_spec_losing_all_active_scenarios_stops():
    scenarios = {"@t1": _result("pertinent", spec_id="sp-a"),
                 "@t2": _result("bug_applicatif", "bug applicatif probable : 500", "sp-b")}
    spec_of = {scenarios["@t1"]["id"]: "sp-a", scenarios["@t2"]["id"]: "sp-b"}
    decision = ar.decide(scenarios, spec_of, max_pct=100)
    assert decision["arret"] and "sp-b" in decision["motifs"][0] and decision["desactiver"] == {}


def test_test_cause_is_never_disabled():
    scenarios = {"@t1": _result("invalide", "échoue sur le client : colonne inconnue"),
                 "@t2": _result("faible", "passe sur le Standard"),
                 "@t3": _result("cascade", "mise en place en échec", cause="technique")}
    for t in ("@t4", "@t5", "@t6", "@t7"):
        scenarios[t] = _result("pertinent")
    decision = ar.decide(scenarios, {}, max_pct=100)
    assert decision["desactiver"] == {} and decision["bloquants"] == ["@t1"] and decision["faibles"] == ["@t2"]
    assert decision["arret"] and "cause côté test" in decision["motifs"][0]
    # Version, donnée absente après correction, cascade hors test : causes prouvées hors test
    assert ar.cause_of(_result("environnement", f"{cv.VERSION_GAP} : Invalid path")) == "version"
    assert ar.cause_of(_result("cascade", "x", cause="donnee_reference")) == "donnee_reference"
    assert ar.cause_of(_result("cascade", "x", cause="assertion")) == ""


# --- RÈGLE 3f / 3g : désactivation sans suppression, nettoyage ---------------------------------------------------

def test_deactivate_keeps_scenario_and_moves_client_tag(tmp_path, monkeypatch):
    monkeypatch.delenv("CUCUMBER_TAG_A_REVOIR", raising=False)
    worktree = str(tmp_path)
    write(os.path.join(worktree, REL), FEATURE)
    item = _result("environnement", "non vérifié — environnement : No route found for GET /x")
    done = ar.deactivate(worktree, REL, {"Motif de blocage": ("environnement", item)}, CLIENT_TAG)
    with open(os.path.join(worktree, REL), encoding="utf-8") as f:
        text = f.read()
    assert done == ["Motif de blocage"]
    scenarios = {s["titre"]: s for s in cv.parse_scenarios(text)}
    assert set(scenarios) == {"Statut du contenant", "Motif de blocage"}  # rien n'est supprimé
    assert "@A_REVOIR" in scenarios["Motif de blocage"]["tags"]
    assert CLIENT_TAG not in scenarios["Motif de blocage"]["tags"]
    assert CLIENT_TAG in scenarios["Statut du contenant"]["tags"]
    assert text.startswith("@WMS\nFeature: Stock")
    assert "  # À revoir : environnement — cause PROUVÉE — non vérifié — environnement : No route found" in text
    assert "réactivation : réactiver quand l'environnement" in text
    assert 'Then STOCK- le motif est "DECANTING"' in text
    # Tag générique configurable
    monkeypatch.setenv("CUCUMBER_TAG_A_REVOIR", "DESACTIVE")
    assert ar.a_revoir_tag() == "@DESACTIVE"


def test_apply_decision_does_nothing_when_stopped(tmp_path):
    worktree = str(tmp_path)
    write(os.path.join(worktree, REL), FEATURE)
    scenarios = {"@agent_clientx_1_2": _result("environnement", "non vérifié — environnement : 503")}
    located = cv.tagged_scenarios(worktree, [REL], scenarios)
    stopped = {"desactiver": {}, "arret": True}
    assert ar.apply_decision(worktree, [REL], scenarios, stopped, CLIENT_TAG, located) == []
    with open(os.path.join(worktree, REL), encoding="utf-8") as f:
        assert f.read() == FEATURE
    entries = ar.apply_decision(worktree, [REL], scenarios, {"desactiver": {"@agent_clientx_1_2": "environnement"}},
                                CLIENT_TAG, located)
    assert entries[0]["titre"] == "Motif de blocage" and entries[0]["reactivation"]


def test_check_deletions_protects_used_files(repo):
    data = f"{RES}/files/{CLIENT}/C1.xml"
    steps = "src/test/java/client/StockSteps.java"
    write(os.path.join(repo, REL), FEATURE + '    And INIT- Intégration des fichiers via l\'écran\n'
                                             "      | fileName |\n      | C1.xml   |\n")
    write(os.path.join(repo, data), "<a/>")
    write(os.path.join(repo, steps), 'class StockSteps {\n  @Then("STOCK- le motif est {string}")\n'
                                     '  public void motif(String m) {}\n}\n')
    write(os.path.join(repo, "src/test/java/client/Unused.java"), 'class Unused {\n  @Then("AUTRE- x")\n'
                                                                    '  public void x() {}\n}\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    for rel in (data, steps, "src/test/java/client/Unused.java"):
        os.remove(os.path.join(repo, *rel.split("/")))
    errors = ar.check_deletions(repo, "HEAD")
    assert len(errors) == 2 and any(data in e for e in errors) and any(steps in e for e in errors)


# --- A_REVOIR.md --------------------------------------------------------------------------------------------------

def test_a_revoir_md_keeps_human_content_and_lists_defects_first(tmp_path):
    worktree = str(tmp_path)
    write(os.path.join(worktree, ar.A_REVOIR_FILE), "# Notes de l'équipe\n\nÀ garder.\n")
    state = {"lots": [{"numero": 1, "a_revoir": [
        {"fichier": REL, "titre": "Motif de blocage", "cause": "bug_applicatif", "preuve": "500 Internal Server Error",
         "reactivation": ar.CAUSES["bug_applicatif"][1]},
        {"fichier": REL, "titre": "Statut", "cause": "version", "preuve": "Invalid path",
         "reactivation": ar.CAUSES["version"][1]}],
        "faibles": [{"fichier": REL, "titre": "Faible", "motif": "passe sur le Standard"}],
        "resultats": [{"id": "sp-1", "constats": [{"type": "ecart_spec_code", "detail": "Jira 10, code 12",
                                                   "fichier": ""}]}]},
        {"numero": 2, "statut": "decision_humaine", "arret": ["2 désactivation(s) sur 3"]}]}
    assert ar.write_a_revoir(worktree, state, {"sp-1": {"libelle": "Stock"}}) == ar.A_REVOIR_FILE
    ar.write_a_revoir(worktree, state, {"sp-1": {"libelle": "Stock"}})  # régénéré, pas dupliqué
    with open(os.path.join(worktree, ar.A_REVOIR_FILE), encoding="utf-8") as f:
        text = f.read()
    assert text.startswith("# Notes de l'équipe") and text.count(ar.BEGIN) == 1
    assert text.index("## Défauts détectés : 2") < text.index("## Scénarios désactivés : 2")
    assert "écart entre spécification et code : Jira 10, code 12" in text
    assert "Condition de réactivation" in text and "## Scénarios faibles, restés actifs : 1" in text
    assert "Lot 2 : 2 désactivation(s) sur 3" in text
    assert ar.write_a_revoir(str(tmp_path / "vide"), {"lots": [{"numero": 1}]}, {}) == ""


# --- Branchement dans main : arrêt au lieu de vider le lot ---------------------------------------------------------

def test_verify_execution_lot_stops_and_keeps_scenarios(repo, main_module, monkeypatch):
    write(os.path.join(repo, REL), FEATURE)
    failing = {"@agent_clientx_1_1": {"id": f"{REL}::Statut du contenant", "statut": "environnement",
                                      "motif": "non vérifié — environnement : 503", "niveaux": {}},
               "@agent_clientx_1_2": {"id": f"{REL}::Motif de blocage", "statut": "invalide",
                                      "motif": "échoue sur le client : colonne", "niveaux": {}}}
    monkeypatch.setattr(main_module.cucumber_verify, "verify_lot",
                        lambda *a, **k: {"scenarios": dict(failing), "builds": []})
    monkeypatch.setenv("CUCUMBER_VERIFY_FIX_ATTEMPTS", "0")
    lot = {"numero": 1, "tags": {t: v["id"] for t, v in failing.items()}, "indicateurs": {"scenarios": []}}
    main_module.verify_execution_lot(lot, [], "1/1", repo, "HEAD", [REL], [], "local", "", CLIENT, "", "", "", {},
                                     "sonnet", "K", "/run", lambda u: None,
                                     context={"conventions": {"client_tag": CLIENT_TAG}})
    assert lot["arret"] and lot["a_revoir"] == []
    with open(os.path.join(repo, REL), encoding="utf-8") as f:
        assert f.read() == FEATURE
