"""Onglet « Cucumber » simplifié : états des 5 étapes, étape 2 (dépôt <client>-cucumber). Client fictif."""
import importlib
import io
import json
import os
import sys

import pytest

from tests.conftest import CLIENT, git, write

from tools import cucumber_profile as cp
from tools import cucumber_repo_setup as rs
from tools import cucumber_simple as cs
from tools import cucumber_tools as ct

DEDUCTION = {"version_client": "1.3.1", "standard": {"version": "26.24"}}


# ---------------------------------------------------------------------------
# États : une étape obligatoire non OK bloque la suivante ; l'étape 3 est optionnelle
# ---------------------------------------------------------------------------

def test_project_step_ok_when_standard_known():
    step = cs.project_step(DEDUCTION, True)
    assert step == {"etat": "ok", "resultat": "Version client 1.3.1 → Standard 26.24."}
    assert cs.project_step({"version_client": "1.3.1"}, True)["etat"] == "echec"
    assert cs.project_step({}, False)["etat"] == "a_faire"


def test_steps_unlock_in_order_and_database_is_optional():
    project = cs.project_step(DEDUCTION, True)
    steps = {s["cle"]: s for s in cs.states(project, {})}
    assert steps["depot"]["etat"] == "a_faire"
    assert [steps[k]["etat"] for k in ("base", "init", "tests")] == ["bloquee"] * 3
    records = {"depot": {"etat": "ok", "resultat": "compilation réussie"}}
    steps = {s["cle"]: s for s in cs.states(project, records)}
    assert steps["base"]["etat"] == "a_faire" and steps["base"]["optionnelle"]
    assert "Optionnelle" in steps["base"]["resultat"]
    assert steps["init"]["etat"] == "a_faire"                                    # base non faite : init débloquée
    assert steps["tests"]["etat"] == "bloquee"
    records["init"] = {"etat": "ok"}
    assert {s["cle"]: s for s in cs.states(project, records)}["tests"]["etat"] == "a_faire"
    blocked = {s["cle"]: s for s in cs.states(cs.project_step({}, True), records)}
    assert blocked["depot"]["etat"] == "bloquee" and blocked["init"]["etat"] == "bloquee"
    assert cs.label(steps["depot"], CLIENT) == f"2. Dépôt {CLIENT}-cucumber — OK"


def test_feature_list_by_module_without_excluded_modules():
    inventory = {"specifiques": [
        {"id": "a", "libelle": "app · reception-order", "dossier": "reception", "depot": "app"},
        {"id": "b", "libelle": "app · shipping-label", "dossier": "shipping", "depot": "app"}]}
    choices = cs.feature_choices(inventory, {})
    assert [c["id"] for c in choices] == ["a", "b"] and all(" — " in c["libelle"] for c in choices)


# ---------------------------------------------------------------------------
# Dépôt : branche de la version, contenu minimal, alignement
# ---------------------------------------------------------------------------

def test_version_branch_follows_the_repository_convention():
    assert rs.version_branch("1.3.1") == "v1.3"                                         # aucune branche de version
    assert rs.version_branch("1.21-SNAPSHOT") == "v1.21"
    assert rs.version_branch("") == ""
    assert rs.version_branch("1.3.1", ["integration", "1.3", "agent"]) == "1.3"        # créée à la main : réutilisée
    assert rs.version_branch("1.3.1", ["v1.3"]) == "v1.3"
    assert rs.version_branch("1.3.1", ["1.3.1"]) == "1.3.1"                             # même version : pas de doublon
    assert rs.version_branch("1.4", ["1.2", "1.3"]) == "1.4"                            # convention sans « v »
    assert rs.version_branch("1.4", ["integration"]) == "v1.4"


def test_scaffold_has_library_runner_tree_and_gitignore():
    files = rs.scaffold_files(CLIENT, "26.22.1", "@A_REVOIR")
    assert "<version>26.22.1</version>" in files["pom.xml"] and f"<artifactId>{CLIENT}-cucumber" in files["pom.xml"]
    runner = files[rs.RUNNER_PATH]
    assert 'glue = "cucumber"' in runner and "json:target/cucumber-reports/Cucumber.json" in runner
    assert 'tags = "not @A_REVOIR"' in runner
    assert f"src/test/resources/cucumber/features/{CLIENT}/.gitkeep" in files and "target/" in files[".gitignore"]


def test_existing_repository_alignment_plan():
    pom = rs.scaffold_files(CLIENT, "26.16", "@A_REVOIR")["pom.xml"]
    runner = 'package cucumber;\n@CucumberOptions(\n        tags = "not @OLD",\n        plugin = {"pretty"}\n)\n'
    plan = rs.align_plan({"pom.xml": pom, rs.RUNNER_PATH: runner}, "26.22.1", "@A_REVOIR")
    assert "<version>26.22.1</version>" in plan["files"]["pom.xml"]
    aligned = plan["files"][rs.RUNNER_PATH]
    assert '"(not @OLD) and not @A_REVOIR"' in aligned and "json:target/cucumber-reports/Cucumber.json" in aligned
    assert "-            <version>26.16</version>" in plan["diff"] and len(plan["motifs"]) == 2
    done = rs.align_plan({"pom.xml": plan["files"]["pom.xml"], rs.RUNNER_PATH: aligned}, "26.22.1", "@A_REVOIR")
    assert done["files"] == {} and done["diff"] == ""
    assert rs.RUNNER_PATH in rs.align_plan({"pom.xml": pom}, "26.16", "@A_REVOIR")["files"]   # runner absent


# ---------------------------------------------------------------------------
# Étape 2 de bout en bout : Bitbucket simulé, origin git local
# ---------------------------------------------------------------------------

