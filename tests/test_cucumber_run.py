"""
Run Jenkins automatique du dépôt client (push contrôlé, rôles des paramètres du job, filtre de tags dans les
arguments, secrets masqués, version testée, suivi, import), Jenkins et dépôt Maven simulés.
"""
import json
import os
import subprocess

import pytest

from tests.conftest import CLIENT, git, write
from tools import cucumber_job as cjob
from tools import cucumber_journey as cj
from tools import cucumber_profile as cp
from tools import cucumber_run as cr
from tools import cucumber_tools as ct
from tools import cucumber_verify as cv
from tools import jenkins_tools, secret_tools

BRANCH = "cucumber-agent"
PROFILE = {"run_job": "DOSSIER/JOB-CLIENT", "verify_branch": BRANCH}
LOGIN, PASSWORD = "jdupont.agent", "S3cr3t-Pwd!"
ARGLINE = f"-Dapi.login={LOGIN} -Dapi.password={PASSWORD} -Dcucumber.filter.tags='@old' -Dlocale=fr"
# Paramètres du job réel (noms propres au job : jamais en dur dans le code, détectés)
DEFINITIONS = {
    "CUCUMBER_SPEC_REPOSITORY": {"type": "StringParameterDefinition", "defaut": "autre-cucumber"},
    "CUCUMBER_SPEC_BRANCH": {"type": "StringParameterDefinition", "defaut": "integration"},
    "CUCUMBER_STANDARD_BRANCH": {"type": "StringParameterDefinition", "defaut": "develop"},
    "ADDITIONAL_ARGLINE": {"type": "StringParameterDefinition", "defaut": ARGLINE},
    "ADDITIONAL_SPEC_TAGS": {"type": "StringParameterDefinition", "defaut": ""},
    "wait": {"type": "BooleanParameterDefinition", "defaut": True},
    "COMPONENT_OWNER": {"type": "StringParameterDefinition", "defaut": "com.client"},
    "COMPONENT_NAME": {"type": "StringParameterDefinition", "defaut": "client-app"},
    "COMPONENT_VERSION": {"type": "StringParameterDefinition", "defaut": "2.1-SNAPSHOT"},
    "DEPLOY_TOKEN": {"type": "PasswordParameterDefinition", "defaut": None},
}
FEATURE = "src/test/resources/cucumber/features/client/a.feature"
CONSOLE = (f"[INFO] Scanning for projects\nScenario: Premier # {FEATURE}:4\nScenario: Second # {FEATURE}:7\n"
           f"echo -Dapi.password={PASSWORD} -Dapi.login={LOGIN}\n"
           "[INFO] Tests run: 2, Failures: 0, Errors: 0, Skipped: 0\n")


