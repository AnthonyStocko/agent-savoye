"""Vérification des tests Cucumber générés : mutation, niveaux, statut final, job Jenkins."""
import os

from tools import cucumber_verify as cv

FEATURE = """Feature: Commandes

  @agent_clientx_1 @agent_clientx_1_1
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
    mutated, counts = cv.mutate_feature(FEATURE, {"@agent_clientx_1_1"})
    assert 'Then le champ "statut" vaut "VALIDE_MUT"' in mutated
    assert '"statut_MUT"' not in mutated
    assert 'Given une commande "CMD-1"' in mutated and 'When je valide la commande "CMD-1"' in mutated
    assert counts["@agent_clientx_1_1"] >= 1


def test_mutation_keeps_key_column_of_then_tables():
    mutated, _ = cv.mutate_feature(FEATURE, {"@agent_clientx_1_1"})
    rows = [line.strip() for line in mutated.splitlines() if line.strip().startswith("|")]
    assert rows[0] == "| champ   | valeur |"
    # Une ligne de données sur deux (1re, 3e) faussée, dans la colonne valeur seulement
    assert [[c.strip() for c in row.split("|")[1:-1]] for row in rows[1:]] == [
        ["statut", "VALIDE_MUT"], ["montant", "12"], ["client", "DUPONT_MUT"]]


def test_mutation_of_table_without_key_header_mutates_all_columns():
    text = FEATURE.replace("| champ   | valeur |", "| statut  | montant |").replace(
        "      | statut  | VALIDE |\n", "      | VALIDE  | 12      |\n")
    mutated, _ = cv.mutate_feature(text, {"@agent_clientx_1_1"})
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
    sources = cv.job_sources("clientx-cucumber", "integration", "abc123", "def456")
    result = cv.run_tags("jenkins", repo, "HEAD", None, "@t", "recette", {}, "lot1-client", sources)
    assert sent["CUCUMBER_SPEC_REPOSITORY"] == "clientx-cucumber" and sent["CUCUMBER_SPEC_COMMIT"] == "abc123"
    assert sent["CUCUMBER_STANDARD_COMMIT"] == "def456" and sent["CUCUMBER_SPEC_BRANCH"] == "integration"
    assert not result["ok"] and result["message"] == cv.PATCH_NOT_APPLICABLE
    level = ("nv", result["message"])
    assert cv.final_status({"client": level}) == ("non_verifie", cv.PATCH_NOT_APPLICABLE)


def _feature_report(*elements):
    return [{"uri": "src/test/resources/cucumber/features/clientx/b.feature", "elements": [
        {"type": "scenario", "name": name, "tags": [{"name": t} for t in tags],
         "steps": [{"keyword": "Given ", "name": "un step",
                    "result": {"status": "passed"} if not error else {"status": "failed", "error_message": error}}]}
        for name, tags, error in elements]}]


def test_failure_kind_categories():
    # Donnée de référence absente de l'environnement
    assert cv.failure_kind("java.lang.AssertionError: 404 - Site non trouvé(e) avec code SITEA")[0] == "donnee_reference"
    assert cv.failure_kind("Donneur d'ordres non trouvé(e) avec code DO-1")[0] == "donnee_reference"
    # Écart de version bibliothèque / serveur : route absente, champ exigé, champ inconnu du POJO
    assert cv.failure_kind("No route found for \"GET /api/v1/x\"")[0] == "environnement"
    assert cv.failure_kind("400 Bad Request : field 'priority' must not be null")[0] == "environnement"
    assert cv.failure_kind("com.fasterxml.jackson.databind.exc.UnrecognizedPropertyException: x")[0] == "environnement"
    # Erreurs du test : colonne, chemin de filtre, fichier de données
    assert cv.failure_kind("No such path 'value' in status")[0] == "technique"
    assert cv.failure_kind("Invalid path: 'wmsContent.wmsHandlingUnit.code'")[0] == "technique"
    assert cv.failure_kind("java.io.FileNotFoundException: ./a.xml")[0] == "technique"
    # Erreur serveur
    assert cv.failure_kind("Request failed : 500 Internal Server Error")[0] == "bug_applicatif"
    # Un statut HTTP attendu dans une assertion reste une assertion
    assert cv.failure_kind("AssertionError: expected status <200> but was <404>")[0] == "assertion"
    assert [cv.failure_category(n) for n in ("assertion", "technique", "environnement", "donnee_reference",
                                              "bug_applicatif", "cascade")] == [
        "test", "test", "environnement", "donnee_reference", "bug_applicatif", "cascade"]


def test_not_test_failures_are_not_invalid():
    expected = {"java.lang.RuntimeException: 404 - Site non trouvé(e) avec code SITEA":
                ("ref", "donnee_reference", cv.REFERENCE_NOT_VERIFIED),
                "No route found for GET /x": ("env", "environnement", cv.ENV_NOT_VERIFIED),
                "HTTP 503 Service Unavailable": ("bug", "bug_applicatif", cv.APPLICATION_BUG)}
    for message, (code, status, prefix) in expected.items():
        result = _result("Given", "", message)
        level = cv._level(result, expect_failure=False)
        assert level[0] == code
        final, reason = cv.final_status({"client": level})
        assert final == status and reason.startswith(prefix) and final in cv.UNJUDGED
        # Contre-épreuve : un échec hors test n'est pas concluant
        assert cv._level(result, expect_failure=True)[0] == "nv"
    # Échec du test (colonne refusée) : invalide, donc corrigé puis retiré s'il le reste
    level = cv._level(_result("Then", "", "No such path 'value'"), expect_failure=False)
    assert cv.final_status({"client": level})[0] == "invalide"


def test_failed_setup_puts_following_scenarios_in_cascade():
    parsed = cv.parse_cucumber_report(_feature_report(
        ("Contexte", [], "404 - Site non trouvé(e) avec code SITEA"),
        ("Test A", ["@agent_clientx_1_1"], "java.lang.AssertionError: expected X"),
        ("Test B", ["@agent_clientx_1_2"], "")))
    rel = "src/test/resources/cucumber/features/clientx/b.feature"
    assert parsed[(rel, "Contexte")]["statut"] == "echoue"
    cascade = parsed[(rel, "Test A")]
    assert cascade["statut"] == "cascade" and cascade["cause"] == "donnee_reference"
    assert "Contexte" in cascade["message"]
    assert parsed[(rel, "Test B")]["statut"] == "passe"
    level = cv._level(cascade, expect_failure=False)
    assert level[0] == "cascade" and cv.final_status({"client": level})[0] == "cascade"
    assert cv._level(cascade, expect_failure=True)[0] == "nv"


def test_setup_detected_by_agent_tag_and_test_cause():
    parsed = cv.parse_cucumber_report(_feature_report(
        ("Préparer les données", ["@agent_clientx_1_0"], "java.lang.NullPointerException"),
        ("Test A", ["@agent_clientx_1_1"], "java.lang.AssertionError: expected X")))
    cascade = parsed[("src/test/resources/cucumber/features/clientx/b.feature", "Test A")]
    assert cascade["statut"] == "cascade" and cascade["cause"] == "technique"


def test_failure_without_setup_failure_is_not_cascade():
    parsed = cv.parse_cucumber_report(_feature_report(
        ("Test A", [], "java.lang.AssertionError: expected X"), ("Test B", [], "java.lang.AssertionError: expected Y")))
    assert {r["statut"] for r in parsed.values()} == {"echoue"}


def test_tag_scenarios_tags_preceding_setup(tmp_path):
    rel = "src/test/resources/cucumber/features/clientx/c.feature"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text("Feature: F\n\n  Scenario: Contexte\n    Given INIT- Purge\n\n"
                    "  Scenario: Test A\n    Then le champ vaut \"X\"\n", encoding="utf-8")
    tagged = cv.tag_scenarios(str(tmp_path), [{"fichier": rel, "titre": "Test A"}], "clientx", 3)
    assert tagged == {"@agent_clientx_3_1": f"{rel}::Test A"}
    text = path.read_text(encoding="utf-8")
    assert "  @agent_clientx_3_0\n  Scenario: Contexte" in text
    assert cv.setup_tags(str(tmp_path), [rel], tagged) == {"@agent_clientx_3_0"}
    # Un second lot ne retague pas la mise en place ; le tag de mise en place est retiré du patch
    cv.tag_scenarios(str(tmp_path), [{"fichier": rel, "titre": "Test A"}], "clientx", 4)
    assert path.read_text(encoding="utf-8").count("_0") == 1
    from tools.cucumber_tools import strip_agent_tags
    assert "@agent_" not in strip_agent_tags(path.read_text(encoding="utf-8"), "clientx")


def test_unjudged_statuses_counted_apart():
    state = {"lots": [{"numero": 1, "execution": {"scenarios": {
        "@a_1": {"id": "f::A", "statut": "environnement", "motif": "", "niveaux": {"client": ["env", ""]}},
        "@a_2": {"id": "f::B", "statut": "cascade", "motif": "", "niveaux": {"client": ["cascade", ""]}},
        "@a_3": {"id": "f::C", "statut": "pertinent", "motif": "", "niveaux": {"client": ["ok", ""]}}}}}]}
    indicators = cv.verification_indicators(state)
    assert (indicators["verifies"], indicators["environnement"], indicators["cascade"]) == (1, 1, 1)
    assert indicators["taux_pertinent"] == 100


def test_not_verified_with_environment_status():
    result = cv.not_verified({"@a_1": "f::A"}, {}, "environnement non initialisé", "environnement")
    assert result["scenarios"]["@a_1"]["statut"] == "environnement"


def test_run_init_stops_on_failed_initialisation(monkeypatch):
    def fake(mode, worktree, base, build_fn, tags, env, overrides, label, sources=None):
        return {"ok": True, "message": "build SUCCESS", "build": "http://jenkins/1", "resultats": {
            ("f", "Intégration des données"): {"statut": "echoue", "step": "Given INIT-",
                                               "message": "Donneur d'ordres non trouvé(e) avec code DO-1"}}}
    monkeypatch.setattr(cv, "run_tags", fake)
    init = cv.run_init("jenkins", "", "", None, "@INIT_CLIENTX_WMS")
    assert not init["ok"] and init["message"].startswith("environnement non initialisé (@INIT_CLIENTX_WMS)")
    assert "DO-1" in init["message"]


STD_FEATURE = """Feature: Standard
  Background:
    Given CONTENT- Création des contenus
      | wmsHandlingUnit.code | status.id | quantity |
      | UM-1                 | FREE      | 2        |

  Scenario: Statut
    Then CONTENT- Contrôle contenu de stock
      | wmsHandlingUnit.code | status.id |
      | UM-1                 | FREE      |
