"""Preuve en base exigée pour corriger un échec lié aux données."""
from tools import cucumber_db_proof as dp

FEAT = "src/test/resources/cucumber/features/clientx/a.feature"
OTHER = "src/test/resources/cucumber/features/clientx/b.feature"
SCENARIOS = [
    {"id": "a::OP", "tag": "@agent_clientx_1", "fichier": FEAT,
     "message": "TOW-730 : customBoolean1 'false' attendus, obtenu OP introuvable"},
    {"id": "b::liste", "tag": "@agent_clientx_2", "fichier": OTHER,
     "message": "Cannot access first() element from an empty List"},
]


def test_data_failures_are_recognised():
    assert dp.data_failure("Index 0 out of bounds for length 0")
    assert dp.data_failure("Pas d'élément correspondant à l'attendu {} pour {code=X}")
    assert dp.data_failure("Unable to create a launching Campaign. Preparation order TOW-730 is not A lancer or "
                           "already associated to a launching campaign")
    assert not dp.data_failure("Step undefined: CLIENTX- foo")
    assert len(dp.data_scenarios(SCENARIOS + [{"message": "compilation"}])) == 2


def test_prompt_requires_a_proof_only_with_db_access():
    assert "PREUVE EN BASE OBLIGATOIRE" in dp.prompt_block(SCENARIOS, True)
    assert "NON PROUVÉE" in dp.prompt_block(SCENARIOS, False)
    assert dp.prompt_block([], True) == ""


def test_correction_without_a_real_query_is_refused():
    entries = [{"commande": "query", "ok": True, "tables": ["KIS.WMS_PREPARATION_ORDER"]},
               {"commande": "query", "ok": False, "tables": ["WMS_DISPATCH_UNIT"]}]
    fixed = {"preuves_base": [
        {"tag": "@agent_clientx_1", "tables": ["wms_preparation_order"], "resultat": "0 ligne"},
        {"tag": "@agent_clientx_2", "tables": ["WMS_DISPATCH_UNIT"], "resultat": "3 lignes"}]}   # requête en erreur
    result = dp.check(SCENARIOS, fixed, entries, {FEAT, OTHER})
    assert [s["id"] for s in result["refuses"]] == ["b::liste"] and result["fichiers"] == [OTHER]
    assert list(result["preuves"]) == ["@agent_clientx_1"]
    # Scénario non modifié (laissé dans « restantes ») : rien à refuser
    assert dp.check(SCENARIOS, {}, [], set())["refuses"] == []


def test_unproven_correction_is_reverted_and_kept_to_fix(tmp_path):
    import importlib
    import io
    import os
    import sys
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        m = importlib.import_module("main")
        before = {}
        for rel in (FEAT, OTHER):
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Feature: avant\n", encoding="utf-8")
            before[rel] = "Feature: avant\n"
            path.write_text("Feature: corrigée\n", encoding="utf-8")
        lot = {"base": [{"commande": "query", "ok": True, "tables": ["OLD"]},
                        {"commande": "query", "ok": True, "tables": ["WMS_PREPARATION_ORDER"]}], "correction": {}}
        fixed = {"preuves_base": [{"tag": "@agent_clientx_1", "tables": ["WMS_PREPARATION_ORDER"],
                                   "resultat": "0 ligne", "conclusion": "donnée absente"},
                                  {"tag": "@agent_clientx_2", "tables": ["OLD"], "resultat": "1 ligne"}]}
        refused = m.enforce_db_proofs(str(tmp_path), lot, SCENARIOS, fixed, before, 1, "54")
    finally:
        sys.stdout, sys.stderr = saved
    assert [s["id"] for s in refused] == ["b::liste"]           # table lue AVANT cet appel : pas une preuve
    assert (tmp_path / OTHER).read_text(encoding="utf-8") == "Feature: avant\n"
    assert (tmp_path / FEAT).read_text(encoding="utf-8") == "Feature: corrigée\n"
    assert "sans preuve en base" in lot["correction"]["restantes"][0]
    assert lot["correction"]["preuves_base"][0]["conclusion"] == "donnée absente"
    assert os.path.exists(tmp_path / FEAT)
