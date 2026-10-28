"""Économie de tokens des lots Cucumber : tri préalable, regroupement des lots, steps candidats, sessions allégées."""
import importlib
import io
import json
import os
import sys

import pytest

from tests.conftest import write
from tools import cucumber_tools as ct


@pytest.fixture(scope="module")
def main_module():
    """main.py, importé sans qu'il remplace sys.stdout (ce qui fermerait la capture de pytest)."""
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


LISTENER = ('<execution-event-listener-definition on-event="clientx/{event}" execute="clientx/{process}">\n'
            '  {condition}<mappings><input to="id">\n    $event.wmsGoodsInFolderId\n  </input></mappings>\n'
            '</execution-event-listener-definition>\n')
EVENT = ('<execution-event-definition name="x">\n  <variable name="wmsGoodsInFolderId" type="java.util.UUID" '
         'scope="in"/>\n</execution-event-definition>\n')


def _studio(tmp_path, spec_id, name, text=None, files=("customized.xml",), **extra):
    fichiers = []
    for file_name in files:
        local = tmp_path / spec_id / file_name
        write(str(local), text if text is not None and file_name == "customized.xml" else "<process/>")
        fichiers.append({"path": f"{name}/{file_name}", "kind": "extension", "local": str(local)})
    spec = {"id": spec_id, "libelle": f"studio · {name}", "type": "extension", "nature": "processus",
            "depot": "studio", "dossier": name, "nb_fichiers": len(fichiers), "fichiers": fichiers,
            "mots_cles": [], "couverture": "non_couvert"}
    spec.update(extra)
    return spec


def test_presort_listener_event_and_technical(tmp_path):
    process = "spec-wms-goodsin-process-controlandvalidate-goodsinfolder"
    event = "spec-wms-goodsin-event-controlandvalidate-goodsinfolder"
    specs = [
        _studio(tmp_path, "sp-proc", process, files=("customized.xml", "customized.json")),
        _studio(tmp_path, "sp-lis", "spec-wms-goodsin-listener-controlandvalidate-goodsinfolder",
                LISTENER.format(event=event, process=process, condition="")),
        _studio(tmp_path, "sp-evt", event, EVENT),
        # Listener avec condition : logique propre, laissé à Claude
        _studio(tmp_path, "sp-if", "spec-wms-preparation-listener-managedangerous",
                LISTENER.format(event="trigger", process=process, condition="<if>$event.name != null</if>")),
        # Process déclenché absent de l'inventaire (Standard) : sans objet, sans lot Claude
        _studio(tmp_path, "sp-orphan", "spec-wms-preparation-listener-render-btocinvoice",
                LISTENER.format(event="e", process="spec-wms-preparation-process-render-btocclientx", condition="")),
        {"id": "sp-tech", "libelle": "wms · config", "type": "extension", "nature": "objet métier", "depot": "wms",
         "dossier": "config", "nb_fichiers": 1, "mots_cles": [],
         "fichiers": [{"path": "src/main/resources/config/beans.xml", "kind": "extension"}]},
        {"id": "sp-java", "libelle": "wms · preparation", "type": "extension", "nature": "objet métier",
         "depot": "wms", "dossier": "preparation", "nb_fichiers": 1, "mots_cles": [],
         "fichiers": [{"path": "src/main/java/a/Preparation.java", "kind": "extension"}]},
    ]
    specifics = {s["id"]: s for s in specs}
    wanted = [s["id"] for s in specs]
    remaining, results = ct.presort(wanted, specifics)
    assert remaining == ["sp-proc", "sp-if", "sp-java"]
    by_id = {r["id"]: r for r in results}
    assert by_id["sp-lis"]["statut"] == by_id["sp-evt"]["statut"] == "couvert_indirectement"
    assert by_id["sp-lis"]["couvert_par"] == by_id["sp-evt"]["couvert_par"] == "sp-proc"
    assert by_id["sp-tech"] == {"id": "sp-tech", "statut": "non_couvert", "raison": ct.TECHNICAL_REASON,
                                "scenarios": [], "fichiers": [], "classes_java": [], "inspire_de": [], "tri": True}
    assert by_id["sp-orphan"]["sans_objet"]["preuve"].endswith("mappings $event.* seuls")
    # Process non sélectionné : le listener est quand même classé sans lot Claude (analyse du XML)
    remaining, sorted_out = ct.presort(["sp-lis"], specifics)
    assert remaining == [] and sorted_out[0]["couvert_par"] == "sp-proc"
    lot = ct.presort_lot(results, 4)
    assert lot["statut"] == "termine" and lot["tri"] and lot["ids"] == ["sp-lis", "sp-evt", "sp-orphan", "sp-tech"]
    # Le statut « couvert_indirectement » du tri reste soumis à la requalification finale
    state = {"lots": [lot]}
    assert set(ct.requalify_indirect(state, {"specifiques": specs})) == {"sp-lis", "sp-evt"}
    assert ct.TECHNICAL_REASON not in ct.completable_specifics(state)
    assert not ct.COMPLETABLE_REASON.search(ct.TECHNICAL_REASON)


