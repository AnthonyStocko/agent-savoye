"""
Étape 4 en un clic : cases du job CUCUMBER-CUSTOMER réglées pour l'init (jamais reprises du build de référence),
repli sur la copie des fichiers du Standard pour une init existante, erreur de console d'un build sans rapport,
enchaînement automatique, et détection des limites d'usage de Claude Code.
"""
import importlib
import io
import sys

import pytest

from tests.conftest import CLIENT, write

from tools import cucumber_job as cj
from tools import cucumber_simple_init as si
from tools import cucumber_tools as ct
from tools import cucumber_verify as cv

# Paramètres du job CUCUMBER-CUSTOMER (captures du job)
BOOL, STR = {"type": "BooleanParameterDefinition", "defaut": False}, {"type": "StringParameterDefinition", "defaut": ""}
DEFINITIONS = {
    "COMPONENT_OWNER": STR, "COMPONENT_NAME": STR, "COMPONENT_VERSION": STR, "KIS_VERSION": STR,
    "ADDITIONAL_ARGLINE": STR, "hasStandard": BOOL, "CUCUMBER_STANDARD_PROJECT": STR,
    "CUCUMBER_STANDARD_REPOSITORY": STR, "CUCUMBER_STANDARD_BRANCH": STR,
    **{name: BOOL for name in ("INIT_WMS", "DEBUT", "PARAMETRAGE", "DIVERS", "INVENTAIRE", "PREPARATION",
                               "RECEPTION", "STOCK", "TRANSPORT", "FIN", "TMS", "OMS", "PERFORMANCE")},
    "ADDITIONAL_STANDARD_TAGS": STR, "STANDARD_TAGS_TO_EXCLUDE": STR, "hasSpecific": BOOL,
    "CUCUMBER_SPEC_REPOSITORY": STR, "CUCUMBER_SPEC_BRANCH": STR, "ADDITIONAL_SPEC_TAGS": STR, "wait": BOOL,
}


def test_job_roles_of_the_customer_job():
    roles, warnings = cj.detect_roles(DEFINITIONS)
    assert (roles["standard_actif"], roles["specifique_actif"], roles["tags_standard"],
            roles["tags_standard_exclus"]) == ("hasStandard", "hasSpecific", "ADDITIONAL_STANDARD_TAGS",
                                               "STANDARD_TAGS_TO_EXCLUDE")
    assert (roles["branche"], roles["depot"], roles["tags"], roles["arguments"], roles["attente"]) == (
        "CUCUMBER_SPEC_BRANCH", "CUCUMBER_SPEC_REPOSITORY", "ADDITIONAL_SPEC_TAGS", "ADDITIONAL_ARGLINE", "wait")
    assert not warnings
    assert set(cj.standard_suites(DEFINITIONS, roles)) == {
        "INIT_WMS", "DEBUT", "PARAMETRAGE", "DIVERS", "INVENTAIRE", "PREPARATION", "RECEPTION", "STOCK",
        "TRANSPORT", "FIN", "TMS", "OMS", "PERFORMANCE"}


def test_standard_init_run_checks_only_its_suite_whatever_the_reference_build():
    roles, _ = cj.detect_roles(DEFINITIONS)
    reference = {"parametres": {"hasStandard": "false", "INIT_WMS": "false", "PREPARATION": "true",
                                "hasSpecific": "true", "ADDITIONAL_STANDARD_TAGS": "@WMS",
                                "ADDITIONAL_SPEC_TAGS": "@CANSON", "CUCUMBER_SPEC_REPOSITORY": "canson-cucumber",
                                "ADDITIONAL_ARGLINE": "-DuserTdm='x' -Dcucumber.filter.tags='@CANSON'"}}
    suites, warnings = cj.suite_values(DEFINITIONS, roles, ["WMS"])
    params = cj.build_parameters(DEFINITIONS, roles, "v1.3", "canson-cucumber", "@INIT_WMS", reference=reference,
                                 suites=suites)
    assert not warnings
    assert (params["hasStandard"], params["INIT_WMS"], params["PREPARATION"], params["hasSpecific"]) == (
        "true", "true", "false", "false")
    assert params["ADDITIONAL_STANDARD_TAGS"] == "" and params["STANDARD_TAGS_TO_EXCLUDE"] == ""
    assert params["CUCUMBER_SPEC_BRANCH"] == "v1.3" and params["wait"] == "false"
    assert params["ADDITIONAL_ARGLINE"] == "-DuserTdm='x' -Dcucumber.filter.tags='@INIT_WMS'"