class FakeBitbucket:
    def __init__(self, exists: bool, admin: bool, origin: str):
        self.exists, self.admin, self.origin, self.calls = exists, admin, origin, []

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path))

        class Response:
            def __init__(self, status, payload=None):
                self.status_code, self._payload, self.text = status, payload or {}, ""

            def json(self):
                return self._payload
        if method == "GET" and path.endswith(f"/repos/{CLIENT}-cucumber"):
            return Response(200 if self.exists else 404)
        if method == "GET" and path == "/rest/api/1.0/projects":
            return Response(200, {"values": [{"key": "CLX"}] if self.admin else []})
        if method == "POST":
            self.exists = True
            git(self.origin, "init", "-q", "--bare", "-b", "integration")
            return Response(201, {"links": {"clone": [{"name": "http", "href": self.origin}]}})
        return Response(500)


@pytest.fixture(scope="module")
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


@pytest.fixture
def workspace(tmp_path, monkeypatch, main_module):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.delenv("AGENT_AUTH_USER", raising=False)
    monkeypatch.setattr(main_module.client_version, "saved", lambda client: {"deduction": DEDUCTION})
    os.makedirs(os.path.dirname(cp.profile_path(CLIENT)), exist_ok=True)
    with open(cp.profile_path(CLIENT), "w", encoding="utf-8") as f:
        json.dump({"detecte": cp.empty_profile(), "saisi": {}}, f)
    root = tmp_path / "client_root"
    root.mkdir()
    origin = str(tmp_path / f"{CLIENT}-cucumber.git")
    os.makedirs(origin)
    return str(root), origin


def _helpers(fake, origin, compiled=True):
    return {"call": fake, "versions": lambda: {"versions": ["26.16", "26.22.1", "26.30"], "erreur": ""},
            "compile": lambda w: {"ok": compiled, "erreurs": [] if compiled else ["Steps.java:1:1 erreur"]},
            "clone_url": origin}


def _record():
    return ct.load_state(CLIENT)["simple"]["depot"]


def test_missing_repository_without_rights_stops_clearly(workspace, main_module):
    root, origin = workspace
    fake = FakeBitbucket(exists=False, admin=False, origin=origin)
    code = main_module.run_simple_depot(CLIENT, "https://git.example/projects/CLX", root, "creer", "integration",
                                        _helpers(fake, origin))
    assert code == 1 and "pas le droit d'en créer" in _record()["resultat"]
    assert not any(m == "POST" for m, _ in fake.calls)


def test_missing_repository_created_with_minimal_content_on_version_branch(workspace, main_module):
    root, origin = workspace
    fake = FakeBitbucket(exists=False, admin=True, origin=origin)
    check = main_module.run_simple_depot(CLIENT, "https://git.example/projects/CLX", root, "verifier",
                                         "integration", _helpers(fake, origin))
    assert check == 0 and _record()["etat"] == "a_faire" and _record()["detail"]["plan"]["action"] == "creer"
    assert "branche v1.3" in _record()["resultat"] and "26.22.1" in _record()["resultat"]   # plus proche ≤ 26.24
    code = main_module.run_simple_depot(CLIENT, "https://git.example/projects/CLX", root, "creer", "integration",
                                        _helpers(fake, origin))
    assert code == 0 and _record()["etat"] == "ok", _record()
    assert git(origin, "rev-parse", "integration") == git(origin, "rev-parse", "v1.3")     # même commit
    files = git(origin, "ls-tree", "-r", "--name-only", "v1.3").splitlines()
    assert "pom.xml" in files and rs.RUNNER_PATH in files and ".gitignore" in files
    assert "<version>26.22.1</version>" in git(origin, "show", "v1.3:pom.xml")


def test_existing_repository_branch_then_alignment_validated(workspace, main_module, tmp_path):
    root, origin = workspace
    git(origin, "init", "-q", "--bare", "-b", "integration")
    seed = str(tmp_path / "seed")
    os.makedirs(seed)
    git(seed, "init", "-q", "-b", "integration")
    for key, value in (("user.name", "t"), ("user.email", "t@example.com"), ("core.autocrlf", "false")):
        git(seed, "config", key, value)
    for rel, text in rs.scaffold_files(CLIENT, "26.16", "@OLD").items():
        write(os.path.join(seed, *rel.split("/")), text)
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "base")
    git(seed, "remote", "add", "origin", origin)
    git(seed, "push", "-q", "origin", "integration", "integration:1.2")                 # convention sans « v »
    fake = FakeBitbucket(exists=True, admin=True, origin=origin)
    run = lambda action, compiled=True: main_module.run_simple_depot(  # noqa: E731
        CLIENT, "https://git.example/projects/CLX", root, action, "integration", _helpers(fake, origin, compiled))
    assert run("verifier") == 0 and _record()["detail"]["plan"]["action"] == "branche"
    assert run("branche") == 0 and _record()["detail"]["plan"]["action"] == "aligner"
    assert "26.22.1" in _record()["detail"]["plan"]["diff"]
    assert git(origin, "rev-parse", "1.3") == git(origin, "rev-parse", "integration")       # rien poussé avant validation
    assert "v1.3" not in git(origin, "branch", "--list")
    assert run("aligner", compiled=False) == 1 and _record()["etat"] == "echec"            # compilation en échec
    assert "<version>26.22.1</version>" in git(origin, "show", "1.3:pom.xml")
    assert git(origin, "rev-parse", "integration") != git(origin, "rev-parse", "1.3")      # intégration intacte
    assert run("verifier") == 0 and _record()["etat"] == "ok"
