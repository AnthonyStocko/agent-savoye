"""Analyse trop longue pour la description Jira : résumé + commentaires marqués, révision sans doublon, relecture
complète pour le patch. Jira simulé en mémoire : aucun appel réseau."""
import os
from datetime import datetime

import pytest

from tools import analysis_publication as ap
from tools import integration_tools, jira_tools


class FakeJira:
    def __init__(self, description=None, limit=None):
        self.description = description
        self.limit = limit
        self.comments = [{"id": "c0", "created": "2026-01-01", "body": jira_tools._markdown_to_adf_rich(
            "Commentaire d'un consultant")}]
        self.next_id = 1
        self.puts = 0

    def get_description(self, key):
        return self.description

    def put_description(self, key, doc):
        if self.limit and ap.adf_size(doc) > self.limit:
            raise RuntimeError(f'Mise à jour de {key} refusée par Jira (400) : {{"errors":{{"description":'
                               f'"CONTENT_LIMIT_EXCEEDED: {self.limit}"}}}}')
        self.description, self.puts = doc, self.puts + 1

    def list_comments(self, key):
        return [dict(c) for c in self.comments]

    def add_comment(self, key, body):
        self.next_id += 1
        self.comments.append({"id": f"c{self.next_id}", "created": f"2026-02-{self.next_id:02d}", "body": body})
        return f"c{self.next_id}"

    def update_comment(self, key, comment_id, body):
        next(c for c in self.comments if c["id"] == comment_id)["body"] = body

    def delete_comment(self, key, comment_id):
        self.comments = [c for c in self.comments if c["id"] != comment_id]

    def analysis(self, key="PROJ-1"):
        return ap.analysis_comments(key, self.comments)


def analysis(sections: int, size: int = 400) -> str:
    lines = ["# Cadrage PROJ-1", "", "## Partie 1 : Analyse Fonctionnelle", "", "### Objectif & Périmètre Métier",
             "Le besoin : tracer les colis préparés. " * 3, "", "### Règles de Gestion & Impacts Métier",
             "Impact : écran de préparation. " * 3, "", "## Partie 2 : Analyse Technique", "",
             "### Détail des Modifications Techniques", "Solution : nouveau champ sur l'OP. " * 3, "",
             "### Points ouverts", "Question : faut-il historiser ? " * 2, ""]
    for n in range(sections):
        lines += [f"## Annexe {n}", "", f"### Détail {n}", ("Ligne d'analyse « détaillée » n° %d. " % n) * (size // 40),
                  "", "```java", f"int x{n} = {n};", "```", ""]
    return "\n".join(lines)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ap, "PUBLICATION_DIR", str(tmp_path / "publication"))
    monkeypatch.setenv("JIRA_DESCRIPTION_LIMIT", "6000")
    monkeypatch.setenv("JIRA_LIMIT_MARGIN", "300")
    return tmp_path


def existing_description():
    return jira_tools._markdown_to_adf_rich("Demande du client : tracer les colis.\n\n## Intégration du besoin\n\n"
                                            "Réponses de l'intégration.")


def test_short_analysis_goes_in_the_description(workspace):
    jira = FakeJira(existing_description())
    state = ap.validate("PROJ-1", analysis(0), jira=jira)
    assert state["publiee"] and state["message"] == "" and jira.analysis() == []
    text = jira_tools._extract_adf_text(jira.description)
    assert "Le besoin : tracer les colis" in text and "Réponses de l'intégration" in text
    assert ap.SUMMARY_SENTENCE not in text


def test_long_analysis_summary_in_description_and_one_comment(workspace):
    # Description du ticket déjà longue : l'analyse seule tient dans un commentaire, pas avec le reste
    jira = FakeJira(jira_tools._markdown_to_adf_rich("Demande détaillée du client. " * 80 + "\n\n## Intégration du "
                                                     "besoin\n\nRéponses de l'intégration."))
    text = analysis(6, 300)
    assert ap.adf_size(ap.comment_adf(ap.marker("PROJ-1", "x"), text)) < 5700
    state = ap.validate("PROJ-1", text, jira=jira)
    found = jira.analysis()
    assert len(found) == 1 and found[0]["total"] == 1 and found[0]["texte"] == text
    head, _ = ap._comment_text(jira.comments[-1]["body"])
    assert head.startswith("[Analyse de l'agent — PROJ-1 — version du ") and "partie" not in head
    description = jira_tools._extract_adf_text(jira.description)
    assert description.index(ap.SUMMARY_SENTENCE) < description.index("Intégration du besoin")
    assert "Besoin" in description and "Solution retenue" in description and "Points ouverts" in description
    assert "Réponses de l'intégration" in description, "sections existantes conservées"
    assert ap.adf_size(jira.description) <= 6000
    assert state["message"].startswith("Analyse trop longue pour la description (")
    assert "limite 6000) : résumé dans la description, analyse complète en 1 commentaire(s)" in state["message"]
    assert "CONTENT_LIMIT" not in state["libelle"]


