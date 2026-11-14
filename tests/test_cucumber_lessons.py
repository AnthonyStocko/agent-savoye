"""Leçons du client, reprise sans régénération, tests verts figés, plafond de coût, branchements externes."""
import json
import os

import pytest

from tests.conftest import CLIENT, git, write
from tools import cucumber_a_revoir as ar
from tools import cucumber_copied_config as cc
from tools import cucumber_cost as cost
from tools import cucumber_lessons as cl
from tools import cucumber_simple as cs
from tools import cucumber_tools as ct

RES = "src/test/resources/cucumber"
REL = f"{RES}/features/{CLIENT}/Stock.feature"
JAVA = "src/test/java/client/StockSteps.java"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    return CLIENT


def _fix(rule="code de 18 chiffres préfixé GS1", control=None, error="The handling unit code 0399 is not valid"):
    return {"tag": "@t", "cause": "UM refusée", "correction": "code changé",
            "cause_prouvee": {"erreur": error, "step": "CONTENT- Le stock suivant", "donnee": "code UM",
                              "regle": rule},
            "lecon": {"regle": rule, "controle": control or {}}}


# --- Leçons -----------------------------------------------------------------------------------------------------

def test_lesson_needs_a_proven_cause_and_a_rule(client):
    assert cl.lesson_from_fix({"tag": "@t", "cause": "x", "lecon": {"regle": "r"}}) == {}
    assert cl.lesson_from_fix({**_fix(), "lecon": {"regle": ""}}) == {}
    lesson = cl.lesson_from_fix(_fix(control={"type": "valeur_prefixe", "step": "CONTENT- Le stock suivant",
                                              "colonne": "wmsHandlingUnitCode", "prefixe": "3148950"}))
    assert lesson["regle"] and lesson["controle"]["type"] == "valeur_prefixe" and lesson["erreur_cle"]
    # Contrôle inconnu, incomplet ou motif invalide : leçon gardée dans le prompt seulement
    assert cl.valid_control({"type": "inconnu"}) == {} and cl.valid_control({"type": "colonne_requise"}) == {}
    assert cl.valid_control({"type": "motif_java_interdit", "motif": "("}) == {}


def test_lessons_are_numbered_kept_and_recognised_when_they_come_back(client):
    first = cl.add(client, cl.lesson_from_fix(_fix()), {"lot": 1})
    again = cl.add(client, cl.lesson_from_fix(_fix(rule="autre formulation")), {"lot": 2})
    assert first["numero"] == 1 and again["numero"] == 1 and len(cl.load(client)) == 1
    # Même erreur serveur (valeurs masquées) sur le même step, autre formulation de la règle : récidive
    recurring = _fix(rule="préfixe du site", error="The handling unit code 0511 is not valid")
    assert cl.match(cl.load(client), recurring)["numero"] == 1
    assert cl.match(cl.load(client), _fix(error="Location not found")) == {}
    cl.record_recurrence(client, 1, {"lot": 3})
    assert cl.load(client)[0]["recidives"][0]["lot"] == 3
    block = cl.prompt_block(client)
    assert "LEÇONS DU CLIENT" in block and "Leçon n° 1" in block and "leçon déjà connue" in block
    assert cl.prompt_block("autre_client") == ""


def test_lesson_controls_run_on_the_lot_files(tmp_path):
    feature = ("Feature: F\n  Background:\n    Given CONTENT- Le stock suivant\n"
               "      | wmsHandlingUnitCode | quantity |\n      | 039999021111197001 | 1 |\n"
               "      | $UM.code | 1 |\n"
               "  Scenario: S\n    Then PREP- Contrôle des ordres de préparation\n"
               "      | code | customString1 |\n      | OP-1 | x |\n"
               "    And CARTO- Les implantations suivantes\n      | refProductCode |\n      | P1 |\n")
    write(str(tmp_path / REL), feature)
    write(str(tmp_path / JAVA), "DataTable t = DataTable.create(rows);\n")
    lessons = [
        {"numero": 1, "regle": "préfixe GS1", "controle": {"type": "valeur_prefixe", "step": "CONTENT- Le stock suivant",
                                                          "colonne": "wmsHandlingUnitCode", "prefixe": "3148950"}},
        {"numero": 2, "regle": "step projet", "controle": {"type": "colonne_interdite",
                                                          "step": "PREP- Contrôle des ordres de préparation",
                                                          "colonne": "customString1"}},
        {"numero": 3, "regle": "statut administratif", "controle": {
            "type": "colonne_requise", "step": "CARTO- Les implantations suivantes",
            "colonne": "refAdministrativeStatusCode"}},
        {"numero": 4, "regle": "convertisseur", "controle": {"type": "motif_java_interdit",
                                                            "motif": r"DataTable\.create\(\s*[\w.()]+\s*\)"}},
        {"numero": 5, "regle": "emplacement préparable", "controle": {}}]
    errors = cl.check_files(str(tmp_path), [REL, JAVA], lessons)
    assert [e.split("leçon n° ")[1][0] for e in errors] == ["1", "2", "3", "4"]
    assert "039999021111197001" in errors[0] and "$UM.code" not in errors[0]
    fixed = (feature.replace("039999021111197001", "314895099970010001").replace(" customString1 |", " status |")
             .replace("| refProductCode |\n      | P1 |", "| refProductCode | refAdministrativeStatusCode |\n"
                                                         "      | P1 | N/A |"))
    write(str(tmp_path / REL), fixed)
    write(str(tmp_path / JAVA), "DataTable t = DataTable.create(rows, converter);\n")
    assert cl.check_files(str(tmp_path), [REL, JAVA], lessons) == []


