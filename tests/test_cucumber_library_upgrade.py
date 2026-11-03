"""Bibliothèque Cucumber alignée sur la version du client : choix de version, pom, priorité Jenkins, non-régression."""
import importlib
import io
import json
import os
import sys

import pytest

from tests.conftest import CLIENT, CLIENT_TAG, git, write

from tools import cucumber_delivery as cd
from tools import cucumber_library_upgrade as clu
from tools import cucumber_profile as cp
from tools import cucumber_tools as ct

POM = """<project>
  <properties>
    <java.version>17</java.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>a-sis</groupId>
      <artifactId>Cucumber</artifactId>
      <version>26.16</version>
      <type>test-jar</type>
    </dependency>
    <dependency>
      <groupId>org.autre</groupId>
      <artifactId>lib</artifactId>
      <version>26.16</version>
    </dependency>
  </dependencies>
</project>
"""
POM_PROPERTY = POM.replace("<java.version>17</java.version>", "<java.version>17</java.version>\n    "
                           "<cucumber.version>26.16</cucumber.version>").replace(
    "<artifactId>Cucumber</artifactId>\n      <version>26.16</version>",
    "<artifactId>Cucumber</artifactId>\n      <version>${cucumber.version}</version>")
FEATURE = "src/test/resources/cucumber/features/clientx/WMS/a.feature"
VERSIONS = ["26.10", "26.16", "26.22", "26.22.1", "26.24-SNAPSHOT", "26.30"]


def test_version_choice_exact_highest_lower_or_none():
    assert clu.choose_version(VERSIONS, "26.22")["version"] == "26.22.1"                 # exacte, plus haute de la ligne
    lower = clu.choose_version(VERSIONS, "26.24")
    assert lower["version"] == "26.22.1" and "inférieure" in lower["motif"]               # jamais 26.30 ni le SNAPSHOT
    assert clu.choose_version(VERSIONS, "26.24-SNAPSHOT")["version"] == "26.22.1"
    assert clu.choose_version(VERSIONS, "25.1")["version"] == ""
    assert clu.choose_version([], "26.24")["version"] == ""
    assert clu.metadata_versions("<metadata><versioning><versions><version>26.22</version><version>26.24</version>"
                                 "</versions></versioning></metadata>") == ["26.22", "26.24"]


def test_only_the_library_version_changes_in_the_pom():
    new, old = clu.set_pom_version(POM, "26.22.1")
    assert old == "26.16" and new.count("26.22.1") == 1 and new.count("26.16") == 1    # autre dépendance intacte
    assert [a for a, b in zip(POM.splitlines(), new.splitlines()) if a != b] == ["      <version>26.16</version>"]
    assert clu.only_version_changed(POM, new)
    assert not clu.only_version_changed(POM, new.replace("<java.version>17", "<java.version>21"))
    assert not clu.only_version_changed(POM, POM.replace("<artifactId>lib</artifactId>\n      <version>26.16",
                                                         "<artifactId>lib</artifactId>\n      <version>27.0"))
    prop, old = clu.set_pom_version(POM_PROPERTY, "26.22.1")
    assert old == "26.16" and "<cucumber.version>26.22.1</cucumber.version>" in prop
    assert "${cucumber.version}" in prop and clu.only_version_changed(POM_PROPERTY, prop)
    with pytest.raises(ValueError):
        clu.set_pom_version("<project/>", "26.22")


def _document(jenkins="26.24", code="26.24"):
    sources = ([{"source": "code", "version": code, "detail": "app/pom.xml"}] if code else []) + (
        [{"source": "jenkins", "version": jenkins, "detail": "build n° 268, CUCUMBER_STANDARD_BRANCH v" + jenkins}]
        if jenkins else [])
    return {"version_standard": {"statut": "ok", "sources": sources}}


def test_jenkins_wins_over_the_application_with_a_warning():
    target = clu.target_version(_document("26.24", "26.25"))
    assert target["version"] == "26.24" and target["source"] == "jenkins"
    assert "Jenkins exécute 26.24" in target["avertissement"]
    assert clu.target_version(_document("", "26.25"))["version"] == "26.25"
    assert clu.target_version(_document("26.24", "26.24"))["avertissement"] == ""
    assert clu.gap("26.16", "26.24") and not clu.gap("26.24.1", "26.24-SNAPSHOT")
    assert clu.enabled({}) and not clu.enabled({"maj_bibliotheque": "non"})


def test_workarounds_made_useless_are_proposed():
    text = "Feature: x\n  Scenario: a\n    # TODO-VERSION: step absent de la bibliothèque 26.16\n    # And Mon step\n"
    markers = [{"fichier": "a.feature", "ligne": 3, "texte": "step absent de la bibliothèque 26.16"}]
    found = clu.workaround_proposals(markers, {"a.feature": text}, lambda step: step == "Mon step")
    assert [w["step"] for w in found] == ["And Mon step"]
    assert clu.workaround_proposals(markers, {"a.feature": text}, lambda step: False) == []


