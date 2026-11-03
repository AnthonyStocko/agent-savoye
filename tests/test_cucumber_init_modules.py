"""
Correction de l'init d'un client fictif WMS + TMS, TMS exclu : transmission de l'exclusion à main.py, retrait des
steps TMS de l'init WMS avant toute analyse, push vérifié et run de l'init, init générée par l'agent corrigée par
l'agent, message final jamais vide.
"""
import importlib
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

from tests.conftest import CLIENT, git, write
from tests.test_cucumber_modules import (CLIENT_TAG, INIT_TMS, INIT_TMS_PATH, INIT_WMS, INIT_WMS_PATH, _profile,
                                         cache, std_dir)  # noqa: F401 - fixtures
from tools import cucumber_import as cimp
from tools import cucumber_init_fix as cif
from tools import cucumber_modules as cm
from tools import cucumber_profile as cp

CLEAR_TMS = 'INIT- Purge de l\'environnement par le clear "tms/all"'
WMS_INIT = INIT_WMS + (
    "\n  Scenario: Intégration des données\n"
    "    Given KIS- Désactivation des jobs\n"
    "      | name                                                         |\n"
    "      | clean-up-tmp                                                 |\n"
    "      # TODO-JOB: job « Daily tariff activation » : aucune définition chez le client ; défini seulement dans "
    "tms-tariff-workflow/tms-tariff-event/src/main/java/a/ActivateTariffJob.java, module non déployé chez le client\n"
    "      # | Daily tariff activation                                      |\n"
    "    And INIT- Purge de l'environnement par le clear \"wms/all\"\n"
    f"    And {CLEAR_TMS}\n"
    "    # TODO-INIT: module TMS non déployé chez le client : route absente (404 - No route found)\n"
    "    # And INIT- Configuration TMS : \"Système\"\n"
    "    #   | transportManagementMode.id |\n"
    "    #   | SLAVE                      |\n"
    "    And INIT- Initialisation globale par fichiers via l'écran\n")
WORK = "cucumber_clientx_agent"


@pytest.fixture
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


@pytest.fixture
def origin(tmp_path):
    """Dépôt clientx-cucumber distant (nu) : init WMS générée par l'agent, init TMS, branche de travail de l'agent."""
    bare = str(tmp_path / "origin.git")
    git(str(tmp_path), "init", "-q", "--bare", "-b", "integration", bare)
    seed = str(tmp_path / "seed")
    git(str(tmp_path), "clone", "-q", bare, seed)
    for key, value in (("user.name", "t"), ("user.email", "t@e.x"), ("core.autocrlf", "false")):
        git(seed, "config", key, value)
    write(os.path.join(seed, *INIT_TMS_PATH.split("/")), INIT_TMS)
    write(os.path.join(seed, *INIT_WMS_PATH.split("/")), WMS_INIT)
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", f"Cucumber initialisation of {CLIENT} (TMS, WMS)")
    git(seed, "push", "-q", "origin", "HEAD:integration", f"HEAD:{WORK}")
    clone = str(tmp_path / "clone")
    git(str(tmp_path), "clone", "-q", bare, clone)
    for key, value in (("user.name", "t"), ("user.email", "t@e.x"), ("core.autocrlf", "false")):
        git(clone, "config", key, value)
    return {"bare": bare, "clone": clone}


def _client_profile(cache, origin, std_dir):  # noqa: F811
    profile = _profile(cache, origin["clone"], std_dir)
    document = cp.load_profile(CLIENT)
    document["saisi"]["verify_branch"] = WORK
    cp._write_json(cp.profile_path(CLIENT), document)
    return cp.effective_profile(cp.load_profile(CLIENT)) or profile


def _imported(numero, step, rel=INIT_WMS_PATH, message="java.lang.RuntimeException: 404 - No route found"):
    folder = os.path.join(cimp.imports_dir(CLIENT), str(numero))
    os.makedirs(folder, exist_ok=True)
    identifier = f"{rel}::Intégration des données"
    with open(os.path.join(folder, "analyse.json"), "w", encoding="utf-8") as f:
        json.dump({"init_en_echec": {"id": identifier, "step": step, "message": message},
                   "resultats": {identifier: {"journal": ""}}}, f)


