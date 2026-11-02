"""
Run complet de la suite importé (client fictif) : un run complet l'emporte sur toute vérification isolée, plus
ancienne ou plus récente. Un test vert seul mais rouge en run complet est « en échec (dépendance entre tests) »,
compté en échec au tableau de bord et proposé à la correction ; chaque échec compté par la console a son résultat
(contrôle automatique) ; la règle « données uniques par feature » accompagne la correction proposée.
"""
import os

import pytest

from tests.conftest import CLIENT, CLIENT_TAG, git, write
from tools import cucumber_coverage as cc
from tools import cucumber_failures as cf
from tools import cucumber_import as ci
from tools import cucumber_overview as co
from tools import cucumber_profile as cp
from tools import cucumber_tools as ct
from tools.secret_tools import mask_secrets

ROOT = f"src/test/resources/cucumber/features/{CLIENT}/WMS"
PAREN = f"{ROOT}/Retours.feature"            # nom de feature avec parenthèse : en-tête Surefire réécrit
SHARED = f"{ROOT}/Dedouanement.feature"     # vérifié vert seul, données partagées avec Quai.feature
OTHER = f"{ROOT}/Quai.feature"
A, B = "Retour : l'UM est validée automatiquement", "Indicateurs de l'OP renseignés"
STAMP = "[2026-10-30T10:15:45.596Z] "


