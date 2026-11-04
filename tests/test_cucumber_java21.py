"""Migration Java 21 du projet de tests et contrôle de non-régression (client fictif, Jenkins simulé)."""
import importlib
import inspect
import io
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

from tests.conftest import CLIENT, CLIENT_TAG, git, write

from tools import cucumber_java21 as cj
from tools import cucumber_profile as cp
from tools import cucumber_tools as ct

POM = """<project>
  <modelVersion>4.0.0</modelVersion>
  <properties>
    <maven.compiler.source>11</maven.compiler.source>
    <maven.compiler.target>11</maven.compiler.target>
  </properties>
  <dependencies>
    <dependency>
      <groupId>a-sis</groupId>
      <artifactId>Cucumber</artifactId>
      <version>26.16</version>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.8.1</version>
      </plugin>
      <plugin>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>3.2.5</version>
      </plugin>
    </plugins>
  </build>
</project>
"""
FEATURE = "src/test/resources/cucumber/features/clientx/WMS/a.feature"
FEATURE_TEXT = (f"{CLIENT_TAG}\nFeature: Réception\n\n  # Jira : SPEC-1234\n  Scenario: Premier\n    Given x\n\n"
                f"  Scenario: Second\n    Given y\n")
FIRST, SECOND = f"{FEATURE}::Premier", f"{FEATURE}::Second"
ROLES = {"branche": "BRANCH", "arguments": "ARGS", "composant_version": "COMPONENT_VERSION",
         "kis_version": "KIS_VERSION"}
AVANT = {"composant_version": "5.1.0", "kis_version": "26.16"}
APRES = {"composant_version": "6.0.0", "kis_version": "26.24"}


# ---------------------------------------------------------------------------
# Pom : seules la version Java et les versions des plugins changent
# ---------------------------------------------------------------------------

def test_pom_migration_only_java_and_build_plugins():
    migrated = cj.migrate_pom(POM)
    assert migrated["erreur"] == "" and cj.java_version(POM) == 11 and cj.java_version(migrated["pom"]) == 21
    changed = [(a.strip(), b.strip()) for a, b in zip(POM.splitlines(), migrated["pom"].splitlines()) if a != b]
    assert changed == [("<maven.compiler.source>11</maven.compiler.source>",
                        "<maven.compiler.source>21</maven.compiler.source>"),
                       ("<maven.compiler.target>11</maven.compiler.target>",
                        "<maven.compiler.target>21</maven.compiler.target>"),
                       ("<version>3.8.1</version>", "<version>3.13.0</version>")]    # surefire 3.2.5 : inchangé
    assert all(c["raison"] for c in migrated["changements"])
    assert "26.16" in migrated["pom"]                                                   # dépendances intactes
    assert cj.migrate_pom(migrated["pom"])["changements"] == []                        # déjà en 21 : rien


def test_pom_without_java_setting_gets_release_property():
    bare = "<project>\n  <modelVersion>4.0.0</modelVersion>\n  <properties>\n    <x>1</x>\n  </properties>\n</project>\n"
    migrated = cj.migrate_pom(bare)
    assert "<maven.compiler.release>21</maven.compiler.release>" in migrated["pom"] and "<x>1</x>" in migrated["pom"]
    assert cj.migrate_pom("pas un pom")["erreur"]


def test_branch_diff_limited_to_pom_otherwise_stop():
    after = cj.migrate_pom(POM)["pom"]
    assert cj.branch_errors(["pom.xml"], POM, after) == []
    errors = cj.branch_errors(["pom.xml", FEATURE, "src/test/java/Steps.java"], POM, after)
    assert len(errors) == 2 and all("seul pom.xml" in e for e in errors)
    extra = after.replace("<version>26.16</version>", "<version>26.24</version>")       # dépendance changée
    assert "changements autres" in cj.branch_errors(["pom.xml"], POM, extra)[0]