def test_very_long_analysis_is_split_at_section_titles_in_numbered_comments(workspace):
    jira = FakeJira(existing_description())
    text = analysis(20, 600)
    ap.validate("PROJ-1", text, jira=jira)
    found = sorted(jira.analysis(), key=lambda c: c["partie"])
    total = len(found)
    assert total >= 3 and all(c["total"] == total for c in found)
    assert [c["partie"] for c in found] == list(range(1, total + 1))
    heads = [ap._comment_text(c["body"])[0] for c in jira.comments if ap._comment_text(c["body"])[0]]
    assert heads[1].endswith(f" — partie 2/{total}]")
    assert "\n".join(c["texte"] for c in found) == text
    for part in found[1:]:
        assert part["texte"].startswith("## ") or part["texte"].startswith("### "), "jamais au milieu d'une section"
    assert all(ap.adf_size(c["body"]) <= 6000 for c in jira.comments)
    assert f"{total} commentaire(s)" in ap.status("PROJ-1")["libelle"]


def test_revision_replaces_the_previous_analysis_comments(workspace, monkeypatch):
    jira = FakeJira(existing_description())
    ap.validate("PROJ-1", analysis(20, 600), jira=jira)
    first = len(jira.analysis())
    ap.validate("PROJ-1", analysis(8, 600) + "\nRévision.", jira=jira)
    second = jira.analysis()
    assert len(second) < first and len({c["version"] for c in second}) == 1
    assert len(second) == second[0]["total"], "une seule version, aucune partie empilée"
    assert any(c["id"] == "c0" for c in jira.comments), "commentaires des utilisateurs conservés"
    assert ap.from_comments("PROJ-1", jira.comments)[0].endswith("Révision.")
    # Analyse redevenue courte : dans la description, commentaires d'analyse retirés
    ap.validate("PROJ-1", analysis(0) + "\nCourte.", jira=jira)
    assert jira.analysis() == [] and [c["id"] for c in jira.comments] == ["c0"]


def test_patch_reads_the_complete_analysis_from_jira_comments(workspace, monkeypatch):
    jira = FakeJira(existing_description())
    text = analysis(20, 600)
    ap.validate("PROJ-1", text, jira=jira)
    os.rename("PROJ-1-cadrage.md", "PROJ-1-cadrage.md.bak")
    monkeypatch.setenv("JIRA_CONTEXT_MAX_CHARS", "1000")
    result = ap.ensure_local("PROJ-1", jira)
    assert result["source"] == "commentaires" and result["texte"] == text and len(text) > 10000
    assert result["message"] == f"analyse lue dans {len(jira.analysis())} commentaire(s) Jira"
    assert open("PROJ-1-cadrage.md", encoding="utf-8").read() == text, "aucune troncature"
    # Fichier local présent : prioritaire, Jira n'est pas relu
    assert ap.full_analysis("PROJ-1", object())["source"] == "fichier"
    # Branché avant le patch et la révision
    main_text = open(os.path.join(os.path.dirname(__file__), "..", "main.py"), encoding="utf-8").read()
    assert 'if args.step in ("patch", "refine") and not is_cucumber_client:' in main_text
    assert "analysis_publication.ensure_local(target_issue)" in main_text


def test_missing_part_stops_before_any_patch(workspace):
    jira = FakeJira(existing_description())
    ap.validate("PROJ-1", analysis(20, 600), jira=jira)
    second = next(c for c in jira.analysis() if c["partie"] == 2)
    jira.delete_comment("PROJ-1", second["id"])
    os.remove("PROJ-1-cadrage.md")
    with pytest.raises(ap.IncompleteAnalysis, match=r"partie\(s\) 2/\d+ absente"):
        ap.ensure_local("PROJ-1", jira)
    assert not os.path.exists("PROJ-1-cadrage.md")
    # Résumé seul dans la description, sans aucun commentaire d'analyse : arrêt aussi
    for c in jira.analysis():
        jira.delete_comment("PROJ-1", c["id"])
    with pytest.raises(ap.IncompleteAnalysis, match="ne porte qu'un résumé"):
        ap.full_analysis("PROJ-1", jira)


def test_jira_limit_from_the_error_message_is_used(workspace, monkeypatch):
    monkeypatch.setenv("JIRA_DESCRIPTION_LIMIT", "200000")
    jira = FakeJira(existing_description(), limit=5000)
    state = ap.validate("PROJ-1", analysis(6, 300), jira=jira)
    assert "limite 5000)" in state["message"] and len(jira.analysis()) >= 1
    assert ap.adf_size(jira.description) <= 5000


def test_split_keeps_the_exact_text_and_marker_format():
    text = analysis(12, 500)
    parts = ap.split_parts(text, "PROJ-1", "10/11/2026 17:00", 3000)
    assert len(parts) > 1 and "\n".join(parts) == text
    assert ap.marker("PROJ-1", "10/11/2026 17:00", 2, 3) == \
        "[Analyse de l'agent — PROJ-1 — version du 10/11/2026 17:00 — partie 2/3]"
    assert ap.limit_from_error('{"errors":{"description":"CONTENT_LIMIT_EXCEEDED"}}') == 0
    assert integration_tools.SECTION_TITLE
    assert datetime.strptime("10/11/2026 17:00", "%d/%m/%Y %H:%M")
