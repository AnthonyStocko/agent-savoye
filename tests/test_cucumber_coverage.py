"""Parcours de l'onglet Cucumber (phase 1) : comportements Studio, vue Couverture, sélection, étapes, client fictif."""
import json
import os

import pytest

from tools import cucumber_checks as cc
from tools import cucumber_coverage as cov
from tools import cucumber_journey as cj
from tools import cucumber_studio as cs
from tools import cucumber_tools as ct
from tools.cucumber_a_revoir import DISABLED_COMMENT

ORIGINAL = """<?xml version="1.0" encoding="UTF-8"?>
<process-definition xmlns="https://exemple.invalid/xsd/process">
  <interaction id="saisie" title="SAISIE">
    <info translate="false">${$translate.message("Scanner l'article")}</info>
    <field binding="code" label="Code" required="true"><submit name="OK" validate="true"/></field>
    <transitions default="fin"/>
  </interaction>
  <action-ref key="fictif/action-valider" id="valider"><transitions default="fin"/></action-ref>
</process-definition>"""

CUSTOMIZED = """<?xml version="1.0" encoding="UTF-8"?>
<process-definition xmlns="https://exemple.invalid/xsd/process">
  <interaction id="saisie" title="SAISIE">
    <info translate="false">${$translate.message("Scanner l'article")}</info>
    <field binding="code" label="Code" required="false"><submit name="OK" validate="true"/></field>
    <submit name="ANNULER" validate="false"/>
    <transitions default="fin">
      <transition to="erreur">$submit == "ANNULER"</transition>
    </transitions>
  </interaction>
  <interaction id="erreur" title="ERREUR">
    <error translate="false">${$translate.message("Article inconnu")}</error>
  </interaction>
</process-definition>"""


def test_studio_behaviours_compare_customized_with_original():
    found = {(b["type"], b["nom"], b["changement"]) for b in cs.behaviours(ORIGINAL, CUSTOMIZED)}
    assert ("écran", "ERREUR", "ajouté") in found
    assert ("champ", "Code", "modifié") in found                     # obligatoire -> facultatif
    assert ("bouton", "ANNULER", "ajouté") in found
    assert ("condition", '$submit == "ANNULER" → erreur', "ajouté") in found
    assert ("message", "erreur : Article inconnu", "ajouté") in found
    assert ("action", "fictif/action-valider", "supprimé") in found
    assert ("bouton", "OK", "ajouté") not in found                   # inchangé
    # Sans référence (processus propre au client) : tout est ajouté ; XML illisible : rien
    assert {b["changement"] for b in cs.behaviours("", CUSTOMIZED)} == {"ajouté"}
    assert cs.behaviours("", "<pas du xml") == []
    assert cs.behaviours(ORIGINAL, CUSTOMIZED)[0]["type"] == "écran"   # écrans d'abord


def test_listener_trigger_is_an_action():
    listener = ('<execution-event-listener-definition xmlns="x" on-event="a/b/fictif-evenement">'
                '<execute>fictif</execute></execution-event-listener-definition>')
    assert any(b["nom"] == "déclenché sur fictif-evenement" for b in cs.behaviours("", listener))


def _spec(spec_id, **extra):
    spec = {"id": spec_id, "libelle": f"depot-fictif · {spec_id}", "type": "extension", "nature": "objet métier",
            "depot": "depot-fictif", "dossier": f"module-{spec_id}/x", "nb_fichiers": 2, "fichiers": [],
            "mots_cles": [], "couverture": "non_couvert"}
    spec.update(extra)
    return spec


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(cc, "WORKSPACE_DIR", str(tmp_path))
    return "clientfictif"


def _feature(client, rel, text):
    path = os.path.join(ct.worktree_dir(client), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _import(client, numero, results):
    from tools import cucumber_import
    folder = os.path.join(cucumber_import.imports_dir(client), str(numero))
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "analyse.json"), "w", encoding="utf-8") as f:
        json.dump({"numero": numero, "non_analyse": "", "resultats": results, "propositions": []}, f)
    index_path = os.path.join(cucumber_import.imports_dir(client), "index.json")
    index = json.load(open(index_path, encoding="utf-8")) if os.path.exists(index_path) else {"imports": []}
    index["imports"].append({"numero": numero, "date": f"2026-10-2{numero}T10:00:00", "run": str(numero),
                             "fichiers": []})
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f)