# ---------------------------------------------------------------------------
# Dry-run, compilation, outillage du job
# ---------------------------------------------------------------------------

def _dry_report(steps_second=1):
    return [{"uri": f"classpath:{FEATURE.split('src/test/resources/', 1)[1]}", "elements": [
        {"type": "background", "name": "", "steps": [{}]},
        {"type": "scenario", "name": "Premier", "steps": [{}]},
        {"type": "scenario", "name": "Second", "steps": [{}] * steps_second}]}]


def test_dry_run_identical_before_and_after():
    before = cj.dry_run_signature(_dry_report())
    assert before == {FIRST: 2, SECOND: 1}                                          # Background compté une fois
    same = cj.compare_dry_runs(before, cj.dry_run_signature(_dry_report()))
    assert same["ok"] and same["scenarios"] == 2 and same["nombre_steps"] == 3
    other = cj.compare_dry_runs(before, cj.dry_run_signature(_dry_report(steps_second=2)))
    assert not other["ok"] and other["steps"] == [f"{SECOND} : 1 step(s) avant, 2 après"]
    missing = cj.compare_dry_runs(before, {FIRST: 2})
    assert not missing["ok"] and missing["manquants"] == [SECOND]


def test_compile_errors_reported_not_fixed():
    findings = cj.compile_findings([
        "src/test/java/a/Steps.java:12:8 package javax.xml.bind does not exist",
        "src/test/java/a/Other.java:40:3 cannot find symbol (symbol: class Foo)"])
    assert findings[0] == {"fichier": "src/test/java/a/Steps.java", "ligne": 12, "api": "javax.xml.bind (JAXB)",
                           "remplacement": findings[0]["remplacement"], "message": "package javax.xml.bind does not exist"}
    assert "jakarta" in findings[0]["remplacement"]
    assert findings[1]["api"] == "" and findings[1]["remplacement"].startswith("à analyser")


def test_job_toolchain_read_from_the_console():
    assert cj.toolchain("[INFO] Apache Maven 3.9.6\nJava version: 21.0.2, vendor: Eclipse")["ok"] is True
    old = cj.toolchain("Java version: 11.0.20, vendor: Eclipse")
    assert old["ok"] is False and old["version"] == 11
    assert cj.toolchain("[ERROR] Fatal error compiling: error: invalid target release: 21")["ok"] is False
    assert cj.toolchain("[INFO] Compiling 3 source files with javac [debug release 21] to target")["ok"] is True
    assert cj.toolchain("rien d'utile")["ok"] is None


def test_environment_confirmation_and_proposals():
    assert cj.environment_errors(AVANT, APRES, ROLES) == []
    assert "identiques" in cj.environment_errors(AVANT, AVANT, ROLES)[0]
    assert "non confirmé" in cj.environment_errors(AVANT, {"composant_version": "6"}, ROLES)[0]
    assert "non détecté" in cj.environment_errors(AVANT, APRES, {"branche": "BRANCH"})[0]
    builds = [{"numero": 9, "resultat": "FAILURE", "parametres": {"REPO": f"{CLIENT}-cucumber",
                                                                  "COMPONENT_VERSION": "6.0.0", "KIS_VERSION": "26.24"}},
              {"numero": 8, "resultat": "SUCCESS", "parametres": {"REPO": f"{CLIENT}-cucumber",
                                                                  "COMPONENT_VERSION": "5.1.0", "KIS_VERSION": "26.16"}},
              {"numero": 7, "resultat": "SUCCESS", "parametres": {"REPO": "autre-cucumber",
                                                                  "COMPONENT_VERSION": "1", "KIS_VERSION": "1"}}]
    proposal = cj.propose_environments({}, builds, ROLES, f"{CLIENT}-cucumber")
    assert proposal["avant"] == AVANT and proposal["apres"] == APRES
    profile = {"version_client": {"deduction": {"jenkins": {"composant_version": "6.1.0", "kis_version": "26.30"}}}}
    assert cj.propose_environments(profile, builds, ROLES, f"{CLIENT}-cucumber")["apres"] == {
        "composant_version": "6.1.0", "kis_version": "26.30"}


