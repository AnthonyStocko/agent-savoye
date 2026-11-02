"""
Build 241 (après corrections) : 19 échecs contre 17 au build 226. Garde-fou de non-régression des corrections,
contrôle des fichiers lus sur toutes les features de la suite (cas exact SPEC-1239 : règle -DEFAUT absente),
données rejouables d'une exécution à l'autre (SPEC-1594 : « Client existe déjà »), données créées avant d'être
utilisées (TOW-SPEC-1047 : UE jamais créée), désactivation prouvée jamais levée sans preuve (SPEC-1239 réactivé).
"""
import os

from tests.conftest import CLIENT, CLIENT_TAG, git, write
from tools import cucumber_checks as cch
from tools import cucumber_data_rules as cdr
from tools import cucumber_disabled as cdis
from tools import cucumber_failures as cf
from tools import cucumber_guard as cg

RES = "src/test/resources/cucumber"
CONTENT = f"{RES}/features/{CLIENT}/WMS/Content"
PREP = f"{RES}/features/{CLIENT}/WMS/Preparation"
OUTSIDE = f"{RES}/features/WMS/03-Preparation/18-Gestion_douaniere"
INVENTORY = f"{CONTENT}/SPEC-1239-Inventaire_miniload_marqueur_OL.feature"
QUALITY = f"{PREP}/SPEC-1594-Controle_qualite_UP_non_preparee.feature"
CUSTOMS = f"{OUTSIDE}/TOW-SPEC-1047.feature"

SPEC_1239 = f"""@WMS @SPEC-1239
Feature: Inventaire - SPEC-1239 - Marqueur sur l'OL de mouvement d'inventaire

  {CLIENT_TAG}
  Scenario: Contexte
    Given INIT- Intégration des règles
      | fileName                                  |
      | TOW-1033-rule-MoveStocktakingHandlingUnit |

  # ENVIRONNEMENT : code SPEC-1239 (content-backend 5d080a21d, 2026-02-17) non déployé sur l'environnement Cucumber
  @A_REVOIR_ENV {CLIENT_TAG}
  Scenario: Création d'un inventaire à l'UM sur le Miniload
    Then INV- Contrôle du marqueur

  # ENVIRONNEMENT : remise à DEFAUT de la règle intégrée par le Contexte désactivé (code SPEC-1239 non déployé)
  @A_REVOIR_ENV {CLIENT_TAG}
  Scenario: Réinitialisation de la règle MoveStocktakingHandlingUnit
    Given INIT- Intégration des règles
      | fileName                                          |
      | SPEC-1239-rule-MoveStocktakingHandlingUnit-DEFAUT |
"""

SPEC_1594 = f"""@WMS @SPEC-1594
Feature: Préparation : SPEC-1594 - Contrôle qualité refusé

  Background:
    Given INIT- Intégration des fichiers GS1
      | fileName        |
      | WOI-TOW-554.xml |
    And INIT- Intégration des fichiers via l'écran
      | fileName                       |
      | SPEC-1594-RefCustomerDraft.csv |

  {CLIENT_TAG}
  Scenario: Contexte : OP TOW-554-1 en préparation
    Then PREP- Contrôle des ordres de préparation
      | code      |
      | TOW-554-1 |

  {CLIENT_TAG}
  Scenario: Contrôle qualité refusé car des lignes de l'UP ne sont pas préparées
    Then PREP- Contrôle des ordres de préparation
      | code      |
      | TOW-554-1 |
"""

SPEC_1047 = f"""@WMS @PREPARATION {CLIENT_TAG} @SPEC-1047
Feature: Préparation : 18 - Gestion douanière - SPEC-1047 - Demander la packing list

  Scenario: Création du contexte
    Given PREP- Initialisation de l’unité d’expédition répondant aux caractéristiques
      | code           |
      | UE-SPEC-1047-1 |

  Scenario: Refus pour une UE inconnue
    When APPAREIL- Demande de packing list pour les unités d'expédition
      | code                 |
      | INVALID-UE-SPEC-1047 |
"""

CUSTOMERS = ("ref-organization-event-save-customer-request;;;\ncode;name\n"
             "CUST-TOW-554-1;CUST-TOW-554-1\nCUST-TOW-554-2;CUST-TOW-554-2\n")
WOI = "<orders><order><code>TOW-554-1</code><customer>CUST-TOW-554-1</customer></order></orders>\n"


def _repo(path, files):
    os.makedirs(path)
    git(path, "init", "-q", "-b", "integration")
    git(path, "config", "user.email", "t@e.x")
    git(path, "config", "user.name", "t")
    for rel, text in files.items():
        write(os.path.join(path, *rel.split("/")), text)
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    return path


