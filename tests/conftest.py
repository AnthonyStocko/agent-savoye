import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Client fictif des tests : ces valeurs ne doivent jamais apparaître dans le code (test_no_client_literals)
CLIENT = "clientx"
CLIENT_TAG = "@CLIENTX"
CLEAR = "init-data-rest-clear-main-x"
SITES = ("SITEA", "SITEB")
OWNERS = ("DO-1", "DO-2")
SECTORS = ("SECT_1", "SECT_MVT", "SECT_STOCK")
FICTIVE_VALUES = (CLIENT, CLIENT_TAG, CLEAR, *SITES, *OWNERS, *SECTORS)


def git(repo, *args):
    result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


@pytest.fixture
def repo(tmp_path):
    """Dépôt git vide avec une identité locale, sur la branche integration."""
    path = str(tmp_path / "repo")
    os.makedirs(path)
    git(path, "init", "-q", "-b", "integration")
    git(path, "config", "user.name", "test")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "core.autocrlf", "false")
    return path


def pytest_configure(config):
    config.addinivalue_line("markers", "real_compile: contrôle de compilation des livraisons réellement exécuté")


@pytest.fixture(autouse=True)
def _delivery_compile_stub(request, monkeypatch):
    """
    Dépôts fictifs des tests (sans pom ni Maven) : le contrôle de compilation des livraisons
    (cucumber_tools.require_compiled) est simulé réussi, sauf pour les tests marqués real_compile qui le vérifient.
    """
    if request.node.get_closest_marker("real_compile"):
        return
    from tools import cucumber_tools
    monkeypatch.setattr(cucumber_tools, "require_compiled", lambda worktree, revisions: {"version": "", "arbre": ""})