def test_recent_green_full_run_reused_as_before():
    now = datetime(2026, 11, 4, 12)
    green = {"run_complet": True, "resultats": {FIRST: {"statut": "passe"}}}
    params = {"BRANCH": "integration", "COMPONENT_VERSION": "5.1.0", "KIS_VERSION": "26.16"}
    item = lambda days, analysis=green: {"entry": {"date": (now - timedelta(days=days)).isoformat()},  # noqa: E731
                                         "analyse": analysis}
    pick = lambda items, p=params: cj.reference_import(items, "integration", AVANT, ROLES, lambda e: p, 7, now)  # noqa: E731
    assert pick([item(2)]) is green
    assert pick([item(9)]) == {}                                                         # trop ancien
    assert pick([item(1, {"run_complet": True, "resultats": {FIRST: {"statut": "echec"}}})]) == {}   # pas vert
    assert pick([item(1)], {**params, "BRANCH": "autre"}) == {}                          # autre branche de tests
    assert pick([item(1)], {**params, "KIS_VERSION": "26.24"}) == {}                     # autre environnement


# ---------------------------------------------------------------------------
# Comparaison : identique, nouvel échec, nouveau succès, instable
# ---------------------------------------------------------------------------

def _results(**statuses):
    return {f"{FEATURE}::{k}": {"statut": v, "message": "" if v == "passe" else f"{k} KO"}
            for k, v in statuses.items()}


def test_comparison_categories_and_verdict():
    before = _results(A="passe", B="passe", C="echec", D="passe")
    after = _results(A="passe", B="echec", C="passe", D="echec")
    rows = cj.compare_results(before, after)
    assert sorted(cj.to_rerun(rows)) == [f"{FEATURE}::B", f"{FEATURE}::C", f"{FEATURE}::D"]
    rerun = _results(B="echec", C="passe", D="passe")                                   # D revient : instable
    rows = {r["id"].split("::")[1]: r for r in cj.compare_results(before, after, rerun)}
    assert rows["A"]["categorie"] == "identique"
    assert rows["B"]["categorie"] == "nouvel_echec" and rows["B"]["confirme"]
    assert rows["C"]["categorie"] == "nouveau_succes" and rows["C"]["confirme"]
    assert rows["D"]["categorie"] == "instable" and not rows["D"]["confirme"]
    assert cj.classify_failure(rows["D"]) == "instabilite"
    assert cj.classify_failure(rows["B"]) == "regression_application"
    assert cj.classify_failure({**rows["B"], "message": "java.lang.UnsupportedClassVersionError: x"}) == \
        "environnement_java21"
    assert cj.verdict(list(rows.values()))["libelle"] == "2 écart(s)"
    clean = cj.compare_results(before, after, _results(B="passe", C="echec", D="passe"))
    assert cj.verdict(clean)["libelle"] == "Aucune régression détectée"


def test_durations_and_targeted_rerun():
    durations = cj.compare_durations({FIRST: 100, SECOND: 10}, {FIRST: 200, SECOND: 12})
    assert durations["notables"] == [(FIRST, 100, 200)] and durations["ecart_pct"] == 92.7
    selection = cj.rerun_selection([SECOND], lambda rel: FEATURE_TEXT)
    assert selection == f"-Dcucumber.features={FEATURE}:8"
    context = cj.scenario_context(FEATURE_TEXT, "Premier")
    assert context == {"fonctionnalite": "Réception", "tickets": ["SPEC-1234"]}


# ---------------------------------------------------------------------------
# Orchestration : branche poussée (pom seul), runs simulés, PR proposée, jamais de fusion
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


