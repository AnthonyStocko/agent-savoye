"""Phase 2 : durées, initialisation en échec, régressions, exports de logs hors fenêtre, bilan après import."""
import json
import os

import pytest

from tests.conftest import CLIENT, CLIENT_TAG, git, write
from tools import cucumber_bilan as cb
from tools import cucumber_import as cimp
from tools import cucumber_journey as cj
from tools import cucumber_tools as ct

RES = "src/test/resources/cucumber"
REL = f"{RES}/features/{CLIENT}/Stock.feature"
INIT = f"{RES}/features/{CLIENT}/01-Contexte/Init.feature"
FEATURE = f"{CLIENT_TAG}\nFeature: Stock\n" + "".join(f"  Scenario: S{i}\n    Given x\n" for i in range(4))


def _element(title, error="", seconds=1.0, tags=()):
    status = {"status": "failed", "error_message": error} if error else {"status": "passed"}
    return {"type": "scenario", "name": title, "start_timestamp": "2026-10-23T08:00:00Z",
            "tags": [{"name": t} for t in tags],
            "steps": [{"keyword": "Then ", "name": f'STOCK- le stock de "{title}"',
                       "result": {**status, "duration": int(seconds * 1e9)}}]}


def _report(outcomes: dict, durations: dict = None, init: str = None) -> bytes:
    """Rapport : feature d'init facultative (message d'échec ou '' ), puis {titre: message d'échec ou ''}."""
    features = []
    if init is not None:
        features.append({"uri": f"classpath:cucumber/features/{CLIENT}/01-Contexte/Init.feature",
                         "elements": [_element("Initialisation", init, tags=[f"@INIT_{CLIENT.upper()}"])]})
    features.append({"uri": f"classpath:cucumber/features/{CLIENT}/Stock.feature",
                     "elements": [_element(t, e, (durations or {}).get(t, 1.0)) for t, e in outcomes.items()]})
    return json.dumps(features).encode()


