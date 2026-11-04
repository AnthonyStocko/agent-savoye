from tools import memory_tools


def _memory_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(memory_tools, "get_memory_dir", lambda: str(tmp_path / "memory"))
    (tmp_path / "memory").mkdir()


def test_analyse_presente_dans_le_workspace(tmp_path, monkeypatch):
    _memory_dir(monkeypatch, tmp_path)
    fichier = tmp_path / "PROJ-1-cadrage.md"
    fichier.write_text("analyse", encoding="utf-8")
    assert memory_tools.restore_cadrage_file("PROJ-1", str(fichier))


def test_analyse_restauree_depuis_la_memoire(tmp_path, monkeypatch):
    _memory_dir(monkeypatch, tmp_path)
    memory_tools.save_cadrage_memory("PROJ-2", "PROJ", "## Analyse mémorisée")
    fichier = tmp_path / "PROJ-2-cadrage.md"
    assert memory_tools.restore_cadrage_file("PROJ-2", str(fichier))
    assert fichier.read_text(encoding="utf-8") == "## Analyse mémorisée"


def test_aucune_analyse(tmp_path, monkeypatch):
    _memory_dir(monkeypatch, tmp_path)
    fichier = tmp_path / "PROJ-3-cadrage.md"
    assert not memory_tools.restore_cadrage_file("PROJ-3", str(fichier))
    assert not fichier.exists()


def test_ouverture_de_la_revision():
    ouvrir = memory_tools.should_open_revision
    assert ouvrir(False, True, "INIT", "", "PROJ-1")
    assert ouvrir(False, True, "REVIEW_ANALYSIS", "PROJ-9", "PROJ-1")      # autre ticket sélectionné
    assert not ouvrir(False, True, "FINISHED", "PROJ-1", "PROJ-1")         # travail en cours sur ce ticket
    assert not ouvrir(False, False, "INIT", "", "PROJ-1")                  # pas d'analyse
    assert not ouvrir(True, True, "INIT", "", "PROJ-1")                    # épopée : parcours dédié
