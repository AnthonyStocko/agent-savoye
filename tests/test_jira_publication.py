"""
Règle : l'agent ne modifie jamais la description Jira tant que l'utilisateur n'a pas validé l'analyse.
Jira est simulé : aucun appel réseau.
"""
import io
import os
import sys
from datetime import datetime

import pytest

from tools import analysis_publication, epic_tools, integration_tools, jira_guard, jira_tools


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(analysis_publication, "PUBLICATION_DIR", str(tmp_path / "publication"))
    return tmp_path


@pytest.fixture
def fake_jira(monkeypatch):
    """Jira simulé au niveau HTTP : enregistre chaque appel ; les lectures renvoient une description fixe."""
    calls = []

    class Response:
        def __init__(self, status, payload=None):
            self.status_code, self._payload, self.text = status, payload or {}, ""

        def json(self):
            return self._payload

    def get(url, **kwargs):
        calls.append(("GET", url))
        return Response(200, {"fields": {"description": {"type": "doc", "version": 1, "content": []}}})

    def write(method):
        def call(url, **kwargs):
            calls.append((method, url))
            return Response(201 if method == "POST" else 204, {"key": "PROJ-99"})
        return call

    monkeypatch.setattr(jira_tools.requests, "get", get)
    monkeypatch.setattr(jira_tools.requests, "put", write("PUT"))
    monkeypatch.setattr(jira_tools.requests, "post", write("POST"))
    monkeypatch.setenv("JIRA_DOMAIN", "jira.example")
    monkeypatch.setenv("JIRA_EMAIL", "agent@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "jeton-ecriture")
    return calls


def writes(calls):
    return [c for c in calls if c[0] in ("PUT", "POST")]


def load_main():
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = io.StringIO()
    try:
        import main
        return main
    finally:
        sys.stdout, sys.stderr = saved


@pytest.mark.parametrize("step", ["cadrage", "refine", "cadrage-reponses", "epic-cadrage"])
def test_aucune_ecriture_jira_possible_pour_l_agent(step, fake_jira, monkeypatch, tmp_path):
    assert jira_guard.applies(step)
    env = jira_guard.sandbox_env(dict(os.environ))
    assert env["JIRA_API_TOKEN"] == "" and env["JIRA_EMAIL"] == "" and env[jira_tools.READ_ONLY_ENV] == "1"
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    args = jira_guard.claude_args(str(tmp_path))
    assert "--strict-mcp-config" in args
    assert any(rule.startswith("Read(") and rule.endswith("/.env)") for rule in args)

    # Le sous-processus de l'agent tente d'écrire avec l'environnement reçu (outil Jira simulé)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    patch_file = tmp_path / "x.patch"
    patch_file.write_text("diff", encoding="utf-8")
    attempts = [
        lambda: jira_tools.update_jira_cadrage("PROJ-1", "# Cadrage PROJ-1\nTexte"),
        lambda: jira_tools.set_jira_description_markdown("PROJ-1", "Texte"),
        lambda: jira_tools.update_jira_section("PROJ-1", "Section", "Texte"),
        lambda: jira_tools.update_jira_release_note("PROJ-1", "Paragraphe"),
        lambda: jira_tools.create_jira_issue("PROJ", "Titre", "Texte", "", []),
    ]
    for attempt in attempts:
        with pytest.raises(jira_tools.JiraReadOnly):
            attempt()
    assert jira_tools.add_jira_attachment("PROJ-1", str(patch_file)).startswith("Erreur")
    assert writes(fake_jira) == []


def test_jeton_de_lecture_seul_transmis_s_il_est_configure():
    env = jira_guard.sandbox_env({"JIRA_API_TOKEN": "ecriture", "JIRA_EMAIL": "a@x",
                                  "JIRA_READ_API_TOKEN": "lecture", "JIRA_READ_EMAIL": "lecteur@x"})
    assert env["JIRA_API_TOKEN"] == "lecture" and env["JIRA_EMAIL"] == "lecteur@x"


def test_les_regles_existantes_sont_fusionnees(tmp_path):
    args = jira_guard.claude_args(str(tmp_path), ("--add-dir", "/k", "--disallowedTools", "Write(//k/**)"))
    assert args.count("--disallowedTools") == 1 and "Write(//k/**)" in args and args[:2] == ("--add-dir", "/k")


def test_hors_analyse_pas_de_garde_fou():
    assert not jira_guard.applies("patch") and not jira_guard.applies("besoin")


def test_description_modifiee_pendant_l_etape_signalee_en_erreur(monkeypatch, capsys):
    main = load_main()
    descriptions = {"PROJ-1": {"type": "doc", "content": []}}
    monkeypatch.setattr(jira_guard, "get_jira_description", lambda key: descriptions[key])
    before = jira_guard.snapshot(["PROJ-1"], "cadrage")
    assert main.check_jira_untouched("cadrage", before, ["PROJ-1"])
    descriptions["PROJ-1"] = {"type": "doc", "content": [{"type": "paragraph"}]}
    assert not main.check_jira_untouched("cadrage", before, ["PROJ-1"])
    assert "❌ Jira modifié pendant l'étape cadrage" in capsys.readouterr().out


def test_creation_de_tache_pendant_l_epopee_signalee(monkeypatch):
    children = [[]]
    monkeypatch.setattr(jira_guard, "get_jira_description", lambda key: None)
    monkeypatch.setattr(jira_guard, "get_epic_children", lambda key: [{"key": k} for k in children[0]])
    keys = jira_guard.watched_keys("EPIC-1", "epic-cadrage", {"tasks": [{"key": "PROJ-2"}, {"file": "x"}]})
    assert keys == ["EPIC-1", "PROJ-2"]
    before = jira_guard.snapshot(keys, "epic-cadrage")
    children[0] = ["PROJ-3"]
    assert jira_guard.changes(before, jira_guard.snapshot(keys, "epic-cadrage")) == ["enfants:EPIC-1"]


def test_publication_au_clic_sur_la_validation(workspace):
    published = []
    publisher = lambda key, text, stop_titles=(): published.append((key, text, stop_titles))
    analysis_publication.save_draft("PROJ-1", "# Cadrage PROJ-1\nV1")
    assert analysis_publication.status("PROJ-1")["libelle"] == "Analyse non publiée (brouillon)"
    assert published == []

    state = analysis_publication.validate("PROJ-1", "# Cadrage PROJ-1\nV1 relue", publisher)
    assert published == [("PROJ-1", "# Cadrage PROJ-1\nV1 relue", (integration_tools.SECTION_TITLE,))]
    assert state["publiee"] and state["libelle"].startswith("Publiée dans Jira le ")
    assert (workspace / "PROJ-1-cadrage.md").read_text(encoding="utf-8") == "# Cadrage PROJ-1\nV1 relue"

    # Validée à nouveau sans changement : rien n'est republié
    analysis_publication.validate("PROJ-1", "# Cadrage PROJ-1\nV1 relue", publisher)
    assert len(published) == 1


def test_publication_refusee_par_jira_laisse_un_brouillon(workspace):
    def refused(*a, **k):
        raise RuntimeError("Jira indisponible")
    with pytest.raises(RuntimeError):
        analysis_publication.validate("PROJ-1", "# Cadrage PROJ-1\nV1", refused)
    assert not analysis_publication.status("PROJ-1")["publiee"]
    assert (workspace / "PROJ-1-cadrage.md").exists()


def test_statut_brouillon_puis_publiee_puis_brouillon_apres_revision(workspace):
    analysis_publication.save_draft("PROJ-1", "# Cadrage\nV1")
    analysis_publication.record_publication("PROJ-1", "# Cadrage\nV1", datetime(2026, 11, 4, 9, 30))
    assert analysis_publication.status("PROJ-1")["libelle"] == "Publiée dans Jira le 2026-11-04 à 09:30"
    analysis_publication.save_draft("PROJ-1", "# Cadrage\nV2 révisée par l'agent")
    state = analysis_publication.status("PROJ-1")
    assert not state["publiee"]
    assert state["libelle"] == ("Analyse non publiée (brouillon) : révisée depuis sa publication du "
                                "2026-11-04 à 09:30")


def test_cadrage_reponses_sans_publication(workspace, fake_jira, monkeypatch):
    main = load_main()
    monkeypatch.setattr(analysis_publication, "update_jira_cadrage",
                        lambda *a, **k: pytest.fail("publication pendant cadrage-reponses"))
    analysis_publication.save_draft("PROJ-1", "# Cadrage PROJ-1\nAvec réponses")
    assert main.finalize_cadrage_answers("PROJ-1", "# Cadrage PROJ-1\nAvec réponses", "# Cadrage PROJ-1\nAvant")
    assert writes(fake_jira) == []
    assert not analysis_publication.status("PROJ-1")["publiee"]


def _epic_workspace(workspace):
    (workspace / "EPIC-1-tache-1.md").write_text("# Cadrage tâche 1", encoding="utf-8")
    (workspace / "EPIC-1-tache-2.md").write_text("# Cadrage tâche 2", encoding="utf-8")
    epic_tools.save_epic_manifest("EPIC-1", {"tasks": [
        {"summary": "Tâche 1", "file": "EPIC-1-tache-1.md", "key": ""},
        {"summary": "Tâche 2", "file": "EPIC-1-tache-2.md", "key": ""}]})


def test_epopee_sans_creation_de_taches_avant_validation(workspace, fake_jira, monkeypatch):
    main = load_main()
    _epic_workspace(workspace)
    assert main.finalize_epic_draft("EPIC-1", {})
    assert writes(fake_jira) == []
    manifest = epic_tools.load_epic_manifest("EPIC-1")
    assert [t["key"] for t in manifest["tasks"]] == ["", ""]
    assert len(epic_tools.draft_tasks(manifest)) == 2
    assert epic_tools.task_file(manifest["tasks"][0]) == "EPIC-1-tache-1.md"


def test_validation_du_decoupage_cree_les_taches_et_publie(workspace, monkeypatch):
    _epic_workspace(workspace)
    created, updated = [], []
    monkeypatch.setattr(epic_tools, "resolve_epic_task_type", lambda project, wanted="": "Tâche")
    monkeypatch.setattr(epic_tools, "save_cadrage_memory", lambda *a, **k: None)
    monkeypatch.setattr(epic_tools, "create_jira_issue",
                        lambda project, summary, *a, **k: created.append(summary) or f"PROJ-{len(created)}")
    monkeypatch.setattr(epic_tools, "set_jira_description_markdown", lambda key, text: updated.append(key))

    assert epic_tools.sync_epic_tasks("EPIC-1", "PROJ")
    assert created == ["Tâche 1", "Tâche 2"] and updated == []
    assert analysis_publication.status("PROJ-1")["publiee"]
    assert (workspace / "PROJ-1-cadrage.md").exists()

    # Nouvelle validation sans changement : rien n'est republié ; analyse corrigée localement : republiée
    assert epic_tools.sync_epic_tasks("EPIC-1", "PROJ")
    assert updated == []
    (workspace / "PROJ-2-cadrage.md").write_text("# Cadrage tâche 2 corrigée", encoding="utf-8")
    assert not analysis_publication.status("PROJ-2")["publiee"]
    assert epic_tools.sync_epic_tasks("EPIC-1", "PROJ")
    assert updated == ["PROJ-2"] and len(created) == 2
    assert analysis_publication.status("PROJ-2")["publiee"]


def test_consignes_des_prompts():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")
    for name in ("cadrage", "refine", "cadrage_reponses", "epic_cadrage", "epic_revision"):
        text = open(os.path.join(root, f"{name}.md"), encoding="utf-8").read()
        assert "N'écris rien dans Jira" in text, name
        assert "Mets à jour la description du ticket Jira" not in text, name
