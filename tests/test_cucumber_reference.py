"""
Jeu de référence (tests/reference/) : pour chaque extrait de console anonymisé, l'échec lu dans la console
(cucumber_import.console_results) est classé (cucumber_failures.classify) avec la catégorie, la nature de la
correction et les mots de la cause attendus (attendus.json). Toute réponse différente fait échouer le test.
"""
import json
import os

import pytest

from tools import cucumber_failures, cucumber_import

REFERENCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference")
with open(os.path.join(REFERENCE, "attendus.json"), encoding="utf-8") as f:
    CASES = json.load(f)["cas"]


def test_reference_covers_the_required_cases():
    assert len(CASES) == 16 and all(os.path.exists(os.path.join(REFERENCE, c["console"])) for c in CASES)
    assert all(c["attendu"]["nature"] in cucumber_failures.NATURES for c in CASES)
    assert all(c["attendu"]["categorie"] in cucumber_failures.CATEGORIES for c in CASES)


@pytest.mark.parametrize("case", CASES, ids=[c["console"].split(".")[0] for c in CASES])
def test_reference_answer(case):
    with open(os.path.join(REFERENCE, case["console"]), encoding="utf-8") as f:
        results = cucumber_import.console_results(f.read())
    failures = [r for r in results.values() if r["statut"] != "passe"]
    assert len(failures) == 1, f"{case['cas']} : {len(failures)} échec(s) lu(s) dans la console"
    got = cucumber_failures.classify({**failures[0], "contexte": case.get("contexte") or {}})
    expected = case["attendu"]
    assert (got["categorie"], got["nature"]) == (expected["categorie"], expected["nature"]), \
        f"{case['cas']} : {got}"
    missing = [w for w in expected["cause_contient"] if w.lower() not in got["cause"].lower()]
    assert not missing, f"{case['cas']} : cause « {got['cause']} » sans {missing}"


RUNS = json.load(open(os.path.join(REFERENCE, "attendus.json"), encoding="utf-8")).get("runs") or []


@pytest.mark.parametrize("run", RUNS, ids=[r["console"].split(".")[0] for r in RUNS])
def test_reference_full_run(run):
    """Console réelle d'un run complet : chaque échec compté par Surefire est lu, classé, et sans écart."""
    with open(os.path.join(REFERENCE, run["console"]), encoding="utf-8") as f:
        console = f.read()
    facts = cucumber_import.console_facts(console)
    assert facts["tests"] == {"executes": run["tests"], "echecs": run["failures"], "erreurs": run["errors"],
                              "ignores": 0}
    assert cucumber_import.full_run({"console": facts}, run["tag_client"]) is run["run_complet"]
    failures = {key: r for key, r in cucumber_import.console_results(console).items() if r["statut"] != "passe"}
    assert len(failures) == run["echecs_lus"] and cucumber_import.console_gap(facts, failures) == ""
    natures = {title: cucumber_failures.classify(r)["nature"] for (_, title), r in failures.items()}
    assert all(cucumber_failures.classify(r)["categorie"] in cucumber_failures.CATEGORIES for r in failures.values())
    for title, nature in run["natures"].items():
        assert natures[title] == nature, f"{title} : {natures[title]}"


ENV_CASES = json.load(open(os.path.join(REFERENCE, "attendus.json"), encoding="utf-8")).get("constats_environnement") or []


@pytest.mark.parametrize("case", ENV_CASES, ids=[c["console"].split(".")[0] for c in ENV_CASES])
def test_reference_environment_constat_is_proven_and_disabled(case, tmp_path):
    """Cause hors test conclue par une correction, preuve dans les logs serveur : statut environnement, A_REVOIR."""
    from tools import cucumber_a_revoir as ar
    from tools import cucumber_diagnostic as cd
    from tools import cucumber_verify as cv
    with open(os.path.join(REFERENCE, case["console"]), encoding="utf-8") as f:
        console = f.read()
    with open(os.path.join(REFERENCE, case["logs"]), encoding="utf-8") as f:
        logs = f.read()
    # Aucun hôte, pod ni identifiant réel dans le cas de référence
    assert "<hote-precolisage>" in logs and "precubing" not in (console + logs).lower()
    (rel, title), failure = next((k, r) for k, r in cucumber_import.console_results(console).items()
                                 if r["statut"] != "passe")
    tag = "@agent_client_1_1"
    item = {"id": f"{rel}::{title}", "statut": "invalide", "motif": failure.get("message", ""), "journal": console}
    checked = []

    def server_log_check(scenario, proof):
        checked.append(proof)
        return " ".join(proof.split()).lower() in " ".join(logs.split()).lower()
    accepted, refused = cd.environment_findings({"constats": [{"tag": tag, **case["constat"]}]}, {tag: item},
                                                server_log_check)
    expected = case["attendu"]
    assert not refused and checked == [case["constat"]["preuve"]]       # absent de la console : relu dans les logs
    item.update(accepted[tag])
    assert item["statut"] == expected["statut"] and expected["source"] in item["motif"]
    decision = ar.decide({tag: item}, {}, max_pct=100, autonomous=True)
    assert decision["desactiver"] == {tag: expected["cause"]} and not decision["bloquants"]
    assert ar.proof_status(expected["cause"], item)[0] is expected["prouvee"]
    comment = ar.disabled_comment(expected["cause"], item)
    assert ar.PROVEN in comment and case["constat"]["reactivation"] in comment
    # Piste de diagnostic de l'échec : cite la source de la bonne adresse
    hint = cv.failure_hint(logs)
    assert all(word in hint for word in expected["piste_contient"])
    # Extrait absent des logs et de la console : constat refusé, statut inchangé
    wrong = {"constats": [{"tag": tag, **case["constat"], "preuve": "Connection refused by remote service X"}]}
    accepted, refused = cd.environment_findings(wrong, {tag: {**item, "statut": "invalide"}}, server_log_check)
    assert accepted == {} and "constat d'environnement refusé" in refused[0]
