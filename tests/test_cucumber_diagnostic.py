"""Point 7 : diagnostic temporaire (D7), nature « déploiement » sur extrait vérifié (D8), init en échec."""
import base64
import importlib
import io
import json
import os
import sys

import pytest

from tests.conftest import CLIENT, CLIENT_TAG, git, write
from tools import cucumber_a_revoir as ar
from tools import cucumber_diagnostic as cd
from tools import cucumber_verify as cv

RES = "src/test/resources/cucumber"
REL = f"{RES}/features/{CLIENT}/Stock.feature"
STEPS_REL = "src/test/java/client/StockSteps.java"
FEATURE = (f"{CLIENT_TAG}\nFeature: Stock\n\n"
           "  @agent_clientx_1 @agent_clientx_1_1\n"
           "  Scenario: Bouton de reconditionnement\n"
           "    When STOCK- j'ouvre l'écran \"REPACK\"\n"
           "    Then STOCK- le bouton \"Reconditionner\" est présent\n")
DIAGNOSED = FEATURE.replace("    Then STOCK-", "    And TEMPORAIRE- affiche l'écran reçu\n"
                                              "      | champ |\n      | boutons |\n    Then STOCK-")
STEPS = ("public class StockSteps {\n"
         "    @Then(\"STOCK- le bouton {string} est présent\")\n"
         "    public void bouton(String b) {\n        check(b);\n    }\n")
TEMP_METHOD = ("    @And(\"TEMPORAIRE- affiche l'écran reçu\")\n"
               "    public void diagnostic(DataTable t) {\n        scenario.log(\"écran : \" + screen());\n    }\n")


@pytest.fixture
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


def test_temporary_steps_are_found_then_removed(repo):
    write(os.path.join(repo, REL), DIAGNOSED)
    write(os.path.join(repo, STEPS_REL), STEPS + TEMP_METHOD + "}\n")
    found = cd.temporary_steps(repo)
    assert sorted(found) == sorted([f"{STEPS_REL}:6", f"{REL}:7"])
    assert sorted(cd.remove_temporary(repo)) == sorted([REL, STEPS_REL])
    with open(os.path.join(repo, REL), encoding="utf-8") as f:
        assert f.read() == FEATURE
    with open(os.path.join(repo, STEPS_REL), encoding="utf-8") as f:
        assert f.read() == STEPS + "}\n"
    assert cd.temporary_steps(repo) == [] and cd.remove_temporary(repo) == []


def _report(outputs=(), embeddings=()):
    return [{"uri": f"classpath:cucumber/features/{CLIENT}/Stock.feature", "elements": [
        {"type": "scenario", "name": "Bouton de reconditionnement", "steps": [
            {"keyword": "And ", "name": "TEMPORAIRE- affiche l'écran reçu", "output": list(outputs),
             "embeddings": list(embeddings), "result": {"status": "passed"}},
            {"keyword": "Then ", "name": "STOCK- le bouton \"Reconditionner\" est présent",
             "result": {"status": "failed", "error_message": "java.lang.AssertionError: expected true"}}]}]}]


def test_report_keeps_step_outputs_in_the_journal():
    attached = {"mime_type": "text/plain", "data": base64.b64encode("pièce jointe".encode()).decode()}
    result = cv.parse_cucumber_report(_report(["écran REPACK : boutons [Valider, Annuler]"], [attached]))
    item = next(v for (_, title), v in result.items() if title == "Bouton de reconditionnement")
    assert item["sorties"] == ["écran REPACK : boutons [Valider, Annuler]", "pièce jointe"]
    assert "expected true" in item["journal"] and "boutons [Valider, Annuler]" in item["journal"]
    assert cd.diagnostic_lines({"sorties": item["sorties"]})[0].startswith("  sortie du diagnostic : écran REPACK")


def test_deployment_requires_an_extract_found_in_the_log():
    scenarios = {"@agent_clientx_1_1": {"id": "x", "statut": "invalide",
                                        "journal": "AssertionError | écran REPACK : boutons [Valider, Annuler]"}}
    fix = {"constats": [
        {"tag": "@agent_clientx_1_1", "type": "deploiement", "detail": "personnalisation absente",
         "preuve": "écran REPACK :  boutons [Valider, Annuler]"},
        {"tag": "@agent_clientx_1_1", "type": "deploiement", "preuve": "bouton Reconditionner absent du code"},
        {"tag": "@inconnu", "type": "deploiement", "preuve": "écran REPACK : boutons"},
        {"tag": "@agent_clientx_1_1", "type": "deploiement", "preuve": "court"}]}
    accepted, refused = cd.deployment_findings(fix, scenarios)
    assert accepted["@agent_clientx_1_1"]["statut"] == "deploiement"
    assert accepted["@agent_clientx_1_1"]["preuve_verifiee"]
    assert len(refused) == 3 and "absent du journal" in refused[0] and "inconnu" in refused[1]
    # Sans extrait vérifié, jamais de déploiement ; avec, la cause est prouvée
    item = {**scenarios["@agent_clientx_1_1"], **accepted["@agent_clientx_1_1"]}
    assert ar.cause_of(item) == "deploiement" and ar.proof_status("deploiement", item)[0]
    assert not ar.proof_status("deploiement", {"statut": "deploiement", "motif": "x"})[0]
    assert cd.parse_fix_result('Fini.\n```json\n{"corrigees": 0, "constats": []}\n```') == {"corrigees": 0,
                                                                                           "constats": []}


