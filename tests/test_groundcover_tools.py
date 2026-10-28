"""Logs groundcover : filtre d'environnement imposé, fenêtres des échecs, CLI bornée."""
import json

import pytest

from tools import groundcover_tools as gc


class _Response:
    def __init__(self, rows, status=200):
        self.status_code, self._rows, self.text = status, rows, json.dumps(rows)

    def json(self):
        return self._rows


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("GROUNDCOVER_API_KEY", "k3y")
    monkeypatch.setenv("CUCUMBER_GROUNDCOVER_FILTER", "namespace:apparel-test")
    monkeypatch.delenv("GROUNDCOVER_BACKEND_ID", raising=False)
    calls = []

    def post(url, headers, timeout, json):
        calls.append({"url": url, "headers": headers, "body": json})
        return _Response([{"_time": "2026-10-28T17:28:30.123Z", "workload": "wms", "level": "error",
                           "content": "refus campagne TOW-730 token=abc123"}])
    monkeypatch.setattr(gc.requests, "post", post)
    return calls


def test_not_configured_without_environment_filter(monkeypatch):
    monkeypatch.setenv("GROUNDCOVER_API_KEY", "k")
    monkeypatch.delenv("CUCUMBER_GROUNDCOVER_FILTER", raising=False)
    assert not gc.configured() and gc.window_extract(["2026-10-28T17:28:38Z"])["texte"] == ""


def test_window_extract_queries_merged_windows_with_the_filter(api):
    out = gc.window_extract(["2026-10-28T17:28:38Z", "2026-10-28T17:29:00Z", "2026-10-28T18:00:00Z"])
    assert len(api) == 2                                                  # deux fenêtres voisines fusionnées
    body = api[0]["body"]
    assert body["start"] == "2026-10-28T17:26:38Z" and body["end"] == "2026-10-28T17:31:00Z"
    assert body["query"].startswith("(namespace:apparel-test) | sort by (_time asc)")
    assert api[0]["headers"]["Authorization"] == "Bearer k3y"
    assert "[wms] 2026-10-28T17:28:30 ERROR refus campagne TOW-730" in out["texte"]
    assert "abc123" not in out["texte"] and out["plages"][0] == ["2026-10-28T17:26:38", "2026-10-28T17:31:00"]


def test_cli_is_bounded_and_keeps_the_environment_filter(api, capsys):
    assert gc.main(["search", "--start", "2026-10-28T17:00:00Z", "--end", "2026-10-28T18:00:00Z"]) == 2
    assert gc.main(["search", "--start", "2026-10-28T17:28:00Z", "--end", "2026-10-28T17:30:00Z",
                    "--query", "x | stats count()"]) == 1                 # pas de pipe
    assert gc.main(["search", "--start", "2026-10-28T17:28:00Z", "--end", "2026-10-28T17:30:00Z",
                    "--query", "TOW-730", "--limit", "5000"]) == 0
    assert api[-1]["body"]["query"] == ("(namespace:apparel-test) AND (TOW-730) | sort by (_time asc) | fields "
                                        "_time, workload, level, content | limit 200")
    assert "refus campagne TOW-730" in capsys.readouterr().out


def test_http_error_is_reported(api, monkeypatch):
    monkeypatch.setattr(gc.requests, "post", lambda *a, **k: _Response({"error": "unauthorized"}, 401))
    out = gc.window_extract(["2026-10-28T17:28:38Z"])
    assert "HTTP 401" in out["erreur"] and out["texte"] == ""
