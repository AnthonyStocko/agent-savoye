"""Phase 3 : décisions humaines, désactivation provisoire, trois tentatives puis preuve, diagnostics d'un run à l'autre."""
import os

import pytest

from tests.conftest import CLIENT, CLIENT_TAG, git, write
from tools import cucumber_a_revoir as ar
from tools import cucumber_bilan as cb
from tools import cucumber_decisions as cd
from tools import cucumber_import as cimp
from tools import cucumber_journey as cj
from tools import cucumber_tools as ct
from tools import cucumber_verify as cv

RES = "src/test/resources/cucumber"
REL = f"{RES}/features/{CLIENT}/Stock.feature"
FEATURE = (f"Feature: Stock\n\n"
           f"  {CLIENT_TAG}\n  Scenario: Contexte du stock\n    Given le site \"S1\"\n\n"
           f"  {CLIENT_TAG}\n  Scenario: Statut du contenant\n    Given le contenant \"C1\"\n"
           f"      | code | qte |\n      | C1   | 2   |\n    Then le statut est \"LIBRE\"\n\n"
           f"  {CLIENT_TAG}\n  Scenario: Motif de blocage\n    Given le contenant \"C2\"\n"
           f"    Then le motif est \"DECANTING\"\n")


def _text(worktree):
    with open(os.path.join(worktree, REL), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_KNOWLEDGE_AUTHOR", "testeur")
    monkeypatch.delenv("CUCUMBER_TAG_A_REVOIR", raising=False)
    return CLIENT


def _scenarios(worktree):
    return {s["titre"]: s for s in cv.parse_scenarios(_text(worktree))}


def test_realignment_is_held_then_validated(client, repo):
    write(os.path.join(repo, REL), FEATURE)
    before = ar.snapshot(repo, [REL])
    write(os.path.join(repo, REL), FEATURE.replace(
        f'  {CLIENT_TAG}\n  Scenario: Statut du contenant', f'  # Réaligné sur le code : StockService.java\n'
                                                             f'  {CLIENT_TAG}\n  Scenario: Statut du contenant')
          .replace('Then le statut est "LIBRE"', 'Then le statut est "BLOQUE"'))
    held = cd.held_changes(repo, before, CLIENT_TAG)
    assert [(h["type"], h["titre"]) for h in held] == [("realignement", "Statut du contenant")]
    # En attente : bloc d'origine, désactivé provisoirement, modification gardée à part
    scenario = _scenarios(repo)["Statut du contenant"]
    assert "@A_REVOIR" in scenario["tags"] and CLIENT_TAG not in scenario["tags"]
    assert '"LIBRE"' in _text(repo) and cd.PENDING_COMMENT in _text(repo)
    decision = cd.propose(client, "realignement", held[0]["proposition"], fichier=REL, titre="Statut du contenant",
                          scenario=f"{REL}::Statut du contenant", lot=1, avant=held[0]["avant"],
                          apres=held[0]["apres"])
    assert cd.propose(client, "realignement", "x", scenario=f"{REL}::Statut du contenant")["id"] == decision["id"]
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    done = cd.resolve(client, decision["id"], "valider", "conforme au code", repo)
    assert done["statut"] == "validee" and done["historique"][-1]["auteur"] == "testeur"
    scenario = _scenarios(repo)["Statut du contenant"]
    assert "@A_REVOIR" not in scenario["tags"] and CLIENT_TAG in scenario["tags"]
    assert '"BLOQUE"' in _text(repo) and cd.PENDING_COMMENT not in _text(repo)
    assert "Décision validée" in git(repo, "log", "-1", "--format=%s")


def test_refused_change_restores_the_scenario_and_feeds_the_next_correction(client, tmp_path):
    worktree = str(tmp_path / "wt")
    write(os.path.join(worktree, REL), FEATURE)
    before = ar.snapshot(worktree, [REL])
    write(os.path.join(worktree, REL), FEATURE.replace("| C1   | 2   |", "| C1   | 5   |"))
    held = cd.held_changes(worktree, before, CLIENT_TAG)
    assert [h["type"] for h in held] == ["donnees"]
    decision = cd.propose(client, "donnees", held[0]["proposition"], fichier=REL, titre="Statut du contenant",
                          scenario="sc-1", avant=held[0]["avant"], apres=held[0]["apres"])
    cd.resolve(client, decision["id"], "commenter", "pourquoi 5 ?")
    assert cd.pending(client)[0]["historique"][-1]["action"] == "commentée"
    cd.resolve(client, decision["id"], "refuser", "la quantité 2 vient du ticket", worktree)
    assert _text(worktree) == FEATURE                          # scénario d'origine, actif
    assert cd.refused_comments(client, "sc-1") == ["la quantité 2 vient du ticket"]
    # Ajouter un step ou une colonne n'est pas un changement de sens
    before = ar.snapshot(worktree, [REL])
    write(os.path.join(worktree, REL), FEATURE.replace('    Given le contenant "C2"\n',
                                                       '    Given le contenant "C2"\n    And le site "S1"\n'))
    assert cd.held_changes(worktree, before, CLIENT_TAG) == []


def test_pending_deactivation_proven_comment_and_unused_setup(client, tmp_path):
    worktree = str(tmp_path / "wt")
    write(os.path.join(worktree, REL), FEATURE)
    item = {"statut": "environnement", "motif": "non vérifié — environnement : No route found for GET /x"}
    ar.deactivate(worktree, REL, {"Statut du contenant": ("environnement", item),
                                  "Motif de blocage": ("environnement", item)}, CLIENT_TAG, pending=True)
    text = _text(worktree)
    assert "# À revoir : environnement — cause PROUVÉE — décision en attente — non vérifié" in text
    assert "réactivation : réactiver quand l'environnement" in text
    # Plus aucun scénario actif : la mise en place qui ne sert qu'à eux est désactivée aussi
    assert "@A_REVOIR" in _scenarios(worktree)["Contexte du stock"]["tags"]
    assert ar.SETUP_LABEL in text
    state = {"lots": [{"numero": 1, "a_revoir": [{"fichier": REL, "titre": "Motif de blocage"}]}]}
    decision = cd.propose(client, "desactivation", "Motif de blocage : environnement", fichier=REL,
                          titre="Motif de blocage", scenario=f"{REL}::Motif de blocage")
    cd.sync_state(state, cd.resolve(client, decision["id"], "refuser", "l'environnement est réparé", worktree,
                                    client_tag=CLIENT_TAG))
    scenario = _scenarios(worktree)["Motif de blocage"]
    assert "@A_REVOIR" not in scenario["tags"] and CLIENT_TAG in scenario["tags"]
    assert state["lots"][0]["a_revoir"] == []
    other = cd.propose(client, "desactivation", "Statut : environnement", fichier=REL, titre="Statut du contenant",
                       scenario=f"{REL}::Statut du contenant")
    cd.resolve(client, other["id"], "valider", "", worktree)
    assert "cause PROUVÉE — non vérifié" in _text(worktree) and ar.PENDING not in _text(worktree).split(
        "Scenario: Statut du contenant")[0].split("Scenario: Contexte du stock")[1]
    assert cd.summary_lines(cd.load_decisions(client))[0].endswith("**refusée** par testeur le " +
                                                                   cd.load_decisions(client)[0]["historique"][-1]
                                                                   ["date"][:10] + " — « l'environnement est réparé »")


def _lot(numero, identifier, message, correction="colonne corrigée"):
    tag = f"@agent_{CLIENT}_1_0"
    return {"numero": numero, "type": "correction", "import": numero,
            "corrections": [{"tag": tag, "id": identifier, "message": message}],
            "correction": {"corrections": [{"tag": tag, "correction": f"{correction} {numero}"}]}}


def test_three_attempts_then_proof_methods(monkeypatch):
    identifier = f"{REL}::S0"
    state = {"lots": [{"numero": 1, "tags": {f"@agent_{CLIENT}_1_0": identifier},
                       "indicateurs": {"scenarios": [{"fichier": REL, "titre": "S0", "spec": "sp-1"}]}}]}
    failure = {identifier: {"statut": "echoue", "nature": "assertion", "step": 'Then le statut "X"',
                            "message": "expected:<LIBRE> but was:<BLOQUE> for C-0001", "heure": "2026-10-23T08:00:00"}}
    proposals, to_prove = cimp.correction_proposals(failure, state, {identifier: {"diagnostic": "écran reçu : A"}})
    assert proposals[0]["scenarios"][0]["diagnostic"] == "écran reçu : A" and to_prove == []
    # Même cause (valeurs variables masquées) après deux corrections : encore proposée, troisième essai annoncé
    state["lots"] += [_lot(2, identifier, "expected:<LIBRE> but was:<BLOQUE> for C-0002"),
                      _lot(3, identifier, "expected:<LIBRE> but was:<BLOQUE> for C-0003")]
    assert cimp.correction_proposals(failure, state)[0][0]["scenarios"][0]["tentatives"] == 2
    state["lots"].append(_lot(4, identifier, "expected:<LIBRE> but was:<BLOQUE> for C-0004"))
    proposals, to_prove = cimp.correction_proposals(failure, state, db_configured=True)
    assert proposals == [] and [t["lot"] for t in to_prove[0]["tentatives"]] == [2, 3, 4]
    assert to_prove[0]["tentatives"][0]["correction"] == "colonne corrigée 2"
    methods = " | ".join(to_prove[0]["methodes"])
    assert "lecture en base" in methods and "interface web" in methods and "UTC" in methods
    assert "lecture en base" not in " | ".join(cimp.correction_proposals(failure, state)[1][0]["methodes"])
    # Autre cause : compteur remis à zéro
    other = {identifier: {**failure[identifier], "message": "Colonne inconnue: statut.code"}}
    assert cimp.correction_proposals(other, state)[1] == []


def test_diagnostics_kept_for_next_run_then_removed(client, repo):
    steps = "src/test/java/client/StockSteps.java"
    write(os.path.join(repo, REL), FEATURE.replace('    Then le motif est "DECANTING"\n',
                                                   '    And TEMPORAIRE- affiche le contenant\n'
                                                   '    Then le motif est "DECANTING"\n'))
    write(os.path.join(repo, steps), 'class StockSteps {\n  @Then("TEMPORAIRE- affiche le contenant")\n'
                                     '  public void diag() { scenario.log("x"); }\n}\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    report = [{"uri": f"classpath:cucumber/features/{CLIENT}/Stock.feature", "elements": [
        {"type": "scenario", "name": "Motif de blocage", "steps": [
            {"keyword": "And ", "name": "TEMPORAIRE- affiche le contenant", "output": ["écran : SAISIE"],
             "result": {"status": "passed"}},
            {"keyword": "Then ", "name": 'le motif est "DECANTING"', "result": {"status": "failed",
                                                                                "error_message": "expected"}}]}]}]
    outputs = cimp.diagnostic_outputs(report)
    assert outputs == {f"{REL}::Motif de blocage": "écran : SAISIE"}
    state = {"base": "", "lots": [{"numero": 1, "diagnostics_en_attente": [f"{REL}:13"]}]}
    removed = cb.remove_run_diagnostics(client, {"numero": 2, "diagnostics": outputs}, state, repo)
    assert sorted(removed) == sorted([REL, steps]) and "TEMPORAIRE-" not in _text(repo)
    assert "Retrait des diagnostics temporaires" in git(repo, "log", "-1", "--format=%s")
    assert "diagnostics_en_attente" not in ct.load_state(client)["lots"][0]


def test_correction_step_lists_pending_decisions(client):
    cd.propose(client, "tautologique", "sp-1 : scénario qui ne vérifie que sa mise en place", spec="sp-1")
    step = cj.correction_step(client, [], {"a_prouver": [{"id": "x"}]},
                              {"lots": [{"diagnostics_en_attente": ["a:1"]}]})
    assert step["resume"] == ("1 décision(s) en attente, 1 cause(s) à prouver, 1 diagnostic(s) en attente du "
                              "prochain run")
    assert step["action"] == "Trancher les décisions en attente"
    bilan = {"manuelles": [], "decisions_en_attente": 1}
    assert cb.next_action({"propositions": [1]}, bilan).startswith("Trancher les 1 décision(s)")