F = "src/test/resources/cucumber/features/clientfictif/"


def _scenario(name, feature):
    return {"nom": f"Feature / {name}", "fichier": F + feature}


def test_coverage_states_rates_and_order(client):
    tag = ct.a_revoir_tag()
    _feature(client, F + "c.feature", f"Feature: C\n\n  {DISABLED_COMMENT} donnée de référence absente\n  {tag}\n"
                                      "  Scenario: c1\n    Given x\n")
    _import(client, 1, {F + "a.feature::a1": {"statut": "echoue", "nature": "assertion", "message": "attendu 2"}})
    _import(client, 2, {F + "a.feature::a1": {"statut": "passe"},
                        F + "b.feature::b1": {"statut": "echoue", "nature": "assertion", "message": "attendu 3"}})
    inventory = {"genere_le": "2026-10-26T10:00:00", "specifiques": [
        _spec("sp-a", type="override"), _spec("sp-b"), _spec("sp-c"), _spec("sp-d"),
        _spec("sp-e", couverture="partiel"), _spec("sp-f", nature="libellés"), _spec("sp-g"),
        _spec("sp-h", couverture="deja_couvert")]}
    state = {"lots": [{"numero": 1, "ids": ["sp-a", "sp-b", "sp-c", "sp-d", "sp-f", "sp-g"], "resultats": [
        {"id": "sp-a", "statut": "couvert", "scenarios": [_scenario("a1", "a.feature")]},
        {"id": "sp-b", "statut": "couvert", "scenarios": [_scenario("b1", "b.feature")]},
        {"id": "sp-c", "statut": "couvert", "scenarios": [_scenario("c1", "c.feature")]},
        {"id": "sp-d", "statut": "non_couvert", "raison": "aucun step pour cet écran"},
        {"id": "sp-g", "statut": "couvert_indirectement", "couvert_par": "sp-a"},
        {"id": "sp-f", "statut": "couvert", "scenarios": [_scenario("f1", "f.feature")]}],
        "execution": {"scenarios": {"@t1": {"id": F + "f.feature::f1", "statut": "faible", "motif": "passe sans"}}}}]}
    rows = {r["id"]: r for r in cov.coverage_rows(client, inventory, state)}
    assert rows["sp-a"]["etat"] == "vert" and rows["sp-a"]["derniere"].startswith("import n° 2")   # dernier import
    assert rows["sp-b"]["etat"] == "echec" and rows["sp-b"]["cause"] == "attendu 3"
    assert rows["sp-c"]["etat"] == "desactive" and rows["sp-c"]["cause"] == "donnée de référence absente"
    # Raison libre sans blocage prouvé ni valeur à demander : jamais « non testable », ni « non couverte » après
    # une génération (le résumé du lot devient l'information à confirmer)
    assert rows["sp-d"]["etat"] == "a_confirmer" and rows["sp-d"]["evaluation"]["questions"] == []
    assert rows["sp-e"]["etat"] == "partiel" and rows["sp-f"]["etat"] == "partiel"   # rapprochement / faibles
    assert rows["sp-g"]["etat"] == "vert" and "indirectement" in rows["sp-g"]["cause"]
    assert rows["sp-h"]["etat"] == "non_execute"
    assert rows["sp-d"]["testabilite"] == "testable" and rows["sp-a"]["criteres"]["donnees"][0] == "oui"
    rates = cov.rates(list(rows.values()))
    assert (rates["reelle"], rates["ecrite"], rates["partielles"]) == (25, 62, 2)   # 2/8 verts, 5/8 écrites
    assert "réelle 25 %, écrite 62 %" in cov.summary_line(list(rows.values()))
    ordered = cov.coverage_rows(client, inventory, state)
    assert ordered[0]["risque"] == "élevé" and ordered[-1]["risque"] == "faible"
    assert cov.partial_ids(ordered) == {"sp-e", "sp-f"}
    assert cov.selection_warnings(ordered, ["sp-d", "sp-e"]) == []
    assert cov.failing_test_causes(ordered) == 1


