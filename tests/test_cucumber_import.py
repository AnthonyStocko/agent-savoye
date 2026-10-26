"""Point 8 : import d'un rapport d'exécution (masquage, doublons, nombre de tests, causes racine, comparaison)."""
import base64
import json
import os

import pytest

from tests.conftest import CLIENT, CLIENT_TAG, git, write
from tools import cucumber_import as cimp
from tools import cucumber_tools as ct

RES = "src/test/resources/cucumber"
REL = f"{RES}/features/{CLIENT}/Stock.feature"
FEATURE = (f"{CLIENT_TAG}\nFeature: Stock\n"
           + "".join(f"  Scenario: S{i}\n    Given x\n" for i in range(4)))
SECRET = "s3cretPwd"
CONSOLE = (f"Started by user\n2026-10-23 08:00:00 mvn test -Dpassword={base64.b64encode(SECRET.encode()).decode()} "
           f"-DuserName=admin \"-Dcucumber.filter.tags={CLIENT_TAG}\"\nAuthorization: Bearer abc.def.ghi\n"
           f"echo {SECRET}\nTests run: 4, Failures: 2, Errors: 0, Skipped: 0\n")


def _report(outcomes: dict, start="2026-10-23T08:00:00Z"):
    """Rapport Cucumber : {titre: message d'échec ou '' (passe)}."""
    elements = []
    for title, error in outcomes.items():
        step = {"keyword": "Then ", "name": f'STOCK- le stock de "{title}"',
                "result": {"status": "failed", "error_message": error} if error else {"status": "passed"}}
        elements.append({"type": "scenario", "name": title, "start_timestamp": start, "steps": [step]})
    return json.dumps([{"uri": f"classpath:cucumber/features/{CLIENT}/Stock.feature", "elements": elements}])