@pytest.fixture
def setup(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("CUCUMBER_SLOW_SCENARIO_SECONDS", raising=False)
    monkeypatch.delenv("CUCUMBER_LOG_EXPORT_MAX_LINES", raising=False)
    write(os.path.join(repo, REL), FEATURE)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    state = {"lots": [{"numero": 1, "tags": {f"@agent_{CLIENT}_1_{i}": f"{REL}::S{i}" for i in range(4)},
                       "indicateurs": {"scenarios": [{"fichier": REL, "titre": f"S{i}", "spec": "sp-1"}
                                                     for i in range(4)]}}]}
    return repo, state


def test_durations_from_report_then_console():
    report = json.loads(_report({"S0": "", "S1": ""}, {"S0": 12.5, "S1": 0}).decode())
    assert cimp.report_durations(report)[f"{REL}::S0"] == pytest.approx(12.5)
    console = ("08:00:00 [INFO] start\n08:00:01   Scenario: S0 # a.feature:3\n08:00:10   Scenario: S1 # a.feature:9\n"
               "08:02:10 Tests run: 2\n")
    assert cimp.console_durations(console) == {"S0": 9.0, "S1": 120.0}
    durations = cimp.scenario_durations(report, console)
    assert durations == {f"{REL}::S0": pytest.approx(12.5), f"{REL}::S1": 120.0}   # S1 : durée JSON nulle


def test_slow_scenarios_and_expired_waits(monkeypatch):
    durations = {"a": 10, "b": 12, "c": 11, "d": 95, "e": 9}
    results = {"e": {"statut": "echoue", "message": "ConditionTimeoutException: condition not met within 30s"}}
    slow = cimp.slow_scenarios(durations, results)
    assert slow["mediane"] == 11 and [e["id"] for e in slow["lents"]] == ["d", "e"]
    assert slow["lents"][1]["motif"].startswith("attente expirée")
    monkeypatch.setenv("CUCUMBER_SLOW_SCENARIO_SECONDS", "20")
    assert [e["id"] for e in cimp.slow_scenarios({"a": 25, "b": 5, "c": 4}, {})["lents"]] == ["a"]


def test_init_failure_stops_the_judgement(setup):
    repo, state = setup
    imported = cimp.import_files(CLIENT, {"cucumber.json": _report(
        {"S0": "java.lang.AssertionError: expected:<A> but was:<B>", "S1": "", "S2": "", "S3": ""},
        init="Site non trouvé(e) avec code SITE1")})
    analysis = cimp.analyse(CLIENT, imported["numero"], state, "")
    assert analysis["init_en_echec"]["id"] == f"{INIT}::Initialisation"
    assert "SITE1" in analysis["init_en_echec"]["message"] and analysis["propositions"] == []
    assert "Initialisation en échec" in cimp.analysis_markdown(analysis)
    assert cb.next_action(analysis, {"manuelles": []}).startswith("Corriger l'initialisation")


def test_log_export_outside_failure_window(setup, monkeypatch):
    repo, state = setup
    monkeypatch.setenv("CUCUMBER_LOG_EXPORT_MAX_LINES", "3")
    logs = "2026-10-23 07:10:00 INFO a\n2026-10-23 07:11:00 ERROR b\n2026-10-23 07:12:00 INFO c\n"
    imported = cimp.import_files(CLIENT, {"cucumber.json": _report({"S0": "expected:<A> but was:<B>", "S1": "",
                                                                    "S2": "", "S3": ""}),
                                          "serveur.csv": logs.encode()})
    warnings = cimp.analyse(CLIENT, imported["numero"], state, "")["avertissements"]
    assert any("plafond de l'outil d'export (3)" in w for w in warnings)
    outside = next(w for w in warnings if "hors de la fenêtre des échecs" in w)
    assert "UTC" in outside and "heure locale" in outside and "filtre" in outside and "au plus 3 lignes" in outside


def _imports(state, first: dict, second: dict) -> dict:
    cimp.analyse(CLIENT, cimp.import_files(CLIENT, {"a.json": _report(first)})["numero"], state, "")
    report = json.loads(_report(second).decode())
    for element in report[0]["elements"]:
        element["start_timestamp"] = "2026-10-24T08:00:00Z"
    return cimp.analyse(CLIENT, cimp.import_files(CLIENT, {"b.json": json.dumps(report).encode()})["numero"],
                        state, "")


def test_bilan_sections_and_next_action(setup, tmp_path):
    repo, state = setup
    state["lots"] += [
        {"numero": 2, "type": "correction", "fichiers": [REL], "corrections": [
            {"tag": f"@agent_{CLIENT}_1_0", "id": f"{REL}::S0", "fichier": REL, "message": "expected:<A> but was:<B>"}],
         "correction": {"corrections": [{"tag": f"@agent_{CLIENT}_1_0", "cause": "colonne fausse",
                                         "correction": "colonne status.id"}], "constats": [
             {"type": "defaut_code", "detail": "variable jamais remplie", "fichier": "Proc/customized.xml",
              "ligne": "42", "extrait": "<output to=\"x\"/>", "effet": "statut jamais posé",
              "correction": "mapper la sortie", "certitude": "probable"}]}},
        {"numero": 3, "resultats": [{"id": "sp-1", "constats": [
            {"type": "comportement_surprenant", "detail": "traitement conditionné au niveau de log",
             "certitude": "à vérifier"}]}],
         "a_revoir": [{"fichier": REL, "titre": "S3", "cause": "bug_applicatif", "preuve": "500 Internal Server Error",
                       "reactivation": "correctif livré", "prouvee": True}]}]
    analysis = _imports(state, {"S0": "expected:<A> but was:<B>", "S1": "Site non trouvé(e) avec code X9",
                                "S2": "", "S3": ""},
                        {"S0": "", "S1": "", "S2": "expected:<C> but was:<D>", "S3": ""})
    assert analysis["comparaison"]["regressions"] == [f"{REL}::S2"]
    cb.add_manual_correction(CLIENT, "paramètre de stock créé à la main")
    worktree = str(tmp_path / "worktree")
    os.makedirs(worktree)
    bilan = cb.after_import(CLIENT, analysis, state, worktree)
    assert [f["scenario"] for f in bilan["corriges"]] == [f"{REL}::S0"]
    assert bilan["corriges"][0]["correction"] == "colonne status.id" and bilan["corriges"][0]["fichiers"] == [REL]
    # S1 : donnée de référence devenue verte sans correction des tests -> correction manuelle détectée
    detected, reported = bilan["manuelles"]
    assert "X9" in detected["description"] and "init du projet" in detected["recreation"]
    assert reported["description"] == "paramètre de stock créé à la main" and reported["origine"].startswith("signalée")
    assert [d["fichier"] for d in bilan["defauts"]] == ["Proc/customized.xml", REL]
    assert bilan["doutes"][0]["certitude"] == "à vérifier" and len(bilan["desactives"]) == 1
    assert bilan["prochaine_action"].startswith("Lancer la correction ciblée")   # S2 : échec « test » de l'agent
    markdown = open(bilan["fichier"], encoding="utf-8").read()
    assert markdown.index("Défauts détectés") < markdown.index("Corrigés") < markdown.index("# Import n°")
    assert "Proc/customized.xml:42" in markdown and "Correction suggérée : mapper la sortie" in markdown
    a_revoir = open(os.path.join(worktree, "A_REVOIR.md"), encoding="utf-8").read()
    assert a_revoir.count(cb.BEGIN) == 1
    cb.after_import(CLIENT, analysis, state, worktree)   # bilan suivant : la section est remplacée
    assert open(os.path.join(worktree, "A_REVOIR.md"), encoding="utf-8").read().count(cb.BEGIN) == 1
    assert cb.latest_bilan(CLIENT)["import"] == analysis["numero"]


def test_bilan_section_committed_in_worktree(setup):
    repo, state = setup
    analysis = cimp.analyse(CLIENT, cimp.import_files(CLIENT, {"a.json": _report(
        {"S0": "", "S1": "", "S2": "", "S3": ""})})["numero"], state, "")
    bilan = cb.after_import(CLIENT, analysis, state, repo)
    assert bilan["a_revoir"] and bilan["prochaine_action"].startswith("Livrer le patch")
    assert "bilan du run importé" in git(repo, "log", "-1", "--format=%s")
    step = cj.review_step(CLIENT, state)
    assert step["action"] == bilan["prochaine_action"] and step["etat"] == "fait"