def test_the_command_of_the_button_carries_the_excluded_modules(cache, origin, std_dir, main_module, capsys):  # noqa: F811
    _client_profile(cache, origin, std_dir)
    assert cm.cli_args(CLIENT) == ["--modules-exclus", "TMS"]
    # main.py : l'exclusion transmise est affichée en tête du journal (et enregistrée si elle diffère du profil)
    assert main_module.apply_excluded_modules(CLIENT, "TMS") == {"TMS"}
    assert "Modules exclus : TMS" in capsys.readouterr().out
    assert main_module.apply_excluded_modules(CLIENT, "") == set()
    assert cm.excluded(cp.effective_profile(cp.load_profile(CLIENT))) == set()
    assert cm.cli_args(CLIENT) == ["--modules-exclus", ""]
    # Bouton de l'IHM : la commande construite contient l'option (app.py la prend dans cucumber_modules.cli_args)
    app_source = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "app.py"), encoding="utf-8").read()
    assert "*cucumber_modules.cli_args(client)" in app_source


def test_tms_steps_active_or_commented_and_tms_jobs_are_removed_from_the_wms_init(cache, origin, std_dir):  # noqa: F811
    profile = _client_profile(cache, origin, std_dir)
    jobs = cm.module_jobs([WMS_INIT], profile)
    assert jobs == {"Daily tariff activation": "TMS"}
    text, removed = cm.strip_excluded_steps(WMS_INIT, profile, jobs=jobs)
    assert "tms/all" not in text and "Configuration TMS" not in text and "SLAVE" not in text
    assert "Daily tariff activation" not in text and "TODO-INIT" not in text
    assert "wms/all" in text and "clean-up-tmp" in text and "Initialisation globale" in text
    assert {r["module"] for r in removed} == {"TMS"} and len(removed) == 3
    assert cm.traces({INIT_WMS_PATH: text}, profile) == []
    # Réversible : remis à l'identique
    assert cm.restore_steps(text, removed)[0] == WMS_INIT


def test_fix_removes_the_tms_clear_pushes_and_runs_the_wms_init(cache, origin, std_dir, main_module,  # noqa: F811
                                                                 monkeypatch, capsys):
    m = main_module
    _client_profile(cache, origin, std_dir)
    _imported(5, CLEAR_TMS)
    monkeypatch.setattr(m.cucumber_verify, "execution_mode", lambda *a: ("branche", "job"))

    def push(worktree, branch, message, decision=True):
        result = m.cucumber_tools._git(worktree, "push", "origin", f"HEAD:refs/heads/{branch}")
        return ("" if result.returncode == 0 else result.stderr), False
    monkeypatch.setattr(m.cucumber_checks, "push_for_verification", push)
    runs = []

    def run_init(mode, worktree, head, build_fn, tags, sources):
        runs.append({"tags": tags, "head": head, "branche": sources["verify_branch"] if "verify_branch" in sources
                     else sources.get(cp.DEFAULT_VERIFY_BRANCH_PARAM)})
        return {"ok": True, "message": "initialisation OK", "build": "https://jenkins/job/clientx/12/"}
    monkeypatch.setattr(m.cucumber_verify, "run_init", run_init)
    monkeypatch.setattr(m.cucumber_import, "build_files", lambda build: {"console.txt": b"ok"})
    monkeypatch.setattr(m.cucumber_import, "import_files", lambda *a: {"numero": 6, "messages": []})
    monkeypatch.setattr(m.cucumber_import, "analyse", lambda *a: {"init_en_echec": {}})
    calls = []
    monkeypatch.setattr(m, "run_cucumber_claude", lambda *a, **k: calls.append(a) or (0, "{}", {}, False))
    args = SimpleNamespace(client_name=CLIENT, cucumber_base_branch="integration", cucumber_repo="clientx-cucumber")
    capsys.readouterr()
    assert m.fix_init_excluded_modules(args, 5, origin["clone"]) == 0
    out = capsys.readouterr().out
    # Retiré de l'init WMS de la branche de travail, poussé (vérifié par ls-remote), init WMS rejouée
    remote = git(origin["clone"], "ls-remote", "origin", f"refs/heads/{WORK}").split()[0]
    git(origin["clone"], "fetch", "-q", "origin")
    pushed = git(origin["clone"], "show", f"{remote}:{INIT_WMS_PATH}")
    assert "tms/all" not in pushed and "Configuration TMS" not in pushed and "wms/all" in pushed
    assert runs and runs[0]["head"] == remote and runs[0]["tags"] == "@INIT_CLIENTX_WMS"
    assert "vérifié par git ls-remote" in out and "Bilan : build #12" in out and "init verte" in out
    assert "à corriger à la main" not in out and calls == []
    # Historique du retrait gardé pour le recochage
    assert cm.retag_state(cp.load_profile(CLIENT), "TMS")["retires"]


