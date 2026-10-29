"""Auto-amélioration : compétences tirées des réussites prouvées, proposées aux tâches semblables."""
import importlib
import io
import os
import sys

from tools import skills as sk

PROPOSAL = {"action": "creer", "nom": "OP introuvable : créer l'OP dans le Background",
            "description": "Un contrôle échoue car l'ordre de préparation n'existe pas après l'init",
            "declencheurs": ["OP introuvable", "Cannot access first() element from an empty List"],
            "etapes": ["Lire en base l'OP cherché par le step", "Absent : le créer dans le Background par le step "
                       "PREP- Création d'un ordre de préparation", "Relancer le scénario seul"],
            "pieges": ["Ne pas modifier l'init globale"], "exemple": "TOW-730 chez apparel ; token=abc123"}


def test_save_load_relevant_and_use(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_DIR", str(tmp_path))
    path = sk.save_proposal("cucumber-correction", PROPOSAL, "apparel — 1 scénario vert", client="apparel")
    assert path == str(tmp_path / "competences" / "op-introuvable-creer-l-op-dans-le-background.md")
    text = open(path, encoding="utf-8").read()
    assert "## Procédure" in text and "1. Lire en base" in text and "abc123" not in text
    loaded = sk.load_skills("cucumber-correction")
    assert len(loaded) == 1 and loaded[0]["client"] == "apparel" and loaded[0]["utilisations"] == 0
    assert sk.load_skills("cucumber-init") == []
    block = sk.prompt_block("cucumber-correction", "TOW-731 : customBoolean1 attendu, obtenu OP introuvable")
    assert "COMPÉTENCES ACQUISES" in block and path in block and "competences_utilisees" in block
    assert sk.record_use(["OP introuvable : créer l'OP dans le Background", "inconnue"]) == [PROPOSAL["nom"]]
    assert sk.load_skills()[0]["utilisations"] == 1
    # Mise à jour : même fichier, compteur conservé
    sk.save_proposal("cucumber-correction", {**PROPOSAL, "action": "mettre_a_jour", "etapes": ["Nouvelle étape"]},
                     "apparel — 2")
    again = sk.load_skills()
    assert len(again) == 1 and again[0]["utilisations"] == 1 and "Nouvelle étape" in again[0]["corps"]
    sk.set_active(path, False)
    assert sk.load_skills() == [] and sk.prompt_block("cucumber-correction", "OP introuvable") == ""
    assert len(sk.load_skills(include_disabled=True)) == 1
    monkeypatch.setenv("SKILLS_AUTO", "0")
    sk.set_active(path, True)
    assert sk.prompt_block("cucumber-correction", "OP introuvable") == ""


def test_nothing_saved_without_reusable_procedure(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_DIR", str(tmp_path))
    assert sk.save_proposal("cucumber-correction", {"action": "aucune"}, "x") == ""
    assert sk.save_proposal("cucumber-correction", {**PROPOSAL, "etapes": []}, "x") == ""
    assert sk.parse_proposal('bla ```json\n{"action": "aucune"}\n```') == {"action": "aucune"}


def test_learn_skill_only_after_a_complex_success(tmp_path, monkeypatch):
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        m = importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved
    monkeypatch.setenv("AGENT_KNOWLEDGE_DIR", str(tmp_path))
    monkeypatch.delenv("SKILLS_AUTO", raising=False)
    monkeypatch.setenv("SKILLS_MIN_TURNS", "8")
    calls = []

    def claude(prompt, *a, **k):
        calls.append(prompt)
        return 0, "```json\n" + __import__("json").dumps(PROPOSAL) + "\n```", {}, False
    monkeypatch.setattr(m, "run_claude", claude)
    monkeypatch.setattr(m, "report_token_usage", lambda *a, **k: None)
    assert m.learn_skill("apparel", "cucumber-correction", "S1 vert", "preuve", str(tmp_path), "", str(tmp_path),
                         "K", 3) == ""                                   # trop simple : rien
    assert calls == []
    path = m.learn_skill("apparel", "cucumber-correction", "- S1 : échec « OP introuvable »", "1 scénario vert sur "
                         "Jenkins", str(tmp_path), "", str(tmp_path), "K", 12)
    assert path and os.path.exists(path) and "OP introuvable" in calls[0] and "(aucune)" in calls[0]
