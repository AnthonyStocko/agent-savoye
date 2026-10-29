"""Revue de PR Bitbucket : adresse, diff et lignes commentables, résultat de l'agent, publication ancrée."""
import json

import pytest

from tools import pr_review as pr

DIFF = {"diffs": [
    {"source": {"toString": "src/A.java"}, "destination": {"toString": "src/A.java"}, "hunks": [
        {"sourceLine": 10, "sourceSpan": 3, "destinationLine": 10, "destinationSpan": 4, "segments": [
            {"type": "CONTEXT", "lines": [{"source": 10, "destination": 10, "line": "int a = 1;"}]},
            {"type": "REMOVED", "lines": [{"source": 11, "destination": 11, "line": "int b = 2;"}]},
            {"type": "ADDED", "lines": [{"source": 11, "destination": 11, "line": "int b = 3;"},
                                        {"source": 11, "destination": 12, "line": "log(b);"}]}]}]},
    {"source": None, "destination": {"toString": "src/New.java"}, "hunks": [
        {"sourceLine": 0, "sourceSpan": 0, "destinationLine": 1, "destinationSpan": 1, "segments": [
            {"type": "ADDED", "lines": [{"source": 0, "destination": 1, "line": "class New {}"}]}]}]}]}


class _Response:
    def __init__(self, data, status=200):
        self.status_code, self._data = status, data
        self.text = json.dumps(data)

    def json(self):
        return self._data


@pytest.fixture
def bitbucket(monkeypatch):
    monkeypatch.setenv("BITBUCKET_URL", "https://git.example.io/")
    monkeypatch.setenv("BITBUCKET_HTTP_TOKEN", "t0k")
    calls = []
    return calls


def test_pr_url_and_jira_keys():
    assert pr.parse_pr_url("https://git.kiscloud.io/projects/apparel/repos/odatio-app/pull-requests/42/diff") == (
        "APPAREL", "odatio-app", 42)
    with pytest.raises(ValueError):
        pr.parse_pr_url("https://git.kiscloud.io/projects/APPAREL/repos/odatio-app")
    assert pr.jira_keys("APP-12 correctif", "feature/APP-12-x", "voir KIS-3") == ["APP-12", "KIS-3"]


def test_diff_text_and_commentable_lines():
    text, lines = pr.diff_to_text(DIFF)
    assert "-int b = 2;" in text and "+int b = 3;" in text and "@@ -10,3 +10,4 @@" in text
    assert "new file" in text and "+++ b/src/New.java" in text
    assert lines == {"src/A.java": {10: "CONTEXT", 11: "ADDED", 12: "ADDED"}, "src/New.java": {1: "ADDED"}}
    assert pr.anchor_type(lines, "src/A.java", "12") == "ADDED" and pr.anchor_type(lines, "src/A.java", 99) == ""


def test_inbox_is_paged_and_summarized(bitbucket, monkeypatch):
    pages = [{"values": [{"id": 1, "title": "APP-1 x", "fromRef": {"displayId": "feature/APP-1", "latestCommit": "c1"},
                          "toRef": {"displayId": "develop", "repository": {"slug": "app", "project": {"key": "P"}}},
                          "author": {"user": {"displayName": "Léa"}}, "links": {"self": [{"href": "u1"}]},
                          "reviewers": [{"user": {"displayName": "Moi"}, "status": "UNAPPROVED"}]}],
              "isLastPage": False, "nextPageStart": 1},
             {"values": [{"id": 2, "title": "y", "toRef": {"repository": {"slug": "app", "project": {"key": "P"}}}}],
              "isLastPage": True}]
    seen = []

    def get(url, headers, params, timeout):
        seen.append((url, params, headers["Authorization"]))
        return _Response(pages[len(seen) - 1])
    monkeypatch.setattr(pr.requests, "get", get)
    prs = pr.review_inbox()
    assert [p["id"] for p in prs] == [1, 2] and prs[0]["jira"] == ["APP-1"] and prs[0]["auteur"] == "Léa"
    assert seen[0][0] == "https://git.example.io/rest/api/1.0/dashboard/pull-requests"
    assert seen[0][1]["role"] == "REVIEWER" and seen[1][1]["start"] == 1 and seen[0][2] == "Bearer t0k"


