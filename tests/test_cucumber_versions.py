"""Écarts de version du Cucumber Standard : steps utilisés comparés entre la bibliothèque du client et le Standard aligné."""
import os

import pytest

from tests.conftest import git, write
from tools import cucumber_checks as cc
from tools import cucumber_tools as ct
from tools import cucumber_versions as cvs

JAVA = "src/test/java/com/std/cucumber"
STEPS_V1 = """package com.std.cucumber.steps;

public class StockSteps {
    @Given("STOCK- je crée le contenant {string}")
    public void createContainer(String code) {
        HandlingUnit unit = new HandlingUnit();
        request.post("/api/v1/handling-units");
    }

    @When("STOCK- je déplace le contenant {string}")
    public void move(String code) {
        request.put("/api/v1/moves");
    }

    @Then("STOCK- ancien contrôle {string}")
    public void oldCheck(String code) {
    }
}
"""
STEPS_V2 = """package com.std.cucumber.steps;

public class StockSteps {
    @Given("STOCK- je crée le contenant {string}")
    public void createContainer(String code, DataTable table) {
        HandlingUnit unit = new HandlingUnit();
        request.post("/api/v2/handling-units");
    }

    @When("STOCK- je déplace le contenant {string}")
    public void move(String code) {
        request.put("/api/v1/moves");
    }

    @Then("STOCK- nouveau contrôle {string}")
    public void newCheck(String code) {
    }
}
"""
POJO_V1 = """package com.std.cucumber.pojo;

public class HandlingUnit {
    private String code;
    @JsonProperty("handlingUnitType")
    private String type;
    private static final String KIND = "HU";
}
"""
POJO_V2 = POJO_V1.replace("    private static final", "    private Integer priority;\n    private static final")
FEATURE = ("Feature: Stock\n  Scenario: A\n    Given STOCK- je crée le contenant \"C1\"\n"
           "    When STOCK- je déplace le contenant \"C1\"\n    Then STOCK- ancien contrôle \"OK\"\n"
           "    And STOCK- nouveau contrôle \"OK\"\n    And step propre au client\n")


@pytest.fixture
def std_repo(repo, tmp_path, monkeypatch):
    """Dépôt Standard : v26.16 (bibliothèque du client) taguée, puis HEAD aligné sur le serveur."""
    monkeypatch.setattr(cvs, "STD_CACHE_DIR", str(tmp_path / "cache"))
    write(os.path.join(repo, JAVA, "steps", "StockSteps.java"), STEPS_V1)
    write(os.path.join(repo, JAVA, "pojo", "HandlingUnit.java"), POJO_V1)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "v26.16")
    git(repo, "tag", "v26.16")
    write(os.path.join(repo, JAVA, "steps", "StockSteps.java"), STEPS_V2)
    write(os.path.join(repo, JAVA, "pojo", "HandlingUnit.java"), POJO_V2)
    git(repo, "commit", "-qam", "v26.21")
    return repo


def test_compare_versions_is_ordered():
    assert cc.compare_versions("26.9", "26.16") == -1 and cc.compare_versions("26.16-SNAPSHOT", "26.16") == 0
    assert cc.compare_versions("26.21.1", "26.21") == 1
    assert "ANTÉRIEURE" in cc.version_gap("26.16", "26.21", "26.21")
    assert "POSTÉRIEURE" in cc.version_gap("26.30", "26.21", "26.21")
    # Version attendue : celle du serveur ; Standard de la branche non aligné signalé
    gap = cc.version_gap("26.21", "26.16", "26.21")
    assert "ANTÉRIEURE" not in gap and "catalogue de génération non aligné" in gap
    assert cc.version_gap("26.21-SNAPSHOT", "26.21", "26.21") == ""


def test_pojo_fields_reads_instance_fields_and_json_names():
    assert cvs.pojo_fields(POJO_V2) == ["code", "handlingUnitType", "priority"]


def test_library_ref_finds_version_tag(std_repo):
    assert cvs.library_ref(std_repo, "26.16-SNAPSHOT") == "refs/tags/v26.16"
    assert cvs.library_ref(std_repo, "26.99") == "" and cvs.library_ref(std_repo, "") == ""


def test_version_report_lists_used_step_differences(std_repo):
    report = cvs.version_report(std_repo, "26.16", [FEATURE])
    assert report["ref_bibliotheque"] == "refs/tags/v26.16" and not report["avertissement"]
    found = {(d["nature"], d["step"]) for d in report["ecarts"]}
    assert found == {("signature", 'STOCK- je crée le contenant "C1"'),
                     ("endpoint", 'STOCK- je crée le contenant "C1"'),
                     ("pojo", 'STOCK- je crée le contenant "C1"'),
                     ("absent_standard", 'STOCK- ancien contrôle "OK"'),
                     ("absent_bibliotheque", 'STOCK- nouveau contrôle "OK"')}
    details = {d["nature"]: d["detail"] for d in report["ecarts"]}
    assert "createContainer(String code) → createContainer(String code, DataTable table)" in details["signature"]
    assert "POST /api/v1/handling-units" in details["endpoint"] and "POST /api/v2/handling-units" in details["endpoint"]
    assert details["pojo"] == "HandlingUnit : champs absents de la bibliothèque : priority"
    assert all(cvs.difference_line(d) for d in report["ecarts"])
    # Instantanés en cache par commit
    assert len(os.listdir(cvs.STD_CACHE_DIR)) == 2
    missing = cvs.version_report(std_repo, "26.99", [FEATURE])
    assert missing["ecarts"] == [] and "introuvable" in missing["avertissement"]


def test_todo_version_markers(repo):
    write(os.path.join(repo, "a.feature"), "Feature: A\n  # TODO-VERSION: colonne absente en 26.16\n")
    write(os.path.join(repo, "B.java"), "class B {}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    assert cvs.todo_markers(repo, "HEAD") == [{"fichier": "a.feature", "ligne": 2, "texte": "colonne absente en 26.16"}]
    assert cvs.todo_markers(repo) == cvs.todo_markers(repo, "HEAD")


def test_health_report_lists_step_differences(std_repo, tmp_path, monkeypatch):
    client = str(tmp_path / "client")
    os.makedirs(client)
    git(client, "init", "-q", "-b", "integration")
    git(client, "config", "user.name", "test")
    git(client, "config", "user.email", "test@example.com")
    write(os.path.join(client, "src/test/resources/cucumber/features/x/A.feature"),
          "@X\n" + FEATURE.replace("And step propre", "# TODO-VERSION: à revoir\n    And step propre"))
    git(client, "add", "-A")
    git(client, "commit", "-qm", "base")
    write(os.path.join(std_repo, "pom.xml"), "<project><version>26.21</version></project>")
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache_client"))
    monkeypatch.setattr(cc, "WORKSPACE_DIR", str(tmp_path))
    report = cc.health_report("x", client, "HEAD", std_repo, "", "",
                              {"features_root": "src/test/resources/cucumber/features/x/", "client_tag": "@X",
                               "library_version": "26.16", "server_version": "26.21"})
    assert "ANTÉRIEURE" in report["ecart_version"] and len(report["ecarts_steps"]["ecarts"]) == 5
    assert [m["texte"] for m in report["todo_version"]] == ["à revoir"]
    with open(report["fichier"], encoding="utf-8") as f:
        text = f.read()
    assert "serveur 26.21" in text and "Écarts des steps utilisés" in text and "Marqueurs TODO-VERSION : 1" in text