@pytest.fixture
def jenkins(tmp_path, repo, monkeypatch):
    """Dépôt avec un origin nu, Jenkins simulé : définitions du job, lancements reçus, console du build."""
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    remote = str(tmp_path / "origin.git")
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
    write(os.path.join(repo, *FEATURE.split("/")),
          "@feature_a\nFeature: A\n\n  Scenario: Premier\n    Given x\n\n  Scenario: Second\n    Given y\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "remote", "add", "origin", remote)
    calls = {"params": [], "definitions": dict(DEFINITIONS), "console": CONSOLE}
    monkeypatch.setattr(jenkins_tools, "jenkins_configured", lambda: True)
    monkeypatch.setattr(jenkins_tools, "job_definitions", lambda job: calls["definitions"])
    monkeypatch.setattr(jenkins_tools, "trigger_job", lambda job, params: calls["params"].append(params) or "q/1")
    monkeypatch.setattr(jenkins_tools, "wait_build_result",
                        lambda queue, timeout: {"url": "https://jenkins/job/x/7", "result": "UNSTABLE",
                                                "artifacts": []})
    monkeypatch.setattr(jenkins_tools, "build_bytes", lambda url, rel: calls["console"].encode()
                        if rel == "consoleText" else b"")
    monkeypatch.delenv("MAVEN_REPOSITORY_URL", raising=False)
    return repo, remote, calls


def test_roles_are_detected_on_the_job_parameters():
    roles, warnings = cjob.detect_roles(DEFINITIONS)
    assert roles == {"branche": "CUCUMBER_SPEC_BRANCH", "depot": "CUCUMBER_SPEC_REPOSITORY",
                     "arguments": "ADDITIONAL_ARGLINE", "attente": "wait", "commit": "", "environnement": "",
                     "composant_proprietaire": "COMPONENT_OWNER", "composant_nom": "COMPONENT_NAME",
                     "composant_version": "COMPONENT_VERSION"}
    assert warnings == []
    # Deux candidats : rôle laissé vide, ambiguïté signalée ; le profil tranche
    ambiguous = {**DEFINITIONS, "FEATURE_BRANCH": {"type": "StringParameterDefinition", "defaut": ""}}
    roles, warnings = cjob.detect_roles(ambiguous)
    assert roles["branche"] == "" and "ambigu" in warnings[0]
    roles, warnings = cjob.effective_roles({"job_roles": {"branche": "FEATURE_BRANCH"}}, ambiguous)
    assert roles["branche"] == "FEATURE_BRANCH" and warnings == []
    roles, warnings = cjob.effective_roles({"job_roles": {"branche": "ABSENT"}}, DEFINITIONS)
    assert roles["branche"] == "CUCUMBER_SPEC_BRANCH" and "ABSENT" in warnings[0]


def test_build_parameters_keep_the_job_configuration_and_add_the_tag_filter():
    roles = cjob.detect_roles(DEFINITIONS)[0]
    params = cjob.build_parameters(DEFINITIONS, roles, BRANCH, "client-cucumber", "@feature_a or @x", "abc123")
    assert params["CUCUMBER_SPEC_BRANCH"] == BRANCH and params["CUCUMBER_SPEC_REPOSITORY"] == "client-cucumber"
    assert params["wait"] == "false" and params["CUCUMBER_STANDARD_BRANCH"] == "develop"
    assert params["ADDITIONAL_SPEC_TAGS"] == "" and "DEPLOY_TOKEN" not in params
    # Valeur actuelle + filtre (l'ancien filtre est remplacé, les autres options gardées)
    assert params["ADDITIONAL_ARGLINE"] == (f"-Dapi.login={LOGIN} -Dapi.password={PASSWORD} -Dlocale=fr "
                                            f"-Dcucumber.filter.tags='@feature_a or @x'")
    assert cjob.with_tag_filter(ARGLINE, "") == f"-Dapi.login={LOGIN} -Dapi.password={PASSWORD} -Dlocale=fr"


def test_job_credentials_are_never_shown_nor_stored(jenkins):
    repo, _, calls = jenkins
    run = cr.launch(CLIENT, repo, PROFILE, "@feature_a", log=lambda line: None)
    shown = json.dumps(run) + json.dumps(ct.load_state(CLIENT)) + json.dumps(cp.load_profile(CLIENT))
    assert LOGIN not in shown and PASSWORD not in shown
    assert run["parametres"]["ADDITIONAL_ARGLINE"] == "**** -Dcucumber.filter.tags='@feature_a'"
    # Console du build : identifiants masqués par le masquage commun (import, verification, connaissances)
    masked = secret_tools.mask_secrets(CONSOLE.replace("-Dapi.login=", "login: "))[0]
    assert LOGIN not in masked and PASSWORD not in masked


def test_preflight_lists_what_is_missing(jenkins, monkeypatch):
    repo, _, calls = jenkins
    monkeypatch.setattr(jenkins_tools, "jenkins_configured", lambda: False)
    monkeypatch.delenv("CUCUMBER_RUN_JOB", raising=False)
    errors = " | ".join(cr.preflight({}, "")["erreurs"])
    assert "JENKINS_URL" in errors and "job Jenkins" in errors and "branche" in errors and "worktree" in errors
    monkeypatch.setattr(jenkins_tools, "jenkins_configured", lambda: True)
    assert "branche d'intégration" in cr.preflight({**PROFILE, "verify_branch": "integration"}, repo,
                                                   base_branch="integration")["erreurs"][0]
    calls["definitions"] = {k: v for k, v in DEFINITIONS.items() if k != "ADDITIONAL_ARGLINE"}
    assert "« arguments »" in cr.preflight(PROFILE, repo)["erreurs"][0]
    calls["definitions"] = DEFINITIONS
    assert cr.preflight(PROFILE, repo, CLIENT)["erreurs"] == []
    # Rôles détectés enregistrés dans le profil client, appliqués par le profil effectif
    profile = cp.effective_profile(cp.load_profile(CLIENT))
    assert profile["job_roles"]["arguments"] == "ADDITIONAL_ARGLINE" and profile["sources"]["job_roles"] == "detecte"
    monkeypatch.setenv("CUCUMBER_RUN_JOB", "JOB-REPLI")
    assert cr.run_job({}) == "JOB-REPLI"


def test_launch_pushes_checks_the_commit_runs_one_feature_and_checks_the_count(jenkins):
    repo, remote, calls = jenkins
    write(os.path.join(repo, "b.txt"), "b\n")                    # modification non commitée : commitée et poussée
    run = cr.launch(CLIENT, repo, PROFILE, "@feature_a", log=lambda line: None, repository="client-cucumber",
                    base_branch="integration")
    head = git(repo, "rev-parse", "HEAD")
    assert run["ok"] and run["resultat"] == "UNSTABLE" and run["commit"] == head
    assert git(remote, "rev-parse", BRANCH) == head                 # commit en tête de la branche distante
    sent = calls["params"][0]
    assert sent["CUCUMBER_SPEC_BRANCH"] == BRANCH and sent["wait"] == "false"
    assert sent["ADDITIONAL_ARGLINE"].endswith("-Dcucumber.filter.tags='@feature_a'")
    assert run["selection"] == ""                                   # 2 tests exécutés = les 2 de la feature
    calls["console"] = CONSOLE.replace("Tests run: 2", "Tests run: 40")
    run = cr.launch(CLIENT, repo, PROFILE, "@feature_a", log=lambda line: None)
    assert "40 test(s) exécuté(s) pour 2 attendu(s)" in run["selection"]
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


class _Response:
    def __init__(self, status, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}


def test_tested_version_is_compared_to_the_task_commits(jenkins, tmp_path, monkeypatch):
    repo, _, _ = jenkins
    monkeypatch.setenv("MAVEN_REPOSITORY_URL", "https://nexus/repo")
    metadata = (b"<metadata><versioning><lastUpdated>20261020100000</lastUpdated></versioning></metadata>")
    urls = []
    monkeypatch.setattr(cjob.requests, "get", lambda url, **k: urls.append(url) or (
        _Response(200, metadata) if url.endswith("2.1-SNAPSHOT/maven-metadata.xml") else _Response(404)))
    artifact = cjob.tested_artifact(cjob.build_parameters(DEFINITIONS, cjob.detect_roles(DEFINITIONS)[0], BRANCH),
                                    cjob.detect_roles(DEFINITIONS)[0])
    published = cjob.publication(artifact)
    assert published["date"] == "2026-10-20T10:00:00+00:00"
    assert "https://nexus/repo/com/client/client-app/2.1-SNAPSHOT/maven-metadata.xml" in urls
    # Deux tâches : l'une commitée avant la publication, l'autre après
    app = str(tmp_path / "clients" / "client-app")
    os.makedirs(app)
    git(app, "init", "-q")
    git(app, "config", "user.name", "t")
    git(app, "config", "user.email", "t@example.com")
    for name, date in (("Old.java", "2026-10-01T09:00:00+00:00"), ("New.java", "2026-10-25T09:00:00+00:00")):
        write(os.path.join(app, name), "class X {}\n")
        git(app, "add", "-A")
        subprocess.run(["git", "-C", app, "commit", "-qm", name], check=True,
                       env={**os.environ, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date})
    inventory = {"specifiques": [{"id": "old", "fichiers": [{"repo": "client-app", "path": "Old.java"}]},
                                 {"id": "new", "fichiers": [{"repo": "client-app", "path": "New.java"}]}]}
    tasks = cjob.deployment(inventory, str(tmp_path / "clients"), artifact, published)
    assert tasks["old"]["etat"] == "deploye" and tasks["new"]["etat"] == "non_deploye"
    assert "postérieur à la publication de client-app 2.1-SNAPSHOT" in tasks["new"]["preuve"]
    line = cjob.deployment_line(cjob.record_deployment(CLIENT, artifact, published, tasks))
    assert line.startswith("Version testée : com.client:client-app 2.1-SNAPSHOT — publiée le 20/10/2026 10:00 UTC")
    assert "1 tâche(s)" in line


def test_verification_runs_on_the_work_branch_without_patch(jenkins, monkeypatch):
    repo, remote, calls = jenkins
    monkeypatch.setenv("CUCUMBER_VERIFY_JOB", "CUCUMBER-CUSTOMER")
    monkeypatch.setenv("JENKINS_URL", "https://jenkins")
    monkeypatch.setattr(jenkins_tools, "job_parameters", lambda job: {k: v["type"] for k, v in DEFINITIONS.items()})
    mode, message = cv.execution_mode(PROFILE, "integration")
    assert mode == "branche" and "CUCUMBER_SPEC_BRANCH" in message
    assert cv.execution_mode({**PROFILE, "verify_branch": "integration"}, "integration")[0] != "branche"
    original = open(os.path.join(repo, *FEATURE.split("/")), encoding="utf-8").read()
    mutated = original.replace("Given x", "Given z")
    pushed = {}
    real_push = cv.cucumber_job.jenkins_tools.trigger_job

    def trigger(job, params):
        pushed["contenu"] = git(remote, "show", f"{BRANCH}:{FEATURE}")
        pushed["tete"] = git(remote, "rev-parse", BRANCH)
        return real_push(job, params)
    monkeypatch.setattr(jenkins_tools, "trigger_job", trigger)
    run = cv.run_tags("branche", repo, "", None, "@feature_a", "", {FEATURE: mutated}, "lot1-mutation",
                      cv.branch_sources(PROFILE, "client-cucumber"))
    assert run["ok"] and "console" in run["message"]
    assert set(t for _, t in run["resultats"]) == {"Premier", "Second"}
    assert "Given z" in pushed["contenu"]                           # mutation poussée pour le build
    assert calls["params"][0]["CUCUMBER_SPEC_REPOSITORY"] == "client-cucumber"
    assert open(os.path.join(repo, *FEATURE.split("/")), encoding="utf-8").read() == original
    assert git(repo, "status", "--porcelain") == ""                 # fichiers d'origine rétablis et commités
    console = open(os.path.join(os.path.dirname(repo), "verification", "lot1-mutation-console.txt"),
                   encoding="utf-8").read()
    assert PASSWORD not in console and LOGIN not in console


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


def test_preview_shows_the_parameters_without_pushing_nor_launching(jenkins):
    repo, remote, calls = jenkins
    head = git(repo, "rev-parse", "HEAD")
    out = cr.preview(CLIENT, repo, PROFILE, "@feature_a", repository="client-cucumber", base_branch="integration")
    assert out["ok"] and out["job"] == "DOSSIER/JOB-CLIENT" and out["attendus"] == 2
    assert out["parametres"]["CUCUMBER_SPEC_BRANCH"] == BRANCH and out["parametres"]["wait"] == "false"
    assert out["parametres"]["ADDITIONAL_ARGLINE"].endswith("-Dcucumber.filter.tags='@feature_a'")
    assert out["roles"]["branche"] == "CUCUMBER_SPEC_BRANCH"
    assert calls["params"] == []                                         # rien n'est lancé
    assert subprocess.run(["git", "-C", remote, "rev-parse", "--verify", BRANCH], capture_output=True).returncode
    assert git(repo, "rev-parse", "HEAD") == head                        # rien n'est commité
    unknown = cr.preview(CLIENT, repo, PROFILE, "@inconnu")
    assert not unknown["ok"] and "aucun scénario actif" in unknown["erreurs"][-1]
