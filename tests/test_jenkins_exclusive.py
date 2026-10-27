"""Un seul build Cucumber à la fois sur Jenkins."""
import os

from tools import jenkins_tools as jt


def test_exclusive_jobs(monkeypatch):
    monkeypatch.setenv("CUCUMBER_VERIFY_JOB", "QA/VERIFY")
    monkeypatch.setenv("JENKINS_CUCUMBER_JOB", "NIGHTLY")
    monkeypatch.delenv("CUCUMBER_EXCLUSIVE_JOBS", raising=False)
    assert jt.exclusive_jobs("QA/VERIFY") == ["QA/VERIFY", "NIGHTLY"]
    monkeypatch.setenv("CUCUMBER_EXCLUSIVE_JOBS", "A, B")
    assert jt.exclusive_jobs("QA/VERIFY") == ["A", "B"]


def test_run_exclusive_waits_for_idle_and_releases_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(jt, "LOCK_FILE", str(tmp_path / "jenkins.lock"))
    monkeypatch.setattr(jt.time, "sleep", lambda s: None)
    states = [["QA/VERIFY : https://j/1"], ["QA/VERIFY : https://j/1"], []]
    events = []
    monkeypatch.setattr(jt, "busy_builds", lambda jobs: events.append("check") or states.pop(0))
    monkeypatch.setattr(jt, "trigger_cucumber_job_with_file",
                        lambda *a: events.append("trigger") or "https://j/queue/2")
    monkeypatch.setattr(jt, "wait_build_result", lambda q, t: {"url": "https://j/2", "result": "SUCCESS",
                                                               "artifacts": []})
    build = jt.run_exclusive("QA/VERIFY", {}, "PATCH", "p.patch", 3600)
    assert build["result"] == "SUCCESS" and events == ["check", "check", "check", "trigger"]
    assert not os.path.exists(jt.LOCK_FILE)


def test_run_exclusive_stops_build_on_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(jt, "LOCK_FILE", str(tmp_path / "jenkins.lock"))
    monkeypatch.setattr(jt, "busy_builds", lambda jobs: [])
    monkeypatch.setattr(jt, "trigger_cucumber_job_with_file", lambda *a: "https://j/queue/2")
    monkeypatch.setattr(jt, "wait_build_result", lambda q, t: {"url": "https://j/2", "result": "TIMEOUT",
                                                               "artifacts": []})
    stopped = []
    monkeypatch.setattr(jt, "stop_build", stopped.append)
    assert jt.run_exclusive("QA/VERIFY", {}, "PATCH", "p.patch", 60)["result"] == "TIMEOUT"
    assert stopped == ["https://j/2"] and not os.path.exists(jt.LOCK_FILE)


def test_run_exclusive_local_lock(monkeypatch, tmp_path):
    lock = tmp_path / "jenkins.lock"
    monkeypatch.setattr(jt, "LOCK_FILE", str(lock))
    monkeypatch.setattr(jt.time, "sleep", lambda s: None)
    lock.write_text("123 0")
    monkeypatch.setattr(jt, "trigger_cucumber_job_with_file", lambda *a: (_ for _ in ()).throw(AssertionError))
    result = jt.run_exclusive("QA/VERIFY", {}, "PATCH", "p.patch", 0)
    assert result["result"] == "INDISPONIBLE" and lock.exists()
