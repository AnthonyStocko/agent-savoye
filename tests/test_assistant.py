"""Onglet Assistant : carte des résultats, conversation, réponse de l'agent (lecture seule, secrets masqués)."""
import importlib
import io
import os
import sys

from tools import assistant as a


def test_results_index_points_to_what_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "TMP", str(tmp_path / ".tmp"))
    monkeypatch.setattr(a, "WORKSPACE_DIR", str(tmp_path))
    client = tmp_path / ".tmp" / "cucumber" / "apparel"
    (client / "imports" / "7").mkdir(parents=True)
    (client / "imports" / "6").mkdir()
    (client / "etat.json").write_text("{}")
    (tmp_path / "apparel-cucumber.patch").write_text("diff")
    index = a.results_index()
    assert "client « apparel »" in index and "le plus récent : n° 7" in index
    assert "etat.json" in index and "apparel-cucumber.patch" in index and "bilan de santé" not in index
    monkeypatch.setattr(a, "TMP", str(tmp_path / "vide"))
    monkeypatch.setattr(a, "WORKSPACE_DIR", str(tmp_path / "vide"))
    assert a.results_index() == "(aucun résultat enregistré pour l'instant)"


def test_history_and_recent_exchanges(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "ROOT", str(tmp_path))
    monkeypatch.setattr(a, "HISTORY", str(tmp_path / "c.json"))
    assert a.load_history() == []
    a.save_history([{"role": "user", "content": "Q1"}, {"role": "assistant", "content": "R1"}])
    assert a.recent_exchanges(a.load_history()) == "Utilisateur : Q1\n\nAssistant : R1"
    a.clear_history()
    assert a.load_history() == [] and a.recent_exchanges([]) == "(début de la conversation)"


def test_ask_step_writes_a_masked_answer(tmp_path, monkeypatch):
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        m = importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved
    monkeypatch.setattr(a, "HISTORY", str(tmp_path / "c.json"))
    a.save_history([{"role": "user", "content": "Avant"}, {"role": "assistant", "content": "Réponse avant"},
                    {"role": "user", "content": "Pourquoi 16 échecs ?"}])
    prompts = []

    def claude(prompt, model, dirs, tools, **kwargs):
        prompts.append((prompt, dirs, tools))
        return 0, "Ils échouent sur **OP introuvable**. Authorization: Bearer abc.def.ghi", {}, False
    monkeypatch.setattr(m, "run_claude", claude)
    monkeypatch.setattr(m, "report_token_usage", lambda *a, **k: None)
    out = tmp_path / "r.md"
    assert m.run_assistant("Pourquoi 16 échecs ?", str(out)) == 0
    answer = out.read_text(encoding="utf-8")
    assert "OP introuvable" in answer and "abc.def.ghi" not in answer
    prompt, dirs, tools = prompts[0]
    assert "Pourquoi 16 échecs ?" in prompt and "Réponse avant" in prompt and ".env" in prompt
    assert tools == ["Read", "Grep", "Glob"]
    assert m.run_assistant("  ", str(out)) == 2
