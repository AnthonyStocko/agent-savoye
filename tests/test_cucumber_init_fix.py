"""Correction autonome des compléments d'init écrits par l'agent : périmètre, contrôles et boucle de correction."""
import importlib
import io
import os
import sys

import pytest

from tests.conftest import CLIENT, git, write
from tools import cucumber_environment as ce
from tools import cucumber_init_fix as cif

INIT = "src/test/resources/cucumber/features/clientx/01-Contexte/01-Initialisation.feature"
COMPLEMENTS = "src/test/resources/cucumber/features/clientx/01-Contexte/99-Complements-initialisation.feature"
JAVA = ce.java_path(CLIENT)
FEATURE = ("@INIT_CLIENTX_WMS @CLIENTX\nFeature: Compléments\n\n"
           "  Scenario: Clientx - Contrôle de l'environnement (lecture seule)\n"
           f"    Then {ce.check_expression(CLIENT)}\n      | type | entite | code |\n"
           "      | champ_personnalise | Order | customString1 |\n")
FAILING = f"""package cucumber.features.clientx;

public class InitClientxEnvironnementSteps {{
    @Then("{ce.check_expression(CLIENT)}")
    public void check(DataTable table) {{
        StringBuilder missing = new StringBuilder();
        missing.append("Dépendances de l'environnement non confirmées (type | entité | code) : ");
        Assertions.fail(missing.toString());
    }}
}}
"""
FIXED = FAILING.replace("Assertions.fail(missing.toString());", "scenario.log(missing.toString());")


@pytest.fixture
def worktree(repo):
    write(os.path.join(repo, *INIT.split("/")), "@INIT_CLIENTX_WMS\nFeature: Init\n  Scenario: Init\n    Given x\n")
    write(os.path.join(repo, *COMPLEMENTS.split("/")), FEATURE)
    write(os.path.join(repo, *JAVA.split("/")), FAILING)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def test_scope_is_the_agent_complements_only(worktree):
    files = cif.own_files(worktree, CLIENT)
    assert files == [COMPLEMENTS, JAVA]
    assert cif.fixable([{"fichier": COMPLEMENTS}], files)
    assert not cif.fixable([{"fichier": COMPLEMENTS}, {"fichier": INIT}], files)   # init globale : jamais
    assert not cif.fixable([], files)


def test_unconfirmed_failure_through_a_variable_is_detected():
    assert ce.unconfirmed_failure_methods(FAILING, ce.check_expression(CLIENT)) == ["check"]
    assert ce.unconfirmed_failure_methods(FIXED, ce.check_expression(CLIENT)) == []
    proven = FAILING.replace("non confirmées", "absentes")
    assert ce.unconfirmed_failure_methods(proven, ce.check_expression(CLIENT)) == []


def test_check_correction_reverts_other_files_and_refuses_bad_fixes(worktree):
    files = cif.own_files(worktree, CLIENT)
    before = cif.snapshot(worktree, files)
    write(os.path.join(worktree, *INIT.split("/")), "Feature: modifiée\n")
    write(os.path.join(worktree, "src/test/resources/cucumber/features/clientx/nouveau.feature"), "Feature: N\n")
    errors = cif.check_correction(worktree, CLIENT, files, before)
    assert any(INIT in e and "annulée" in e for e in errors) and any("nouveau.feature" in e for e in errors)
    assert git(worktree, "status", "--porcelain") == ""                          # rétablis
    assert any("non confirmée" in e for e in errors)                              # le step échoue encore
    write(os.path.join(worktree, *JAVA.split("/")), FIXED.replace("scenario.log", "request.post"))
    write(os.path.join(worktree, *COMPLEMENTS.split("/")), "Feature: Compléments\n")
    errors = cif.check_correction(worktree, CLIENT, files, before)
    assert any("lecture seule" in e for e in errors) and any("scénario retiré" in e for e in errors)
    assert any("tag retiré (@INIT_CLIENTX_WMS)" in e for e in errors)
    write(os.path.join(worktree, *JAVA.split("/")), FIXED)
    write(os.path.join(worktree, *COMPLEMENTS.split("/")), FEATURE)
    assert cif.check_correction(worktree, CLIENT, files, before) == []


@pytest.fixture(scope="module")
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


def test_fix_own_init_corrects_commits_and_reruns_the_init(main_module, worktree, monkeypatch, tmp_path):
    m = main_module
    monkeypatch.setattr(m.cucumber_tools, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("CUCUMBER_COMPILE_CMD", "")
    monkeypatch.delenv("CUCUMBER_INIT_SELF_FIX", raising=False)
    calls = []

    def claude(prompt, model, tools, turns, run_dir, effort, *a, **k):
        calls.append(prompt)
        write(os.path.join(worktree, *JAVA.split("/")), FIXED)
        return 0, '```json\n{"cause": "échec sur dépendance non confirmée", "corrections": ["step"]}\n```', {}, False
    monkeypatch.setattr(m, "run_cucumber_claude", claude)
    monkeypatch.setattr(m, "report_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(m.cucumber_verify, "run_init", lambda *a, **k: {"ok": True, "message": "init OK",
                                                                         "build": "b2"})
    failure = {"fichier": COMPLEMENTS, "titre": "Contrôle", "step": ce.check_expression(CLIENT),
               "message": "AssertionError: Dépendances non confirmées", "journal": ""}
    state = {}
    head = git(worktree, "rev-parse", "HEAD")
    result = m.fix_own_init(CLIENT, worktree, {"ok": False, "message": "KO", "echecs": [failure]}, "branche",
                            "@INIT_CLIENTX_WMS", {}, {"verify_branch": ""}, state, "", {"java_std": "j"},
                            {"sources": "s"}, "", sys.executable, os.getcwd(), "w")
    assert result["ok"] and len(calls) == 1 and "Dépendances non confirmées" in calls[0]
    assert JAVA in state["init_corrigee"]["fichiers"] and git(worktree, "rev-parse", "HEAD") != head
    assert "par l'agent" in git(worktree, "log", "-1", "--format=%s")
    # Échec dans l'init globale : rien n'est tenté
    calls.clear()
    other = m.fix_own_init(CLIENT, worktree, {"ok": False, "message": "KO", "echecs": [{"fichier": INIT}]},
                           "branche", "@INIT", {}, {}, {}, "", {"java_std": "j"}, {"sources": "s"}, "",
                           sys.executable, os.getcwd(), "w")
    assert not other["ok"] and calls == []
