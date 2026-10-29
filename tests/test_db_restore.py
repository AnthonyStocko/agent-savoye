"""Recopie de la config client dans le schéma des tests Cucumber (méthode du script restore.ps1)."""
import os
import shutil
import subprocess

import pytest

from tools import db_restore as r

LISTING = """;
; Archive created at 2026-10-29
3; 2615 16386 SCHEMA - apparel_prod pgadm
218; 1259 16387 TABLE apparel_prod ref_site pgadm
3401; 0 16387 TABLE DATA apparel_prod ref_site pgadm
3402; 0 16390 TABLE DATA apparel_prod wms_operation_history pgadm
3403; 0 16391 TABLE DATA apparel_prod kis_execution_log pgadm
3404; 0 16392 TABLE DATA apparel_prod temp_ pgadm
3405; 0 16393 TABLE DATA apparel_prod mon_historique pgadm
"""


def test_listing_excludes_history_data_like_the_script():
    filtered, excluded = r.filter_listing(LISTING, ["mon_historique"])
    lines = filtered.splitlines()
    assert ";3402; 0 16390 TABLE DATA apparel_prod wms_operation_history pgadm" in lines
    assert ";3403; 0 16391 TABLE DATA apparel_prod kis_execution_log pgadm" in lines      # préfixe kis_execution
    assert ";3404; 0 16392 TABLE DATA apparel_prod temp_ pgadm" in lines                  # nom finissant par _
    assert ";3405; 0 16393 TABLE DATA apparel_prod mon_historique pgadm" in lines         # RESTORE_EXCLUDE_DATA
    assert "3401; 0 16387 TABLE DATA apparel_prod ref_site pgadm" in lines                # config gardée
    assert "218; 1259 16387 TABLE apparel_prod ref_site pgadm" in lines                   # structure gardée
    assert excluded == 4


def test_plan_refuses_dangerous_targets():
    pg = {"type": "postgresql", "host": "h", "port": "5432", "name": "db", "user": "u", "password": ""}
    assert r.check_plan("apparel_prod", "cucumber_apparel", pg, {**pg, "name": "tests"}) == []
    assert "interdit" in r.check_plan("apparel_prod", "public", pg, {**pg, "name": "tests"})[0]
    assert "même schéma" in r.check_plan("x", "X", pg, pg)[0]
    assert "invalide" in r.check_plan("a;drop", "b", pg, {**pg, "name": "t"})[0]
    assert "PostgreSQL" in r.check_plan("a", "b", {**pg, "type": "oracle"}, {**pg, "name": "t"})[0]


def test_source_falls_back_to_client_db(monkeypatch):
    monkeypatch.setenv("DB_HOST", "client-host")
    monkeypatch.setenv("DB_NAME", "db_client")
    monkeypatch.setenv("CLIENT_SOURCE_DB_NAME", "autre")
    monkeypatch.delenv("CLIENT_SOURCE_DB_HOST", raising=False)
    source = r.source_connection()
    assert (source["host"], source["name"]) == ("client-host", "autre")


PORT = os.getenv("PGTEST_PORT", "5434")


def _server() -> bool:
    if not shutil.which("psql"):
        return False
    probe = subprocess.run(["psql", "-h", "localhost", "-p", PORT, "-U", "pgadm", "-d", "postgres", "-tAc",
                            "select 1"], capture_output=True, text=True)
    return probe.returncode == 0


def _sql(db, sql):
    return subprocess.run(["psql", "-h", "localhost", "-p", PORT, "-U", "pgadm", "-d", db, "-tA", "-q", "-c", sql],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.skipif(not _server(), reason="pas de serveur PostgreSQL de test (PGTEST_PORT)")
def test_real_restore_then_rollback(tmp_path, monkeypatch):
    for db in ("t_client", "t_tests"):
        subprocess.run(["psql", "-h", "localhost", "-p", PORT, "-U", "pgadm", "-d", "postgres", "-q", "-c",
                        f"drop database if exists {db}", "-c", f"create database {db}"], check=True)
    _sql("t_client", "create schema src; create table src.ref_site(code text); insert into src.ref_site values "
                     "('VDA'); create table src.wms_operation_history(id int); insert into src.wms_operation_history "
                     "values (1)")
    _sql("t_tests", "create schema cuc; create table cuc.ref_site(code text); insert into cuc.ref_site values "
                    "('OLD'); create schema autre; create table autre.t(x int); insert into autre.t values (42)")
    for prefix, db in (("CLIENT_SOURCE_DB_", "t_client"), ("CUCUMBER_DB_", "t_tests")):
        for key, value in (("HOST", "localhost"), ("PORT", PORT), ("NAME", db), ("USER", "pgadm"),
                           ("TYPE", "postgresql")):
            monkeypatch.setenv(prefix + key, value)
    monkeypatch.setattr(r, "ROOT", str(tmp_path))
    entry = r.restore_client_config("clientx", "src", "cuc", log=lambda m: None)
    assert entry["ok"] and entry["exclus"] == 1, entry
    assert _sql("t_tests", "select code from cuc.ref_site") == "VDA"
    assert _sql("t_tests", "select count(*) from cuc.wms_operation_history") == "0"      # structure, sans données
    assert _sql("t_tests", "select x from autre.t") == "42"                              # autre schéma intact
    back = r.restore_backup("clientx", entry["sauvegarde"], "cuc", log=lambda m: None)
    assert back["ok"] and _sql("t_tests", "select code from cuc.ref_site") == "OLD"
    busy = r.restore_client_config("clientx", "src", "cuc", log=lambda m: None, jenkins_busy=lambda: ["JOB : 12"])
    assert not busy["ok"] and "Jenkins" in busy["message"] and _sql("t_tests", "select code from cuc.ref_site") == "OLD"
    assert [h["ok"] for h in r.load_history("clientx")] == [True, True, False]
