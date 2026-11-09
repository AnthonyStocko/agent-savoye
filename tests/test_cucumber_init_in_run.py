"""
Init du client rejouée dans chaque run de vérification (environnement remonté à chaque build) : expression de tags,
ordre d'exécution du runner, lot non jugé sur une init en échec, durées séparées ; « non testable : dépend du module
exclu ».
"""
import os

from tests.conftest import write
from tools import cucumber_run, cucumber_testability
from tools import cucumber_verify as cv
from tools.cucumber_import import console_results

ROOT = "src/test/resources/cucumber/features/canson/"
INIT_REL = ROOT + "01-Contexte/WMS/01-Initialisation.feature"
LOT_REL = ROOT + "WMS/03-Preparation/SPEC-APPAIRAGE-BL.feature"
INIT = """@INIT_CANSON_WMS @CANSON @WMS
Feature: Canson - 01 - Initialisation des données

  Scenario:  Intégration des données
    Given INIT- Purge de l'environnement par le clear "wms/all"

  Scenario: Paramétrage
    Given KIS- Activation du toggle "X"
"""
LOT = """@CANSON @WMS
Feature: Appairage

  @agent_canson_1 @agent_canson_1_1
  Scenario: Appairage validé
    When je valide "BL-1"
    Then le BL "BL-1" est appairé

  @agent_canson_1 @agent_canson_1_2
  Scenario: Appairage modifié
    When je modifie "BL-1"
    Then le BL "BL-1" est modifié
"""
SCENARIOS = {"@agent_canson_1_1": f"{LOT_REL}::Appairage validé", "@agent_canson_1_2": f"{LOT_REL}::Appairage modifié"}
PASSE = {"statut": "passe", "step": "", "phase": "", "message": ""}


def _worktree(repo, lot_rel=LOT_REL):
    write(os.path.join(repo, *INIT_REL.split("/")), INIT)
    write(os.path.join(repo, *lot_rel.split("/")), LOT)
    return repo


def _results(init_status="passe", init_first=True, lot_rel=LOT_REL):
    init = {(INIT_REL, "Intégration des données"): {**PASSE, "statut": init_status, "duree": 100.0,
                                                    **({"step": "Given INIT- Purge", "message": "404"}
                                                       if init_status != "passe" else {})},
            (INIT_REL, "Paramétrage"): {**PASSE, "duree": 20.5}}
    lot = {(lot_rel, "Appairage validé"): {**PASSE, "duree": 7.0}, (lot_rel, "Appairage modifié"): {**PASSE, "duree": 3.0}}
    return {**init, **lot} if init_first else {**lot, **init}


def test_with_init_joins_init_and_lot_tags():
    assert cv.with_init("@agent_canson_1_1 or @agent_canson_1_2", "@INIT_CANSON_WMS") == \
        "@INIT_CANSON_WMS or @agent_canson_1_1 or @agent_canson_1_2"
    assert cv.with_init("@a and not @b", "@INIT_A or @INIT_B") == "@INIT_A or @INIT_B or (@a and not @b)"
    assert cv.with_init("@a", "") == "@a"


def test_excluded_module_negations_apply_to_the_whole_expression():
    from tools.cucumber_modules import run_expression
    profile = {"modules_exclus": ["TMS"], "init_tags": "@INIT_CANSON_WMS or @INIT_CANSON_TMS"}
    expression = run_expression(cv.with_init("@agent_canson_1_1", profile["init_tags"]), profile, "canson")
    assert expression == "(@INIT_CANSON_WMS or @agent_canson_1_1) and not @TMS and not @INIT_CANSON_TMS"


def test_runner_order_init_first_by_path():
    units = {INIT_REL: cv.scenario_units(INIT), LOT_REL: cv.scenario_units(LOT)}
    init = cv.init_scenarios(units, "@INIT_CANSON_WMS")
    assert init["fichiers"] == {INIT_REL} and len(init["cles"]) == 2
    assert cv.init_order_error(units, init, "@agent_canson_1_1 or @agent_canson_1_2") == ""
    # Feature du lot rangée avant l'init (« 00- » < « 01- ») : le runner l'exécuterait d'abord
    early = ROOT + "00-Avant/SPEC.feature"
    units[early] = cv.scenario_units(LOT)
    error = cv.init_order_error(units, init, "@agent_canson_1_1")
    assert error.startswith(early) and "avant la fin de l'init" in error


