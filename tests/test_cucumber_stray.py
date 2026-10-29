"""Fichiers de test écrits par l'agent hors du dépôt client : rapatriés dans le worktree."""
import os
import time

from tools import cucumber_stray as cs


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_files_written_in_a_parasite_folder_are_moved_into_the_worktree(tmp_path):
    run = tmp_path / "apparel"
    worktree = run / "worktree"
    write(str(worktree / ".git" / "HEAD"), "ref")
    write(str(worktree / "src/test/resources/cucumber/features/apparel/A.feature"), "Feature: ancienne\n")
    write(str(run / "build" / "src/test/java/X.java"), "ignoré : dossier de compilation")
    old = run / "vieux/src/test/java/Old.java"
    write(str(old), "ancien")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    since = time.time() - 1
    write(str(run / "cucumber_apparel_agent2/src/test/resources/cucumber/features/apparel/A.feature"),
          "Feature: corrigée\n")
    write(str(run / "cucumber_apparel_agent2/src/test/java/cucumber/features/apparel/BSteps.java"), "class B {}")
    moved = cs.relocate(str(run), str(worktree), since)
    assert sorted(t for _, t in moved) == ["src/test/java/cucumber/features/apparel/BSteps.java",
                                           "src/test/resources/cucumber/features/apparel/A.feature"]
    assert (worktree / "src/test/resources/cucumber/features/apparel/A.feature").read_text() == "Feature: corrigée\n"
    assert (worktree / "src/test/java/cucumber/features/apparel/BSteps.java").exists()
    assert not (run / "cucumber_apparel_agent2").exists()
    assert old.exists() and (run / "build" / "src/test/java/X.java").exists()      # ancien / compilation : intacts


def test_catch_up_never_overwrites_a_newer_worktree_version(tmp_path):
    run = tmp_path / "apparel"
    worktree = run / "worktree"
    write(str(worktree / ".git" / "HEAD"), "ref")
    stray = run / "egare/src/test/java/A.java"
    write(str(stray), "ancienne version égarée")
    os.utime(stray, (time.time() - 3600, time.time() - 3600))
    write(str(worktree / "src/test/java/A.java"), "version du dépôt, plus récente")
    write(str(run / "egare/src/test/java/B.java"), "nouveau")
    moved = cs.relocate(str(run), str(worktree))
    assert [t for _, t in moved] == ["src/test/java/B.java"]
    assert (worktree / "src/test/java/A.java").read_text() == "version du dépôt, plus récente"