@pytest.fixture(scope="module")
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


@pytest.fixture
def client_repo(tmp_path, monkeypatch):
    """Dépôt clientx-cucumber (origin nu) : pom en 26.16, une feature ; branche de travail et cible poussées."""
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_AUTH_USER", raising=False)
    origin = str(tmp_path / f"{CLIENT}-cucumber.git")
    os.makedirs(origin)
    git(origin, "init", "-q", "--bare", "-b", "integration")
    repo = str(tmp_path / "clone")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "integration")
    for key, value in (("user.name", "test"), ("user.email", "t@example.com"), ("core.autocrlf", "false")):
        git(repo, "config", key, value)
    git(repo, "remote", "add", "origin", origin)
    write(os.path.join(repo, "pom.xml"), POM)
    write(os.path.join(repo, *FEATURE.split("/")), f"{CLIENT_TAG}\nFeature: A\n\n  Scenario: Premier\n    Given x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "push", "-q", "origin", "integration", "integration:cucumber-agent")
    git(repo, "fetch", "-q", "origin")
    with open(cp.profile_path(CLIENT), "w", encoding="utf-8") as f:
        json.dump({**_document("26.24", "26.25"),
                   "detecte": {**cp.empty_profile(), "tag_client": CLIENT_TAG, "library_version": "26.16",
                               "cucumber_repo_url": origin},
                   "saisi": {"livraison_autonome": True, "verify_branch": "cucumber-agent",
                             "branche_cible": "integration"}}, f)
    return repo, origin


def _analysis(numero, failing=()):
    ids = [f"{FEATURE}::Premier", f"{FEATURE}::Second"]
    return {"numero": numero, "run_complet": True, "build": f"https://j/{numero}",
            "resultats": {i: {"statut": "echec" if i in failing else "passe"} for i in ids}}


def test_upgrade_not_delivered_when_a_new_failure_appears(client_repo, main_module, monkeypatch):
    m = main_module
    repo, origin = client_repo
    monkeypatch.setattr(m, "latest_full_run", lambda client: _analysis(1))
    target = git(origin, "rev-parse", "integration")
    result = m.align_library(CLIENT, repo, "origin/integration", "",
                             versions=lambda: {"versions": VERSIONS, "erreur": ""},
                             check=lambda dry=False: [], fix=lambda *a: [],
                             init_launch=lambda: _analysis(2), launch=lambda: _analysis(3, [f"{FEATURE}::Second"]))
    assert result["ancienne"] == "26.16" and result["nouvelle"] == "26.22.1" and result["source"] == "jenkins"
    assert "Jenkins exécute 26.24" in result["avertissement"]
    assert not result["livree"] and "nouvel(s) échec(s)" in result["motif"]
    assert result["casse"] == [f"{FEATURE}::Second"]
    assert git(origin, "rev-parse", "integration") == target                          # pas de promotion
    pushed = git(origin, "rev-parse", "cucumber-agent")
    assert git(origin, "diff", "--stat", target, pushed).splitlines()[0].strip().startswith("pom.xml")
    assert git(origin, "show", f"{pushed}:pom.xml") == clu.set_pom_version(POM, "26.22.1")[0].rstrip("\n")
    saved = ct.load_state(CLIENT)["bibliotheque"][-1]
    assert saved["nouvelle"] == "26.22.1" and not saved["livree"]


def test_upgrade_promoted_without_regression_and_compile_errors_block_it(client_repo, main_module, monkeypatch):
    m = main_module
    repo, origin = client_repo
    monkeypatch.setattr(m, "latest_full_run", lambda client: _analysis(1, [f"{FEATURE}::Second"]))
    common = dict(versions=lambda: {"versions": VERSIONS, "erreur": ""}, fix=lambda *a: ["Steps.java : appel adapté"],
                  init_launch=lambda: _analysis(2), launch=lambda: _analysis(3, [f"{FEATURE}::Second"]))
    blocked = m.align_library(CLIENT, repo, "origin/integration", "", check=lambda dry=False: ["Steps.java:3 erreur"],
                              **common)
    assert not blocked["livree"] and "erreur(s) de compilation" in blocked["motif"]
    assert git(origin, "rev-parse", "cucumber-agent") == git(origin, "rev-parse", "integration")   # rien poussé
    done = m.align_library(CLIENT, repo, "origin/integration", "", check=lambda dry=False: [], **common)
    assert done["livree"], done["motif"]                                               # autant d'échecs : accepté
    assert "26.22.1" in git(origin, "show", "integration:pom.xml")
    # Push vers la cible : pom autorisé sans décision humaine parce que seule la version a changé
    assert [e["type"] for e in cd.events(CLIENT, ("promotion",))] == ["promotion"]