def test_review_result_is_normalized():
    out = pr.parse_review('blabla ```json\n{"resume": "OK", "verdict": "Changements_demandes", "risque": "moyen", '
                          '"commentaires": [{"fichier": "src/A.java", "ligne": "12", "gravite": "mineur", "message": '
                          '"Nommer mieux"}, {"fichier": "src/A.java", "ligne": 11, "gravite": "BLOQUANT", "message": '
                          '"b vaut 3 au lieu de 2 : régression"}, {"fichier": "x", "message": ""}]}\n```')
    assert out["verdict"] == "changements_demandes" and out["risque"] == "moyen"
    assert [(c["gravite"], c["ligne"]) for c in out["commentaires"]] == [("bloquant", 11), ("mineur", 12)]
    assert pr.parse_review("rien")["verdict"] == "a_discuter"


def test_comment_is_anchored_only_on_a_diff_line(bitbucket, monkeypatch):
    posted = []

    def post(url, headers, json, timeout):
        posted.append((url, json))
        return _Response({"id": len(posted)}, 201)
    monkeypatch.setattr(pr.requests, "post", post)
    _, lines = pr.diff_to_text(DIFF)
    assert pr.post_comment("P", "app", 7, "Régression", "src/A.java", 11, lines) == {"id": 1, "ancre": "ligne"}
    assert posted[0][0] == "https://git.example.io/rest/api/1.0/projects/P/repos/app/pull-requests/7/comments"
    assert posted[0][1]["anchor"] == {"path": "src/A.java", "line": 11, "lineType": "ADDED", "fileType": "TO",
                                      "diffType": "EFFECTIVE"}
    # Ligne hors des parties modifiées : commentaire posé sur le FICHIER (chemin abrégé retrouvé)
    assert pr.post_comment("P", "app", 7, "Hors diff", "A.java", 99, lines)["ancre"] == "fichier"
    assert posted[1][1]["anchor"] == {"path": "src/A.java", "fileType": "TO", "diffType": "EFFECTIVE"}
    assert posted[1][1]["text"] == "Ligne 99 : Hors diff"
    # Fichier hors de la PR : commentaire général qui le cite
    assert pr.post_comment("P", "app", 7, "Ailleurs", "src/Autre.java", 3, lines)["ancre"] == "general"
    assert "anchor" not in posted[2][1] and posted[2][1]["text"].startswith("`src/Autre.java:3` — Ailleurs")
    monkeypatch.setattr(pr.requests, "post", lambda *a, **k: _Response({"errors": []}, 403))
    with pytest.raises(pr.BitbucketError, match="droit d'écriture"):
        pr.post_comment("P", "app", 7, "x")


def test_results_are_saved_per_feature(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "ROOT", str(tmp_path))
    prs = [{"projet": "P", "depot": "app", "id": 7}, {"projet": "P", "depot": "ui", "id": 3}]
    assert pr.feature_id(prs, "APP-12") == "APP-12" and pr.feature_id(prs) == "P_app_7+P_ui_3"
    pr.save_result("APP-12", {"verdict": "approuver"})
    assert pr.load_result("APP-12") == {"verdict": "approuver"} and pr.load_result("APP-13") == {}


