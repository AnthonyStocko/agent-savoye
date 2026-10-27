"""Vérification des tests Cucumber générés : mutation, niveaux, statut final, job Jenkins."""
import os

from tools import cucumber_verify as cv

FEATURE = """Feature: Commandes

  @agent_apparel_1 @agent_apparel_1_1
  Scenario: Statut de la commande
    Given une commande "CMD-1"
    When je valide la commande "CMD-1"
    Then le champ "statut" vaut "VALIDE"
    And les champs de la commande sont :
      | champ   | valeur |
      | statut  | VALIDE |
      | montant | 12     |
      | client  | DUPONT |
"""


def test_mutation_only_last_quoted_value_of_then():
    mutated, counts = cv.mutate_feature(FEATURE, {"@agent_apparel_1_1"})
    assert 'Then le champ "statut" vaut "VALIDE_MUT"' in mutated
    assert '"statut_MUT"' not in mutated
    assert 'Given une commande "CMD-1"' in mutated and 'When je valide la commande "CMD-1"' in mutated
    assert counts["@agent_apparel_1_1"] >= 1


def test_mutation_keeps_key_column_of_then_tables():
    mutated, _ = cv.mutate_feature(FEATURE, {"@agent_apparel_1_1"})
    rows = [line.strip() for line in mutated.splitlines() if line.strip().startswith("|")]
    assert rows[0] == "| champ   | valeur |"
    # Une ligne de données sur deux (1re, 3e) faussée, dans la colonne valeur seulement
    assert [[c.strip() for c in row.split("|")[1:-1]] for row in rows[1:]] == [
        ["statut", "VALIDE_MUT"], ["montant", "12"], ["client", "DUPONT_MUT"]]


def test_mutation_of_table_without_key_header_mutates_all_columns():
    text = FEATURE.replace("| champ   | valeur |", "| statut  | montant |").replace(
        "      | statut  | VALIDE |\n", "      | VALIDE  | 12      |\n")
    mutated, _ = cv.mutate_feature(text, {"@agent_apparel_1_1"})
    rows = [line.strip() for line in mutated.splitlines() if line.strip().startswith("|")]
    assert [c.strip() for c in rows[1].split("|")[1:-1]] == ["VALIDE_MUT", "13"]


def test_mutation_ignores_untagged_scenarios():
    mutated, counts = cv.mutate_feature(FEATURE, {"@autre"})
    assert mutated == FEATURE and counts == {}


def _report(failed_keyword, failed_name, error):
    steps = [{"keyword": "Given ", "name": "une commande", "result": {"status": "passed"}},
             {"keyword": "Then ", "name": "le champ vaut", "result": {"status": "passed"}}]
    for step in steps:
        if step["keyword"].strip() == failed_keyword:
            step["result"] = {"status": "failed", "error_message": error}
    return [{"uri": "src/test/resources/cucumber/features/a.feature",
             "elements": [{"type": "scenario", "name": "Statut", "steps": steps}]}]


def _result(failed_keyword, failed_name, error):
    parsed = cv.parse_cucumber_report(_report(failed_keyword, failed_name, error))
    return parsed[("src/test/resources/cucumber/features/a.feature", "Statut")]


def test_failure_kind_classification():
    assert _result("Then", "", "org.opentest4j.AssertionFailedError: expected: <A> but was: <B>")["nature"] == "assertion"
    assert _result("Then", "", "java.lang.NullPointerException\n\tat Steps.check(Steps.java:12)")["nature"] == "technique"


def test_level_technical_failure_is_not_conclusive():
    result = _result("Then", "", "java.lang.NullPointerException\n\tat Steps.check(Steps.java:12)")
    level, detail = cv._level(result, expect_failure=True)
    assert level == "nv" and "NullPointerException" in detail
    # Contre-épreuve Standard : même règle
    assert cv._level(result, expect_failure=True, then_only=True)[0] == "nv"


def test_level_assertion_failure_counts():
    result = _result("Then", "", "java.lang.AssertionError: expected VALIDE")
    assert cv._level(result, expect_failure=True)[0] == "ok"


def test_override_failing_at_given_on_standard_is_not_verified():
    standard = cv._level(_result("Given", "", "java.lang.AssertionError: expected commande"), True, True)
    assert standard[0] == "nv"
    status, reason = cv.final_status({"client": ("ok", "passe"), "standard": standard,
                                      "mutation": ("ok", "échoue comme attendu")})
    assert status == "non_verifie" and "avant les Then" in reason


def test_final_status_weak_only_when_passing():
    assert cv.final_status({"client": ("ok", ""), "standard": ("ko", cv.PASSES_STILL),
                            "mutation": ("ok", "")})[0] == "faible"
    assert cv.final_status({"client": ("ok", ""), "mutation": ("ko", cv.PASSES_STILL)})[0] == "faible"
    assert cv.final_status({"client": ("ok", ""), "mutation": ("nv", cv.NO_VALUE_TO_MUTATE)}) == (
        "non_verifie", cv.NO_VALUE_TO_MUTATE)
    assert cv.final_status({"client": ("ok", ""), "standard": ("ok", ""), "mutation": ("ok", "")})[0] == "pertinent"


def test_review_sample_puts_unverified_first():
    state = {"debut": "x", "lots": [{"numero": 1, "execution": {"scenarios": {
        f"@t{i}": {"id": f"f::s{i}", "statut": "pertinent" if i % 2 else "non_verifie", "niveaux": {}}
        for i in range(6)}}}]}
    sample = cv.review_sample(state, size=4)
    assert [s["statut"] for s in sample] == ["non_verifie"] * 3 + ["pertinent"]