def test_init_outcome_durations_and_failures():
    init = cv.init_scenarios({INIT_REL: cv.scenario_units(INIT)}, "@INIT_CANSON_WMS")
    ok = cv.init_outcome(_results(), init, "@INIT_CANSON_WMS")
    assert ok["ok"] and ok["scenarios_init"] == 2 and ok["duree_init"] == 120.5 and ok["duree_tests"] == 10.0
    assert cv.init_line(ok) == "init @INIT_CANSON_WMS ✅ 2 min 00 s ; tests 10 s"
    failed = cv.init_outcome(_results("echoue"), init, "@INIT_CANSON_WMS")
    assert not failed["ok"] and failed["message"].startswith("environnement non initialisé (@INIT_CANSON_WMS)")
    late = cv.init_outcome(_results(init_first=False), init, "@INIT_CANSON_WMS")
    assert not late["ok"] and "avant la fin de l'init" in late["message"]
    missing = cv.init_outcome({k: v for k, v in _results().items() if k[0] == LOT_REL}, init, "@INIT_CANSON_WMS")
    assert not missing["ok"] and "aucun scénario de l'init" in missing["message"]


def _fake_runs(monkeypatch, init_status="passe"):
    calls = []

    def run_tags(mode, worktree, base, build_fn, tags, env, overrides, label, sources=None):
        calls.append(tags)
        results = _results(init_status)
        if label.endswith("mutation"):
            for key in [k for k in results if k[0] == LOT_REL]:
                results[key] = {"statut": "echoue", "step": "Then le BL", "phase": "then", "message": "_MUT",
                                "nature": "assertion", "exception": ""}
        return {"ok": True, "message": "build UNSTABLE", "build": f"http://jenkins/{len(calls)}", "resultats": results}
    monkeypatch.setattr(cv, "run_tags", run_tags)
    monkeypatch.delenv("CUCUMBER_VERIFY_ENV_STD", raising=False)
    monkeypatch.delenv("CUCUMBER_MERGED_MUTATION", raising=False)
    return calls


def test_every_verification_run_contains_the_init(monkeypatch, repo):
    calls = _fake_runs(monkeypatch)
    result = cv.verify_lot("branche", _worktree(repo), "HEAD", None, [LOT_REL], SCENARIOS, {}, "lot1",
                           init_tags="@INIT_CANSON_WMS")
    assert calls and all(c.startswith("@INIT_CANSON_WMS or ") for c in calls)
    assert {s["statut"] for s in result["scenarios"].values()} == {"pertinent"}
    assert result["init"]["ok"] and result["init"]["duree_init"] == 120.5 and result["init"]["duree_tests"] == 10.0
    assert all("init" in b for b in result["builds"])


def test_lot_not_judged_when_init_fails_in_the_same_build(monkeypatch, repo):
    calls = _fake_runs(monkeypatch, "echoue")
    result = cv.verify_lot("branche", _worktree(repo), "HEAD", None, [LOT_REL], SCENARIOS, {}, "lot1",
                           init_tags="@INIT_CANSON_WMS")
    assert len(calls) == 1                                           # ni mutation ni contre-épreuve
    for item in result["scenarios"].values():
        assert item["statut"] == "environnement" and item["motif"].startswith("environnement non initialisé")
        assert item["build"] == "http://jenkins/1"
    assert not result["init"]["ok"]


def test_lot_not_launched_when_runner_would_run_it_before_init(monkeypatch, repo):
    calls = _fake_runs(monkeypatch)
    early = ROOT + "00-Avant/SPEC-APPAIRAGE-BL.feature"
    result = cv.verify_lot("branche", _worktree(repo, early), "HEAD", None, [early],
                           {t: v.replace(LOT_REL, early) for t, v in SCENARIOS.items()}, {}, "lot1",
                           init_tags="@INIT_CANSON_WMS")
    assert calls == []
    assert {s["statut"] for s in result["scenarios"].values()} == {"environnement"}
    assert "avant la fin de l'init" in result["init"]["message"]


def test_without_init_tags_runs_are_unchanged(monkeypatch, repo):
    calls = _fake_runs(monkeypatch)
    cv.verify_lot("branche", _worktree(repo), "HEAD", None, [LOT_REL], SCENARIOS, {}, "lot1")
    assert calls[0] == "@agent_canson_1_1 or @agent_canson_1_2"