def _spec(spec_id, test="", dossier="", nature="objet métier", depot="wms"):
    return {"id": spec_id, "nature": nature, "depot": depot, "dossier": dossier or spec_id,
            "tests_std": [{"path": test}] if test else []}


def test_make_lots_groups_by_main_test_then_domain_and_labels():
    specs = [_spec("a", "features/B.feature", "goodsin"), _spec("b", "", "preparation"),
             _spec("c", "features/A.feature", "goodsin"), _spec("lab", nature="libellés", depot="studio"),
             _spec("d", "features/B.feature", "content"), _spec("e", "", "content"),
             _spec("lab2", nature="libellés", depot="wms"), _spec("f", "features/A.feature", "content")]
    specifics = {s["id"]: s for s in specs}
    ids = [s["id"] for s in specs]
    assert ct.order_specifics([i for i in ids if not i.startswith("lab")], specifics) == ["f", "c", "d", "a", "e", "b"]
    lots = ct.make_lots(ids, 2, specifics)
    assert [lot["ids"] for lot in lots] == [["f", "c"], ["d", "a"], ["e", "b"], ["lab", "lab2"]]
    assert [lot["numero"] for lot in lots] == [1, 2, 3, 4]
    # Sans spécifiques : ordre de l'inventaire, comme avant
    assert [lot["ids"] for lot in ct.make_lots(["x", "y", "z"], 2)] == [["x", "y"], ["z"]]


STEPS_MD = """# Steps du Cucumber Standard
Une ligne par step : expression — fichier:ligne.

## KIS
- KIS- Ouvrir une interaction {string} — src/test/java/cucumber/features/kis/KISInteractionStep.java:10
- KIS- Entrer sur cette interaction — src/test/java/cucumber/features/kis/KISInteractionStep.java:20

## REC
- REC- Contrôle dossier d'arrivage {string} — src/test/java/cucumber/features/goodsin/GoodsInFolderStep.java:5
- REC- Création description d'arrivage — src/test/java/cucumber/features/goodsin/GoodsInFolderStep.java:9
- REC- Suppression du dossier d'arrivage — src/test/java/cucumber/features/goodsin/GoodsInFolderStep.java:12

## PREP
- PREP- Valider l'unité de préparation {string} — src/test/java/cucumber/features/prep/PrepStep.java:3
"""


def test_candidate_steps(tmp_path):
    steps_md = tmp_path / "steps_std.md"
    write(str(steps_md), STEPS_MD)
    extract = tmp_path / "TOW-1.feature"
    write(str(extract), "Feature: Arrivage\n  Scenario: Contrôle\n    Given REC- Création description d'arrivage\n"
                        "    When REC- Contrôle dossier d'arrivage \"DA1\"\n")
    endpoints = {"endpoints": [{"methode": "POST", "chemin": "/wms/goodsinfolders", "java": ["E#find"],
                                "steps": ["REC- Contrôle dossier d'arrivage {string}"]}]}
    steps = ct.std_steps(str(steps_md), endpoints)
    assert len(steps) == 6 and steps[2]["classe"] == "GoodsInFolderStep"
    assert steps[2]["endpoints"] == ["POST /wms/goodsinfolders"]
    spec = {"id": "sp-1", "dossier": "spec-wms-goodsin-process-controlandvalidate-goodsinfolder",
            "nature": "processus", "depot": "studio", "mots_cles": ["goodsinfolder"], "fichiers": [],
            "tests_std": [{"path": "features/TOW-1.feature", "extrait": str(extract), "score": 0.8,
                           "step_definitions": []}],
            "recettes": [{"titre": "Process interactif", "steps": "`KIS- Ouvrir une interaction {string}` ; `KIS- Fermer cette interaction`"}]}
    found = ct.candidate_steps(spec, steps, client_name="clientx")
    exprs = [s["expr"] for s in found]
    # Steps du test équivalent (dont celui qui nomme l'objet métier) et de la recette d'abord ; le step sans lien
    # n'est pas proposé
    assert exprs[0] == "REC- Contrôle dossier d'arrivage {string}"
    assert set(exprs[1:3]) == {"KIS- Ouvrir une interaction {string}", "REC- Création description d'arrivage"}
    assert "PREP- Valider l'unité de préparation {string}" not in exprs
    assert found[exprs.index("REC- Contrôle dossier d'arrivage {string}")] == {
        "expr": "REC- Contrôle dossier d'arrivage {string}", "classe": "GoodsInFolderStep",
        "fichier": "src/test/java/cucumber/features/goodsin/GoodsInFolderStep.java",
        "endpoints": ["POST /wms/goodsinfolders"]}
    assert len(ct.candidate_steps(spec, steps, client_name="clientx", limit=2)) == 2

    inventory = {"client": "clientx", "specifiques": [spec]}
    assert ct.attach_candidate_steps(inventory, str(steps_md), endpoints) == 1
    spec.update(libelle="studio · controlandvalidate", type="extension", couverture="non_couvert", nb_fichiers=0)
    for test in spec["tests_std"]:
        test.update(feature="Arrivage", scenarios=["Contrôle"], raison="mots-clés")
    block = ct.lot_block([spec], inline_max=10_000)
    assert "- Steps candidats (Standard, les plus proches, à réutiliser tels quels) :" in block
    assert "  - `REC- Contrôle dossier d'arrivage {string}` — GoodsInFolderStep — POST /wms/goodsinfolders" in block
    # Extrait du test Standard injecté en fin de lot, signalé dans la liste
    assert f"'{extract}' (injecté plus bas)" in block and "When REC- Contrôle dossier d'arrivage" in block
    # Sans injection : seulement le chemin
    assert "(injecté plus bas)" not in ct.lot_block([spec]) and "Contenu injecté" not in ct.lot_block([spec])