def test_risk_override_and_detected_factors_never_entered(client):
    inventory = {"specifiques": [_spec("sp-a", fichiers=[{"path": "p/customized.xml", "kind": "extension",
                                                          "nb_comportements_studio": {"écran": 2}}])]}
    cov.save_override(client, "sp-a", {"risque": "faible", "testabilite": {"studio": "non"}})
    row = cov.coverage_rows(client, inventory, {"lots": []})[0]
    assert row["risque"] == "faible" and row["risque_propose"] == "moyen"
    # Facteurs détectés : une ancienne saisie est ignorée ; non détecté = « non déterminé », sans blocage
    assert row["criteres"]["studio"] == (cov.UNDETERMINED, "aucun scénario lié exécuté")
    assert row["etat"] == "non_couvert" and row["testabilite"] == "testable"
    assert row["titre"] and row["phrase"].startswith("Vérifier le comportement propre au client (2 écran(s))")
    assert row["point_entree"] == "processus interactif (2 écran(s))"
    cov.save_override(client, "sp-a", {})
    assert cov.load_overrides(client) == {}


def test_tickets_read_from_jira_extract(tmp_path):
    jira = tmp_path / "jira.md"
    jira.write_text("# Tickets\n\n## FIC-12 — Un ticket\ntexte\n\n## FIC-7 — Autre\n", encoding="utf-8")
    assert cov.spec_tickets({"jira": str(jira)}) == ["FIC-12", "FIC-7"]
    assert cov.spec_tickets({"jira": ""}) == []


def test_journey_steps_summaries_and_next_action(client):
    steps = cj.journey(client, {}, {}, {}, [])
    assert [s["cle"] for s in steps] == [k for k, _ in cj.STEPS]
    assert steps[0]["action"] == "Lancer l'inventaire des spécifiques" and cj.next_step(steps) == "projet"
    rows = [{"etat": "non_couvert", "scenarios": []}, {"etat": "vert", "scenarios": []}]
    inventory = {"specifiques": [_spec("sp-a")]}
    with open(cc.health_path(client), "w", encoding="utf-8") as f:
        f.write("# Bilan\n\n- Verdict : 2 défaut(s)\n")
    with open(ct.init_patch_path(client), "w", encoding="utf-8") as f:
        f.write("diff")
    state = {"init": {"ok": False, "message": "step x en échec"},
             "lots": [{"numero": 1, "ids": ["sp-a"], "statut": "termine"}, {"numero": 2, "ids": [], "statut": "a_faire"}]}
    steps = {s["cle"]: s for s in cj.journey(client, inventory, {"date": "x"}, state, rows, 4, 2)}
    assert steps["projet"]["etat"] == "attention" and "en échec (step x en échec)" in steps["projet"]["resume"]
    assert "bilan de santé : 2 défaut(s)" in steps["projet"]["resume"]
    assert steps["couverture"]["resume"].startswith("1 tâche(s) couverte(s) et vertes sur 2")
    assert steps["selection"]["resume"] == "4 tâche(s) sélectionnée(s) → 2 lot(s)"
    assert steps["generation"]["action"] == "Reprendre la génération"
    assert cj.next_step(list(steps.values())) == "couverture"
    _import(client, 1, {"f::a": {"statut": "passe"}, "f::b": {"statut": "echoue"}})
    steps = {s["cle"]: s for s in cj.journey(client, inventory, {"date": "x"}, state, rows)}
    assert steps["execution"]["resume"] == "dernier run : 1 vert(s) / 1 échec(s) (import n° 1)"


def test_lot_block_asks_partial_tasks_to_test_the_effect():
    spec = _spec("sp-a", fichiers=[{"path": "p/customized.xml", "kind": "extension",
                                    "comportements_studio": cs.behaviours("", CUSTOMIZED),
                                    "nb_comportements_studio": {"écran": 2, "bouton": 2}}])
    block = ct.lot_block([{**spec, "partiel": True}])
    assert "TÂCHE PARTIELLE" in block and "EFFET" in block
    assert "Comportements Studio à couvrir (2 écran(s), 2 bouton(s)" in block
    assert "ajouté — écran « ERREUR » (élément erreur)" in block
    assert "TÂCHE PARTIELLE" not in ct.lot_block([spec])
