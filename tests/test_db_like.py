"""
Requête avec « % » (LIKE '%x%') : jamais lue comme un marqueur de paramètre par un pilote à paramètres « format »
ou « pyformat » (psycopg2 : « immutabledict is not a sequence »).
"""
import sqlite3

import pytest

from tools import db_tools


@pytest.mark.parametrize("paramstyle, expected", [
    ("pyformat", "SELECT code FROM kis_job WHERE code LIKE '%%Purge%%'"),
    ("format", "SELECT code FROM kis_job WHERE code LIKE '%%Purge%%'"),
    ("qmark", "SELECT code FROM kis_job WHERE code LIKE '%Purge%'"),
    ("named", "SELECT code FROM kis_job WHERE code LIKE '%Purge%'")])
def test_percent_escaped_for_format_drivers(paramstyle, expected):
    assert db_tools.driver_sql("SELECT code FROM kis_job WHERE code LIKE '%Purge%'", paramstyle) == expected


def test_like_query_end_to_end(tmp_path, monkeypatch):
    path = str(tmp_path / "base.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE kis_job (code TEXT)")
        conn.executemany("INSERT INTO kis_job VALUES (?)", [("PurgeStock",), ("Export",), ("PurgeOrder",)])
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_NAME", path)
    previous = db_tools.set_profile("")
    try:
        out = db_tools.run_query("SELECT code FROM kis_job WHERE code LIKE '%Purge%' ORDER BY code")
    finally:
        db_tools.set_profile(previous)
    assert out.splitlines() == ["code", "PurgeOrder", "PurgeStock", "(2 ligne(s))"]
