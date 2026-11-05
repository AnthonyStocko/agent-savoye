"""Copies de l'agent remises à jour quand c'est sans risque (Claude lit le code dans leurs fichiers)."""
import importlib
import io
import os
import sys

from tests.conftest import git, write


def _main():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


def _repos(tmp_path):
    origin, seed, clone = (str(tmp_path / n) for n in ("origin.git", "seed", "clone"))
    os.makedirs(origin)
    git(origin, "init", "-q", "--bare", "-b", "integration")
    os.makedirs(seed)
    git(seed, "init", "-q", "-b", "integration")
    for repo in (seed,):
        git(repo, "config", "user.name", "t")
        git(repo, "config", "user.email", "t@e.x")
    write(os.path.join(seed, "a.txt"), "1\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "1")
    git(seed, "remote", "add", "origin", origin)
    git(seed, "push", "-q", "origin", "integration")
    git(str(tmp_path), "clone", "-q", origin, clone)
    git(clone, "config", "user.name", "t")
    git(clone, "config", "user.email", "t@e.x")
    for n in (2, 3):
        write(os.path.join(seed, "a.txt"), f"{n}\n")
        git(seed, "commit", "-qam", str(n))
    git(seed, "push", "-q", "origin", "integration")
    git(clone, "fetch", "-q", "origin")
    return clone


def test_tracked_branch_behind_is_fast_forwarded(tmp_path):
    main, clone = _main(), _repos(tmp_path)
    assert "2 commit(s) appliqué(s) en avance rapide" in main.sync_working_copy(clone)
    assert open(os.path.join(clone, "a.txt")).read() == "3\n"
    assert main.sync_working_copy(clone) == ""


def test_untracked_work_branch_back_to_integration_branch_kept(tmp_path):
    main, clone = _main(), _repos(tmp_path)
    git(clone, "checkout", "-q", "-b", "SPEC-1996_dev_pts")
    write(os.path.join(clone, "b.txt"), "travail\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "travail local")
    done = main.sync_working_copy(clone)
    assert "copie remise sur integration (la branche SPEC-1996_dev_pts est conservée)" in done
    assert git(clone, "rev-parse", "--abbrev-ref", "HEAD").strip() == "integration"
    assert open(os.path.join(clone, "a.txt")).read() == "3\n"
    assert "SPEC-1996_dev_pts" in git(clone, "branch", "--list")


def test_uncommitted_changes_never_touched(tmp_path):
    main, clone = _main(), _repos(tmp_path)
    write(os.path.join(clone, "a.txt"), "modif en cours\n")
    assert main.sync_working_copy(clone) == ""
    assert open(os.path.join(clone, "a.txt")).read() == "modif en cours\n"