# --- Reprise sans régénération, tests verts figés ---------------------------------------------------------------

def _lot(numero, ids, statut="termine", green=False, files=("a.feature",)):
    scenarios = {"@t": {"id": "a.feature::S", "statut": "pertinent" if green else "invalide"}}
    return {"numero": numero, "ids": ids, "statut": statut, "fichiers": list(files), "resultats": [],
            "execution": {"scenarios": scenarios}}


def test_already_generated_specific_is_resumed_never_regenerated():
    state = {"lots": [_lot(1, ["sp-a"]), _lot(2, ["sp-a"]), _lot(3, ["sp-b"], green=True),
                      {"numero": 4, "ids": ["sp-c"], "statut": "erreur", "resultats": None}]}
    marked = ct.mark_reprise(state, ["sp-a", "sp-b", "sp-c"])
    assert marked == {"reprises": [2], "verts": ["sp-b"], "absents": ["sp-c"]}
    assert state["lots"][1]["statut"] == ct.REPRISE and state["lots"][0]["statut"] == "termine"
    assert state["lots"][2]["statut"] == "termine"                    # vert : jamais relancé
    # Étape 5 : reprise par défaut, nouvelle génération seulement sur demande confirmée
    assert cs.generation_mode(state, "sp-a") == ["--reprise"]
    assert cs.generation_mode(state, "sp-c") == ["--complete"]
    assert cs.generation_mode(state, "sp-a", renew=True) == [] and cs.generation_mode({}, "sp-a") == []


def test_green_scenario_is_frozen(repo):
    feature = "Feature: F\n  Scenario: S\n    Given A\n    Then B \"1\"\n"
    write(os.path.join(repo, "a.feature"), feature)
    scenarios = {"@t": {"id": "a.feature::S", "statut": "pertinent", "build": "build 401"},
                 "@u": {"id": "a.feature::Absent", "statut": "invalide"}}
    assert ar.record_green(repo, scenarios) == 1 and scenarios["@t"]["empreinte_verte"]
    state = {"lots": [{"execution": {"scenarios": scenarios}}]}
    assert ar.check_green(repo, state) == []
    write(os.path.join(repo, "a.feature"), "@tag\n" + feature + "  # commentaire\n")    # tags, commentaires : permis
    assert ar.check_green(repo, state) == []
    write(os.path.join(repo, "a.feature"), feature.replace('"1"', '"2"'))
    assert "scénario vérifié vert (build 401) modifié" in ar.check_green(repo, state)[0]
    write(os.path.join(repo, "a.feature"), "Feature: F\n")
    assert "retiré" in ar.check_green(repo, state)[0]


# --- Coût -------------------------------------------------------------------------------------------------------

def test_lot_budget_stops_the_lot_and_cost_is_cumulated_per_specific(client, monkeypatch, capsys):
    monkeypatch.delenv("CUCUMBER_LOT_BUDGET_USD", raising=False)
    assert cost.lot_budget() == 15.0
    lot = {"numero": 1, "ids": ["sp-a", "sp-b"], "tokens": {"cout_usd": 20.0}}
    cost.start_lot(lot)                                  # exécution précédente déjà comptée
    lot["tokens"]["cout_usd"] = 30.0
    assert cost.run_cost(lot) == 10.0 and not cost.over_budget(lot, "1/1")
    assert cost.call_budget(lot, "") == "5.00" and cost.call_budget(lot, "2") == "2.00"
    lot["tokens"]["cout_usd"] = 36.0
    assert cost.over_budget(lot, "1/1") and lot["plafond_cout"]["budget"] == 15.0
    assert "Plafond de coût du lot atteint" in capsys.readouterr().out
    data = cost.record_lot_run(client, lot, "g1")
    assert data["sp-a"]["total"] == 8.0 and cost.spec_total(client, "sp-b") == 8.0
    assert data["sp-a"]["executions"][0]["plafond"] is True
    cost.start_lot(lot)
    lot["tokens"]["cout_usd"] = 40.0
    cost.record_lot_run(client, lot, "g2")
    assert cost.spec_total(client, "sp-a") == 10.0 and cs.spec_cost(client, "sp-a,sp-b") == 20.0
    monkeypatch.setenv("CUCUMBER_LOT_BUDGET_USD", "0")
    assert not cost.over_budget({"tokens": {"cout_usd": 999}}, "x") and cost.call_budget(lot, "") == ""


# --- Branchements externes de la copie --------------------------------------------------------------------------

def test_external_connections_of_the_copy_are_listed_without_credentials():
    rows = {"wms_preparation_saas_param": ["CUBING@@http://precolisage-client:80/ws"],
            "exc_message_partner": ["LM Connect@@https://partenaire.example/api?token=x", "WCS@@localhost:8080"]}

    def query(sql, schema):
        table = sql.split(" FROM ")[1].split()[0]
        return [{"code": r} for r in rows.get(table, [])]
    found = cc.external_connections("schema_test", query)
    assert [(c["type"], c["code"], c["hote"]) for c in found] == [
        ("précolisage", "CUBING", "precolisage-client"), ("partenaire de messages", "LM Connect", "partenaire.example"),
        ("partenaire de messages", "WCS", "localhost")]
    lines = cc.connections_lines(found)
    assert "INIT- Configuration du colisage" in lines[0] and "token" not in " ".join(lines)
    assert cc.new_reference_entry("s", "t", "v", "d", {}, "", found)["branchements"] == found