@pytest.fixture
def client_repo(tmp_path, monkeypatch, main_module):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENT_AUTH_USER", raising=False)
    monkeypatch.setattr(main_module.jenkins_tools, "jenkins_configured", lambda: True)
    monkeypatch.setattr(main_module, "share_knowledge", lambda *a, **k: {})
    origin = str(tmp_path / f"{CLIENT}-cucumber.git")
    os.makedirs(origin)
    git(origin, "init", "-q", "--bare", "-b", "integration")
    repo = str(tmp_path / "clone")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "integration")
    for key, value in (("user.name", "test"), ("user.email", "t@example.com"), ("core.autocrlf", "false")):
        git(repo, "config", key, value)
    git(repo, "remote", "add", "origin", origin)
    write(os.path.join(repo, "pom.xml"), POM)
    write(os.path.join(repo, *FEATURE.split("/")), FEATURE_TEXT)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "push", "-q", "origin", "integration")
    git(repo, "fetch", "-q", "origin")
    os.makedirs(os.path.dirname(cp.profile_path(CLIENT)), exist_ok=True)
    with open(cp.profile_path(CLIENT), "w", encoding="utf-8") as f:
        json.dump({"detecte": {**cp.empty_profile(), "tag_client": CLIENT_TAG, "cucumber_repo_url": origin},
                   "saisi": {"run_job": "job-clientx", "verify_branch": "cucumber-agent"}}, f)
    return repo, origin


def _fake_runs(after_rerun, console="Java version: 21.0.2, vendor: Eclipse"):
    calls = []

    def run(branch, values, tags=None, extra=""):
        calls.append((branch, dict(values), tags, extra))
        if tags == cj.ESSAI_TAG:
            return {"build": "https://j/1", "console": console}
        if values == AVANT:
            return {"build_url": "https://j/2", "resultats": _results(Premier="passe", Second="passe"),
                    "durees": {"par_scenario": {FIRST: 10, SECOND: 10}}}
        if extra:
            return {"build_url": "https://j/4", "resultats": after_rerun}
        return {"build_url": "https://j/3", "resultats": _results(Premier="passe", Second="echec"),
                "durees": {"par_scenario": {FIRST: 11, SECOND: 9}}}
    return run, calls


def _steps(run, prs, local=None):
    return {"prepare": lambda: {"roles": ROLES, "erreurs": [], "definitions": {}},
            "local": local or (lambda worktree: {"dry_run": {"ok": True, "scenarios": 2, "nombre_steps": 2}}),
            "run": run, "reference": lambda *a: {}, "pull_request": lambda payload: prs.append(payload) or "https://pr/1"}


def test_unstable_difference_no_regression_pr_proposed_never_merged(client_repo, main_module):
    repo, origin = client_repo
    target = git(origin, "rev-parse", "integration")
    run, calls = _fake_runs(_results(Second="passe"))                                    # relance : de nouveau vert
    prs = []
    result = main_module.run_java21_migration(CLIENT, repo, "integration", "", f"{CLIENT}-cucumber", "CLIENTX",
                                              AVANT, APRES, _steps(run, prs))
    assert result["verdict"]["libelle"] == "Aucune régression détectée", result["motif"]
    assert git(origin, "rev-parse", "integration") == target                              # jamais fusionnée
    pushed = git(origin, "rev-parse", cj.DEFAULT_BRANCH)
    assert git(origin, "diff", "--name-only", target, pushed) == "pom.xml"               # pom seul
    assert git(origin, "show", f"{pushed}:pom.xml") == cj.migrate_pom(POM)["pom"].rstrip("\n")
    # Essai, AVANT sur la branche lue par le job, APRÈS et relance ciblée sur la branche migrée
    assert [(c[0], c[2]) for c in calls] == [(cj.DEFAULT_BRANCH, cj.ESSAI_TAG), ("integration", None),
                                            (cj.DEFAULT_BRANCH, None), (cj.DEFAULT_BRANCH, None)]
    assert calls[3][3] == f"-Dcucumber.features={FEATURE}:8"
    assert len(prs) == 1 and prs[0]["toRef"]["id"] == "refs/heads/integration"
    assert prs[0]["fromRef"]["id"] == f"refs/heads/{cj.DEFAULT_BRANCH}" and result["pr"] == "https://pr/1"
    report = open(os.path.join(main_module.java21_dir(CLIENT), cj.REPORT_FILE), encoding="utf-8").read()
    assert "Aucune régression détectée" in report and "instable" in report and "https://j/3" in report