def test_failed_init_names_its_first_failing_step_and_cause(monkeypatch):
    monkeypatch.setattr(cv, "run_tags", lambda *a, **k: {"ok": True, "build": "", "resultats": {
        ("a.feature", "Init sites"): {"statut": "echoue", "step": "Given INIT- sites", "message": "Site non trouvé(e) "
                                      "avec code S1", "nature": "donnee_reference"},
        ("a.feature", "Init stock"): {"statut": "cascade", "step": "", "message": "mise en place", "cause": ""}}})
    init = cv.run_init("local", "", "HEAD", None, "@INIT")
    assert not init["ok"] and init["message"].startswith("environnement non initialisé (@INIT) : Init sites : Given")
    assert "(cause : donnée de référence absente)" in init["message"]


def test_fix_loop_runs_diagnostic_once_and_accepts_verified_deployment(repo, main_module, monkeypatch):
    write(os.path.join(repo, REL), FEATURE)
    write(os.path.join(repo, STEPS_REL), STEPS + "}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    tag = "@agent_clientx_1_1"
    failing = {"id": f"{REL}::Bouton de reconditionnement", "statut": "invalide", "niveaux": {},
               "motif": "échoue sur le client : expected true", "journal": "expected true", "sorties": []}
    diagnosed = {**failing, "journal": "expected true | écran REPACK : boutons [Valider, Annuler]",
                 "sorties": ["écran REPACK : boutons [Valider, Annuler]"]}
    runs, prompts = [], []

    def verify_lot(mode, worktree, base, build, files, scenarios, *rest):
        with open(os.path.join(worktree, REL), encoding="utf-8") as f:
            runs.append("TEMPORAIRE-" in f.read())
        return {"scenarios": {tag: dict(diagnosed if runs[-1] else failing)}, "builds": []}

    def claude(prompt, *args, **kwargs):
        prompts.append(prompt)
        if len(prompts) == 1:  # 1re correction : diagnostic temporaire
            write(os.path.join(repo, REL), DIAGNOSED)
            write(os.path.join(repo, STEPS_REL), STEPS + TEMP_METHOD + "}\n")
            return 0, '{"corrigees": 0, "restantes": ["à trancher"]}', {}, False
        return 0, json.dumps({"corrigees": 0, "constats": [{"tag": tag, "type": "deploiement", "detail": "bouton "
                              "absent", "preuve": "boutons [Valider, Annuler]"}]}), {}, False
    monkeypatch.setattr(main_module.cucumber_verify, "verify_lot", verify_lot)
    monkeypatch.setattr(main_module, "run_cucumber_claude", claude)
    monkeypatch.setattr(main_module, "render_prompt", lambda name, **values: values["erreurs"])
    monkeypatch.setattr(main_module, "report_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "verify_cucumber_lot", lambda *a, **k: {"ok": True})
    monkeypatch.setenv("CUCUMBER_MAX_RETRACT_PCT", "100")
    monkeypatch.setenv("CUCUMBER_VERIFY_FIX_ATTEMPTS", "2")
    lot = {"numero": 1, "tags": {tag: failing["id"]}, "indicateurs": {"scenarios": []}}
    main_module.verify_execution_lot(lot, [], "1/1", repo, "HEAD", [REL], [], "local", "", CLIENT, "", "", "", {},
                                     "sonnet", "K", "/run", lambda u: None,
                                     context={"conventions": {"client_tag": CLIENT_TAG}})
    # Diagnostic exécuté une fois, retiré, sa sortie rendue à la 2e correction
    assert runs == [False, True] and "sortie du diagnostic : écran REPACK" in prompts[1]
    assert cd.temporary_steps(repo) == []
    # Déploiement prouvé par l'extrait : scénario désactivé (jamais supprimé), cause prouvée
    assert lot["execution"]["scenarios"][tag]["statut"] == "deploiement"
    assert [e["cause"] for e in lot["a_revoir"]] == ["deploiement"] and lot["a_revoir"][0]["prouvee"]
    with open(os.path.join(repo, REL), encoding="utf-8") as f:
        text = f.read()
    assert "@A_REVOIR" in text and "Scenario: Bouton de reconditionnement" in text