def test_console_results_give_start_and_duration():
    console = "\n".join([
        f"[2026-11-06T20:50:00.000Z] Scenario:  Intégration des données    # {INIT_REL}:4",
        f"[2026-11-06T20:54:00.000Z] Scenario: Appairage validé    # {LOT_REL}:5",
        "[2026-11-06T20:54:30.500Z] [ERROR] Tests run: 2, Failures: 0, Errors: 0, Skipped: 0"])
    results = console_results(console)
    assert results[(INIT_REL, "Intégration des données")]["duree"] == 240.0
    assert results[(LOT_REL, "Appairage validé")]["duree"] == 30.5
    assert list(results)[0] == (INIT_REL, "Intégration des données")


def test_report_durations_in_nanoseconds():
    report = [{"uri": "file:" + INIT_REL, "elements": [{
        "type": "scenario", "name": "Paramétrage", "start_timestamp": "2026-11-06T20:50:00.000Z",
        "before": [{"result": {"status": "passed", "duration": 500_000_000}}],
        "steps": [{"keyword": "Given ", "name": "x", "result": {"status": "passed", "duration": 2_000_000_000}}]}]}]
    result = cv.parse_cucumber_report(report)[(INIT_REL, "Paramétrage")]
    assert result["duree"] == 2.5 and result["debut"].startswith("2026-11-06T20:50:00")


def test_targeted_agent_run_gets_client_init():
    profile = {"init_tags": "@INIT_CANSON_WMS"}
    assert cucumber_run.with_client_init("@agent_canson_1_1", profile, [INIT, LOT]) == \
        "@INIT_CANSON_WMS or @agent_canson_1_1"
    assert cucumber_run.with_client_init("@CANSON", profile, [INIT, LOT]) == "@CANSON"      # init déjà comprise
    assert cucumber_run.with_client_init("@INIT_CANSON_WMS", profile, [INIT, LOT]) == "@INIT_CANSON_WMS"
    assert cucumber_run.with_client_init("@agent_canson_1_1", {}, [INIT, LOT]) == "@agent_canson_1_1"


def _non_covered(blocker):
    return {"statut": "non_couvert", "blocage": blocker, "recherches": []}


def test_feature_depending_on_excluded_module_is_not_testable():
    profile = {"modules_exclus": ["TMS"]}
    result = cucumber_testability.assess(_non_covered(
        {"type": "module_exclu", "module": "TMS", "preuve": "step « affecter transport » : POST tms/dispatch"}),
        {}, profile)
    assert result["etat"] == "non_testable" and result["blocage"] == "module_exclu"
    assert cucumber_testability.blocker_line(result).startswith("non testable : dépend du module exclu (TMS)")
    # Module cité par la preuve seulement
    inferred = cucumber_testability.assess(_non_covered(
        {"type": "module_exclu", "preuve": "endpoint TMS tms/dispatch/assign"}), {}, profile)
    assert inferred["etat"] == "non_testable"


def test_excluded_module_blocker_refused_for_a_tested_module():
    result = cucumber_testability.assess(_non_covered(
        {"type": "module_exclu", "module": "WMS", "preuve": "step WMS"}), {}, {"modules_exclus": ["TMS"]})
    assert result["etat"] != "non_testable" and any("non exclu" in r for r in result["rejets"])
    none = cucumber_testability.assess(_non_covered(
        {"type": "module_exclu", "module": "TMS", "preuve": "tms"}), {}, {})
    assert none["etat"] != "non_testable"


def test_profile_block_lists_excluded_modules():
    from tools import cucumber_profile
    line = cucumber_profile.excluded_modules_block({"modules_exclus": ["TMS"]})
    assert "TMS" in line and '"module_exclu"' in line and "affectation transport" in line
    assert cucumber_profile.excluded_modules_block({}) == ""


def test_lot_bilan_shows_init_and_test_durations_separately():
    from tools import cucumber_bilan
    lot = {"execution": {"mode": "branche", "scenarios": {}, "init": {
        "tags": "@INIT_CANSON_WMS", "ok": True, "duree_init": 754.0, "duree_tests": 27.6}}}
    line = cucumber_bilan.lot_outcome(lot, [], [], False, 1)["ligne"]
    assert "init @INIT_CANSON_WMS ✅ 12 min 34 s ; tests 28 s" in line
    lot["execution"]["init"].update(ok=False, duree_tests=None)
    line = cucumber_bilan.lot_outcome(lot, [], [], False, 1)["ligne"]
    assert "tests durée inconnue (lot non jugé : environnement non initialisé)" in line