def test_fixed_references_respect_budget(tmp_path):
    catalogue, steps_example, runner = (tmp_path / n for n in ("catalogue.md", "Steps.java", "Runner.java"))
    write(str(catalogue), "# Catalogue\n" + "x" * 100)
    write(str(steps_example), "class Steps {}\n" + "y" * 500)
    write(str(runner), "class Runner {}")
    text, inlined = ct.fixed_references(str(catalogue), str(steps_example), [], 300)
    assert inlined == {str(catalogue)} and "# Catalogue" in text
    assert f"Non injecté (taille), à lire une seule fois si nécessaire : '{steps_example}'" in text
    text, inlined = ct.fixed_references(str(catalogue), str(steps_example), [], 10_000)
    assert inlined == {str(catalogue), str(steps_example)} and "class Runner" not in text
    # Runner : dans la partie variable du lot, seulement s'il faut le créer
    assert ct.needs_runner("aucun runner ne couvre 'x' : crée …") and not ct.needs_runner("le runner X couvre déjà")
    assert "class Runner" in ct.runner_block(str(runner), "aucun runner ne couvre 'x' : crée …", 10_000)
    assert ct.runner_block(str(runner), "le runner X couvre déjà", 10_000) == ""


def test_studio_excerpt_keeps_modified_nodes_with_context():
    original = "\n".join(f"<step id=\"s{n}\"/>" for n in range(40))
    customized = original.replace('<step id="s20"/>', '<step id="s20" spec="1"/>\n<action name="clientx/x"/>')
    excerpt = ct.studio_excerpt(original, customized, 150)
    lines = excerpt.splitlines()
    assert lines[0].startswith("# lignes 18-") and "1 ligne(s) du Standard retirée(s) ou remplacée(s)" in lines[0]
    assert any('<action name="clientx/x"/>' in l for l in lines) and any('s17' in l for l in lines)
    assert not any('"s5"' in l for l in lines)
    # Borne en lignes ; processus nouveau : début du fichier
    assert len(ct.studio_excerpt("", customized, 10).splitlines()) == 11
    assert "lire le fichier" in ct.studio_excerpt(original, customized.replace('"s2"', '"s2b"'), 9)


def test_claude_command_lean_options(main_module, monkeypatch):
    tools = ["Read", "Write", "Edit", "Grep", "Glob", "Bash(C:/venv/python.exe:*)"]
    cmd = main_module.claude_command("sonnet", ["/std"], tools, ("--max-turns", "25"), "medium", lean=True,
                                     budget_usd="0.5")
    i = cmd.index("--tools")
    assert cmd[i + 1:i + 7] == ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections"):
        assert flag in cmd
    assert "--mcp-config" not in cmd
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.5"
    assert cmd[cmd.index("--allowedTools") + 1:] == tools
    plain = main_module.claude_command("sonnet", [], tools)
    assert not {"--tools", "--strict-mcp-config", "--max-budget-usd"} & set(plain)

    monkeypatch.delenv("CLAUDE_LEAN_SESSION", raising=False)
    assert main_module.lean_session()
    monkeypatch.setenv("CLAUDE_LEAN_SESSION", "0")
    assert not main_module.lean_session()
    monkeypatch.setenv("CUCUMBER_MAX_BUDGET_USD", "0,75")
    assert main_module.cucumber_budget() == "0.75"
    monkeypatch.setenv("CUCUMBER_MAX_BUDGET_USD", "abc")
    assert main_module.cucumber_budget() == ""