def _suite(tmp_path):
    return _repo(str(tmp_path / "wt"), {
        INVENTORY: SPEC_1239, QUALITY: SPEC_1594, CUSTOMS: SPEC_1047,
        # Règle -DEFAUT livrée dans le dossier de « Réinitialisation des règles » au lieu de celui de la feature
        f"{RES}/rules/WMS/01-Contexte/SPEC-1239-rule-MoveStocktakingHandlingUnit-DEFAUT.json": "{}\n",
        f"{RES}/rules/{CLIENT}/WMS/Content/TOW-1033-rule-MoveStocktakingHandlingUnit.json": "{}\n",
        f"{RES}/files/{CLIENT}/WMS/Preparation/SPEC-1594-RefCustomerDraft.csv": CUSTOMERS,
        f"{RES}/files/{CLIENT}/WMS/Preparation/WOI-TOW-554.xml": WOI})


# ---------------------------------------------------------------------------
# 2. Fichiers lus : cas exact SPEC-1239, toutes les features de la suite
# ---------------------------------------------------------------------------

def test_rule_file_of_spec_1239_is_missing_where_the_step_reads_it(tmp_path):
    worktree = _suite(tmp_path)
    errors = cch.check_data_files(worktree, [INVENTORY], "", copy=False)["erreurs"]
    assert len(errors) == 1
    assert (f"{RES}/rules/{CLIENT}/WMS/Content/SPEC-1239-rule-MoveStocktakingHandlingUnit-DEFAUT.json"
            in errors[0]) and errors[0].startswith(f"{INVENTORY}:")


def test_every_feature_run_by_the_suite_is_checked(tmp_path):
    worktree = _suite(tmp_path)
    features = cch.suite_features(worktree, CLIENT_TAG, f"{RES}/features/{CLIENT}")
    # Rangée hors de la racine du client mais exécutée par le tag client de sa ligne Feature
    assert CUSTOMS in features and INVENTORY in features and QUALITY in features


def test_file_reading_steps_are_all_covered():
    text = ("Feature: x\n  Scenario: x\n    Given INIT- Initialisation globale des règles\n"
            "    Then PRODUCT- Export \"CSV\" dans le fichier \"export.csv\" de la liste des produits\n"
            "    Then MESS- Comparaison du contenu du message envoyé avec le fichier \"attendu.xml\"\n")
    refs = cch.data_references(text)
    assert [r["nom"] for r in refs] == [cch.WHOLE_DIR, "attendu.xml"]   # l'export écrit, il ne lit pas
    rel = f"{CONTENT}/X.feature"
    assert not cch.is_present(cch.resolved_path(rel, refs[0]), {f"{RES}/rules/{CLIENT}/WMS/Product/a.json"})
    assert cch.is_present(cch.resolved_path(rel, refs[0]), {f"{RES}/rules/{CLIENT}/WMS/Content/a.json"})


# ---------------------------------------------------------------------------
# 3. Données rejouables d'une exécution à l'autre : SPEC-1594
# ---------------------------------------------------------------------------

def test_fixed_customer_created_in_background_is_a_lot_error(tmp_path):
    worktree = _suite(tmp_path)
    errors = cdr.check_idempotent_creations(worktree, [QUALITY])
    assert len(errors) == 1 and errors[0].startswith(f"{QUALITY}:8 ")
    assert "CUST-TOW-554-1" in errors[0] and "Background" in errors[0] and "2e exécution" in errors[0]


def test_deletion_before_creation_makes_it_replayable(tmp_path):
    fixed = SPEC_1594.replace("    And INIT- Intégration des fichiers via l'écran",
                              "    And ORGA- Suppression des clients\n      | code           |\n"
                              "      | CUST-TOW-554-1 |\n      | CUST-TOW-554-2 |\n"
                              "    And INIT- Intégration des fichiers via l'écran")
    worktree = _suite(tmp_path)
    write(os.path.join(worktree, *QUALITY.split("/")), fixed)
    assert cdr.check_idempotent_creations(worktree, [QUALITY]) == []


def test_upsert_drafts_are_not_flagged(tmp_path):
    worktree = _suite(tmp_path)
    write(os.path.join(worktree, RES, "files", CLIENT, "WMS", "Preparation", "SPEC-1594-RefCustomerDraft.csv"),
          "ref-product-event-save-logisticunit-request;;\ncode;name\nP1-TOW-554;P1\n")
    assert cdr.check_idempotent_creations(worktree, [QUALITY]) == []