def test_confirmed_regression_no_pr(client_repo, main_module):
    repo, origin = client_repo
    run, _ = _fake_runs(_results(Second="echec"))                                        # écart confirmé
    prs = []
    result = main_module.run_java21_migration(CLIENT, repo, "integration", "", f"{CLIENT}-cucumber", "CLIENTX",
                                              AVANT, APRES, _steps(run, prs))
    assert result["verdict"]["libelle"] == "1 écart(s)" and prs == [] and result["pr"] == ""
    row = next(r for r in result["lignes"] if r["id"] == SECOND)
    assert row["classement"] == "regression_application"
    assert result["contextes"][SECOND]["fonctionnalite"] == "Réception"


def test_change_outside_pom_stops_before_push(client_repo, main_module):
    repo, origin = client_repo

    def local(worktree):                                                                 # un test touché : interdit
        write(os.path.join(worktree, *FEATURE.split("/")), FEATURE_TEXT.replace("Given y", "Given z"))
        git(worktree, "commit", "-qam", "test modifié")
        return {"dry_run": {"ok": True}}
    run, calls = _fake_runs({})
    result = main_module.run_java21_migration(CLIENT, repo, "integration", "", f"{CLIENT}-cucumber", "CLIENTX",
                                              AVANT, APRES, _steps(run, [], local))
    assert "seul pom.xml" in result["motif"] and not result["verdict"] and calls == []
    assert cj.DEFAULT_BRANCH not in git(origin, "branch", "--list")


def test_toolchain_or_environment_stops_before_runs(client_repo, main_module):
    repo, _ = client_repo
    run, calls = _fake_runs({}, console="Java version: 11.0.20")
    result = main_module.run_java21_migration(CLIENT, repo, "integration", "", f"{CLIENT}-cucumber", "CLIENTX",
                                              AVANT, APRES, _steps(run, []))
    assert "ne compile pas en Java 21" in result["motif"] and len(calls) == 1          # essai seulement
    stopped = main_module.run_java21_migration(CLIENT, repo, "integration", "", f"{CLIENT}-cucumber", "CLIENTX",
                                               AVANT, {}, _steps(run, []))
    assert "APRÈS non confirmé" in stopped["motif"]


def test_compile_error_waits_for_a_human_decision(client_repo, main_module):
    repo, origin = client_repo
    findings = cj.compile_findings(["src/test/java/a/Steps.java:12:8 package javax.xml.bind does not exist"])
    run, calls = _fake_runs({})
    result = main_module.run_java21_migration(
        CLIENT, repo, "integration", "", f"{CLIENT}-cucumber", "CLIENTX", AVANT, APRES,
        _steps(run, [], lambda w: {"compilation": findings, "erreur": "1 erreur(s) de compilation en Java 21 : non "
                                                                     "corrigées, décision humaine attendue"}))
    assert "décision humaine" in result["motif"] and calls == []
    assert cj.DEFAULT_BRANCH not in git(origin, "branch", "--list")
    report = open(os.path.join(main_module.java21_dir(CLIENT), cj.REPORT_FILE), encoding="utf-8").read()
    assert "javax.xml.bind" in report and "jakarta" in report


def test_no_automatic_merge_anywhere(main_module):
    source = "\n".join(inspect.getsource(f) for f in (
        main_module.run_java21_migration, main_module.java21_push, main_module.java21_run, cj.pull_request_payload))
    for forbidden in ("merge", "promote", "--force", "push -f", "approve"):
        assert forbidden not in source, forbidden
