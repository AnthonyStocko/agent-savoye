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
    assert len(CASES) == 10 and all(os.path.exists(os.path.join(REFERENCE, c["console"])) for c in CASES)
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
