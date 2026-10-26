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


# ---------------------------------------------------------------------------
# Étape 2 : format d'un document (client et projet fictifs)
# ---------------------------------------------------------------------------

def _meta(**overrides):
    values = dict(projet="PROJX", doc_type="cadrage", sujet="Calcul du stock : écart", client="ClientFictif",
                  tickets=["PROJX-12", "PROJX-12", " "], composants=["stock"], tags=["inventaire"],
                  resume="ligne 1\n\nligne 2", depots=[{"nom": "depot-fictif", "commit": "abc123"}],
                  auteur="auteur.fictif", date="2026-10-26T09:05:07")
    values.update(overrides)
    return kt.make_meta(**values)


@pytest.mark.parametrize("text, expected", [
    ('a<b>c:d"e/f\\g|h?i*j', "a-b-c-d-e-f-g-h-i-j"),
    ("  plusieurs   mots\t ici ", "plusieurs-mots-ici"),
    ("fin avec points...", "fin-avec-points"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("", "sans-titre"),
    ("???", "sans-titre"),
])
def test_names_are_cleaned_for_windows(text, expected):
    assert kt.safe_name(text) == expected


def test_document_path_follows_the_tree_and_is_bounded(tmp_path):
    path = kt.document_path(str(tmp_path), _meta())
    rel = os.path.relpath(path, tmp_path).split(os.sep)
    assert rel == ["ClientFictif", "cadrage", "2026-10-26_090507_Calcul-du-stock-écart_auteur.fictif.md"]
    # Sans client : le dossier est le projet Jira
    assert os.path.relpath(kt.document_path(str(tmp_path), _meta(client="")), tmp_path).startswith("PROJX")
    # « _ » est réservé au séparateur ; sujet très long raccourci
    long_path = kt.document_path(str(tmp_path), _meta(sujet="mot_" * 200, auteur="a_b"))
    assert len(long_path) <= kt.MAX_PATH_LEN and os.path.basename(long_path).count("_") == 3
    assert long_path.endswith("_a-b.md")


def test_meta_is_normalized_and_type_is_closed():
    meta = _meta(resume="\n".join(f"ligne {i}" for i in range(9)) + "\n" + "x" * 500)
    assert meta["tickets"] == ["PROJX-12"] and meta["statut"] == kt.STATUS_ACTIVE
    assert len(meta["resume"].splitlines()) == kt.SUMMARY_MAX_LINES and meta["version_agent"]
    assert _meta(resume="a\n" + "x" * 500)["resume"].splitlines()[1].endswith("…")
    with pytest.raises(ValueError):
        _meta(doc_type="inconnu")


def test_header_round_trip_and_body_kept_as_is():
    meta = _meta()
    body = "# Titre\n\n---\n\ncorps : avec « deux-points »\r\nfin sans saut"
    text = kt.render_document(meta, body)
    assert text.startswith("---\nprojet: \"PROJX\"\nclient: \"ClientFictif\"\ntype: \"cadrage\"\n")
    parsed, parsed_body = kt.parse_document(text)
    assert parsed == meta and parsed_body == body


def test_header_edited_by_hand_and_missing_header():
    meta, body = kt.parse_document("---\nprojet: PROJX\ntickets: PROJX-1\nstatut: 'actif'\n---\ncorps")
    assert meta == {"projet": "PROJX", "tickets": ["PROJX-1"], "statut": "actif"} and body == "corps"
    assert kt.parse_document("# pas d'en-tête") == ({}, "# pas d'en-tête")


def test_documents_are_never_overwritten(tmp_path):
    meta = _meta()
    first = kt.write_document(str(tmp_path), meta, "premier")
    second = kt.write_document(str(tmp_path), meta, "second")
    third = kt.write_document(str(tmp_path), meta, "troisième")
    assert first != second != third and second.endswith("-2.md") and third.endswith("-3.md")
    assert [kt.read_document(p)[1] for p in (first, second, third)] == ["premier", "second", "troisième"]
    # Aucun fichier temporaire laissé
    assert sorted(os.listdir(os.path.dirname(first))) == sorted(os.path.basename(p) for p in (first, second, third))


def test_atomic_write_leaves_target_intact_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "doc.md"
    assert kt.write_atomic(str(target), "v1")
    assert not kt.write_atomic(str(target), "v2") and target.read_text(encoding="utf-8") == "v1"

    def broken(*args):
        raise OSError("disque plein")
    monkeypatch.setattr(kt.os, "fsync", broken)
    with pytest.raises(OSError):
        kt.write_atomic(str(target), "v3", overwrite=True)
    assert target.read_text(encoding="utf-8") == "v1" and os.listdir(tmp_path) == ["doc.md"]


def test_update_header_changes_status_only(tmp_path):
    path = kt.write_document(str(tmp_path), _meta(), "corps\nintact\n")
    meta = kt.update_header(path, statut=kt.STATUS_REPLACED, remplace_par="ClientFictif/cadrage/autre.md")
    reread, body = kt.read_document(path)
    assert reread == meta and reread["statut"] == "remplacé" and body == "corps\nintact\n"
    with pytest.raises(ValueError):
        kt.update_header(path, statut="supprimé")
