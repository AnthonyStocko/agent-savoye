"""Aucune route HTTP sans preuve : index des routes REST de la version testée, bibliothèque d'abord, 404 classé."""
import os
import subprocess

import pytest

from tools import cucumber_failures, cucumber_routes, cucumber_tools, cucumber_verify

RESOURCE = """package com.x.ref.report.rest;

@RestxResource(group = "report")
public class ReportDocRest {

    @GET("/reportdocs")
    public Iterable<ReportDoc> findAll() { return null; }

    @GET("/reportdocs/count")
    public long count() { return 0; }

    @GET("/reportdocs/defaultexchanges/:reportCode")
    public Iterable<String> defaultExchanges(String reportCode) { return null; }

    @POST("/reportdocs/delete")
    public void delete() { }
}
"""
CLIENT_RESOURCE = """package com.x.client.rest;

@RequestMapping("/canson")
public class PairingController {
    @GetMapping("/pairing/{code}")
    public String pairing(String code) { return code; }
}
"""
LIBRARY = """package cucumber.endPoints.refReport;

public class RefReportDocEndPoints extends EndPointsNotification {
    private static final String URL_REPORT_DOCS = "reportdocs";

    public static RefReportDoc findRefReportDocByCode(String code) {
        return sendGetRestToDisplayList(URL_REPORT_DOCS, RefReportDoc.class, null, null, true).get(0);
    }
}
"""
STEPS = "src/test/java/cucumber/features/acme/AcmeSteps.java"