def test_budget_result_is_detected(main_module):
    raw = json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "num_turns": 3,
                      "total_cost_usd": 0.51, "usage": {"input_tokens": 0, "output_tokens": 0},
                      "modelUsage": {"claude-sonnet": {"inputTokens": 12, "outputTokens": 300,
                                                       "cacheReadInputTokens": 1670, "cacheCreationInputTokens": 30}}})
    _, usage = main_module.parse_claude_output(raw)
    assert main_module.budget_hit(usage) and usage["tours"] == 3
    assert (usage["tokens_entree"], usage["tokens_sortie"], usage["cache_lu"], usage["cache_ecrit"]) == (12, 300,
                                                                                                         1670, 30)
    _, usage = main_module.parse_claude_output(json.dumps({"type": "result", "subtype": "success", "result": "ok",
                                                           "usage": {"input_tokens": 5}}))
    assert not main_module.budget_hit(usage) and usage["tokens_entree"] == 5


def test_fix_plan(main_module, monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL_CUCUMBER_FIX", raising=False)
    monkeypatch.delenv("CLAUDE_EFFORT_CUCUMBER_FIX", raising=False)
    monkeypatch.setenv("CLAUDE_EFFORT_CUCUMBER", "medium")
    assert main_module.cucumber_fix_plan("sonnet", 2) == [("haiku", "low"), ("haiku", "low"), ("sonnet", "medium")]
    assert main_module.cucumber_fix_plan("sonnet", 0) == []
    monkeypatch.setenv("CLAUDE_MODEL_CUCUMBER_FIX", "sonnet")
    assert main_module.cucumber_fix_plan("sonnet", 1) == [("sonnet", "low")]


def test_review_skipped_for_empty_fields_only(main_module, monkeypatch):
    calls = []
    monkeypatch.setattr(main_module, "run_claude", lambda *a, **k: calls.append(a) or (0, "[]", None, False))
    monkeypatch.setattr(main_module, "report_token_usage", lambda *a, **k: None)

    def analysis(motifs):
        return {"nb_scenarios": 1, "taux_reutilisation": 80, "scenarios": [
            {"fichier": "a.feature", "titre": "T", "ligne": 3, "spec": "sp-1", "motifs": motifs}]}
    monkeypatch.setattr(main_module.cucumber_verify, "analyse_lot",
                        lambda *a: analysis(["« source » vide dans le JSON", "« difference_standard » vide dans le JSON"]))
    lot = {}
    main_module.review_cucumber_lot(lot, [], "1/1", "/wt", [], [], {}, {}, "sonnet", "K", "/run", lambda u: None)
    assert not calls and lot["revue"]["sans_appel"][0]["titre"] == "T"

    monkeypatch.setattr(main_module.cucumber_verify, "analyse_lot",
                        lambda *a: analysis(["« source » vide dans le JSON", "aucune assertion (pas de Then)"]))
    monkeypatch.setattr(main_module, "render_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(main_module.cucumber_verify, "review_block", lambda *a: "")
    monkeypatch.setattr(main_module.cucumber_verify, "parse_review", lambda *a: [])
    monkeypatch.setattr(main_module.cucumber_verify, "apply_review", lambda *a: [])
    monkeypatch.setattr(main_module.cucumber_tools, "lot_files", lambda *a: [])
    lot = {}
    main_module.review_cucumber_lot(lot, [], "1/1", "/wt", [], [], {}, {}, "sonnet", "K", "/run", lambda u: None)
    assert len(calls) == 1 and "sans_appel" not in lot["revue"]


def test_summary_notes_unreviewed_and_presorted(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))
    results = [{"id": "sp-t", "statut": "non_couvert", "raison": ct.TECHNICAL_REASON, "tri": True}]
    state = {"lots": [{"numero": 1, "ids": ["sp-1"], "statut": "termine", "resultats": [{"id": "sp-1",
                                                                                         "statut": "couvert"}],
                       "revue": {"signales": [], "supprimes": [], "sans_appel": [
                           {"fichier": "a.feature", "titre": "T", "motifs": ["« source » vide dans le JSON"]}]}},
                      ct.presort_lot(results, 2)]}
    inventory = {"specifiques": [{"id": "sp-1", "libelle": "un", "fichiers": []},
                                 {"id": "sp-t", "libelle": "tech", "fichiers": []}]}
    text = open(ct.write_coverage_summary("clientx", inventory, state), encoding="utf-8").read()
    assert "Scénarios signalés sans appel de revue" in text and "a.feature / T" in text
    assert "Tri automatique sans appel Claude : 0 listener(s) / événement(s) sans logique propre, 1 spécifique(s)" \
           in text


def test_system_prompt_asks_to_write_early(main_module):
    assert main_module.DEFAULT_CUCUMBER_MAX_TURNS == 40
    assert main_module.warning_turn(40) == 30 and main_module.warning_turn(25) == 19
    names = ("client_name token_saving_rules references_injectees steps_std features_std endpoints_std java_std "
             "java_client package_steps package_support couverture_existante inventaire cucumber_repo worktree "
             "class_suffix features_root client_tag clear_name tag_a_revoir knowledge_block valeurs_connues sources_std compilation_agent").split()
    text = main_module.render_prompt("cucumber_client_lot_systeme", **{n: n for n in names}, max_turns="40",
                                     tour_alerte=main_module.warning_turn(40))
    assert "=== ÉCRIRE TÔT ===" in text and "au plus 40 tours" in text and "au tour 30 (75 %)" in text
    assert "$" not in text.replace("$event", "")


def test_claude_env_config_dir(main_module, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/.claude-pro")
    assert main_module.claude_env()["CLAUDE_CONFIG_DIR"] == os.path.expanduser("~/.claude-pro")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", " ")
    assert "CLAUDE_CONFIG_DIR" not in main_module.claude_env()


def test_lot_result_constats_are_normalized():
    text = ('```json\n[{"id": "sp-1", "statut": "couvert", "constats": ['
            '{"type": "donnee_introuvable", "detail": "  code   transporteur absent ", "fichier": "a.feature"},'
            '{"type": "ECART_SPEC_CODE", "detail": "Jira : seuil 10 ; code : 12"}, {"type": "tautologique"},'
            '"texte libre", {"type": "inconnu", "detail": "x"}]}, {"id": "sp-2", "statut": "non_couvert"}]\n```')
    results = ct.parse_lot_result(text)
    assert results[0]["constats"] == [
        {"type": "donnee_introuvable", "detail": "code transporteur absent", "fichier": "a.feature"},
        {"type": "ecart_spec_code", "detail": "Jira : seuil 10 ; code : 12", "fichier": "", "certitude": "à vérifier"},
        {"type": "autre", "detail": "texte libre", "fichier": ""},
        {"type": "autre", "detail": "x", "fichier": ""}]
    assert results[1]["constats"] == []
    assert ct.constat_line(results[0]["constats"][0]) == "donnée introuvable : code transporteur absent (a.feature)"
    assert len(ct.normalize_constats({"constats": ["c"] * 20})) == ct.MAX_CONSTATS


def test_summary_lists_constats(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))
    result = {"id": "sp-1", "statut": "non_couvert", "raison": "r",
              "constats": [{"type": "tautologique", "detail": "relecture de la valeur créée", "fichier": ""}]}
    state = {"lots": [{"numero": 1, "ids": ["sp-1"], "statut": "termine", "resultats": [result]}]}
    inventory = {"specifiques": [{"id": "sp-1", "libelle": "un", "fichiers": []}]}
    text = open(ct.write_coverage_summary("clientx", inventory, state), encoding="utf-8").read()
    assert "## Constats des lots" in text and "- un — scénario tautologique écarté : relecture de la valeur créée" in text


def test_system_prompt_describes_constats(main_module):
    names = ("client_name token_saving_rules references_injectees steps_std features_std endpoints_std java_std "
             "java_client package_steps package_support couverture_existante inventaire cucumber_repo worktree "
             "class_suffix features_root client_tag clear_name tag_a_revoir knowledge_block valeurs_connues "
             "sources_std compilation_agent").split()
    text = main_module.render_prompt("cucumber_client_lot_systeme", **{n: n for n in names}, max_turns="40",
                                     tour_alerte=30)
    assert "=== CONSTATS" in text and '"constats": [{"type": "donnee_introuvable|ecart_spec_code|defaut_code|comportement_surprenant|tautologique"' in text
    assert all(kind in text for kind in ct.CONSTAT_TYPES) and "Il n'est pas tautologique" in text
    assert "=== CONTRÔLES (Then) ===" in text and "jamais un comptage ou une recherche globale" in text
