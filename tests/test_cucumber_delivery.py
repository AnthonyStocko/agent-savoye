"""Livraison autonome : garde d'écriture (dépôt, fichiers, pause), promotion, annulation par revert sans force-push."""
import importlib
import io
import json
import os
import sys

import pytest

from tests.conftest import CLIENT, CLIENT_TAG, git, write

from tools import cucumber_checks as cc
from tools import cucumber_delivery as cd
from tools import cucumber_profile as cp
from tools import cucumber_tools as ct

FEATURE = "src/test/resources/cucumber/features/clientx/WMS/a.feature"
INIT = "src/test/resources/cucumber/features/clientx/01-Contexte/WMS/01-Initialisation.feature"
TEXT = f"{CLIENT_TAG}\nFeature: A\n\n  Scenario: Premier\n    Given x\n\n  Scenario: Second\n    Given y\n"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Dépôt clientx-cucumber nu (origin), son clone dans l'espace de l'agent, un dépôt de code client à part."""
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.delenv("AGENT_AUTH_USER", raising=False)
    origin = str(tmp_path / "remotes" / f"{CLIENT}-cucumber.git")
    other = str(tmp_path / "remotes" / f"odatio-{CLIENT}.git")
    for bare in (origin, other):
        os.makedirs(bare)
        git(bare, "init", "-q", "--bare", "-b", "integration")
    worktree = os.path.join(ct.cache_dir(CLIENT), "worktree")
    os.makedirs(worktree)
    git(worktree, "init", "-q", "-b", "integration")
    for key, value in (("user.name", "test"), ("user.email", "t@example.com"), ("core.autocrlf", "false")):
        git(worktree, "config", key, value)
    git(worktree, "remote", "add", "origin", origin)
    write(os.path.join(worktree, *FEATURE.split("/")), TEXT)
    write(os.path.join(worktree, *INIT.split("/")), f"@INIT_CLIENTX_WMS {CLIENT_TAG}\nFeature: Init\n")
    write(os.path.join(worktree, "pom.xml"), "<project/>\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "base")
    git(worktree, "push", "-q", "origin", "integration")
    git(worktree, "push", "-q", "origin", "integration:cucumber-agent")
    with open(cp.profile_path(CLIENT), "w", encoding="utf-8") as f:
        json.dump({"detecte": {**cp.empty_profile(), "tag_client": CLIENT_TAG, "protected_files": [INIT],
                               "cucumber_repo_url": origin},
                   "saisi": {"livraison_autonome": True, "verify_branch": "cucumber-agent",
                             "branche_cible": "integration"}}, f)
    return {"worktree": worktree, "origin": origin, "other": other}


@pytest.fixture(scope="module")
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


def _change(worktree, rel, text, message="correction"):
    write(os.path.join(worktree, *rel.split("/")), text)
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", message)


def _remote_head(origin, branch):
    return git(origin, "rev-parse", branch)


def test_push_refused_outside_the_client_cucumber_repo(setup):
    worktree = setup["worktree"]
    git(worktree, "remote", "set-url", "origin", setup["other"])
    _change(worktree, FEATURE, TEXT + "\n  Scenario: Troisième\n    Given z\n")
    error, _ = cc.push_for_verification(worktree, "cucumber-agent", "x")
    assert "n'est pas le dépôt Cucumber du profil" in error
    assert git(setup["other"], "branch", "--list") == ""                           # rien n'a été envoyé
    assert cd.events(CLIENT, ("refus",))[-1]["motif"].startswith(cd.normalize_url(setup["other"]))
    # Copie de lecture : adresse d'envoi désactivée, refusée avant Git
    git(worktree, "remote", "set-url", "origin", setup["origin"])
    cd.disable_push(worktree)
    assert "copie de lecture" in cd.check_push(worktree, "cucumber-agent")


def test_push_refused_for_pom_protected_file_or_deleted_scenario_without_decision(setup):
    worktree = setup["worktree"]
    before = _remote_head(setup["origin"], "cucumber-agent")
    _change(worktree, "pom.xml", "<project><x/></project>\n")
    assert "pom.xml modifié sans décision humaine" in cd.check_push(worktree, "cucumber-agent", decision=False)
    git(worktree, "reset", "-q", "--hard", "HEAD~1")
    _change(worktree, INIT, f"@INIT_CLIENTX_WMS {CLIENT_TAG}\nFeature: Init modifiée\n")
    assert "init globale" in cd.check_push(worktree, "cucumber-agent", decision=False)
    git(worktree, "reset", "-q", "--hard", "HEAD~1")
    _change(worktree, FEATURE, f"{CLIENT_TAG}\nFeature: A\n\n  Scenario: Premier\n    Given x\n")
    error, _ = cc.push_for_verification(worktree, "cucumber-agent", "x", decision=False)
    assert "scénario(s) supprimé(s) sans décision humaine : Second" in error
    git(worktree, "reset", "-q", "--hard", "HEAD~1")
    _change(worktree, ".gitlab-ci.yml", "x: 1\n")
    assert "hors des fichiers autorisés" in cd.check_push(worktree, "cucumber-agent", decision=True)
    assert _remote_head(setup["origin"], "cucumber-agent") == before
    # Feature et données de test : autorisées
    git(worktree, "reset", "-q", "--hard", "HEAD~1")
    _change(worktree, FEATURE, TEXT + "\n  Scenario: Troisième\n    Given z\n")
    _change(worktree, "src/test/resources/cucumber/files/clientx/WMS/a.csv", "code\nA\n")
    error, _ = cc.push_for_verification(worktree, "cucumber-agent", "x", decision=False)
    assert error == ""
    assert _remote_head(setup["origin"], "cucumber-agent") == git(worktree, "rev-parse", "HEAD")


def test_pause_blocks_every_write(setup):
    worktree = setup["worktree"]
    cd.set_pause(True, "Une Personne")
    _change(worktree, FEATURE, TEXT + "\n  Scenario: Troisième\n    Given z\n")
    error, _ = cc.push_for_verification(worktree, "cucumber-agent", "x")
    assert "écriture en pause" in error
    assert not cd.promote(worktree, "integration", CLIENT)["ok"]
    cd.set_pause(False, "Une Personne")
    assert cc.push_for_verification(worktree, "cucumber-agent", "x")[0] == ""


def _analysis(numero, failing=(), complete=True, init=None):
    ids = ["f.feature::A", "f.feature::B", "f.feature::C"]
    return {"numero": numero, "run_complet": complete, "init_en_echec": init or {}, "build": f"https://j/{numero}",
            "resultats": {i: {"statut": "echec" if i in failing else "passe"} for i in ids}}


def test_promotion_rules():
    reference = _analysis(1, ["f.feature::A", "f.feature::B"])
    assert cd.promotion_verdict(reference, [_analysis(2, ["f.feature::B"])])["ok"]
    new = cd.promotion_verdict(reference, [_analysis(2, ["f.feature::C"])])
    assert not new["ok"] and "nouvel(s) échec(s)" in new["motif"]
    assert not cd.promotion_verdict(reference, [_analysis(2, ["f.feature::A", "f.feature::B"])])["ok"]
    assert "init en échec" in cd.promotion_verdict(reference, [_analysis(2, [], init={"id": "x"})])["motif"]
    assert not cd.promotion_verdict(reference, [_analysis(2, [], complete=False)])["ok"]
    green = _analysis(1)
    assert not cd.promotion_verdict(green, [_analysis(2)])["ok"]                 # un seul run vert
    assert cd.promotion_verdict(green, [_analysis(2), _analysis(3)])["ok"]


def test_promotion_refused_on_new_failure_then_fast_forward_when_better(setup, monkeypatch, main_module):
    main = main_module
    worktree = setup["worktree"]
    profile = cp.effective_profile(cp.load_profile(CLIENT))
    reference = _analysis(1, ["f.feature::A", "f.feature::B"])
    monkeypatch.setattr(main.cucumber_import, "load_analysis", lambda client, n: reference if n == 1 else {})
    monkeypatch.setattr(main, "latest_full_run", lambda client: reference)
    target = _remote_head(setup["origin"], "integration")
    _change(worktree, FEATURE, TEXT + "\n  Scenario: Troisième\n    Given z\n")
    state = {}
    refused = main.autonomous_delivery(CLIENT, state, worktree, profile,
                                       launch=lambda: _analysis(2, ["f.feature::B", "f.feature::C"]))
    assert not refused["promu"] and "nouvel(s) échec(s)" in refused["motif"]
    assert _remote_head(setup["origin"], "integration") == target                    # cible inchangée
    assert _remote_head(setup["origin"], "cucumber-agent") == git(worktree, "rev-parse", "HEAD")
    done = main.autonomous_delivery(CLIENT, state, worktree, profile, launch=lambda: _analysis(3, ["f.feature::B"]))
    assert done["promu"] and _remote_head(setup["origin"], "integration") == git(worktree, "rev-parse", "HEAD")
    assert [e["type"] for e in cd.events(CLIENT, ("promotion", "non_promu"))] == ["non_promu", "promotion"]
    # Désactivée par défaut : rien
    assert main.autonomous_delivery(CLIENT, {}, worktree, {**profile, "livraison_autonome": False}) == {}


def test_regression_after_promotion_is_reverted_without_force_push(setup, monkeypatch, main_module):
    main = main_module
    worktree = setup["worktree"]
    before = _remote_head(setup["origin"], "integration")
    _change(worktree, FEATURE, TEXT + "\n  Scenario: Troisième\n    Given z\n")
    assert cc.push_for_verification(worktree, "cucumber-agent", "x", decision=False)[0] == ""
    promoted = cd.promote(worktree, "integration", CLIENT)
    assert promoted["ok"]
    branch_run = {"numero": 5, "resultats": {f"{FEATURE}::Premier": {"statut": "passe"}}}
    cd.log_event(CLIENT, "promotion", cible="integration", branche="cucumber-agent", commit=promoted["commit"],
                 avant=before, run_branche=5)
    monkeypatch.setattr(main.cucumber_import, "load_analysis", lambda client, n: branch_run)
    target_run = {"numero": 6, "build": "https://j/6", "resultats": {f"{FEATURE}::Premier": {"statut": "echec"}}}
    history = git(setup["origin"], "rev-list", "integration").split()
    out = main.check_promoted_regression(CLIENT, target_run, {"BRANCH": "integration"})
    assert out["revert"] and out["ok"]
    after = git(setup["origin"], "rev-list", "integration").split()
    assert after[1:] == history                                    # un commit ajouté, historique intact (pas de force)
    assert "Revert" in git(setup["origin"], "log", "-1", "--format=%s", "integration")
    assert git(setup["origin"], "show", f"integration:{FEATURE}") == TEXT.rstrip("\n")
    # Promotion déjà contrôlée : plus rien à faire
    assert main.check_promoted_regression(CLIENT, target_run, {"BRANCH": "integration"}) == {}


def test_commit_message_and_identity(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_USER", "Une Personne <une.personne@example.com>")
    monkeypatch.setenv("AGENT_GIT_NAME", "agent-technique")
    monkeypatch.setenv("AGENT_GIT_EMAIL", "agent@example.com")
    lot = {"numero": 4, "import": 7, "spec_correction": "sp-1", "ids": [],
           "corrections": [{"id": "a.feature::Premier", "titre": "Premier", "fichier": "a.feature",
                            "message": "Expected 1 but was 2"}]}
    analysis = {"build": "https://jenkins/job/x/7", "echecs": [{"id": "a.feature::Premier", "cause": "écart",
                                                                "action": "colonne corrigée"}]}
    message = cd.commit_message(lot, analysis, {"sp-1": "Stock"}, CLIENT)
    for part in ("Cause : écart", "Correction : colonne corrigée", "import n° 7 — https://jenkins/job/x/7",
                 "Tâches : sp-1 (Stock)", "Lancé-par: Une Personne <une.personne@example.com>"):
        assert part in message
    env = cd.identity_env()
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "agent-technique"


def test_token_scope_flags_write_access_outside_cucumber_repos():
    pages = {0: {"values": [{"slug": f"{CLIENT}-cucumber", "project": {"key": "P"}}], "isLastPage": False,
                 "nextPageStart": 1},
             1: {"values": [{"slug": "wms-content-backend", "project": {"key": "STD"}}], "isLastPage": True}}
    scope = cd.token_scope(lambda start: pages[start])
    assert scope["ok"] is False and scope["trop_larges"] == ["STD/wms-content-backend"]
    assert "trop large" in cd.token_scope_line(scope)
    assert cd.token_scope(lambda start: pages[1] | {"values": pages[0]["values"]})["ok"] is True


def test_push_integrates_a_remote_branch_that_moved_ahead(setup, tmp_path):
    worktree, origin = setup["worktree"], setup["origin"]
    other = str(tmp_path / "autre")
    git(str(tmp_path), "clone", "-q", "-b", "cucumber-agent", origin, other)
    for key, value in (("user.name", "test"), ("user.email", "t@example.com")):
        git(other, "config", key, value)
    _change(other, "src/test/resources/cucumber/files/clientx/WMS/init.csv", "code\nB\n", "init poussée")
    git(other, "push", "-q", "origin", "cucumber-agent")
    _change(worktree, FEATURE, TEXT + "\n  Scenario: Troisième\n    Given z\n")
    error, _ = cc.push_for_verification(worktree, "cucumber-agent", "x")
    assert error == ""
    assert _remote_head(origin, "cucumber-agent") == git(worktree, "rev-parse", "HEAD")
    assert os.path.exists(os.path.join(worktree, "src", "test", "resources", "cucumber", "files", "clientx", "WMS",
                                       "init.csv"))
    # Conflit : merge annulé, rien n'est écrasé sur origin
    git(other, "fetch", "-q", "origin")
    git(other, "reset", "-q", "--hard", "origin/cucumber-agent")
    _change(other, FEATURE, TEXT + "\n  Scenario: Manuel\n    Given m\n", "manuel")
    git(other, "push", "-q", "origin", "cucumber-agent")
    remote = _remote_head(origin, "cucumber-agent")
    _change(worktree, FEATURE, TEXT + "\n  Scenario: Agent\n    Given a\n")
    error, _ = cc.push_for_verification(worktree, "cucumber-agent", "x")
    assert "conflit" in error and "a.feature" in error and _remote_head(origin, "cucumber-agent") == remote
    assert git(worktree, "status", "--porcelain") == ""


def test_previous_agent_runs_on_the_remote_branch_are_replaced_without_force(setup, tmp_path):
    worktree, origin = setup["worktree"], setup["origin"]
    other = str(tmp_path / "ancien-run")
    git(str(tmp_path), "clone", "-q", "-b", "cucumber-agent", origin, other)
    for key, value in (("user.name", "test"), ("user.email", "t@example.com")):
        git(other, "config", key, value)
    old = "src/test/resources/cucumber/features/clientx/WMS/ancien.feature"
    _change(other, old, TEXT, "Cucumber coverage of clientx specifics - lot 1 (vérification)")
    _change(other, FEATURE, TEXT + "\n  Scenario: Ancien\n    Given o\n",
            "Cucumber coverage of clientx specifics - lot 1 (vérification)")
    git(other, "push", "-q", "origin", "cucumber-agent")
    previous = _remote_head(origin, "cucumber-agent")
    _change(worktree, FEATURE, TEXT + "\n  Scenario: Nouveau\n    Given n\n")
    error, _ = cc.push_for_verification(worktree, "cucumber-agent", "x")
    assert error == "" and _remote_head(origin, "cucumber-agent") == git(worktree, "rev-parse", "HEAD")
    assert git(worktree, "merge-base", "--is-ancestor", previous, "HEAD") == ""      # historique conservé
    assert not os.path.exists(os.path.join(worktree, *old.split("/")))               # ancien run non réintroduit
    with open(os.path.join(worktree, *FEATURE.split("/")), encoding="utf-8") as f:
        assert "Nouveau" in f.read()
