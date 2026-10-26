"""Connaissances partagées : configuration et test du répertoire (étape 1)."""
import os

import pytest

from tools import knowledge_tools as kt


@pytest.mark.parametrize("value, expected", [
    ("", ""),
    ("   ", ""),
    (r'"\\serveur\partage\agent\"', r"\\serveur\partage\agent"),
    (r"X:\agent-savoye\\", r"X:\agent-savoye"),
    ("X:\\", "X:\\"),
    ("/mnt/partage/agent/", "/mnt/partage/agent"),
    ("/", "/"),
    ("'/srv/agent'", "/srv/agent"),
])
def test_paths_are_normalized_without_breaking_unc_or_roots(value, expected):
    assert kt.normalize_dir(value) == expected


def test_configuration_from_environment(monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_DIR", "")
    assert not kt.enabled() and kt.knowledge_dir() == ""
    monkeypatch.setenv("AGENT_KNOWLEDGE_DIR", "$KNOWLEDGE_ROOT/agent")
    monkeypatch.setenv("KNOWLEDGE_ROOT", "/srv/partage")
    assert kt.enabled() and kt.knowledge_dir().replace("\\", "/") == "/srv/partage/agent"
    monkeypatch.setenv("AGENT_KNOWLEDGE_AUTHOR", "  auteur.fictif ")
    assert kt.author() == "auteur.fictif"
    monkeypatch.setenv("AGENT_KNOWLEDGE_AUTHOR", "")
    assert kt.author()  # utilisateur système
    monkeypatch.setenv("AGENT_KNOWLEDGE_MAX_CONTEXT", "abc")
    assert kt.max_context() == kt.DEFAULT_MAX_CONTEXT
    monkeypatch.setenv("AGENT_KNOWLEDGE_MAX_CONTEXT", "0")
    assert kt.max_context() == 0


def test_writable_directory_passes_and_leaves_nothing(tmp_path):
    result = kt.test_dir(str(tmp_path))
    assert result["ok"] and [s[0] for s in result["etapes"]] == ["existence", "lecture", "écriture"]
    assert "temps d'accès" in result["message"] and os.listdir(tmp_path) == []


def test_failures_say_what_to_check(tmp_path, monkeypatch):
    assert "désactivées" in kt.test_dir("")["message"]
    missing = kt.test_dir(str(tmp_path / "absent"))
    assert not missing["ok"] and "introuvable" in missing["message"] and "VPN" in missing["message"]
    file_path = tmp_path / "fichier.txt"
    file_path.write_text("x")
    assert "n'est pas un dossier" in kt.test_dir(str(file_path))["message"]

    def refuse(path):
        raise PermissionError("refusé")
    monkeypatch.setattr(kt, "_write_witness", refuse)
    denied = kt.test_dir(str(tmp_path))
    assert not denied["ok"] and "pas de droit d'écriture" in denied["message"]
    assert denied["etapes"][-1][0] == "écriture" and not denied["etapes"][-1][1]


def test_unreachable_share_times_out(tmp_path, monkeypatch):
    def blocked(function, *args):
        raise TimeoutError("aucune réponse en 15 s")
    monkeypatch.setattr(kt, "_timed", blocked)
    result = kt.test_dir(str(tmp_path))
    assert not result["ok"] and "injoignable" in result["message"]
