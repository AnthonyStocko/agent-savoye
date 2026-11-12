"""Commits de l'agent (identité dédiée) et intégration des commits humains de la branche distante avant un push."""
import os
import subprocess

import pytest

from tools import cucumber_checks, cucumber_delivery, cucumber_tools
from tools.cucumber_checks import _AGENT_COMMIT, integrate_remote, is_agent_commit

INIT = "src/test/resources/cucumber/features/acme/01-Contexte/WMS/01-Initialisation.feature"
TEST = "src/test/resources/cucumber/features/acme/EPO-2412.feature"
OTHER = "Jenkinsfile"


def test_agent_commit_subjects_recognised():
    for subject in ("Cucumber coverage of canson specifics - lot 1 (vérification)",
                    "Cucumber initialisation complements of canson",
                    "Cucumber canson : init de la version v1.3",
                    "Cucumber verification (EPO-2412)",
                    "Intégration de origin/cucumber_canson_agent avant la vérification"):
        assert _AGENT_COMMIT.match(subject), subject


def test_human_commit_not_recognised():
    for subject in ("Clear wms/all commenté pour tester sans purge", "Pull request #12: V1.5"):
        assert not _AGENT_COMMIT.match(subject), subject
    assert is_agent_commit("Agent Cucumber", "Clear wms/all commenté")
    assert not is_agent_commit("amourier", "Clear wms/all commenté")


def _git(repo, *args, human=False):
    identity = ["-c", "user.name=Humain", "-c", "user.email=humain@example.com"] if human else []
    out = subprocess.run(["git", "-C", repo, *identity, *args], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full) or repo, exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """Dépôt distant, espace de l'agent (worktree) et clone d'un humain partant du même commit."""
    events = []
    monkeypatch.setattr(cucumber_delivery, "log_event", lambda client, kind, **data: events.append((kind, data)))
    monkeypatch.delenv("CUCUMBER_AGENT_GIT_NAME", raising=False)
    remote, agent, human = (str(tmp_path / n) for n in ("remote.git", "agent", "human"))
    subprocess.run(["git", "init", "-q", "--bare", "-b", "work", remote], check=True)
    subprocess.run(["git", "clone", "-q", remote, agent], check=True, capture_output=True)
    _git(agent, "checkout", "-q", "-b", "work")
    for path in (INIT, TEST, OTHER):
        _write(agent, path, "ligne 1\nligne 2\nligne 3\n")
    _git(agent, "add", "-A")
    _git(agent, "commit", "-qm", "base", human=True)
    _git(agent, "push", "-q", "origin", "work")
    subprocess.run(["git", "clone", "-q", "-b", "work", remote, human], check=True, capture_output=True)
    return agent, human, events


def _human_push(human, changes, message="Modification à la main"):
    for path, text in changes.items():
        _write(human, path, text)
    _git(human, "commit", "-qam", message, human=True)
    _git(human, "push", "-q", "origin", "work")


def _agent_commit(agent, changes):
    for path, text in changes.items():
        _write(agent, path, text)
    _git(agent, "add", "-A")
    assert cucumber_tools._git(agent, "commit", "-qm", "Travail de l'agent").returncode == 0


def test_agent_commits_signed_with_dedicated_identity(repos):
    agent, _, _ = repos
    _agent_commit(agent, {TEST: "agent\n"})
    assert _git(agent, "log", "-1", "--format=%an|%cn") == "Agent Cucumber|Agent Cucumber"


def test_human_init_wins_agent_test_wins(repos):
    agent, human, events = repos
    _human_push(human, {INIT: "# clear commenté\nligne 2\nligne 3\n", TEST: "humain\nligne 2\nligne 3\n"})
    _agent_commit(agent, {INIT: "init agent\nligne 2\nligne 3\n", TEST: "agent\nligne 2\nligne 3\n"})
    assert integrate_remote(agent, "work") == ""
    with open(os.path.join(agent, INIT), encoding="utf-8") as f:
        assert f.read().startswith("# clear commenté")
    with open(os.path.join(agent, TEST), encoding="utf-8") as f:
        assert f.read().startswith("agent")
    _git(agent, "merge-base", "--is-ancestor", "origin/work", "HEAD")
    kind, data = events[-1]
    assert kind == "integration" and data["version_humaine"] == [INIT] and data["version_agent"] == [TEST]


def test_other_file_modified_both_sides_stops_with_diff(repos, tmp_path, monkeypatch):
    agent, human, _ = repos
    monkeypatch.setattr(cucumber_checks, "cache_dir", lambda client: str(tmp_path / "cache"))
    monkeypatch.setattr(cucumber_delivery, "client_of", lambda worktree: "acme")
    _human_push(human, {OTHER: "humain\nligne 2\nligne 3\n"})
    _agent_commit(agent, {OTHER: "agent\nligne 2\nligne 3\n"})
    head = _git(agent, "rev-parse", "HEAD")
    reason = integrate_remote(agent, "work")
    assert "Jenkinsfile" in reason and "merge annulé" in reason
    assert _git(agent, "rev-parse", "HEAD") == head and not _git(agent, "status", "--porcelain")
    assert os.path.exists(tmp_path / "cache" / "integration_conflit.diff")


def test_remote_with_agent_commits_only_keeps_current_run(repos):
    agent, human, events = repos
    _write(human, INIT, "ancien run\n")
    subprocess.run(["git", "-C", human, "-c", "user.name=Agent Cucumber", "-c", "user.email=a@b", "commit", "-qam",
                    "Ancien run"], check=True)
    _git(human, "push", "-q", "origin", "work")
    _agent_commit(agent, {INIT: "run courant\n"})
    assert integrate_remote(agent, "work") == ""
    with open(os.path.join(agent, INIT), encoding="utf-8") as f:
        assert f.read() == "run courant\n"
    assert not events