def test_client_runs_uncheck_every_standard_suite():
    roles, _ = cj.detect_roles(DEFINITIONS)
    suites, _ = cj.suite_values(DEFINITIONS, roles)
    params = cj.build_parameters(DEFINITIONS, roles, "v1.3", "canson-cucumber", "@INIT_CANSON_WMS",
                                 reference={"parametres": {"hasStandard": "true", "INIT_WMS": "true"}}, suites=suites)
    assert (params["hasStandard"], params["INIT_WMS"], params["hasSpecific"]) == ("false", "false", "true")


def test_unknown_module_suite_is_reported_and_other_jobs_untouched():
    roles, _ = cj.detect_roles(DEFINITIONS)
    _, warnings = cj.suite_values(DEFINITIONS, roles, ["OMS"])
    assert warnings == ["aucune case INIT_OMS dans le job : l'init OMS du Standard n'y est pas sélectionnable"]
    plain = {"CUCUMBER_SPEC_BRANCH": STR, "ADDITIONAL_ARGLINE": STR}
    assert cj.suite_values(plain, cj.detect_roles(plain)[0], ["WMS"]) == ({}, [])


def test_console_error_of_a_build_without_report():
    console = ("[INFO] Running cucumber.RunnerTest\n"
               "java.lang.IllegalStateException: Dossier de données du Standard introuvable : /home/jenkins/agent/"
               "workspace/CUCUMBER-CUSTOMER/spec/./src/test/resources/cucumber/files/WMS/01-Contexte\n"
               "[ERROR] Tests run: 0\n")
    error = cv.console_error(console)
    assert error.startswith("java.lang.IllegalStateException: Dossier de données du Standard introuvable")
    assert si.standard_files_error(error)
    assert not si.standard_files_error("java.lang.NullPointerException at QARuleStep")
    assert cv.console_error("tout va bien\n") == ""


def test_feature_files_steps_restored_from_standard_files_steps():
    text = ("Feature: Init\n  Scenario: Init\n"
            f'    Given {si.STD_FILES_STEP} "WMS"\r\n'
            f'    And {si.STD_RULES_STEP} "WMS"\n'
            f'    # And {si.STD_FILES_STEP} "TMS"\n')
    out = si.use_feature_files(text)
    assert f"    Given {si.GLOBAL_INIT_STEP}\r\n" in out and f"    And {si.RULES_STEP}\n" in out
    assert f'    # And {si.STD_FILES_STEP} "TMS"\n' in out
    assert not si.uses_standard_files(out)
    # Aller-retour avec use_standard_files
    assert si.use_feature_files(si.use_standard_files(out, "WMS")) == out


@pytest.fixture(scope="module")
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


@pytest.fixture
def main(tmp_path, monkeypatch, main_module):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    return main_module


def test_auto_chain_goes_from_generation_to_green_init(main):
    script = {"verifier": ("a_faire", "appliquer"), "appliquer": ("a_faire", "lancer"), "lancer": ("ok", "")}
    done = []

    def step(action):
        done.append(action)
        status, following = script[action]
        main.simple_record(CLIENT, "init", status, f"{action} fait", {"plan": {"action": following}})
        return 0 if status != "echec" else 1

    assert main.run_simple_init_auto(CLIENT, "", "", None, "26.22", run_step=step) == 0
    assert done == ["verifier", "appliquer", "lancer"]


def test_auto_chain_follows_known_fix_then_stops_on_repeated_failure(main):
    outcomes = iter([("a_faire", "lancer", "prête"), ("a_faire", "verifier", "fichiers du Standard illisibles"),
                     ("a_faire", "appliquer", "copie proposée"), ("a_faire", "lancer", "prête"),
                     ("echec", "lancer", "init en échec : NPE")])
    done = []

    def step(action):
        done.append(action)
        status, following, result = next(outcomes)
        main.simple_record(CLIENT, "init", status, result, {"plan": {"action": following}})
        return 1 if status == "echec" else 0

    assert main.run_simple_init_auto(CLIENT, "", "", None, "26.22", run_step=step) == 1
    assert done == ["verifier", "lancer", "verifier", "appliquer", "lancer"]