def test_execution_mode_requires_spec_repository(monkeypatch):
    monkeypatch.setenv("CUCUMBER_VERIFY_JOB", "QA/VERIFY")
    monkeypatch.setenv("JENKINS_URL", "https://jenkins")
    monkeypatch.delenv("CUCUMBER_VERIFY_LOCAL_CMD", raising=False)
    base = {"PATCH": "FileParameterDefinition", "TAGS": "String", "ENV": "String"}
    monkeypatch.setattr(cv.jenkins_tools, "job_parameters", lambda job: base)
    assert cv.execution_mode()[0] == ""
    monkeypatch.setattr(cv.jenkins_tools, "job_parameters", lambda job: {**base, "CUCUMBER_SPEC_REPOSITORY": "String"})
    mode, message = cv.execution_mode()
    assert mode == "jenkins" and "CUCUMBER_SPEC_COMMIT" in message


def test_run_tags_sends_sources_and_detects_unapplicable_patch(monkeypatch, repo):
    from tests.conftest import git, write
    write(os.path.join(repo, "a.txt"), "a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    sent = {}
    monkeypatch.setenv("CUCUMBER_VERIFY_JOB", "QA/VERIFY")
    monkeypatch.setattr(cv.jenkins_tools, "trigger_cucumber_job_with_file",
                        lambda job, params, name, path: sent.update(params) or "https://jenkins/queue/1")
    monkeypatch.setattr(cv.jenkins_tools, "wait_build_result", lambda queue, timeout: {
        "url": "https://jenkins/job/1", "result": "FAILURE", "artifacts": [cv.PATCH_STATUS_ARTIFACT]})

    def fetch(build, artifact, target):
        write(target, "PATCH_NON_APPLICABLE\n")
        return True
    monkeypatch.setattr(cv.jenkins_tools, "fetch_artifact", fetch)
    sources = cv.job_sources("apparel-cucumber", "integration", "abc123", "def456")
    result = cv.run_tags("jenkins", repo, "HEAD", None, "@t", "recette", {}, "lot1-client", sources)
    assert sent["CUCUMBER_SPEC_REPOSITORY"] == "apparel-cucumber" and sent["CUCUMBER_SPEC_COMMIT"] == "abc123"
    assert sent["CUCUMBER_STANDARD_COMMIT"] == "def456" and sent["CUCUMBER_SPEC_BRANCH"] == "integration"
    assert not result["ok"] and result["message"] == cv.PATCH_NOT_APPLICABLE
    level = ("nv", result["message"])
    assert cv.final_status({"client": level}) == ("non_verifie", cv.PATCH_NOT_APPLICABLE)


PRICE_FEATURE = """Feature: Prix
  @agent_acme_1 @agent_acme_1_1
  Scenario: Prix client
    Given un article "A"
    Then le prix de "A" vaut "12"

  @agent_acme_1 @agent_acme_1_2
  Scenario: Sans valeur
    Then le prix est calculé
"""


def test_client_and_mutation_in_one_build(monkeypatch, tmp_path):
    from tests.conftest import write
    rel = "src/test/resources/cucumber/features/acme/prix.feature"
    write(str(tmp_path / rel), PRICE_FEATURE)
    monkeypatch.delenv("CUCUMBER_VERIFY_MERGE_MUTATION", raising=False)
    calls = []
    copy = rel[:-8] + "_mut.feature"

    def run_tags(mode, worktree, base, build_fn, tags, env, overrides, label, sources=None):
        calls.append((tags, sorted(overrides), label))
        assert '"12_MUT"' in overrides[copy] and "@agent_acme_1_1_mut" in overrides[copy]
        assert "@agent_acme_1_1 " not in overrides[copy]
        return {"ok": True, "message": "ok", "build": "b1", "resultats": {
            (rel, "Prix client"): {"statut": "passe", "step": "", "phase": "", "message": ""},
            (rel, "Sans valeur"): {"statut": "passe", "step": "", "phase": "", "message": ""},
            (copy, "Prix client"): {"statut": "echoue", "step": "Then le prix", "phase": "then",
                                    "message": "expected 12_MUT", "nature": "assertion", "exception": ""}}}
    monkeypatch.setattr(cv, "run_tags", run_tags)
    scenarios = {"@agent_acme_1_1": f"{rel}::Prix client", "@agent_acme_1_2": f"{rel}::Sans valeur"}
    result = cv.verify_lot("jenkins", str(tmp_path), "HEAD", None, [rel], scenarios, {}, "lot1")
    assert len(calls) == 1 and calls[0][1] == [copy]
    assert calls[0][0] == "@agent_acme_1_1 or @agent_acme_1_2 or @agent_acme_1_1_mut"
    assert result["scenarios"]["@agent_acme_1_1"]["statut"] == "pertinent"
    assert result["scenarios"]["@agent_acme_1_2"]["statut"] == "non_verifie"
    assert result["builds"][0]["execution"] == "client + mutation"


def test_apply_overrides_restores_and_removes(tmp_path):
    from tests.conftest import write
    write(str(tmp_path / "a.feature"), "orig\n")
    restore = cv._apply_overrides(str(tmp_path), {"a.feature": "mut\n", "b_mut.feature": "copy\n"})
    assert (tmp_path / "a.feature").read_text() == "mut\n" and (tmp_path / "b_mut.feature").exists()
    restore()
    assert (tmp_path / "a.feature").read_text() == "orig\n" and not (tmp_path / "b_mut.feature").exists()