def git(cwd, *args):
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def repo(path, files, refs=(), branch="main"):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", branch)
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    for rel, text in files.items():
        target = path.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    for ref in refs:
        git(path, "update-ref", ref, "HEAD")
    return path


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(cucumber_routes, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(cucumber_tools, "CACHE_ROOT", str(tmp_path / "clients"))
    std = tmp_path / "std"
    repo(std / "ref-report-backend", {"ref-report-rest/src/main/java/com/x/ReportDocRest.java": RESOURCE},
         refs=("refs/remotes/origin/v26.24",))
    # Dépôt du Standard sans la référence de la version : jamais lu sur une autre branche
    repo(std / "tooling", {"src/main/java/Other.java": RESOURCE.replace("/reportdocs", "/autre")})
    client = tmp_path / "client"
    repo(client / "canson-odatio-app", {"src/main/java/PairingController.java": CLIENT_RESOURCE},
         refs=("refs/tags/odatio-app-1.3.0",))
    # Route présente seulement sur integration, pas au tag de la version : non indexée
    repo(client / "wms-preparation-backend",
         {"src/main/java/Late.java": CLIENT_RESOURCE.replace("/pairing/", "/integration-only/")},
         branch="integration")
    repo(client / "canson-cucumber", {"src/main/java/Ignored.java": CLIENT_RESOURCE})
    library = repo(tmp_path / "cucumber_std", {"src/test/java/cucumber/endPoints/refReport/RefReportDocEndPoints.java":
                                               LIBRARY}, refs=("refs/remotes/origin/v26.22",))
    logs = []
    ctx = cucumber_routes.prepare("acme", str(std), str(client), "26.24", "refs/tags/odatio-app-1.3.0", str(library),
                                  "26.22", log=logs.append)
    return ctx, logs, tmp_path


def steps(tmp_path, body: str) -> str:
    work = tmp_path / "work"
    target = work.joinpath(*STEPS.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("package cucumber.features.acme;\n\npublic class AcmeSteps extends BaseStep {\n" + body + "\n}\n",
                      encoding="utf-8")
    return str(work)


def test_index_reads_each_repo_at_the_tested_version_only(context):
    ctx, logs, _ = context
    routes = {(r["methode"], r["chemin"]) for r in ctx["index"]["routes"]}
    assert ("GET", "/reportdocs/defaultexchanges/:reportCode") in routes and ("GET", "/canson/pairing/{code}") in routes
    assert not any("/autre" in path or "integration-only" in path for _, path in routes)
    proof = next(r["preuve"] for r in ctx["index"]["routes"] if r["chemin"] == "/reportdocs")
    assert proof == "ref-report-backend/ref-report-rest/src/main/java/com/x/ReportDocRest.java:6"
    missing = " ".join(ctx["index"]["indisponibles"])
    assert "tooling : aucune référence de la version 26.24" in missing
    assert "wms-preparation-backend : pas de refs/tags/odatio-app-1.3.0" in missing and "integration jamais lue" in missing
    assert "canson-cucumber" not in missing
    assert any("⚠️" in line and "wms-preparation-backend" in line for line in logs)
    assert ctx["bibliotheque"]["routes"][0]["fonctions"] == ["RefReportDocEndPoints#findRefReportDocByCode"]


def test_existing_route_with_its_proof_is_accepted(context):
    ctx, _, tmp_path = context
    work = steps(tmp_path, """
    // Route : GET /reportdocs/defaultexchanges/:reportCode — ReportDocRest.java:12 (Standard v26.24)
    private static final String URL_DEFAULT_EXCHANGES = "/reportdocs/defaultexchanges/";

    public List<String> exchanges(String code) {
        return sendGetRest(URL_DEFAULT_EXCHANGES + code).getBody().as(List.class);
    }

    public String pairing(String code) {
        // Route : GET /canson/pairing/{code} — PairingController.java:5 (client odatio-app-1.3.0)
        return sendGetRest("/canson/pairing/" + code).asString();
    }""")
    calls = cucumber_routes.project_calls(work, [STEPS])
    assert [(c["methode"], c["chemin"]) for c in calls] == [("GET", "/reportdocs/defaultexchanges/{…}"),
                                                            ("GET", "/canson/pairing/{…}")]
    assert cucumber_routes.check_lot(work, [STEPS], ctx) == []


def test_invented_route_is_refused_with_the_nearest_existing_routes(context):
    ctx, _, tmp_path = context
    work = steps(tmp_path, """
    public void create(Object body) {
        sendPostRest("/reportdocs", body);
    }""")
    errors = cucumber_routes.check_lot(work, [STEPS], ctx)
    assert len(errors) == 1
    error = errors[0]
    assert error.startswith(f"{STEPS}:6 route POST /reportdocs sans preuve")
    assert "Standard 26.24, client odatio-app-1.3.0" in error and "404 - No route found" in error
    assert "routes existantes proches : POST /reportdocs/delete" in error and "GET /reportdocs (" in error
    assert "ReportDocRest.java:6" in error


def test_proof_must_be_cited_and_true(context):
    ctx, _, tmp_path = context
    work = steps(tmp_path, """
    public void exchanges(String code) {
        sendGetRest("/reportdocs/defaultexchanges/" + code);
    }""")
    error = cucumber_routes.check_lot(work, [STEPS], ctx)[0]
    assert "preuve à citer en commentaire" in error
    assert "« // Route : GET /reportdocs/defaultexchanges/:reportCode — ref-report-backend/" in error
    # Preuve citée mais fausse (mauvaise ligne) : refusée
    work = steps(tmp_path, """
    public void exchanges(String code) {
        // Route : GET /reportdocs/defaultexchanges/:reportCode — ReportDocRest.java:99
        sendGetRest("/reportdocs/defaultexchanges/" + code);
    }""")
    assert "preuve à citer" in cucumber_routes.check_lot(work, [STEPS], ctx)[0]


def test_library_function_is_preferred_to_a_hand_written_call(context):
    ctx, _, tmp_path = context
    work = steps(tmp_path, """
    // Route : GET /reportdocs — ReportDocRest.java:6
    private static final String URL_REPORT_DOCS = "reportdocs";

    public List<RefReportDoc> docs(Map<String, Object> params) {
        return sendGetRestToDisplayList(URL_REPORT_DOCS, RefReportDoc.class, params);
    }""")
    error = cucumber_routes.check_lot(work, [STEPS], ctx)[0]
    assert "écrite à la main alors que la bibliothèque 26.22 la couvre : RefReportDocEndPoints#findRefReportDocByCode" in error
    assert "appelle cette fonction" in error
    # Appel direct justifié (la fonction relance un POST absent) et prouvé : accepté
    work = steps(tmp_path, """
    // Route : GET /reportdocs — ReportDocRest.java:6
    // — hors bibliothèque : findRefReportDocByCode relance un POST /reportdocs absent du serveur.
    private static final String URL_REPORT_DOCS = "reportdocs";

    public List<RefReportDoc> docs(Map<String, Object> params) {
        return sendGetRestToDisplayList(URL_REPORT_DOCS, RefReportDoc.class, params);
    }""")
    assert cucumber_routes.check_lot(work, [STEPS], ctx) == []
    # Fonction de la bibliothèque appelée : aucune route écrite à la main
    work = steps(tmp_path, """
    public RefReportDoc doc(String code) throws Exception {
        return RefReportDocEndPoints.findRefReportDocByCode(code);
    }""")
    assert cucumber_routes.project_calls(work, [STEPS]) == []


def test_no_route_found_on_a_project_step_is_a_route_without_proof():
    trace = ("java.lang.AssertionError: Lecture du document X impossible : 404 - No route found\n"
             "\tat cucumber.features.canson.WmsPreparationCansonSteps.findDoc(WmsPreparationCansonSteps.java:160)\n"
             "\tat ✽.CANSON- Création si absent du document \"X\"(file:///x.feature:29)\n"
             "Caused by: java.lang.RuntimeException: 404 - No route found")
    step = 'CANSON- Création si absent du document "X"'
    got = cucumber_failures.classify({"message": trace.splitlines()[0], "erreur": trace, "step": step})
    assert (got["categorie"], got["nature"]) == ("correction", "route_sans_preuve")
    assert "404 - No route found" in got["cause"] and "bibliothèque" in got["action"]
    # Vérification d'un lot : échec du test à corriger, jamais un écart de version de l'environnement
    assert cucumber_verify.failure_kind(trace)[0] == "technique"
    assert "route sans preuve" in cucumber_verify.failure_hint(trace)
    # Step du Standard : écart de version (inchangé)
    standard = ("java.lang.RuntimeException: 404 - No route found\n\tat cucumber.features.wmsPreparation.X.y(X.java:1)"
                "\n\tat ✽.PREP- Campagne(file:///x.feature:3)")
    assert cucumber_verify.failure_kind(standard)[0] == "version"
    assert cucumber_failures.classify({"message": standard, "step": "PREP- Campagne"})["nature"] == "version"
    # Client donné par le contexte du run
    assert cucumber_routes.project_step_without_route("Création", trace, "canson")


def test_rule_is_in_generation_and_correction_prompts_and_wired_in_lot_checks():
    root = os.path.join(os.path.dirname(__file__), "..")
    read = lambda rel: open(os.path.join(root, *rel.split("/")), encoding="utf-8").read()  # noqa: E731
    assert "AUCUNE route HTTP sans preuve" in read("prompts/cucumber_client_lot_systeme.md")
    assert "route sans preuve" in read("prompts/cucumber_client_fix.md")
    main = read("main.py")
    assert 'cucumber_routes.check_lot(worktree, files, context.get("routes"))' in main
    assert "cucumber_routes.prepare(" in main and '"routes": routes_context' in main