def test_init_generated_by_the_agent_is_corrected_by_the_agent(cache, origin, std_dir, main_module,  # noqa: F811
                                                               monkeypatch, capsys, tmp_path):
    m = main_module
    _client_profile(cache, origin, std_dir)
    monkeypatch.setattr(m.cucumber_tools, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("CUCUMBER_COMPILE_CMD", "")
    monkeypatch.delenv("CUCUMBER_AGENT_JENKINS", raising=False)
    worktree = m.cucumber_tools.worktree_dir(CLIENT)
    git(str(tmp_path), "clone", "-q", origin["bare"], worktree)
    for key, value in (("user.name", "t"), ("user.email", "t@e.x"), ("core.autocrlf", "false")):
        git(worktree, "config", key, value)
    assert cif.init_author(worktree, INIT_WMS_PATH)["auteur"] == "agent"
    _imported(7, "INIT- Initialisation globale par fichiers via l'écran", message="Timeout 360 s")
    monkeypatch.setattr(m.cucumber_tools, "standard_java_catalogue", lambda *a: {"java_std": "j"})
    monkeypatch.setattr(m.cucumber_tools, "std_step_code", lambda *a: {"sources": "s"})
    monkeypatch.setattr(m, "report_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(m, "share_knowledge", lambda *a, **k: None)
    prompts = []

    def claude(prompt, *a, **k):
        prompts.append(prompt)
        path = os.path.join(worktree, *INIT_WMS_PATH.split("/"))
        text = open(path, encoding="utf-8").read().replace("wms/all", "wms/stock")
        write(path, text)
        return 0, '{"cause": "clear trop large"}', {}, False
    monkeypatch.setattr(m, "run_cucumber_claude", claude)
    args = SimpleNamespace(client_name=CLIENT, cucumber_base_branch="integration", cucumber_repo="clientx-cucumber")
    capsys.readouterr()
    assert m.fix_init_from_import(args, 7, "", "", "", sys.executable, os.getcwd()) == 0
    out = capsys.readouterr().out
    assert prompts and "à corriger à la main" not in out and "générée par l'agent" in out
    patch = tmp_path / f"{CLIENT}-cucumber-init-correction.patch"
    assert patch.exists() and "wms/stock" in patch.read_text(encoding="utf-8")


def test_hand_written_init_gets_a_diff_and_failure_message_is_never_empty(cache, repo, std_dir, main_module,  # noqa: F811
                                                                          monkeypatch):
    write(os.path.join(repo, *INIT_WMS_PATH.split("/")), WMS_INIT)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init écrite par l'équipe")
    assert cif.init_author(repo, INIT_WMS_PATH)["auteur"] == "main"
    assert cif.init_author(repo, "absent.feature")["auteur"] == "inconnu"
    line = main_module.init_failure_line({"id": f"{INIT_WMS_PATH}::x", "step": "", "message": ""}, "")
    assert "step « inconnu »" in line and "message absent du rapport" in line and "action : aucune" in line
    # Échec hors des fichiers corrigeables : le message dit le fichier et la raison, jamais vide
    out = main_module.fix_own_init(CLIENT, repo, {"ok": False, "message": "", "echecs": [{"fichier": INIT_WMS_PATH}]},
                                   "branche", "@INIT", {}, {}, {}, "", {"java_std": "j"}, {"sources": "s"}, "",
                                   sys.executable, os.getcwd(), "w")
    assert out["ok"] is False and INIT_WMS_PATH in out["message"]


def test_reference_is_the_latest_import(cache, main_module, capsys):  # noqa: F811
    os.makedirs(cimp.imports_dir(CLIENT), exist_ok=True)
    index = os.path.join(cimp.imports_dir(CLIENT), "index.json")
    with open(index, "w", encoding="utf-8") as f:
        json.dump({"imports": [{"numero": 5}, {"numero": 6}]}, f)
    if cimp.load_index(CLIENT).get("imports"):
        assert main_module.reference_import(CLIENT, 5) == 6
        assert "dernier import (n° 6)" in capsys.readouterr().out
    assert main_module.reference_import(CLIENT, 9) == 9