def test_duplicate_refusals_are_learned_from_the_console(tmp_path, monkeypatch):
    from tools import cucumber_tools as ct
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(cdr, "cache_dir", lambda name: str(tmp_path / "cache" / name))
    worktree = _suite(tmp_path)
    write(os.path.join(worktree, RES, "files", CLIENT, "WMS", "Preparation", "WmsMeansDraft.csv"),
          "wms-cartography-event-save-means-request;;\ncode\nM-TOW-1\n")
    console = ("1 // 1  -  WmsMeansDraft.csv / INTEGRATED_WITH_ERROR [Moyen existe déjà avec code M-TOW-1 ]\n"
               "java.lang.AssertionError: Le support existe déjà\n"
               "\tat ✽.STOCK- Création des supports(file:///w/x.feature:5)\n")
    learned = cdr.learn_from_console(CLIENT, console, worktree)
    assert learned == {"steps": ["STOCK- Création des supports"],
                       "evenements": ["wms-cartography-event-save-means-request"]}
    assert "wms-cartography-event-save-means-request" in cdr.not_idempotent(CLIENT)["evenements"]
    assert cdr.learn_from_console(CLIENT, console, worktree) == {"steps": [], "evenements": []}


# ---------------------------------------------------------------------------
# 4. Données créées avant d'être utilisées : TOW-SPEC-1047
# ---------------------------------------------------------------------------

def test_object_used_but_never_created_is_a_lot_error(tmp_path):
    worktree = _suite(tmp_path)
    errors = cdr.check_created_before_use(worktree, [CUSTOMS, QUALITY])
    # UE-SPEC-1047-1 jamais créée ; le code volontairement invalide et l'OP créée par le WOI ne sont pas signalés
    assert len(errors) == 1 and errors[0].startswith(f"{CUSTOMS}:5 « UE-SPEC-1047-1 »")


def test_object_created_by_the_client_init_is_accepted(tmp_path):
    worktree = _suite(tmp_path)
    init_rel = f"{RES}/features/{CLIENT}/00-Init/Init.feature"
    write(os.path.join(worktree, *init_rel.split("/")),
          "Feature: init\n  Scenario: init\n    Given PREP- Création des UE\n      | code |\n"
          "      | UE-SPEC-1047-1 |\n")
    assert cdr.check_created_before_use(worktree, [CUSTOMS], {"features": [{"chemin": init_rel}]}) == []


def test_static_checks_classify_the_build_241_failures(tmp_path):
    worktree = _suite(tmp_path)
    checks = cdr.feature_checks(worktree, [INVENTORY, QUALITY, CUSTOMS])
    assert set(checks) == {INVENTORY, QUALITY, CUSTOMS}
    duplicate = cf.classify({"message": "java.lang.AssertionError: [Client existe déjà avec code CUST-TOW-554-1 ]",
                             "step": "INIT- Intégration des fichiers via l'écran",
                             "contexte": {"controles": checks[QUALITY]}})
    assert (duplicate["categorie"], duplicate["nature"]) == ("correction", "donnee_non_idempotente")
    assert "Background" in duplicate["cause"]
    never = cf.classify({"message": "java.lang.IndexOutOfBoundsException: Index 0 out of bounds for length 0",
                         "contexte": {"controles": checks[CUSTOMS]}})
    assert (never["nature"], "UE-SPEC-1047-1" in never["cause"]) == ("donnee_non_creee", True)
    missing = cf.classify({"message": f"java.io.FileNotFoundException: ./{RES}/rules/{CLIENT}/WMS/Content/"
                                      f"SPEC-1239-rule-MoveStocktakingHandlingUnit-DEFAUT.json (No such file)",
                           "contexte": {"controles": checks[INVENTORY]}})
    assert missing["nature"] == "chemin_fichier" and "SPEC-1239-rule-MoveStocktakingHandlingUnit-DEFAUT" in \
        missing["cause"]


# ---------------------------------------------------------------------------
# 5. Désactivation prouvée : jamais levée sans preuve que la cause a disparu
# ---------------------------------------------------------------------------

DEPLOYED = {"taches": {"sp-1": {"etat": "deploye", "commit": {"sujet": "SPEC-1239 marqueur miniload"},
                                "preuve": "commit abc (2026-02-17) antérieur à la publication de app 1.20"}}}


def test_reactivated_spec_1239_stays_disabled_without_proof():
    text, actions = cdis.enforce(SPEC_1239, CLIENT_TAG, {"taches": {}})
    assert [a["action"] for a in actions] == ["maintenu", "maintenu"]
    assert f"@A_REVOIR_ENV {CLIENT_TAG}" not in text and text.count("@A_REVOIR_ENV @A_REVOIR") == 2
    # Règle rétroactive du tag client : un scénario désactivé par une variante ne le reçoit plus
    assert cch.add_client_tag(text, CLIENT_TAG)[1] == 0
    assert cch.check_client_tags({"x.feature": text}, CLIENT_TAG) == []
    assert len(cch.check_client_tags({"x.feature": SPEC_1239}, CLIENT_TAG)) == 2


