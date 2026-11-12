"""
Navigation : seule la page choisie est exécutée ; hors des pages Cucumber, aucune tâche de fond ni aucun fragment
à rafraîchissement automatique (l'application ne se réexécute plus d'elle-même sur l'onglet Intégration).
"""
import os
import time

from streamlit.testing.v1 import AppTest

from tools import cucumber_detection, standard_version_task, ui_timer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _app(monkeypatch, tmp_path, page: str = "") -> tuple:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.delenv("AGENT_AUTH", raising=False)
    calls = []
    monkeypatch.setattr(cucumber_detection, "start", lambda *a, **k: calls.append(("detection", a, k)) or False)
    monkeypatch.setattr(standard_version_task, "start", lambda *a, **k: calls.append(("version", a, k)) or False)
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=120)
    if page:
        at.session_state["nav_page"] = page
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at, calls


def test_integration_page_by_default_runs_nothing_in_background(monkeypatch, tmp_path):
    at, calls = _app(monkeypatch, tmp_path)
    assert at.session_state["nav_page"] == "integration"
    assert "1. Critères de Recherche Jira" in " ".join(m.value for m in at.markdown)
    # Rien des autres pages : ni la configuration, ni l'onglet Cucumber et ses tâches de fond
    assert not [s for s in at.subheader if "Configuration globale" in s.value]
    assert calls == []


def test_only_the_chosen_page_is_executed(monkeypatch, tmp_path):
    at, _ = _app(monkeypatch, tmp_path, "configuration")
    assert [s for s in at.subheader if "Configuration globale" in s.value]
    assert "1. Critères de Recherche Jira" not in " ".join(m.value for m in at.markdown)


def test_search_criteria_kept_when_changing_page(monkeypatch, tmp_path):
    at, _ = _app(monkeypatch, tmp_path)
    at.text_input(key="integ_search_project").input("ABC").run()
    at.session_state["nav_page"] = "configuration"
    at.run()
    at.session_state["nav_page"] = "integration"
    at.run()
    assert at.text_input(key="integ_search_project").value == "ABC"


def test_set_timer_outside_a_fragment_does_nothing():
    assert ui_timer.set_timer(3) is False
    assert ui_timer.set_timer(None) is False


def test_commit_check_every_15_minutes_and_recheck_now(monkeypatch, tmp_path):
    monkeypatch.delenv("CUCUMBER_DETECTION_INTERVAL", raising=False)
    assert cucumber_detection.interval() == 900
    checked = time.time() - 60
    monkeypatch.setattr(cucumber_detection, "load_detection", lambda client: {
        "verifie_le": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(checked))})
    assert 830 <= cucumber_detection.next_check_in("x") <= 840
    assert not cucumber_detection.due("x")
    launched = []

    class Process:
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(cucumber_detection, "log_path", lambda client: str(tmp_path / "detection.log"))
    monkeypatch.setattr(cucumber_detection.subprocess, "Popen", lambda cmd, **k: launched.append(cmd) or Process())
    assert cucumber_detection.start("x", ["main.py"]) is False              # contrôle récent : pas avant 15 min
    assert cucumber_detection.start("x", ["main.py"], now=True) is True     # « Revérifier maintenant »
    assert launched == [["main.py", "--detect-only"]]                       # contrôle des commits, sans --detect-force
    cucumber_detection._PROCESSES.pop("x", None)
