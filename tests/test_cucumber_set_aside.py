"""Mise de côté de tests en échec (pour analyse ultérieure), puis réactivation : rien n'est supprimé."""
import os

from tests.conftest import CLIENT, CLIENT_TAG, git, write
from tools import cucumber_coverage as cc
from tools import cucumber_set_aside as sa
from tools import cucumber_tools as ct

REL = f"src/test/resources/cucumber/features/{CLIENT}/Stock.feature"
FEATURE = (f"Feature: Stock\n\n  {CLIENT_TAG}\n  Scenario: Statut du contenant\n    Given x\n    Then y\n\n"
           f"  {CLIENT_TAG}\n  Scenario: Motif\n    Given a\n    Then b\n")


def test_set_aside_then_reactivate(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("CUCUMBER_TAG_A_REVOIR", raising=False)
    worktree = ct.worktree_dir(CLIENT)
    os.makedirs(worktree)
    git(worktree, "init", "-q", "-b", "integration")
    git(worktree, "config", "user.email", "t@e.x")
    git(worktree, "config", "user.name", "t")
    write(os.path.join(worktree, REL), FEATURE)
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "base")
    base = git(worktree, "rev-parse", "HEAD")
    ct.save_state(CLIENT, {"base": base, "lots": []})
    done = sa.set_aside(CLIENT, [{"id": f"{REL}::Statut du contenant", "motif": "OP introuvable"},
                                 {"id": f"{REL}::Inconnu", "motif": "x"}], CLIENT_TAG)
    assert done == [f"{REL}::Statut du contenant"]
    text = open(os.path.join(worktree, REL), encoding="utf-8").read()
    assert "Scenario: Statut du contenant" in text and "mis de côté" in text and "OP introuvable" in text
    assert cc.disabled_scenarios(worktree) == {f"{REL}::Statut du contenant": cc.disabled_scenarios(worktree)[
        f"{REL}::Statut du contenant"]}
    state = ct.load_state(CLIENT)
    assert [e["id"] for e in state["mis_de_cote"]] == [f"{REL}::Statut du contenant"]
    assert "mis de côté" in git(worktree, "log", "-1", "--format=%s")
    assert "mis de côté par l'utilisateur" in open(os.path.join(worktree, "A_REVOIR.md"), encoding="utf-8").read()
    assert os.path.exists(ct.patch_path(CLIENT))
    # Réactivation : bloc d'origine rétabli, tag client remis
    assert sa.reactivate(CLIENT, [f"{REL}::Statut du contenant"], CLIENT_TAG) == [f"{REL}::Statut du contenant"]
    assert open(os.path.join(worktree, REL), encoding="utf-8").read() == FEATURE
    assert ct.load_state(CLIENT)["mis_de_cote"] == [] and cc.disabled_scenarios(worktree) == {}


def test_preparation_and_set_aside_are_shared(tmp_path, monkeypatch):
    from tools import cucumber_results as cr
    from tools import knowledge_tools as kt
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("AGENT_KNOWLEDGE_DIR", str(tmp_path / "partage"))
    published = []
    monkeypatch.setattr(kt, "publish_knowledge", lambda body, projet, doc_type, sujet, **f: published.append(
        (projet, doc_type, sujet, body)) or {"statut": "depose", "message": "ok", "chemin": "", "remplacements": []})
    patch = tmp_path / "init.patch"
    patch.write_text("diff --git a/x b/x\n+Feature: Compléments\n")
    cr.record_preparation(CLIENT, True, "Compléments écrits (1 fichier).", ["a.feature"], str(patch),
                          ["entité inconnue"])
    projet, doc_type, sujet, body = published[-1]
    assert (projet, doc_type) == (CLIENT.upper(), "cucumber-initialisation") and "entité inconnue" in body
    assert "+Feature: Compléments" in body
