"""Profil de connexion « cucumber » : base de l'environnement de test, à défaut la base du socle."""
from tools import db_tools


def test_cucumber_profile(monkeypatch):
    monkeypatch.setenv("DB_TYPE", "postgresql")
    monkeypatch.setenv("DB_HOST", "std-host")
    monkeypatch.setenv("DB_SCHEMA", "STD")
    for k in ("HOST", "NAME", "TYPE", "SCHEMA"):
        monkeypatch.delenv(f"CUCUMBER_DB_{k}", raising=False)
    previous = db_tools.set_profile("cucumber")
    try:
        assert db_tools.get_db_config()["host"] == "std-host"
        monkeypatch.setenv("CUCUMBER_DB_TYPE", "oracle")
        monkeypatch.setenv("CUCUMBER_DB_HOST", "test-host")
        monkeypatch.setenv("CUCUMBER_DB_SCHEMA", "APPAREL")
        cfg = db_tools.get_db_config()
        assert (cfg["type"], cfg["host"], cfg["schema"]) == ("oracle", "test-host", "APPAREL")
        db_tools.set_profile("")
        assert db_tools.get_db_config()["host"] == "std-host"
    finally:
        db_tools.set_profile(previous)