@pytest.fixture
def setup(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    write(os.path.join(repo, REL), FEATURE)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    state = {"selection": {"nombre_attendu": 4}, "lots": [{"numero": 1, "tags": {
        f"@agent_{CLIENT}_1_{i}": f"{REL}::S{i}" for i in range(4)}, "indicateurs": {"scenarios": [
            {"fichier": REL, "titre": f"S{i}", "spec": "sp-1" if i < 2 else "sp-2"} for i in range(4)]}}]}
    return repo, state


def test_secrets_are_masked_before_storage(setup):
    masked, counts = cimp.mask_secrets(CONSOLE)
    assert SECRET not in masked and base64.b64encode(SECRET.encode()).decode() not in masked
    assert "admin" not in masked and "abc.def.ghi" not in masked
    assert counts == {"mot de passe": 1, "utilisateur": 1, "jeton": 1}
    imported = cimp.import_files(CLIENT, {"console.txt": CONSOLE.encode(), "cucumber.json": _report(
        {"S0": "", "S1": "", "S2": "", "S3": ""}).encode()})
    folder = os.path.join(cimp.imports_dir(CLIENT), str(imported["numero"]))
    stored = "".join(open(os.path.join(folder, f), encoding="utf-8").read() for f in os.listdir(folder))
    assert SECRET not in stored and "admin" not in stored
    assert any("secrets masqués" in m for m in imported["messages"])


def test_same_file_and_same_run_are_detected(setup):
    report = _report({"S0": "", "S1": "", "S2": "", "S3": ""}).encode()
    first = cimp.import_files(CLIENT, {"cucumber.json": report})
    again = cimp.import_files(CLIENT, {"copie.json": report})
    assert again["numero"] == 0 and "déjà importé (import n° 1)" in again["messages"][0]
    # Console du même run (même début) : rattachée à l'import n° 1
    console = cimp.import_files(CLIENT, {"console.txt": CONSOLE.encode(), "cucumber.json": _report(
        {"S0": "", "S1": "", "S2": "", "S3": "x"}).encode()})
    assert console["numero"] == first["numero"] and any("même run que l'import n° 1" in m
                                                        for m in console["messages"])


def test_run_with_another_test_count_is_not_analysed(setup):
    repo, state = setup
    imported = cimp.import_files(CLIENT, {"cucumber.json": _report({f"S{i}": "" for i in range(9)}).encode()})
    analysis = cimp.analyse(CLIENT, imported["numero"], state, repo)
    assert "9 test(s) exécuté(s)" in analysis["non_analyse"] and analysis["groupes"] == []


def test_failures_grouped_compared_and_proposed(setup):
    repo, state = setup
    first = cimp.import_files(CLIENT, {"cucumber.json": _report({
        "S0": "java.lang.AssertionError: expected:<LIBRE> but was:<BLOQUE> for C-0001",
        "S1": "java.lang.AssertionError: expected:<LIBRE> but was:<BLOQUE> for C-0002",
        "S2": "Site non trouvé(e) avec code SITE9", "S3": ""}).encode()})
    analysis = cimp.analyse(CLIENT, first["numero"], state, repo)
    assert analysis["non_analyse"] == ""
    groups = {g["categorie"]: g for g in analysis["groupes"]}
    assert len(groups["test"]["scenarios"]) == 2 and groups["donnee_reference"]["scenarios"] == [f"{REL}::S2"]
    # Lots proposés : échecs « test » des scénarios de l'agent seulement, un par spécifique
    assert [(p["spec"], [s["titre"] for s in p["scenarios"]]) for p in analysis["propositions"]] == [
        ("sp-1", ["S0", "S1"])]
    assert "expected:<LIBRE>" in analysis["propositions"][0]["scenarios"][0]["message"]
    second = cimp.import_files(CLIENT, {"cucumber.json": _report({
        "S0": "", "S1": "java.lang.AssertionError: expected:<LIBRE> but was:<BLOQUE> for C-0002",
        "S2": "Site non trouvé(e) avec code SITE9", "S3": "Request failed : 500 Internal Server Error"},
        start="2026-10-24T08:00:00Z").encode()})
    comparison = cimp.analyse(CLIENT, second["numero"], state, repo)["comparaison"]
    assert comparison["import"] == first["numero"] and comparison["corriges"] == [f"{REL}::S0"]
    assert comparison["regressions"] == [f"{REL}::S3"] and comparison["nouveaux"] == []
    assert len(comparison["inchanges"]) == 2
    md = open(os.path.join(cimp.imports_dir(CLIENT), str(second["numero"]), "analyse.md"), encoding="utf-8").read()
    assert "Corrigés : 1 ; nouveaux : 0 ; régressions : 1" in md


def test_server_log_export_must_cover_failed_steps(setup):
    repo, state = setup
    logs = "2026-10-23 12:00:00;ERROR;autre chose\n2026-10-23 12:05:00;INFO;fin\n"
    imported = cimp.import_files(CLIENT, {"cucumber.json": _report(
        {"S0": "expected:<A> but was:<B>", "S1": "", "S2": "", "S3": ""}).encode(), "serveur.csv": logs.encode()})
    entry = cimp.analyse(CLIENT, imported["numero"], state, repo)["logs"][0]
    assert not entry["couvert"] and "2026-10-23 07:59:00 au 2026-10-23 08:01:00 UTC" in entry["a_exporter"]
    covering = "2026-10-23 07:59:30;INFO;début\n2026-10-23 08:00:10;ERROR;NullPointerException\n2026-10-23 08:02:00;INFO;fin\n"
    again = cimp.import_files(CLIENT, {"serveur2.csv": covering.encode(), "cucumber.json": _report(
        {"S0": "expected:<A> but was:<B>", "S1": "", "S2": "", "S3": ""}, start="2026-10-23T08:00:00Z").encode()
        + b" "})
    entry = cimp.analyse(CLIENT, again["numero"], state, repo)["logs"][0]
    assert entry["couvert"] and "NullPointerException" in entry["preuves"][0]


def test_correction_lots_from_an_import(setup):
    repo, state = setup
    imported = cimp.import_files(CLIENT, {"cucumber.json": _report({
        "S0": "expected:<A> but was:<B>", "S1": "", "S2": "", "S3": ""}).encode()})
    analysis = cimp.analyse(CLIENT, imported["numero"], state, repo)
    lots = cimp.add_correction_lots(state, analysis)
    assert len(lots) == 1 and lots[0]["type"] == "correction" and lots[0]["ids"] == []
    assert lots[0]["tags"] == {f"@agent_{CLIENT}_1_0": f"{REL}::S0"} and lots[0]["numero"] == 2
    assert lots[0]["corrections"][0]["message"] == "expected:<A> but was:<B>"


def test_file_kinds():
    assert cimp.file_kind("r.json", _report({"S0": ""})) == "cucumber_json"
    assert cimp.file_kind("serveur.csv", "a;b\n") == "logs"
    assert cimp.file_kind("console.txt", CONSOLE) == "console"
    facts = cimp.console_facts(CONSOLE)
    assert facts["tags"] == CLIENT_TAG and facts["tests"]["executes"] == 4


def test_correction_lot_sends_the_real_messages(setup, monkeypatch):
    import importlib
    import io
    import sys
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        main = importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved
    repo, state = setup
    imported = cimp.import_files(CLIENT, {"cucumber.json": _report({
        "S0": "expected:<A> but was:<B>", "S1": "", "S2": "", "S3": ""}).encode()})
    lot = cimp.add_correction_lots(state, cimp.analyse(CLIENT, imported["numero"], state, repo))[0]
    prompts = []
    monkeypatch.setattr(main, "render_prompt", lambda name, **values: prompts.append(values) or "p")
    monkeypatch.setattr(main, "run_cucumber_claude", lambda *a, **k: (0, "{}", {}, False))
    monkeypatch.setattr(main, "report_token_usage", lambda *a, **k: None)
    files = main.run_correction_lot(lot, "2/2", repo, {}, "sonnet", "K", "/run", lambda u: None)
    assert files == [REL] and prompts[0]["etape"] == "RAPPORT D'EXÉCUTION IMPORTÉ"
    assert f"@agent_{CLIENT}_1_0" in prompts[0]["erreurs"] and "expected:<A> but was:<B>" in prompts[0]["erreurs"]
