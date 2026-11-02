"""
Mise à jour des copies de code (ensure_local_repo) : « git fetch », jamais « git pull » ; un commit local non poussé
est sauvegardé dans un bundle avant la mise à jour, la branche locale et l'arbre de travail ne bougent pas.
"""
import importlib
import io
import os
import subprocess
import sys

import pytest

from tests.conftest import git, write


@pytest.fixture
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


def test_fetch_only_and_unpushed_commits_saved(repo, tmp_path, main_module, monkeypatch):
    monkeypatch.setattr(main_module, "BACKUP_DIR", str(tmp_path / "sauvegardes"))
    write(os.path.join(repo, "a.txt"), "v1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "v1")
    clone = str(tmp_path / "clone")
    subprocess.run(["git", "clone", "-q", repo, clone], check=True)
    git(clone, "config", "user.name", "test")
    git(clone, "config", "user.email", "test@example.com")
    # Commit local non poussé dans la copie, nouveau commit sur le distant
    write(os.path.join(clone, "local.txt"), "local\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "local non poussé")
    local_head = git(clone, "rev-parse", "HEAD")
    write(os.path.join(repo, "a.txt"), "v2\n")
    git(repo, "commit", "-qam", "v2")

    ok, message = main_module.update_clone(clone, "clone")
    assert ok and "(fetch)" in message and "1 commit(s) local(aux) non poussé(s) sauvegardé(s)" in message
    # Aucun pull : branche locale et arbre de travail inchangés, référence distante à jour
    assert git(clone, "rev-parse", "HEAD") == local_head
    with open(os.path.join(clone, "a.txt"), encoding="utf-8") as f:
        assert f.read() == "v1\n"
    assert git(clone, "rev-parse", "origin/integration") == git(repo, "rev-parse", "HEAD")
    bundles = os.listdir(tmp_path / "sauvegardes")
    assert len(bundles) == 1 and bundles[0].startswith("clone-") and bundles[0].endswith(".bundle")
    assert git(clone, "bundle", "verify", str(tmp_path / "sauvegardes" / bundles[0])).count("\n") >= 0

    # Rien de local : aucune sauvegarde
    git(clone, "reset", "-q", "--hard", "origin/integration")
    ok, message = main_module.update_clone(clone, "clone")
    assert ok and "sauvegard" not in message and len(os.listdir(tmp_path / "sauvegardes")) == 1


def test_no_pull_left_in_main():
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"),
              encoding="utf-8") as f:
        assert '"pull"' not in f.read()
