"""Sélection des scénarios (tags, dry-runs, nombre attendu), version exécutée, champs contrôlés, preuves et logs."""
import importlib
import io
import json
import os
import sys

import pytest

from tests.conftest import CLIENT, CLIENT_TAG, git, write
from tools import cucumber_a_revoir as ar
from tools import cucumber_checks as cc
from tools import cucumber_verify as cv
from tools import cucumber_versions as cvs

RES = "src/test/resources/cucumber"
REL = f"{RES}/features/{CLIENT}/Stock.feature"
FEATURE = (f"@WMS {CLIENT_TAG}\n"
           "Feature: Stock\n\n"
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


@pytest.fixture
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


# --- B : tag client hérité de la ligne Feature --------------------------------------------------------------------

def test_feature_tag_is_inherited_and_moves_down_when_a_scenario_is_disabled(tmp_path):
    assert cc.check_client_tags({REL: FEATURE}, CLIENT_TAG) == []
    # Scénario désactivé à la main, tag client resté sur la ligne Feature : il en hérite
    disabled = FEATURE.replace("  @agent_clientx_1 @agent_clientx_1_2\n", "  @agent_clientx_1 @agent_clientx_1_2 "
                                                                        "@A_REVOIR\n")
    errors = cc.check_client_tags({REL: disabled}, CLIENT_TAG)
    assert len(errors) == 1 and "hérite du tag client" in errors[0] and "Motif de blocage" in errors[0]
    # La désactivation de l'agent descend le tag client sur les seuls scénarios actifs
    write(os.path.join(str(tmp_path), REL), FEATURE)
    ar.deactivate(str(tmp_path), REL, {"Motif de blocage": ("environnement", {"motif": "503"})}, CLIENT_TAG)
    with open(os.path.join(str(tmp_path), REL), encoding="utf-8") as f:
        text = f.read()
    assert cc.check_client_tags({REL: text}, CLIENT_TAG) == []
    units = {u["titre"]: u for u in cv.scenario_units(text)}
    assert CLIENT_TAG in units["Statut du contenant"]["propres"] and CLIENT_TAG not in units["Motif de blocage"]["tags"]
    # Scénario actif sans le tag client (feature sans tag de Feature) : erreur, init comprise
    untagged = FEATURE.replace(f"@WMS {CLIENT_TAG}\n", "@WMS\n")
    assert len(cc.check_client_tags({REL: untagged}, CLIENT_TAG)) == 2


def test_expected_count_expands_examples_and_excludes_disabled():
    outline = (f"{CLIENT_TAG}\nFeature: F\n  Scenario Outline: O\n    Given x <a>\n    Examples:\n      | a |\n"
               "      | 1 |\n      | 2 |\n    @A_REVOIR\n    Examples:\n      | a |\n      | 3 |\n"
               "  @A_REVOIR\n  Scenario: Désactivé\n    Given y\n  Scenario: Actif\n    Given z\n")
    assert cv.expected_count([outline]) == 5
    assert cv.expected_count([outline], CLIENT_TAG, "@A_REVOIR") == 3
    assert cv.expected_count([outline], "@AUTRE") == 0


def test_two_dry_runs_must_select_the_same_count(monkeypatch, tmp_path):
    texts = {REL: FEATURE}
    counts = {"": 2, CLIENT_TAG: 2}
    monkeypatch.setattr(cc, "_dry_run_count", lambda build, cmd, paths, tags="": {
        "ok": True, "nombre": counts[tags], "erreurs": [], "environnement": False})
    result = cc.selection_check(str(tmp_path), "mvn test", texts, CLIENT_TAG)
    assert result["ok"] and result["nombre_attendu"] == 2 and result["attendu"] == 2
    # Un scénario actif ne porte pas le tag client : les deux dry-runs divergent
    counts[CLIENT_TAG] = 1
    result = cc.selection_check(str(tmp_path), "mvn test", texts, CLIENT_TAG)
    assert not result["ok"] and result["nombre_attendu"] == 0 and "même nombre" in result["erreurs"][0]
    # Mêmes nombres, mais différents des features : filtre de tags inopérant ou désactivés non exclus
    counts.update({"": 3, CLIENT_TAG: 3})
    result = cc.selection_check(str(tmp_path), "mvn test", texts, CLIENT_TAG)
    assert not result["ok"] and "inopérant" in result["erreurs"][0]


def test_dry_run_count_reads_the_json_report(monkeypatch, tmp_path):
    build = str(tmp_path)
    report = [{"uri": "x.feature", "elements": [{"type": "background"}, {"type": "scenario"}, {"type": "scenario"}]}]
    commands = []

    def maven(command, cwd, timeout=1800):
        commands.append(command)
        write(os.path.join(build, "target", "cucumber-reports", "Cucumber.json"), json.dumps(report))
        return 0, "BUILD SUCCESS"
    monkeypatch.setattr(cc, "run_maven", maven)
    result = cc._dry_run_count(build, "mvn -q test", ["a"], CLIENT_TAG)
    assert result == {"ok": True, "nombre": 2, "erreurs": [], "environnement": False}
    assert f'"-Dcucumber.filter.tags={CLIENT_TAG}"' in commands[0] and "dry-run=true" in commands[0]


# --- C : version exécutée par le job ------------------------------------------------------------------------------

def _pushed_repo(tmp_path, repo):
    remote = str(tmp_path / "remote.git")
    git(str(tmp_path), "init", "-q", "--bare", remote)
    git(repo, "remote", "add", "origin", remote)
    write(os.path.join(repo, REL), FEATURE)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return remote


def test_verification_refused_without_pushed_commit(tmp_path, repo):
    _pushed_repo(tmp_path, repo)
    assert "introuvable sur origin" in cc.verify_branch_error(repo, "verif")
    git(repo, "push", "-q", "origin", "HEAD:verif")
    assert cc.verify_branch_error(repo, "verif") == ""
    # Modification non commitée, puis commitée mais non poussée : le job exécuterait une ancienne version
    write(os.path.join(repo, REL), FEATURE + "  Scenario: Nouveau\n    Given x\n")
    assert "non commité" in cc.verify_branch_error(repo, "verif")
    git(repo, "commit", "-qam", "lot")
    error = cc.verify_branch_error(repo, "verif")
    assert "ancienne version" in error and "pousse sur verif" in error


def test_push_for_verification_commits_and_pushes_without_force(tmp_path, repo):
    remote = _pushed_repo(tmp_path, repo)
    write(os.path.join(repo, REL), FEATURE + "  Scenario: Nouveau\n    Given x\n")
    error, committed = cc.push_for_verification(repo, "verif", "lot 1 (vérification)")
    assert error == "" and committed and cc.verify_branch_error(repo, "verif") == ""
    assert git(remote, "rev-parse", "verif") == git(repo, "rev-parse", "HEAD")
    # Rien de nouveau : pas de commit, la branche reste à jour
    assert cc.push_for_verification(repo, "verif", "lot 1 (vérification)") == ("", False)
    # Branche distante divergente : jamais de push forcé, la vérification s'arrête
    other = str(tmp_path / "autre")
    git(str(tmp_path), "clone", "-q", "-b", "verif", remote, other)
    git(other, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "autre")
    git(other, "push", "-q", "origin", "verif")
    write(os.path.join(repo, REL), FEATURE)
    error, committed = cc.push_for_verification(repo, "verif", "lot 2 (vérification)")
    assert committed and "push sur verif refusé" in error and "ancienne version" in error


def test_verify_execution_lot_pushes_then_runs_on_the_branch(tmp_path, repo, main_module, monkeypatch):
    remote = _pushed_repo(tmp_path, repo)
    write(os.path.join(repo, REL), FEATURE + "  Scenario: Nouveau\n    Given x\n")  # lot non commité
    called = []
    monkeypatch.setattr(main_module.cucumber_verify, "verify_lot", lambda *a, **k: called.append(a) or {
        "scenarios": {}, "builds": []})
    lot = {"numero": 1, "tags": {"@agent_clientx_1_1": f"{REL}::Statut du contenant"},
           "indicateurs": {"scenarios": []}}
    conventions = {"client_tag": CLIENT_TAG, "verify_branch": "verif", "verify_branch_param": "BRANCHE"}
    main_module.verify_execution_lot(lot, [], "1/1", repo, "HEAD", [REL], [], "jenkins", "job", CLIENT, "", "", "",
                                     {}, "sonnet", "K", "/run", lambda u: None, context={"conventions": conventions})
    head = git(repo, "rev-parse", "HEAD")
    assert lot["commit"] and git(remote, "rev-parse", "verif") == head
    assert called[0][2] == head and called[0][8]["BRANCHE"] == "verif" and called[0][8]["CUCUMBER_SPEC_COMMIT"] == head
    # Push refusé (branche distante divergente) : aucune exécution, scénarios non vérifiés
    other = str(tmp_path / "autre")
    git(str(tmp_path), "clone", "-q", "-b", "verif", remote, other)
    git(other, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "autre")
    git(other, "push", "-q", "origin", "verif")
    write(os.path.join(repo, REL), FEATURE)
    called.clear()
    main_module.verify_execution_lot(lot, [], "1/1", repo, "HEAD", [REL], [], "jenkins", "job", CLIENT, "", "", "",
                                     {}, "sonnet", "K", "/run", lambda u: None, context={"conventions": conventions})
    assert not called and "ancienne version" in lot["execution"]["message"]
    assert all(s["statut"] == "non_verifie" for s in lot["execution"]["scenarios"].values())


def _report(n):
    return [{"uri": f"classpath:cucumber/features/{CLIENT}/Stock.feature", "elements": [
        {"type": "scenario", "name": f"S{i}", "steps": [{"keyword": "Given ", "name": "x",
                                                        "result": {"status": "passed"}}]} for i in range(n)]}]


def test_report_with_another_test_count_is_never_analysed(monkeypatch, repo):
    write(os.path.join(repo, REL), FEATURE)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    assert cv.selection_gap(_report(1), [FEATURE], "@agent_clientx_1_1") == ""
    # Le job a tout exécuté (filtre de tags non transmis) ou une ancienne version : écart, rapport non analysé
    gap = cv.selection_gap(_report(41), [FEATURE], "@agent_clientx_1_1")
    assert "41 test(s) exécuté(s) pour 1 attendu(s)" in gap
    monkeypatch.setenv("CUCUMBER_VERIFY_JOB", "QA/VERIFY")
    monkeypatch.setattr(cv.jenkins_tools, "trigger_cucumber_job_with_file", lambda *a: "https://jenkins/queue/1")
    monkeypatch.setattr(cv.jenkins_tools, "wait_build_result", lambda queue, timeout: {
        "url": "https://jenkins/job/1", "result": "UNSTABLE", "artifacts": ["target/cucumber.json"]})

    def fetch(build, artifact, target):
        write(target, json.dumps(_report(41)))
        return True
    monkeypatch.setattr(cv.jenkins_tools, "fetch_artifact", fetch)
    result = cv.run_tags("jenkins", repo, "HEAD", None, "@agent_clientx_1_1", "recette", {}, "lot1-client")
    assert not result["ok"] and result["ecart_selection"] and result["resultats"] == {}
    assert cv.final_status({"client": ("nv", result["message"])})[0] == "non_verifie"


# --- D : champs contrôlés réellement renvoyés ---------------------------------------------------------------------

STEPS = """package client;
public class StockSteps {
    @Then("STOCK- le contenant a les valeurs")
    public void valeurs(DataTable table) {
        ContainerResponse response = get("/containers");
    }
}
"""
POJOS = {
    "ContainerResponse": "package client.pojo;\npublic class ContainerResponse extends BaseResponse {\n"
                         "    private String code;\n    @JsonProperty(\"status\")\n    private String statut;\n"
                         "    private List<LocationPojo> locations;\n}\n",
    "BaseResponse": "package client.pojo;\npublic class BaseResponse {\n    private String id;\n}\n",
    "LocationPojo": "package client.pojo;\npublic class LocationPojo {\n    private String zone;\n}\n",
}


def test_unmapped_control_column_is_a_lot_error(repo):
    write(os.path.join(repo, "src/test/java/client/StockSteps.java"), STEPS)
    for name, text in POJOS.items():
        write(os.path.join(repo, f"src/test/java/client/pojo/{name}.java"), text)
    write(os.path.join(repo, REL), "Feature: F\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    write(os.path.join(repo, REL), "Feature: F\n  Scenario: S\n    Then STOCK- le contenant a les valeurs\n"
                                   "      | code | status | id | locations[0].zone | locations.zonne | prioriti |\n"
                                   "      | C1   | LIBRE  | 1  | Z1                | Z1              | 1        |\n")
    errors = cvs.check_response_fields(repo, "HEAD", [REL], "")
    assert len(errors) == 2
    assert "« locations.zonne »" in errors[0] and "« zone »" in errors[0] and "le contrôle lirait null" in errors[0]
    assert "« prioriti »" in errors[1] and "ContainerResponse" in errors[1] and "status" in errors[1]
    # Table déjà présente à la base : non contrôlée
    git(repo, "commit", "-qam", "lot")
    assert cvs.check_response_fields(repo, "HEAD", [REL], "") == []


def test_null_field_failure_is_a_test_cause():
    nature, _ = cv.failure_kind("org.junit.ComparisonFailure: expected:<LIBRE> but was:<null>")
    assert nature == "champ_absent" and cv.failure_category(nature) == "test"
    level = cv._level({"statut": "echoue", "step": "Then x", "message": "expected:<LIBRE> but was:<null>",
                       "nature": nature}, False)
    assert level[0] == "ko" and cv.NULL_FIELD_HINT in level[1]
    assert cv.final_status({"client": level})[0] == "invalide"
    assert ar.cause_of({"statut": "invalide", "motif": level[1]}) == ""


# --- E : hypothèses, preuves et fenêtres de logs ------------------------------------------------------------------

def test_failed_step_time_from_report():
    report = [{"uri": "a.feature", "elements": [{"type": "scenario", "name": "S",
                                                 "start_timestamp": "2026-10-23T08:00:00.000Z", "steps": [
        {"keyword": "Given ", "name": "a", "result": {"status": "passed", "duration": 30_000_000_000}},
        {"keyword": "Then ", "name": "b", "result": {"status": "failed", "error_message": "500 Internal Server "
                                                                                          "Error"}}]}]}]
    result = cv.parse_cucumber_report(report)[("a.feature", "S")]
    assert result["heure"] == "2026-10-23T08:00:30+00:00"


def test_unproven_cause_is_marked_with_the_check_to_do(tmp_path):
    proven, _ = ar.proof_status("donnee_reference", {"motif": "Site non trouvé(e) avec code SITEA"})
    assert proven
    item = {"statut": "bug_applicatif", "motif": "bug applicatif probable : 500 Internal Server Error",
            "step": 'When je valide la commande "CMD-42"', "heure": "2026-10-23T08:00:30+00:00"}
    proven, check = ar.proof_status("bug_applicatif", item)
    assert not proven and "2026-10-23 07:59:30 au 2026-10-23 08:01:30 UTC" in check and "« CMD-42 »" in check
    entry = {"fichier": REL, "titre": "T", "cause": "bug_applicatif", "preuve": "500", "prouvee": False,
             "verification": check, "reactivation": ar.CAUSES["bug_applicatif"][1]}
    text = ar.generated_section({"lots": [{"numero": 1, "a_revoir": [entry]}]}, {})
    assert f"**{ar.UNPROVEN}**" in text and "Vérification à faire" in text and "heure locale" in text


def test_log_export_must_cover_the_failed_step():
    when = "2026-10-23T08:00:30+00:00"
    ok, message = cv.log_coverage("2026-10-23 07:59:40 INFO a\n2026-10-23 08:01:10 ERROR b\n", when)
    assert ok
    # Export plafonné par l'outil : il ne garde que les lignes les plus récentes, le step est perdu
    ok, message = cv.log_coverage("2026-10-23 12:00:00 INFO a\n2026-10-23 12:05:00 INFO b\n", when)
    assert not ok and "les plus récentes" in message
    assert not cv.log_coverage("pas d'horodatage", when)[0]
    window = cv.log_window(when)
    assert window["debut_utc"] == "2026-10-23 07:59:30" and window["fin_utc"] == "2026-10-23 08:01:30"


def test_check_deletions_protects_steps_removed_from_a_kept_class(repo):
    steps = "src/test/java/client/StockSteps.java"
    write(os.path.join(repo, REL), FEATURE)
    write(os.path.join(repo, steps), 'class StockSteps {\n  @Then("STOCK- le motif est {string}")\n'
                                     '  public void motif(String m) {}\n  @Then("AUTRE- inutilisé")\n'
                                     '  public void x() {}\n}\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    write(os.path.join(repo, steps), "class StockSteps {\n}\n")
    errors = ar.check_deletions(repo, "HEAD")
    assert len(errors) == 1 and "STOCK- le motif est {string}" in errors[0] and "RÈGLE 3g" in errors[0]