def _console(failures=2, errors=0, with_b=True, stamp=STAMP):
    lines = ["+ mvn test -Dcucumber.filter.tags='" + CLIENT_TAG + "' -DuserName='Test' -Dpassword='secret'",
             "[INFO] Scanning for projects...",
             f"Scenario: {A}   # {PAREN}:5",
             f"Scenario: {B}   # {SHARED}:5",
             f"Scenario: Envoi au quai   # {OTHER}:5",
             f"[ERROR] Tests run: 3, Failures: {failures}, Errors: {errors}, Skipped: 0, Time elapsed: 9 s "
             f"<<< FAILURE! -- in cucumber.Runner",
             f"[ERROR] (contrôle) fictifs.{A}(Retours  -- Time elapsed: 3.2 s <<< FAILURE!",
             "java.lang.AssertionError: Il n'existe pas 1 enregistrement(s) pour l'élément {code=UM-FIC-1}",
             f"\tat ✽.CONTENT- unités de manutention(file:///home/jenkins/workspace/spec/{PAREN}:8)", ""]
    if with_b:
        lines += [f"[ERROR] Dédouanement fictif.{B} -- Time elapsed: 120 s <<< FAILURE!",
                  "java.lang.AssertionError: Unable to create a launching Campaign. Preparation order OP-FIC-730 is "
                  "not A lancer or already associated to a launching campaign",
                  f"\tat ✽.PREP- Campagne et Mise en Préparation(file:///home/jenkins/workspace/spec/{SHARED}:9)", ""]
    lines += ["[INFO] Results:", "[INFO] BUILD FAILURE"]
    return "\n".join(stamp + line for line in lines)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.delenv("CUCUMBER_TAG_A_REVOIR", raising=False)
    cp.save_user_fields(CLIENT, {"tag_client": CLIENT_TAG, "features_root": ROOT.rsplit("WMS", 1)[0]})
    worktree = ct.worktree_dir(CLIENT)
    os.makedirs(worktree)
    git(worktree, "init", "-q", "-b", "integration")
    git(worktree, "config", "user.email", "t@e.x")
    git(worktree, "config", "user.name", "t")
    write(os.path.join(worktree, PAREN), f"Feature: Retours (contrôle) fictifs\n\n  {CLIENT_TAG}\n  Scenario: {A}\n"
                                         f"    Then CONTENT- unités de manutention\n")
    # Désactivé par l'agent après une vérification isolée : le run complet l'a pourtant exécuté
    write(os.path.join(worktree, SHARED), f"Feature: Dédouanement fictif\n\n  # À revoir : cause NON PROUVÉE\n"
                                          f"  @A_REVOIR\n  Scenario: {B}\n    Given PREP- Campagne\n"
                                          f"      | code       |\n      | OP-FIC-730 |\n")
    write(os.path.join(worktree, OTHER), f"Feature: Quai\n\n  {CLIENT_TAG}\n  Scenario: Envoi au quai\n"
                                         f"    Given PREP- Campagne\n      | code       |\n      | OP-FIC-730 |\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "base")
    # Lot 2 : B vérifié VERT par une exécution isolée de l'agent, APRÈS le run importé
    ct.save_state(CLIENT, {"client": CLIENT, "base": git(worktree, "rev-parse", "HEAD"), "lots": [
        {"numero": 2, "ids": [], "statut": "termine", "fin": "2099-01-01T00:00:00",
         "tags": {f"@agent_{CLIENT}_2_1": f"{SHARED}::{B}"},
         "execution": {"scenarios": {f"@agent_{CLIENT}_2_1": {"id": f"{SHARED}::{B}", "statut": "pertinent",
                                                               "motif": ""}}}}]})
    return worktree


def test_short_user_name_is_masked_as_a_word_only():
    masked, counts = mask_secrets(_console())
    assert "Tests run: 3" in masked and "'Test'" not in masked and "secret" not in masked
    assert counts == {"mot de passe": 1, "utilisateur": 1}


def test_surefire_header_rewritten_by_a_parenthesis_is_read():
    results = ci.console_results(_console())
    assert results[(PAREN, A)]["statut"] == "echoue" and results[(SHARED, B)]["statut"] == "echoue"
    assert ci.console_facts(_console().replace("Tests run", "****s run"))["tests"]["echecs"] == 2


def test_console_gap_is_detected():
    facts = ci.console_facts(_console(failures=2, errors=1))
    failures = {k: r for k, r in ci.console_results(_console()).items() if r["statut"] != "passe"}
    assert "3 échec(s)" in ci.console_gap(facts, failures) and "en lit 2" in ci.console_gap(facts, failures)
    assert ci.console_gap(ci.console_facts(_console()), failures) == ""


def test_full_run_puts_isolated_greens_back_in_failure(project):
    imported = ci.import_files(CLIENT, {"console.txt": _console().encode("utf-8")})
    state = ct.load_state(CLIENT)
    analysis = ci.analyse(CLIENT, imported["numero"], state, project)
    assert analysis["run_complet"] is True and analysis["ecart_console"] == ""
    # Chaque échec de la console a son résultat, tous proposés à la correction
    results = {e["id"]: e for e in analysis["echecs"]}
    assert set(results) == {f"{PAREN}::{A}", f"{SHARED}::{B}"}
    dependent = results[f"{SHARED}::{B}"]
    assert (dependent["categorie"], dependent["nature"]) == ("correction", "dependance_tests")
    assert "dépendance entre tests" in dependent["cause"] and "OP-FIC-730" in dependent["cause"]
    proposed = {s["id"]: s for p in ci.remaining_proposals(state, analysis) for s in p["scenarios"]}
    assert set(proposed) == set(results)
    # Règle « données uniques par feature » jointe à la correction proposée
    assert proposed[f"{SHARED}::{B}"]["dependance"].startswith("vert seul (lot 2)")
    assert proposed[f"{SHARED}::{B}"]["donnees_partagees"] == [{"code": "OP-FIC-730", "features": ["Quai.feature"]}]
    # Tableau de bord : le vert isolé plus récent et la désactivation par l'agent ne masquent pas l'échec
    sources = cc.coverage_sources(CLIENT, state)
    status = cc.scenario_status(f"{SHARED}::{B}", sources)
    assert status["etat"] == "echec" and status["cause"].startswith("en échec (dépendance entre tests)")
    assert {f["id"] for f in co.failing([], sources)} == set(results)
    # Le résultat est classé par cucumber_failures, jamais absent
    assert cf.missing(analysis) == []


def test_later_full_run_green_wins_over_an_isolated_failure(project):
    first = ci.import_files(CLIENT, {"console.txt": _console().encode("utf-8")})
    ci.analyse(CLIENT, first["numero"], ct.load_state(CLIENT), project)
    second = ci.import_files(CLIENT, {"console2.txt": _console(failures=1, with_b=False,
                                                               stamp="[2026-10-31T10:15:45.596Z] ").encode("utf-8")})
    ci.analyse(CLIENT, second["numero"], ct.load_state(CLIENT), project)
    sources = cc.coverage_sources(CLIENT, ct.load_state(CLIENT))
    assert cc.scenario_status(f"{SHARED}::{B}", sources)["etat"] == "vert"
    assert {f["id"] for f in co.failing([], sources)} == {f"{PAREN}::{A}"}


def test_partial_run_is_not_a_full_run():
    assert ci.full_run({"console": {"tags": f"{CLIENT_TAG} and not @A_REVOIR"}}, CLIENT_TAG)
    assert ci.full_run({"console": {"tags": ""}}, CLIENT_TAG)
    assert not ci.full_run({"console": {"tags": f"@agent_{CLIENT}_2_1"}}, CLIENT_TAG)
    assert not ci.full_run({"console": {"tags": "@SPEC-1047"}}, CLIENT_TAG)
    assert not ci.full_run({}, CLIENT_TAG)
