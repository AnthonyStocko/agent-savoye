"""Verrou par client : une seule génération Cucumber à la fois sur un client."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tools import cucumber_lock, cucumber_tools


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cucumber_tools, "CACHE_ROOT", str(tmp_path))
    return tmp_path


def other_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    return proc


def test_second_generation_on_the_same_client_is_refused(cache):
    proc = other_process()
    try:
        alive, start = cucumber_lock.process_info(proc.pid)
        assert alive
        with open(cucumber_lock.lock_path("acme"), "w", encoding="utf-8") as f:
            json.dump({"pid": proc.pid, "depart": start, "debut": "2026-11-09T13:22:51",
                       "commande": "--specifics sp-1"}, f)
        ok, reason = cucumber_lock.acquire("acme", "--specifics sp-1")
        assert not ok and f"processus {proc.pid}" in reason and "13:22:51" in reason
        # Autre client : libre
        assert cucumber_lock.acquire("autre")[0]
        cucumber_lock.release("autre")
    finally:
        proc.kill()
        proc.wait()
    # Processus terminé (arrêt brutal) : verrou repris
    ok, _ = cucumber_lock.acquire("acme", "--specifics sp-1")
    assert ok and cucumber_lock.holder("acme")["pid"] == os.getpid()
    cucumber_lock.release("acme")
    assert not os.path.exists(cucumber_lock.lock_path("acme"))


def test_lock_is_reentrant_and_released_only_by_its_owner(cache):
    assert cucumber_lock.acquire("acme")[0]
    assert cucumber_lock.acquire("acme")[0]  # même processus
    with open(cucumber_lock.lock_path("acme"), encoding="utf-8") as f:
        data = json.load(f)
    data["pid"] = os.getpid() + 1_000_000  # verrou d'un autre processus (inexistant) : non supprimé par release
    with open(cucumber_lock.lock_path("acme"), "w", encoding="utf-8") as f:
        json.dump(data, f)
    cucumber_lock.release("acme")
    assert os.path.exists(cucumber_lock.lock_path("acme"))
    assert cucumber_lock.holder("acme") == {}  # mais son détenteur n'existe plus : verrou libre


def test_reused_pid_does_not_hold_the_lock(cache):
    alive, start = cucumber_lock.process_info(os.getpid())
    assert alive
    with open(cucumber_lock.lock_path("acme"), "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "depart": start + "-autre", "debut": "x"}, f)
    if start:
        assert cucumber_lock.holder("acme") == {}


def test_read_only_commands_and_main_wiring():
    assert cucumber_lock.read_only(SimpleNamespace(detect_only=True))
    assert cucumber_lock.read_only(SimpleNamespace(import_run=[], import_url=""))
    assert not cucumber_lock.read_only(SimpleNamespace(detect_only=False, profile_only=False, server_log="",
                                                       import_run=None, import_url="", specifics="sp-1"))
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "main.py"), encoding="utf-8") as f:
        main = f.read()
    assert "cucumber_lock.acquire(args.client_name" in main and "sys.exit(3)" in main
    with open(os.path.join(root, "app.py"), encoding="utf-8") as f:
        assert "render_lock_banner(client)" in f.read()
