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


def test_adaptive_budget_stops_when_the_failure_repeats_and_continues_while_it_changes(repo, main_module,
                                                                                         monkeypatch):
    from tools import cucumber_fix_budget as fb
    moving, repeating = "@agent_clientx_1_1", "@agent_clientx_1_2"
    write(os.path.join(repo, REL), f"{CLIENT_TAG}\nFeature: Stock\n\n  {moving}\n  Scenario: Mouvant\n    Then a\n\n"
                                   f"  {repeating}\n  Scenario: Bloqué\n    Then b\n")
    write(os.path.join(repo, STEPS_REL), STEPS + "}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    messages = iter(["expected <A> but was <B>", "expected <C> but was <D>", "OP introuvable", "liste vide"])
    calls = []

    def verify_lot(mode, worktree, base, build, files, scenarios, *rest):
        out = {}
        if moving in scenarios:
            out[moving] = {"id": f"{REL}::Mouvant", "statut": "invalide", "motif": next(messages), "niveaux": {}}
        if repeating in scenarios:
            # Valeur variable différente, même échec : bloqué après une correction
            out[repeating] = {"id": f"{REL}::Bloqué", "statut": "invalide", "niveaux": {},
                              "motif": f"Index 0 out of bounds (essai {len(calls)})"}
        return {"scenarios": out, "builds": []}

    def claude(prompt, *args, **kwargs):
        calls.append(prompt)
        # Chaque correction modifie la feature (sans modification, le scénario ne serait pas relancé)
        with open(os.path.join(repo, REL), "a", encoding="utf-8") as f:
            f.write("    # correction %d\n" % len(calls))
        return 0, '{"corrigees": 1}', {}, False
    monkeypatch.setattr(main_module.cucumber_verify, "verify_lot", verify_lot)
    monkeypatch.setattr(main_module, "run_cucumber_claude", claude)
    monkeypatch.setattr(main_module, "render_prompt", lambda name, **values: values["erreurs"])
    monkeypatch.setattr(main_module, "report_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "verify_cucumber_lot", lambda *a, **k: {"ok": True})
    monkeypatch.setenv("CUCUMBER_MAX_RETRACT_PCT", "100")
    monkeypatch.delenv("CUCUMBER_VERIFY_FIX_ATTEMPTS", raising=False)
    monkeypatch.setenv("CUCUMBER_VERIFY_FIX_MAX", "3")
    lot = {"numero": 1, "tags": {moving: f"{REL}::Mouvant", repeating: f"{REL}::Bloqué"},
           "indicateurs": {"scenarios": []}}
    main_module.verify_execution_lot(lot, [], "1/1", repo, "HEAD", [REL], [], "local", "", CLIENT, "", "", "", {},
                                     "sonnet", "K", "/run", lambda u: None,
                                     context={"conventions": {"client_tag": CLIENT_TAG}})
    # Plafond : 3 corrections (le scénario mouvant change à chaque fois), puis le dernier run, jamais lu par une
    # correction, est analysé en lecture seule
    assert len(calls) == 4 and "ANALYSE DU DERNIER RUN" in calls[3] and "ANALYSE DU DERNIER RUN" not in calls[2]
    # Même échec une fois : renvoyé avec l'obligation de prouver la cause ; deux fois : bloqué
    assert "Bloqué" in calls[0] and "Bloqué" in calls[1] and "Bloqué" not in calls[2]
    assert "MÊME ÉCHEC" not in calls[0] and "MÊME ÉCHEC" in calls[1] and "PROUVE la cause" in calls[1]
    assert fb.max_attempts() == 3
    monkeypatch.setenv("CUCUMBER_VERIFY_FIX_ATTEMPTS", "0")
    assert fb.max_attempts() == 0


def test_held_scenario_is_not_rerun_so_its_diagnostic_is_not_claimed_and_fixing_stops(repo, main_module, monkeypatch,
                                                                                       capsys):
    """Scénario en attente de décision humaine : pas relancé, donc diagnostic non exécuté et plus de correction."""
    write(os.path.join(repo, REL), FEATURE)
    write(os.path.join(repo, STEPS_REL), STEPS + "}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    tag = "@agent_clientx_1_1"
    failing = {"id": f"{REL}::Bouton de reconditionnement", "statut": "invalide", "niveaux": {},
               "motif": "échoue sur le client : expected true", "journal": "expected true", "sorties": []}
    runs, prompts = [], []

    def verify_lot(mode, worktree, base, build, files, scenarios, *rest):
        runs.append(sorted(scenarios))
        return {"scenarios": {tag: dict(failing)}, "builds": []}

    def claude(prompt, *args, **kwargs):
        prompts.append(prompt)
        write(os.path.join(repo, REL), DIAGNOSED)
        write(os.path.join(repo, STEPS_REL), STEPS + TEMP_METHOD + "}\n")
        return 0, '{"corrigees": 0, "restantes": ["à trancher"]}', {}, False
    monkeypatch.setattr(main_module.cucumber_verify, "verify_lot", verify_lot)
    monkeypatch.setattr(main_module, "run_cucumber_claude", claude)
    monkeypatch.setattr(main_module, "render_prompt", lambda name, **values: values["erreurs"])
    monkeypatch.setattr(main_module, "report_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "verify_cucumber_lot", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(main_module, "hold_meaning_changes", lambda *a, **k: {failing["id"]})
    monkeypatch.setenv("CUCUMBER_MAX_RETRACT_PCT", "100")
    monkeypatch.setenv("CUCUMBER_VERIFY_FIX_ATTEMPTS", "4")
    lot = {"numero": 1, "tags": {tag: failing["id"]}, "indicateurs": {"scenarios": []}}
    main_module.verify_execution_lot(lot, [], "1/1", repo, "HEAD", [REL], [], "local", "", CLIENT, "", "", "", {},
                                     "sonnet", "K", "/run", lambda u: None,
                                     context={"conventions": {"client_tag": CLIENT_TAG}})
    out = capsys.readouterr().out
    assert len(runs) == 1 and len(prompts) == 1                  # run initial seulement, une seule correction
    assert "retiré sans avoir été exécuté" in out and "exécuté puis retiré" not in out
    assert "non relancé (décision humaine en attente)" in out
    assert cd.temporary_steps(repo) == []


def _fix_loop(repo, main_module, monkeypatch, claude, verify_lot, attempts="3"):
    """Boucle de correction après exécution d'un lot d'un scénario, Claude et Jenkins simulés."""
    write(os.path.join(repo, REL), FEATURE)
    write(os.path.join(repo, STEPS_REL), STEPS + "}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    tag = "@agent_clientx_1_1"
    monkeypatch.setattr(main_module.cucumber_verify, "verify_lot", verify_lot)
    monkeypatch.setattr(main_module, "run_cucumber_claude", claude)
    monkeypatch.setattr(main_module, "render_prompt", lambda name, **values: values["erreurs"])
    monkeypatch.setattr(main_module, "report_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "verify_cucumber_lot", lambda *a, **k: {"ok": True})
    monkeypatch.setenv("CUCUMBER_MAX_RETRACT_PCT", "100")
    monkeypatch.setenv("CUCUMBER_VERIFY_FIX_ATTEMPTS", attempts)
    monkeypatch.setenv("CUCUMBER_AUTONOMY", "1")
    lot = {"numero": 1, "tags": {tag: f"{REL}::Bouton de reconditionnement"}, "indicateurs": {"scenarios": []}}
    main_module.verify_execution_lot(lot, [], "1/1", repo, "HEAD", [REL], [], "local", "", CLIENT, "", "", "", {},
                                     "sonnet", "K", "/run", lambda u: None,
                                     context={"conventions": {"client_tag": CLIENT_TAG}})
    return lot, tag


def test_environment_cause_concluded_by_the_fix_is_disabled_with_its_proof(repo, main_module, monkeypatch):
    """« restantes » d'une cause hors test, avec constat « environnement » prouvé : désactivé, PROUVÉE, bilan."""
    line = "Impossible de précoliser après avoir tenté toutes les URL [http://<hote>:80]"
    failing = {"id": f"{REL}::Bouton de reconditionnement", "statut": "invalide", "niveaux": {},
               "motif": "Unable to create a launching Campaign", "journal": f"log serveur : {line}", "sorties": []}
    runs = []

    def verify_lot(mode, worktree, base, build, files, scenarios, *rest):
        runs.append(1)
        return {"scenarios": {"@agent_clientx_1_1": dict(failing)}, "builds": []}

    def claude(prompt, *args, **kwargs):
        return 0, json.dumps({"corrigees": 0, "restantes": ["cause hors test : précolisage injoignable"],
                              "constats": [{"tag": "@agent_clientx_1_1", "type": "environnement",
                                            "detail": "précolisage de production injoignable", "preuve": line,
                                            "reactivation": "réactiver quand le précolisage est branché sur le test"}]}
                             ), {}, False
    lot, tag = _fix_loop(repo, main_module, monkeypatch, claude, verify_lot)
    item = lot["execution"]["scenarios"][tag]
    assert item["statut"] == "environnement" and item["preuve_verifiee"] and len(runs) == 1
    assert [(e["cause"], e["prouvee"]) for e in lot["a_revoir"]] == [("environnement", True)]
    assert lot["a_revoir"][0]["reactivation"] == "réactiver quand le précolisage est branché sur le test"
    # Ancien format (chaîne) accepté : gardé « à vérifier » pour le bilan
    assert lot["correction"]["restantes"] == ["[À vérifier au prochain run] cause hors test : précolisage injoignable"]
    assert lot["correction"]["restantes_detail"][0]["source"] == "correction 1"
    with open(os.path.join(repo, REL), encoding="utf-8") as f:
        text = f.read()
    assert "@A_REVOIR" in text and ar.PROVEN in text and "Scenario: Bouton de reconditionnement" in text


def test_fix_without_any_change_does_not_rerun_the_scenario(repo, main_module, monkeypatch, capsys):
    """Correction qui ne modifie aucun fichier : pas de nouvelle exécution (restauration + build évités)."""
    failing = {"id": f"{REL}::Bouton de reconditionnement", "statut": "invalide", "niveaux": {},
               "motif": "expected true", "journal": "expected true", "sorties": []}
    runs, prompts = [], []

    def verify_lot(mode, worktree, base, build, files, scenarios, *rest):
        runs.append(1)
        return {"scenarios": {"@agent_clientx_1_1": dict(failing)}, "builds": []}

    def claude(prompt, *args, **kwargs):
        prompts.append(prompt)
        return 0, '{"corrigees": 0, "restantes": ["NON PROUVÉE : cause inconnue"]}', {}, False
    lot, tag = _fix_loop(repo, main_module, monkeypatch, claude, verify_lot)
    out = capsys.readouterr().out
    assert len(runs) == 1 and len(prompts) == 1
    assert "exécution non relancée" in out and "non relancé (aucune modification)" in out
    assert lot["execution"]["scenarios"][tag]["statut"] == "invalide"


def test_remaining_points_are_read_in_both_formats():
    """« restantes » : objets structurés (nature, cause, preuve) et ancien format (chaînes), mélangés."""
    fix = {"restantes": [
        "@agent_clientx_1_1 NON PROUVÉE : délai du canal form",
        {"tag": "@agent_clientx_1_1", "nature": "hors_test_prouvee", "cause": "précolisage injoignable",
         "preuve": "Impossible de précoliser après avoir tenté toutes les URL", "reactivation": "brancher le test"},
        {"tag": "@agent_clientx_1_1", "nature": "validation_humaine", "cause": "code d'UM imposé par le site",
         "valeur": {"step": "CONTENT- Le stock suivant", "avant": "HU-01", "apres": "<code conforme>"}},
        {"tag": "@agent_clientx_1_1", "nature": "inconnue", "cause": "écrans suivants pas encore exécutés"}, "  "]}
    remaining = cd.parse_remaining(fix)
    assert [(e["tag"], e["nature"]) for e in remaining] == [
        ("@agent_clientx_1_1", "a_verifier"), ("@agent_clientx_1_1", "hors_test_prouvee"),
        ("@agent_clientx_1_1", "validation_humaine"), ("@agent_clientx_1_1", "a_verifier")]
    assert cd.remaining_constats(remaining) == [{"tag": "@agent_clientx_1_1", "type": "environnement",
                                                 "detail": "précolisage injoignable",
                                                 "preuve": "Impossible de précoliser après avoir tenté toutes les URL",
                                                 "reactivation": "brancher le test"}]
    assert "« HU-01 » → « <code conforme> »" in cd.remaining_line(remaining[2])
    section = "\n".join(cd.remaining_markdown([{"numero": 1, "correction": {"restantes_detail": remaining}}]))
    assert "### Validation humaine demandée" in section and "### À vérifier au prochain run" in section
    assert cd.parse_remaining({}) == [] and cd.parse_remaining({"restantes": None}) == []


def test_two_rules_behind_the_same_error_are_two_proven_causes():
    from tools import cucumber_fix_budget as fb

    def fix(rule, code):
        return {"corrections": [{"tag": "@t", "cause_prouvee": {
            "erreur": f"The handling unit code {code} is not valid", "step": "CONTENT- Le stock suivant",
            "donnee": "code UM", "regle": rule}}]}
    length = fb.proven_causes(fix("code numérique de 18 caractères", "039999021111197001"))
    prefix = fb.proven_causes(fix("code qui commence par le préfixe GS1 du site", "039999021111197001"))
    again = fb.proven_causes(fix("code numérique de 18 caractères", "039999021111197999"))
    assert length["@t"] != prefix["@t"] and length == again
    assert fb.proven_causes({"corrections": [{"tag": "@t", "cause": "sans preuve"}]}) == {}


def test_new_proven_cause_under_the_same_message_keeps_the_scenario_analysed(repo, main_module, monkeypatch, capsys):
    """Même message Cucumber, causes prouvées différentes : pas « bloqué » ; même cause deux fois : bloqué."""
    failing = {"id": f"{REL}::Bouton de reconditionnement", "statut": "invalide", "niveaux": {},
               "motif": "Pas d'élément correspondant à l'attendu", "journal": "x", "sorties": []}
    rules = iter(["code numérique de 18 caractères", "préfixe GS1 du site", "préfixe GS1 du site",
                  "préfixe GS1 du site", "préfixe GS1 du site"])
    runs, calls = [], []

    def verify_lot(mode, worktree, base, build, files, scenarios, *rest):
        runs.append(1)
        return {"scenarios": {"@agent_clientx_1_1": dict(failing)}, "builds": []}

    def claude(prompt, *args, **kwargs):
        calls.append(prompt)
        with open(os.path.join(repo, REL), "a", encoding="utf-8") as f:
            f.write("    # correction %d\n" % len(calls))
        return 0, json.dumps({"corrigees": 1, "corrections": [{"tag": "@agent_clientx_1_1", "cause": "UM refusée",
                              "correction": "code", "cause_prouvee": {"erreur": "code 1 is not valid",
                                                                       "step": "CONTENT- Le stock suivant",
                                                                       "donnee": "code UM",
                                                                       "regle": next(rules)}}]}), {}, False
    monkeypatch.setenv("CUCUMBER_VERIFY_FIX_MAX", "6")
    _fix_loop(repo, main_module, monkeypatch, claude, verify_lot, attempts="")
    out = capsys.readouterr().out
    # Corr. 1 (règle A, 1re cause) → run 2 ; corr. 2 (règle B, nouvelle) → run 3 ; corr. 3 (B encore) → 1er
    # « même échec » ; corr. 4 (B) → bloqué ; le dernier run n'a pas été lu : analyse finale
    assert out.count("cause nouvelle prouvée") == 2 and "arrêt des tentatives" in out
    assert len(runs) == 5 and len(calls) == 5 and "ANALYSE DU DERNIER RUN" in calls[-1]


def test_last_run_analysis_is_read_only_and_reaches_the_bilan(repo, main_module, monkeypatch, capsys):
    """Plafond atteint : le dernier run est analysé sans modification possible, son diagnostic va au bilan."""
    line = "Impossible de précoliser après avoir tenté toutes les URL [http://<hote>:80]"
    failing = {"id": f"{REL}::Bouton de reconditionnement", "statut": "invalide", "niveaux": {},
               "motif": "Unable to create a launching Campaign", "journal": f"serveur : {line}", "sorties": []}
    calls, tools_seen = [], []

    def verify_lot(mode, worktree, base, build, files, scenarios, *rest):
        return {"scenarios": {"@agent_clientx_1_1": dict(failing)}, "builds": []}

    def followup(lot, prompt, model, effort, tools, *args, **kwargs):
        calls.append(prompt)
        tools_seen.append(list(tools))
        with open(os.path.join(repo, REL), "a", encoding="utf-8") as f:
            f.write("    # modification %d\n" % len(calls))
        if "ANALYSE DU DERNIER RUN" not in prompt:
            return 0, '{"corrigees": 1}', {}, False, model, effort
        return 0, json.dumps({"corrigees": 0, "corrections": [
            {"tag": "@agent_clientx_1_1", "cause": "OP en CUBING_ERROR : précolisage injoignable",
             "correction": "aucune (analyse seule)"}], "restantes": [
            {"tag": "@agent_clientx_1_1", "nature": "hors_test_prouvee", "cause": "précolisage injoignable",
             "preuve": line, "reactivation": "brancher le précolisage de test"},
            {"tag": "@agent_clientx_1_1", "nature": "validation_humaine", "cause": "code UM",
             "valeur": {"step": "CONTENT- Le stock suivant", "avant": "A", "apres": "B"}}]}), {}, False, model, effort
    monkeypatch.setattr(main_module, "cucumber_followup", followup)
    monkeypatch.setenv("CUCUMBER_VERIFY_FIX_MAX", "1")
    lot, tag = _fix_loop(repo, main_module, monkeypatch, lambda *a, **k: (0, "{}", {}, False), verify_lot,
                         attempts="1")
    out = capsys.readouterr().out
    assert len(calls) == 2 and "ANALYSE DU DERNIER RUN" in calls[1]
    assert "Write" not in tools_seen[1] and "Edit" not in tools_seen[1] and "Edit" in tools_seen[0]
    with open(os.path.join(repo, REL), encoding="utf-8") as f:
        text = f.read()
    assert "# modification 1" in text and "# modification 2" not in text      # écriture de l'analyse annulée
    assert "modifications annulées" in out
    item = lot["execution"]["scenarios"][tag]
    assert item["statut"] == "environnement" and "diagnostic du dernier run" in item["motif"]
    assert [e["cause"] for e in lot["a_revoir"]] == ["environnement"] and lot["a_revoir"][0]["prouvee"]
    natures = {e["nature"]: e["source"] for e in lot["correction"]["restantes_detail"]}
    assert natures == {"hors_test_prouvee": "analyse du dernier run", "validation_humaine": "analyse du dernier run"}
    from tools import cucumber_decisions
    assert any(d["type"] == "donnees" and "validation demandée" in d["proposition"]
               for d in cucumber_decisions.load_decisions(CLIENT))


def test_fix_prompt_asks_for_structured_remaining_points():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "prompts", "cucumber_client_fix.md"), encoding="utf-8").read()
    assert all(n in text for n in ("hors_test_prouvee", "validation_humaine", "a_verifier", "cause_prouvee"))
