"""Phase 4 : run Jenkins automatique (push contrôlé, paramètres du profil, suivi, import), Jenkins simulé."""
import os
import subprocess

import pytest

from tests.conftest import CLIENT, git, write
from tools import cucumber_journey as cj
from tools import cucumber_run as cr
from tools import cucumber_tools as ct
from tools import jenkins_tools

BRANCH = "cucumber-agent"
PROFILE = {"run_job": "DOSSIER/JOB-CLIENT", "verify_branch": BRANCH, "verify_branch_param": "BRANCHE"}


@pytest.fixture
def jenkins(tmp_path, repo, monkeypatch):
    """Dépôt avec un origin nu, Jenkins simulé : paramètres acceptés, lancements reçus."""
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    remote = str(tmp_path / "origin.git")
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
    write(os.path.join(repo, "a.feature"), "Feature: A\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "remote", "add", "origin", remote)
    calls = {"params": [], "accepted": {"BRANCHE": "", "TAGS": "", "CUCUMBER_SPEC_COMMIT": ""}}
    monkeypatch.setattr(jenkins_tools, "jenkins_configured", lambda: True)
    monkeypatch.setattr(jenkins_tools, "job_parameters", lambda job: calls["accepted"])
    monkeypatch.setattr(jenkins_tools, "trigger_job", lambda job, params: calls["params"].append(params) or "q/1")
    monkeypatch.setattr(jenkins_tools, "wait_build_result",
                        lambda queue, timeout: {"url": "https://jenkins/job/x/7", "result": "UNSTABLE"})
    return repo, remote, calls


def test_preflight_lists_what_is_missing(jenkins, monkeypatch):
    repo, _, calls = jenkins
    monkeypatch.setattr(jenkins_tools, "jenkins_configured", lambda: False)
    monkeypatch.delenv("CUCUMBER_RUN_JOB", raising=False)
    errors = " | ".join(cr.preflight({}, "", "")["erreurs"])
    assert "JENKINS_URL" in errors and "job Jenkins" in errors and "branche" in errors and "worktree" in errors
    monkeypatch.setattr(jenkins_tools, "jenkins_configured", lambda: True)
    calls["accepted"] = {"TAGS": ""}
    assert "n'a pas de paramètre BRANCHE" in cr.preflight(PROFILE, repo)["erreurs"][0]
    calls["accepted"] = {"BRANCHE": ""}
    assert "n'a pas de paramètre TAGS" in cr.preflight(PROFILE, repo, "@x")["erreurs"][0]
    assert cr.preflight(PROFILE, repo)["erreurs"] == []
    monkeypatch.setenv("CUCUMBER_RUN_JOB", "JOB-REPLI")
    assert cr.run_job({}) == "JOB-REPLI"


def test_launch_pushes_checks_the_commit_and_records(jenkins):
    repo, remote, calls = jenkins
    write(os.path.join(repo, "b.feature"), "Feature: B\n")       # modification non commitée : commitée et poussée
    run = cr.launch(CLIENT, repo, PROFILE, "@agent_x_1", log=lambda line: None)
    head = git(repo, "rev-parse", "HEAD")
    assert run["ok"] and run["resultat"] == "UNSTABLE" and run["commit"] == head
    assert git(remote, "rev-parse", BRANCH) == head                 # la branche lue par le job a le commit
    assert calls["params"] == [{"BRANCHE": BRANCH, "TAGS": "@agent_x_1", "CUCUMBER_SPEC_COMMIT": head}]
    assert cr.last_run(ct.load_state(CLIENT))["build"] == "https://jenkins/job/x/7"


def test_nothing_is_launched_when_the_branch_cannot_be_pushed(jenkins, tmp_path):
    repo, remote, calls = jenkins
    # La branche distante a avancé ailleurs : le push (jamais forcé) est refusé
    other = str(tmp_path / "other")
    subprocess.run(["git", "clone", "-q", remote, other], check=True)
    git(other, "config", "user.email", "x@example.com")
    git(other, "config", "user.name", "x")
    git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
    git(other, "fetch", "-q", "origin")
    git(other, "checkout", "-q", "-b", BRANCH, f"origin/{BRANCH}")
    write(os.path.join(other, "c.feature"), "Feature: C\n")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "ailleurs")
    git(other, "push", "-q", "origin", BRANCH)
    write(os.path.join(repo, "d.feature"), "Feature: D\n")
    run = cr.launch(CLIENT, repo, PROFILE, log=lambda line: None)
    assert not run["ok"] and run["message"].startswith("rien n'est lancé") and calls["params"] == []
    step = cj.execution_step(CLIENT, {}, {"saisi": PROFILE}, ct.load_state(CLIENT))
    assert step["etat"] == "attention" and "non abouti" in step["resume"]


def test_main_imports_the_finished_build(jenkins, monkeypatch):
    import importlib
    import io
    import sys
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        main = importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved
    imported = []
    monkeypatch.setattr(main.cucumber_run, "launch", lambda *a, **k: {"ok": True, "message": "build terminé",
                                                                      "build": "https://jenkins/job/x/7"})
    monkeypatch.setattr(main, "run_cucumber_import", lambda client, paths, url, project: imported.append(url) or 0)
    assert main.run_cucumber_jenkins(CLIENT, "", "") == 0 and imported == ["https://jenkins/job/x/7"]
    monkeypatch.setattr(main.cucumber_run, "launch", lambda *a, **k: {"ok": False, "message": "non", "build": ""})
    assert main.run_cucumber_jenkins(CLIENT, "", "") == 2


def test_journey_proposes_the_automatic_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))
    write(ct.patch_path(CLIENT), "diff")
    assert cj.execution_step(CLIENT, {}, {"saisi": PROFILE}, {})["action"] == "Pousser et lancer le run Jenkins"
    assert cj.execution_step(CLIENT, {}, {}, {})["action"].startswith("Lancer le job Jenkins puis importer")
