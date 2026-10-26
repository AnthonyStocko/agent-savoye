"""Accès base des lots Cucumber : tous les lots et leurs corrections, commandes journalisées et affichées."""
import argparse
import importlib
import io
import json
import os
import sys

import pytest

from tools import cucumber_tools as ct
from tools import db_tools


@pytest.fixture
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


def test_query_tables_ignores_literals_and_comments():
    sql = ("SELECT a.code FROM stock.article a JOIN emplacement e ON e.id = a.empl "
           "WHERE a.libelle = 'FROM client_secret' -- JOIN commentee\n AND a.id IN (SELECT id FROM reserve)")
    assert db_tools.query_tables(sql) == ["stock.article", "emplacement", "reserve"]
    assert db_tools.query_tables("SELECT 'non fermé") == []


def test_log_command_records_tables_but_never_values(tmp_path, monkeypatch):
    journal = tmp_path / "journal.jsonl"
    monkeypatch.delenv("AGENT_DB_LOG", raising=False)
    query = argparse.Namespace(command="query", sql="SELECT nom FROM personne WHERE nom = 'Durand'", like="")
    db_tools.log_command(query, "SCHEMA_FICTIF", "")
    assert not journal.exists()  # sans AGENT_DB_LOG, rien n'est journalisé
    monkeypatch.setenv("AGENT_DB_LOG", str(journal))
    db_tools.log_command(query, "SCHEMA_FICTIF", "")
    db_tools.log_command(argparse.Namespace(command="describe", table="ARTICLE"), "SCHEMA_FICTIF", "table absente")
    db_tools.log_command(argparse.Namespace(command="tables", like="ART%"), "SCHEMA_FICTIF", "")
    entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [e["commande"] for e in entries] == ["query", "describe", "tables"]
    assert entries[0]["tables"] == ["personne"] and entries[0]["ok"] and "Durand" not in journal.read_text("utf-8")
    assert entries[1] == {**entries[1], "tables": ["ARTICLE"], "ok": False, "erreur": "table absente"}
    assert entries[2]["motif"] == "ART%"


def test_every_lot_call_collects_the_db_journal(main_module, tmp_path, monkeypatch):
    def fake_claude(prompt, model, add_dirs, allowed_tools, **kwargs):
        # L'agent lance deux commandes base pendant l'appel (environnement hérité par db_tools)
        with open(os.environ["AGENT_DB_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"commande": "query", "tables": ["article"], "ok": True}) + "\n")
            f.write("ligne illisible\n")
            f.write(json.dumps({"commande": "describe", "tables": ["emplacement"], "ok": False,
                                "erreur": "absente"}) + "\n")
        return 0, "", {}, False
    monkeypatch.setattr(main_module, "run_claude", fake_claude)
    log = []
    main_module.run_cucumber_claude("p", "m", [], "10", str(tmp_path), "", db_log=log, db_step="écriture")
    assert [(e["commande"], e["etape"]) for e in log] == [("query", "écriture"), ("describe", "écriture")]
    assert "AGENT_DB_LOG" not in os.environ and os.listdir(tmp_path) == []
    assert main_module.db_summary(log) == "2 commande(s), tables : article, emplacement, 1 en erreur"
    assert main_module.db_summary([]) == "aucune commande"


def test_db_tools_granted_with_the_db_block_only(main_module):
    assert main_module.lot_tools({"db_block": ""}) == main_module.CUCUMBER_TOOLS
    assert main_module.lot_tools({"db_block": "\n=== ACCÈS BASE ==="})[-1] == f"Bash({sys.executable}:*)"


def test_db_block_for_cucumber_covers_reference_data(main_module, monkeypatch):
    monkeypatch.setattr(main_module.db_tools, "is_db_configured", lambda: True)
    monkeypatch.setattr(main_module.db_tools, "get_db_config", lambda: {"type": "postgresql"})
    monkeypatch.setattr(main_module.db_tools, "describe_db_target", lambda: "base fictive")
    block = main_module.build_db_block("SCHEMA_FICTIF", sys.executable, ".", step="generate-cucumber-client")
    assert "SCHEMA_FICTIF" in block and "donnée de référence absente" in block and "« restantes »" in block
    assert main_module.build_db_block("", sys.executable, ".", step="generate-cucumber-client") == ""


def test_fix_and_lot_prompts_receive_the_db_block():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("cucumber_client_fix.md", "cucumber_client_lot.md"):
        with open(os.path.join(root, "prompts", name), encoding="utf-8") as f:
            assert "${db_block}" in f.read(), name


def test_coverage_summary_lists_db_usage_per_lot():
    state = {"lots": [
        {"numero": 1, "base": [{"etape": "écriture", "tables": ["article"], "ok": True},
                               {"etape": "écriture", "tables": ["article", "stock"], "ok": True},
                               {"etape": "correction après exécution", "tables": [], "ok": False,
                                "erreur": "DB_ERROR"}]},
        {"numero": 2, "base": []},
    ]}
    lines = ct.db_usage_lines(state)
    assert "## Base de données consultée" in lines
    assert "- Lot 1, écriture : 2 commande(s) — tables : article, stock" in lines
    assert "- Lot 1, correction après exécution : 1 commande(s) — 1 en erreur (DB_ERROR)" in lines
    assert not any(line.startswith("- Lot 2") for line in lines)
    assert ct.db_usage_lines({"lots": [{"numero": 1}]}) == []
