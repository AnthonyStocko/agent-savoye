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
    assert pr.post_comment("P", "app", 7, "Régression", "src/A.java", 11, lines) == {"id": 1, "ancre": True}
    assert posted[0][0] == "https://git.example.io/rest/api/1.0/projects/P/repos/app/pull-requests/7/comments"
    assert posted[0][1]["anchor"] == {"path": "src/A.java", "line": 11, "lineType": "ADDED", "fileType": "TO",
                                      "diffType": "EFFECTIVE"}
    assert pr.post_comment("P", "app", 7, "Hors diff", "src/A.java", 99, lines)["ancre"] is False
    assert "anchor" not in posted[1][1] and posted[1][1]["text"].startswith("`src/A.java:99` — Hors diff")
    monkeypatch.setattr(pr.requests, "post", lambda *a, **k: _Response({"errors": []}, 403))
    with pytest.raises(pr.BitbucketError, match="droit d'écriture"):
        pr.post_comment("P", "app", 7, "x")


def test_results_are_saved_per_pr(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "ROOT", str(tmp_path))
    pr.save_result("P", "app", 7, {"verdict": "approuver"})
    assert pr.load_result("P", "app", 7) == {"verdict": "approuver"} and pr.load_result("P", "app", 8) == {}


def test_review_step_writes_a_result_without_publishing(tmp_path, monkeypatch):
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
    monkeypatch.setattr(pr, "pr_details", lambda *a: {"titre": "APP-1 x", "auteur": "Léa", "source": "f", "cible": "d",
                                                       "commit": "c1", "description": "", "jira": [], "id": 7,
                                                       "projet": "P", "depot": "app"})
    monkeypatch.setattr(pr, "pr_diff", lambda *a: (pr.diff_to_text(DIFF)[0], pr.diff_to_text(DIFF)[1],
                                                   ["src/A.java", "src/New.java"]))
    monkeypatch.setattr(pr, "existing_comments", lambda *a: [{"auteur": "Tom", "fichier": "", "ligne": None,
                                                              "texte": "déjà dit"}])
    monkeypatch.setattr(pr, "prepare_code", lambda *a: ("", "hors ligne"))
    monkeypatch.setattr(pr, "post_comment", lambda *a, **k: pytest.fail("rien n'est publié par l'analyse"))
    prompts = []

    def claude(prompt, *a, **k):
        prompts.append(prompt)
        return 0, ('{"resume": "R", "verdict": "a_discuter", "risque": "faible", "commentaires": ['
                   '{"fichier": "src/A.java", "ligne": 11, "gravite": "majeur", "message": "M"},'
                   '{"fichier": "src/A.java", "ligne": 99, "gravite": "mineur", "message": "N"}]}'), {}, False
    monkeypatch.setattr(m, "run_claude", claude)
    monkeypatch.setattr(m, "report_token_usage", lambda *a, **k: None)
    assert m.run_pr_review("https://git.example.io/projects/P/repos/app/pull-requests/7") == 0
    assert "déjà dit" in prompts[0] and "diff seul" in prompts[0]
    result = pr.load_result("P", "app", 7)
    assert [(c["ligne"], c["ancrable"], c["publie"]) for c in result["commentaires"]] == [(11, True, False),
                                                                                         (99, False, False)]
    assert result["commit"] == "c1" and result["lignes"]["src/A.java"]["11"] == "ADDED"
    assert m.run_pr_review("pas une adresse") == 2