def test_prs_are_grouped_by_feature_and_searched_across_repos(bitbucket, monkeypatch):
    a = {"projet": "P", "depot": "app", "id": 7, "titre": "APP-12 back", "jira": ["APP-12"], "maj": 2}
    b = {"projet": "P", "depot": "ui", "id": 3, "titre": "APP-12 front", "jira": ["APP-12"], "maj": 5}
    c = {"projet": "P", "depot": "app", "id": 8, "titre": "sans ticket", "jira": [], "maj": 1}
    groups = pr.group_by_feature([a, b, c, a])
    assert [(g["cle"], len(g["prs"])) for g in groups] == [("APP-12", 2), ("P/app#8", 1)]

    def paged(path, params=None, limit=200):
        if path.endswith("/repos"):
            return [{"slug": "app"}, {"slug": "ui"}, {"slug": "studio"}]
        if "/repos/studio/" in path:
            assert params["filterText"] == "APP-12"
            return [{"id": 1, "title": "Studio pour APP-12", "fromRef": {"displayId": "feature/APP-12"},
                     "toRef": {"repository": {"slug": "studio", "project": {"key": "P"}}}},
                    {"id": 2, "title": "APP-120 autre", "toRef": {"repository": {"slug": "studio",
                                                                               "project": {"key": "P"}}}}]
        return []
    monkeypatch.setattr(pr, "_paged", paged)
    found = pr.feature_prs("APP-12", ["P", "p"], [a, b])
    assert [(f["depot"], f["id"]) for f in found] == [("app", 7), ("studio", 1), ("ui", 3)]
    assert pr.pr_label(found[1]) == "studio#1"


def test_feature_review_reads_all_prs_together_without_publishing(tmp_path, monkeypatch):
    import importlib
    import io
    import sys
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        m = importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved
    monkeypatch.setattr(pr, "ROOT", str(tmp_path))

    def details(project, repo, number):
        return {"titre": f"APP-1 {repo}", "auteur": "Léa", "source": "f", "cible": "d", "commit": f"c{number}",
                "description": "", "jira": ["APP-1"], "id": number, "projet": project, "depot": repo, "url": ""}
    monkeypatch.setattr(pr, "pr_details", details)
    monkeypatch.setattr(pr, "pr_diff", lambda p, r, n: (pr.diff_to_text(DIFF)[0], pr.diff_to_text(DIFF)[1] if r == "app"
                                                        else {"web/x.ts": {5: "ADDED"}}, ["src/A.java"]))
    monkeypatch.setattr(pr, "existing_comments", lambda *a: [{"auteur": "Tom", "fichier": "", "ligne": None,
                                                              "texte": "déjà dit"}])
    monkeypatch.setattr(pr, "prepare_code", lambda *a: ("", "hors ligne"))
    monkeypatch.setattr(pr, "post_comment", lambda *a, **k: pytest.fail("rien n'est publié par l'analyse"))
    monkeypatch.setattr(m, "get_jira_issue_details", lambda key: f"Ticket {key} : afficher le statut")
    monkeypatch.setattr(pr, "merged_feedback", lambda key, projects: [
        {"pr": "app#2", "titre": "APP-1 socle", "auteur": "Léa", "commentaires": [
            {"auteur": "Moi", "fichier": "src/A.java", "ligne": 4, "texte": "nommer la constante"}]}])
    prompts = []

    def claude(prompt, *a, **k):
        prompts.append(prompt)
        return 0, ('{"resume": "R", "verdict": "a_discuter", "risque": "faible", "coherence": "le front attend '
                   'statusCode", "commentaires": ['
                   '{"pr": "app#7", "fichier": "src/A.java", "ligne": 11, "gravite": "majeur", "message": "M"},'
                   '{"pr": "app#7", "fichier": "src/A.java", "ligne": 99, "gravite": "mineur", "message": "N"},'
                   '{"fichier": "web/x.ts", "ligne": 5, "gravite": "bloquant", "message": "champ renommé"}]}'), {}, \
            False
    monkeypatch.setattr(m, "run_claude", claude)
    monkeypatch.setattr(m, "report_token_usage", lambda *a, **k: None)
    assert m.run_pr_review(["https://git.example.io/projects/P/repos/app/pull-requests/7",
                            "https://git.example.io/projects/P/repos/ui/pull-requests/3"], "APP-1") == 0
    assert "déjà dit" in prompts[0] and "diff seul" in prompts[0] and "2 PULL REQUEST(S)" in prompts[0]
    assert "PR « app#7 »" in prompts[0] and "PR « ui#3 »" in prompts[0] and "afficher le statut" in prompts[0]
    result = pr.load_result("APP-1")
    assert [(c["pr"], c["ligne"], c["ancrage"]) for c in result["commentaires"]] == [
        ("ui#3", 5, "ligne"), ("app#7", 11, "ligne"), ("app#7", 99, "fichier")]   # sans repère : PR du fichier
    assert "PR fusionnée « app#2 »" in prompts[0] and "nommer la constante" in prompts[0]
    assert result["coherence"].startswith("le front") and [p["repere"] for p in result["prs"]] == ["app#7", "ui#3"]
    assert result["lignes"]["app#7"]["src/A.java"]["11"] == "ADDED"
    assert m.run_pr_review(["pas une adresse"]) == 2 and m.run_pr_review([]) == 2


