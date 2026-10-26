"""Phase 6 : audit des tests existants (absence du spécifique, assertions, robustesse, durée), client fictif."""
import json
import os

import pytest

from tests.conftest import CLIENT, write
from tools import cucumber_audit as ca
from tools import cucumber_coverage as cov
from tools import cucumber_journey as cj
from tools import cucumber_tools as ct
from tools.cucumber_verify import parse_scenarios

F = f"src/test/resources/cucumber/features/{CLIENT}/Stock.feature"
FEATURE = """Feature: Stock

  Scenario: Valeurs métier
    Given le contenant "C1"
    When je valide l'écran REPACKING
    Then le contenant a les valeurs
      | code | statut |
      | C1   | LIBRE  |

  Scenario: Présence seulement
    Given le contenant "C2"
    Then le contenant "C2" existe

  Scenario: Statut HTTP seulement
    When j'appelle l'API
    Then la réponse a le statut 200

  Scenario: Configuration seulement
    Then le paramétrage "MAX_COLIS" vaut "5"

  Scenario: Sans contrôle
    Given le contenant "C3"

  Scenario: Fragile
    Given le contenant "C4"
    And j'attends 30 secondes
    Then le nombre de contenants est "12"
    And la date de création est "2026-10-26"
"""


def _scenarios():
    return {s["titre"]: s for s in parse_scenarios(FEATURE)}


@pytest.mark.parametrize("title, quality", [("Valeurs métier", "metier"), ("Présence seulement", "presence"),
                                            ("Statut HTTP seulement", "generique"),
                                            ("Configuration seulement", "configuration"), ("Sans contrôle", "aucune")])
def test_assertion_quality(title, quality):
    assert ca.assertion_quality(_scenarios()[title]) == quality


def test_robustness_issues():
    issues = " | ".join(ca.robustness(F, _scenarios()["Fragile"], []))
    assert "attente fixe" in issues and "date en dur" in issues and "comptage global" in issues
    scenario = _scenarios()["Valeurs métier"]
    assert ca.robustness(F, scenario, [f"{F}:{scenario['ligne']} « Valeurs métier » utilise C9 créé par « X »"]) \
        == ["« Valeurs métier » utilise C9 créé par « X »"]


def test_absence_detection():
    scenario = _scenarios()["Valeurs métier"]
    studio = {"fichiers": [{"path": "p/customized.xml", "comportements_studio": [
        {"type": "écran", "element": "e", "nom": "REPACKING", "changement": "ajouté"}]}]}
    assert ca.absence_detection(scenario, {}, "pertinent")[0] == "oui"
    assert ca.absence_detection(scenario, studio, "faible")[0] == "non"
    assert ca.absence_detection(scenario, studio, "") == ("probable", "cite repacking")
    assert ca.absence_detection(scenario, {"mots_cles": ["contenant"]}, "")[0] == "inconnu"   # un seul mot-clé
    assert ca.absence_detection(scenario, {"mots_cles": ["contenant", "valeurs"]}, "")[0] == "probable"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))
    write(os.path.join(ct.worktree_dir(CLIENT), F), FEATURE)
    return CLIENT


def test_audit_feeds_partial_state_and_real_coverage(client):
    folder = os.path.join(ct.cache_dir(client), "imports", "1")
    os.makedirs(folder)
    with open(os.path.join(folder, "analyse.json"), "w", encoding="utf-8") as f:
        json.dump({"numero": 1, "non_analyse": "", "resultats": {
            f"{F}::Valeurs métier": {"statut": "passe"}, f"{F}::Présence seulement": {"statut": "passe"},
            f"{F}::Fragile": {"statut": "passe"}},
            "durees": {"mediane": 10, "lents": [{"id": f"{F}::Fragile", "secondes": 400, "motif": "400 s"}],
                       "par_scenario": {f"{F}::Fragile": 400}}}, f)
    with open(os.path.join(ct.cache_dir(client), "imports", "index.json"), "w", encoding="utf-8") as f:
        json.dump({"imports": [{"numero": 1, "date": "2026-10-26T10:00:00"}]}, f)
    studio = [{"path": "p/customized.xml", "comportements_studio": [
        {"type": "écran", "element": "e", "nom": "REPACKING", "changement": "ajouté"}],
        "nb_comportements_studio": {"écran": 1}}]
    inventory = {"specifiques": [
        {"id": "sp-a", "libelle": "a", "type": "extension", "nature": "processus", "fichiers": studio,
         "scenarios_existants": [f"{F} (Stock)"], "couverture": "deja_couvert"}]}
    state = {"lots": [{"numero": 1, "resultats": [{"id": "sp-a", "statut": "couvert", "scenarios": [
        {"nom": "Stock / Présence seulement", "fichier": F}, {"nom": "Stock / Fragile", "fichier": F}]}]}]}
    rows = cov.coverage_rows(client, inventory, state)
    assert rows[0]["etat"] == "vert"
    audit = ca.run_audit(client, inventory, rows, {})
    verdicts = {i.split("::")[1]: r["verdict"] for i, r in audit["scenarios"].items()}
    assert verdicts["Présence seulement"] == "partiel" and verdicts["Fragile"] == "fragile"
    assert verdicts["Valeurs métier"] == "pertinent"
    assert audit["scenarios"][f"{F}::Fragile"]["duree"] == 400
    assert any("400 s" in m for m in audit["scenarios"][f"{F}::Fragile"]["motifs"])
    # Écrits par l'agent : un scénario partiel et un fragile, sans scénario « pertinent » : la tâche reste verte
    assert cov.coverage_rows(client, inventory, state)[0]["etat"] == "vert"
    # Seulement le scénario partiel : la tâche devient partielle, hors couverture réelle
    state["lots"][0]["resultats"][0]["scenarios"] = [{"nom": "Stock / Présence seulement", "fichier": F}]
    inventory["specifiques"][0]["scenarios_existants"] = []
    row = cov.coverage_rows(client, inventory, state)[0]
    assert row["etat"] == "partiel" and row["cause"].startswith("audit : vérifie seulement la présence")
    assert cov.rates([row])["reelle"] == 0
    assert "audit du" in cj.coverage_step([row], client)["resume"]
