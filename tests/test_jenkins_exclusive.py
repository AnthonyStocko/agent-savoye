"""Un seul build Cucumber à la fois sur Jenkins."""
import os

from tools import jenkins_tools as jt


def test_exclusive_jobs(monkeypatch):
    monkeypatch.setenv("CUCUMBER_VERIFY_JOB", "QA/VERIFY")
    monkeypatch.setenv("JENKINS_CUCUMBER_JOB", "NIGHTLY")
    monkeypatch.setenv("CUCUMBER_RUN_JOB", "CUCUMBER-CUSTOMER")
    monkeypatch.delenv("CUCUMBER_EXCLUSIVE_JOBS", raising=False)
    assert jt.exclusive_jobs("QA/VERIFY") == ["QA/VERIFY", "CUCUMBER-CUSTOMER", "NIGHTLY"]
    monkeypatch.setenv("CUCUMBER_EXCLUSIVE_JOBS", "A, B")
    assert jt.exclusive_jobs("QA/VERIFY") == ["A", "B"]


def test_run_exclusive_waits_for_idle_and_releases_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(jt, "LOCK_FILE", str(tmp_path / "jenkins.lock"))
    monkeypatch.setattr(jt.time, "sleep", lambda s: None)
    states = [["QA/VERIFY : https://j/1"], ["QA/VERIFY : https://j/1"], []]
    events = []
    monkeypatch.setattr(jt, "busy_builds", lambda jobs: events.append("check") or states.pop(0))
    trigger = lambda: events.append("trigger") or "https://j/queue/2"
    monkeypatch.setattr(jt, "wait_build_result", lambda q, t, *a, **k: {"url": "https://j/2", "result": "SUCCESS",
                                                               "artifacts": []})
    build = jt.run_exclusive("QA/VERIFY", trigger, 3600)
    assert build["result"] == "SUCCESS" and events == ["check", "check", "check", "trigger"]
    assert not os.path.exists(jt.LOCK_FILE)


def test_run_exclusive_stops_build_on_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(jt, "LOCK_FILE", str(tmp_path / "jenkins.lock"))
    monkeypatch.setattr(jt, "busy_builds", lambda jobs: [])
    monkeypatch.setattr(jt, "wait_build_result", lambda q, t, *a, **k: {"url": "https://j/2", "result": "TIMEOUT",
                                                               "artifacts": []})
    stopped = []
    monkeypatch.setattr(jt, "stop_build", stopped.append)
    assert jt.run_exclusive("QA/VERIFY", lambda: "https://j/queue/2", 60)["result"] == "TIMEOUT"
    assert stopped == ["https://j/2"] and not os.path.exists(jt.LOCK_FILE)


def test_run_exclusive_local_lock(monkeypatch, tmp_path):
    lock = tmp_path / "jenkins.lock"
    monkeypatch.setattr(jt, "LOCK_FILE", str(lock))
    monkeypatch.setattr(jt.time, "sleep", lambda s: None)
    lock.write_text("123 0")
    result = jt.run_exclusive("QA/VERIFY", lambda: (_ for _ in ()).throw(AssertionError), 0)
    assert result["result"] == "INDISPONIBLE" and lock.exists()


class _Response:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


def test_jenkins_urls_follow_jenkins_url(monkeypatch):
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.io/")
    assert jt.same_base("http://jenkins.example.io/queue/item/12/") == "https://jenkins.example.io/queue/item/12"
    assert jt.same_base("http://jenkins:8080/job/QA/job/X/7/") == "https://jenkins.example.io/job/QA/job/X/7"
    assert jt.same_base("") == ""


def test_wait_finds_the_build_when_the_queue_item_is_gone(monkeypatch):
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.example.io")
    monkeypatch.setattr(jt.time, "sleep", lambda s: None)
    seen = []

    def get(url, **kwargs):
        seen.append(url)
        if "/queue/item/" in url:
            return _Response(404, {})
        if url.endswith("/job/QA/job/CUCUMBER-CUSTOMER/api/json"):
            return _Response(200, {"builds": [{"url": "http://jenkins.example.io/job/QA/job/CUCUMBER-CUSTOMER/180/",
                                               "queueId": 1363262}]})
        return _Response(200, {"building": False, "result": "SUCCESS", "artifacts": []})
    monkeypatch.setattr(jt.requests, "get", get)
    build = jt.wait_build_result("http://jenkins.example.io/queue/item/1363262/", 60, job_name="QA/CUCUMBER-CUSTOMER")
    assert build == {"url": "https://jenkins.example.io/job/QA/job/CUCUMBER-CUSTOMER/180", "result": "SUCCESS",
                     "artifacts": []}
    assert all(u.startswith("https://") for u in seen)