def test_project_view_lists_all_open_prs_of_a_project(bitbucket, monkeypatch):
    def paged(path, params=None, limit=200):
        if path == "/rest/api/1.0/projects/OXY/repos":
            return [{"slug": "oxy-back"}, {"slug": "oxy-ui"}]
        if path == "/rest/api/1.0/projects/NOPE/repos":
            raise pr.BitbucketError("HTTP 404")
        if path.endswith("/oxy-back/pull-requests"):
            assert params["state"] == "OPEN"
            return [{"id": 4, "title": "OXY-7 back", "updatedDate": 10,
                     "toRef": {"repository": {"slug": "oxy-back", "project": {"key": "OXY"}}}}]
        if path.endswith("/oxy-ui/pull-requests"):
            return [{"id": 9, "title": "OXY-7 front", "updatedDate": 20,
                     "toRef": {"repository": {"slug": "oxy-ui", "project": {"key": "OXY"}}}}]
        return []
    monkeypatch.setattr(pr, "_paged", paged)
    found, errors = pr.project_prs(["oxy", " nope ", ""])
    assert [(p["depot"], p["id"]) for p in found] == [("oxy-ui", 9), ("oxy-back", 4)]
    assert errors == ["NOPE : HTTP 404"]
    assert [(g["cle"], len(g["prs"])) for g in pr.group_by_feature(found)] == [("OXY-7", 2)]


def test_merged_prs_feedback_is_collected(bitbucket, monkeypatch):
    calls = []

    def feature(key, projects, known=(), state="OPEN"):
        calls.append(state)
        return [{"projet": "P", "depot": "app", "id": 2, "titre": "APP-1 socle", "auteur": "Léa"},
                {"projet": "P", "depot": "ui", "id": 1, "titre": "APP-1 écran", "auteur": "Tom"}]
    monkeypatch.setattr(pr, "feature_prs", feature)
    monkeypatch.setattr(pr, "existing_comments", lambda p, r, n: [{"auteur": "Moi", "fichier": "A.java", "ligne": 4,
                                                                   "texte": "constante"}] if r == "app" else [])
    feedback = pr.merged_feedback("APP-1", ["P"])
    assert calls == ["MERGED"] and [f["pr"] for f in feedback] == ["app#2"]
    assert feedback[0]["commentaires"][0]["texte"] == "constante"


def test_comment_paths_are_matched_to_the_diff():
    _, lines = pr.diff_to_text(DIFF)
    assert pr.diff_path(lines, "./src/A.java") == "src/A.java" and pr.diff_path(lines, "New.java") == "src/New.java"
    assert pr.placement(lines, "A.java", 12) == ("src/A.java", "ligne")
    assert pr.placement(lines, "src/A.java", None) == ("src/A.java", "fichier")
    assert pr.placement(lines, "x/Inconnu.java", 1) == ("", "general")
