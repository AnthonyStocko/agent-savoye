"""Logs applicatifs déposés dans un répertoire : fenêtre des échecs extraite à l'import."""
import gzip
import os

from tools import cucumber_server_logs as sl


def test_extract_keeps_the_failure_window_with_stack_traces(tmp_path, monkeypatch):
    monkeypatch.setenv("CUCUMBER_SERVER_LOG_MARGIN", "60")
    (tmp_path / "sub").mkdir()
    (tmp_path / "server.log").write_text(
        "2026-10-28 17:10:00,001 INFO  hors fenêtre\n"
        "2026-10-28 17:28:30,100 ERROR CampaignService refus TOW-730 password=secret123\n"
        "java.lang.IllegalStateException: not A lancer\n"
        "\tat com.kis.Campaign.launch(Campaign.java:42)\n"
        "2026-10-28 17:40:00,000 INFO  hors fenêtre\n", encoding="utf-8")
    with gzip.open(tmp_path / "sub" / "server.log.2026-10-28.gz", "wt", encoding="utf-8") as f:
        f.write("2026-10-28T17:28:45 WARN notification non envoyée\n")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    out = sl.extract(str(tmp_path), ["2026-10-28T17:28:38+00:00"])
    assert out["lignes"] == 4 and "hors fenêtre" not in out["texte"]
    assert "Campaign.java:42" in out["texte"] and "notification non envoyée" in out["texte"]
    assert "secret123" not in out["texte"]
    assert sorted(out["fichiers"]) == ["server.log", os.path.join("sub", "server.log.2026-10-28.gz")]


def test_no_directory_or_no_time_gives_nothing(tmp_path):
    assert sl.extract("", ["2026-10-28T17:28:38Z"])["texte"] == ""
    assert sl.extract(str(tmp_path), [])["texte"] == ""


def test_line_cap_truncates(tmp_path, monkeypatch):
    monkeypatch.setenv("CUCUMBER_SERVER_LOG_MAX_LINES", "2")
    (tmp_path / "a.log").write_text("".join(f"2026-10-28 17:28:3{i} ERROR x{i}\n" for i in range(5)),
                                    encoding="utf-8")
    out = sl.extract(str(tmp_path), ["2026-10-28T17:28:33Z"])
    assert out["lignes"] == 2 and out["tronque"]