"""


def test_table_columns_indexed_by_step():
    expressions = ["CONTENT- Création des contenus", "CONTENT- Contrôle contenu de stock"]
    columns = cv.table_columns_by_step(expressions, [STD_FEATURE])
    assert columns["CONTENT- Création des contenus"] == ["quantity", "status.id", "wmsHandlingUnit.code"]
    assert columns["CONTENT- Contrôle contenu de stock"] == ["status.id", "wmsHandlingUnit.code"]


def test_unknown_table_column_reported_with_closest(repo):
    from tests.conftest import git, write
    rel = "src/test/resources/cucumber/features/clientx/WMS/A.feature"
    write(os.path.join(repo, *rel.split("/")), "Feature: A\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    write(os.path.join(repo, *rel.split("/")),
          "Feature: A\n  Scenario: S\n    Then CONTENT- Contrôle contenu de stock\n"
          "      | wmsContent.wmsHandlingUnit.code | status.value.id |\n      | UM-1 | FREE |\n"
          "    Then un step client\n      | colonne libre |\n      | x |\n")
    reference = {"colonnes_par_step": cv.table_columns_by_step(
        ["CONTENT- Contrôle contenu de stock"], [STD_FEATURE])}
    errors = cv.check_table_columns(repo, "HEAD", [rel], reference)
    assert len(errors) == 2 and all(e.startswith(f"{rel}:3 ") for e in errors)
    assert any("« status.value.id »" in e and "plus proche « status.id »" in e for e in errors)
    assert any("« wmsContent.wmsHandlingUnit.code »" in e and "« wmsHandlingUnit.code »" in e for e in errors)
    # Table identique déjà présente à la base : non contrôlée
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "lot")
    assert cv.check_table_columns(repo, "HEAD", [rel], reference) == []