def test_reactivation_with_deployment_proof():
    text, actions = cdis.enforce(SPEC_1239, CLIENT_TAG, DEPLOYED)
    assert [a["action"] for a in actions] == ["reactive", "reactive"]
    assert "@A_REVOIR" not in text and text.count(cdis.REACTIVATED) == 2


def test_removing_the_disabled_tag_without_proof_is_undone():
    before = SPEC_1239.replace(f"@A_REVOIR_ENV {CLIENT_TAG}", "@A_REVOIR_ENV")
    after = SPEC_1239.replace(f"@A_REVOIR_ENV {CLIENT_TAG}", CLIENT_TAG)
    lost = cdis.reactivated_without_proof(before, after, CLIENT_TAG, {"taches": {}})
    assert [i["titre"] for i in lost] == ["Création d'un inventaire à l'UM sur le Miniload",
                                          "Réinitialisation de la règle MoveStocktakingHandlingUnit"]
    restored = cdis.restore_disabled(after, lost, CLIENT_TAG)
    assert restored.count("@A_REVOIR_ENV @A_REVOIR") == 2 and restored.count(CLIENT_TAG) == 1
    assert cdis.reactivated_without_proof(before, after, CLIENT_TAG, DEPLOYED) == []


# ---------------------------------------------------------------------------
# 1. Garde-fou de non-régression
# ---------------------------------------------------------------------------

def _analysis(numero, failing, full=True):
    results = {f"f.feature::{i}": {"statut": "echoue" if i in failing else "passe"} for i in "ABCDEFGH"}
    return {"numero": numero, "run_complet": full, "ecart_console": "", "resultats": results}


def _corrected(tmp_path):
    worktree = _repo(str(tmp_path / "g"), {"a.feature": "Feature: a\n"})
    start = git(worktree, "rev-parse", "HEAD")
    write(os.path.join(worktree, "a.feature"), "Feature: a corrigée\n")
    write(os.path.join(worktree, "b.feature"), "Feature: b\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "lot correction")
    return worktree, start


def test_guard_delivers_when_both_full_runs_improve_without_new_failure(tmp_path):
    worktree, start = _corrected(tmp_path)
    runs = iter([_analysis(11, "AB"), _analysis(12, "A")])
    state = {}
    result = cg.guard(state, _analysis(10, "ABC"), worktree, start, lambda: next(runs), str(tmp_path), True,
                      log=lambda m: None)
    assert result["ok"] and [r["apres"] for r in result["runs"]] == [2, 1] and result["annules"] == []
    assert os.path.exists(os.path.join(worktree, "b.feature")) and state["garde_fou"][0]["ok"]


def test_guard_reverts_on_a_new_failure_even_with_fewer_failures(tmp_path):
    worktree, start = _corrected(tmp_path)
    state = {}
    result = cg.guard(state, _analysis(10, "ABC"), worktree, start, lambda: _analysis(11, "AD"), str(tmp_path),
                      True, log=lambda m: None)
    assert not result["ok"] and "nouvel(s) échec(s)" in result["motif"] and len(result["runs"]) == 1
    assert result["annules"] == ["a.feature", "b.feature"]
    assert not os.path.exists(os.path.join(worktree, "b.feature"))
    assert open(os.path.join(worktree, "a.feature"), encoding="utf-8").read() == "Feature: a\n"
    assert "annulées" in git(worktree, "log", "-1", "--format=%s")
    assert "corrections annulées" in open(os.path.join(str(tmp_path), cg.REPORT), encoding="utf-8").read()


def test_guard_second_run_catches_data_that_does_not_replay(tmp_path):
    worktree, start = _corrected(tmp_path)
    runs = iter([_analysis(11, "A"), _analysis(12, "ABC")])
    result = cg.guard({}, _analysis(10, "ABC"), worktree, start, lambda: next(runs), "", True, log=lambda m: None)
    assert not result["ok"] and result["motif"].startswith("run 2/2 : pas d'amélioration")


def test_guard_reverts_without_a_full_run_launched_by_the_agent(tmp_path):
    worktree, start = _corrected(tmp_path)
    result = cg.guard({}, _analysis(10, "ABC"), worktree, start, lambda: None, "", False, log=lambda m: None)
    assert not result["ok"] and "CUCUMBER_AGENT_JENKINS" in result["motif"] and result["runs"] == []
    partial = cg.verdict(_analysis(10, "ABC"), _analysis(11, "A", full=False))
    assert not partial["ok"] and "run complet" in partial["motif"]