def test_auto_chain_stops_when_nothing_changes(main):
    done = []

    def step(action):
        done.append(action)
        main.simple_record(CLIENT, "init", "a_faire", "même chose", {"plan": {"action": "verifier"}})
        return 0

    assert main.run_simple_init_auto(CLIENT, "", "", None, "26.22", run_step=step) == 1
    assert done == ["verifier", "verifier"]


def test_usage_limits_of_claude_code_are_detected(main):
    five_hours = "You've hit your 5-hour limit · resets 7:30pm (Europe/Paris)"
    assert main.usage_limit_hit(five_hours) and main.usage_limit_hit("You've hit your session limit")
    assert main.usage_limit_hit("You've hit your weekly limit · resets Mon 9am")
    assert main.usage_limit_reset(five_hours) == "resets 7:30pm (Europe/Paris)"
    assert not main.usage_limit_hit("Compilation OK, 3 fichiers écrits.")


def test_existing_init_falls_back_to_copied_standard_files(main, monkeypatch, tmp_path):
    std = tmp_path / "std"
    res = "src/test/resources/cucumber"
    write(str(std / res / "features/WMS/01-Contexte/01-Initialisation.feature"), "@INIT_WMS @WMS\nFeature: Init\n")
    write(str(std / res / "files/WMS/01-Contexte/01-SITE.xml"), "<site/>")
    write(str(std / res / "rules/WMS/01-Contexte/rule-A.json"), "{}")
    worktree = tmp_path / "wt"
    rel = f"{res}/features/{CLIENT}/01-Contexte/WMS/01-Initialisation.feature"
    init = (f"@INIT_{CLIENT.upper()}_WMS @WMS\nFeature: Init\n  Scenario: Init\n"
            f'    Given {si.STD_FILES_STEP} "WMS"\n    And {si.STD_RULES_STEP} "WMS"\n')
    write(str(worktree / rel), init)
    monkeypatch.setattr(main.cucumber_init_data, "client_init", lambda wt, client: {"features": [{"chemin": rel}]})
    monkeypatch.setattr(main.cucumber_checks, "library_version", lambda wt: "26.22")
    monkeypatch.setattr(main.cucumber_init, "_library_snapshots", lambda d, lib: (None, None, ""))
    decision = {"mode": "standard", "routes": ["full/all"], "preuves": []}
    plan = main._simple_init_plan(CLIENT, str(worktree), str(std), "26.22", ["WMS"], set(), {}, decision,
                                  copy_files={"WMS"}, std_files=lambda: {})
    assert plan["changes"][rel] == (f"@INIT_{CLIENT.upper()}_WMS @WMS\nFeature: Init\n  Scenario: Init\n"
                                    f"    Given {si.GLOBAL_INIT_STEP}\n    And {si.RULES_STEP}\n")
    segment = ct.java_package_segment(CLIENT)
    assert set(plan["binaries"]) == {f"{res}/files/{segment}/01-Contexte/WMS/01-SITE.xml",
                                     f"{res}/rules/{segment}/01-Contexte/WMS/rule-A.json"}


def test_spec_tags_parameter_gets_the_positive_tags_of_the_run():
    roles, _ = cj.detect_roles(DEFINITIONS)
    assert cj.tag_target({}, roles) == "les_deux"
    params = cj.build_parameters(DEFINITIONS, roles, "v1.3", "canson-cucumber", "@INIT_CANSON_WMS and not @TMS",
                                 target=cj.tag_target({}, roles),
                                 reference={"parametres": {"ADDITIONAL_SPEC_TAGS": ""}})
    assert params["ADDITIONAL_SPEC_TAGS"] == "@INIT_CANSON_WMS"
    assert params["ADDITIONAL_ARGLINE"].endswith("-Dcucumber.filter.tags='@INIT_CANSON_WMS and not @TMS'")
    assert cj.job_tag_list("(@A or @B) and not @A_REVOIR and @C") == "@A,@B,@C"
