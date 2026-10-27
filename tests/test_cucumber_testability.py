"""
Testabilité (client fictif) : donnée retrouvée dans l'init du projet ou un initializer → testable ; raison sans
sources citées rejetée ; « non testable » exige un blocage prouvé ; réponse de l'utilisateur réutilisée sans
nouvelle question ; règle du témoin positif ; rétro-analyse ; rendu dans la vue Couverture.
"""
import os

import pytest

from tests.conftest import CLIENT, CLIENT_TAG, write
from tools import cucumber_coverage as cov
from tools import cucumber_profile as cp
from tools import cucumber_testability as cte
from tools import cucumber_tools as ct
from tools import cucumber_verify as cv

RES = "src/test/resources/cucumber"
INIT_REL = f"{RES}/features/{CLIENT}/01-Contexte/01-Initialisation.feature"
# Valeurs fictives, propres aux tests
DOC_KEYS = ("DOC_FICTIF_A", "DOC_FICTIF_B")
ALL_SOURCES = [{"source": k, "cherche": "DOC_FICTIF_Z", "trouve": "rien"} for k, _ in cte.SOURCES]


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path / "ws"))


@pytest.fixture
def worktree(tmp_path):
    """Dépôt client : init du projet qui recrée un document, feature existante."""
    root = str(tmp_path / "wt")
    write(os.path.join(root, *INIT_REL.split("/")),
          f"@INIT_{CLIENT.upper()} {CLIENT_TAG}\nFeature: Init\n  Scenario: S\n"
          f"    Given REPORT- Création des documents\n      | code |\n      | {DOC_KEYS[0]} |\n")
    write(os.path.join(root, RES, "features", CLIENT, "REPORT", "Docs.feature"),
          f"{CLIENT_TAG}\nFeature: Docs\n  Scenario: S\n    Then contrôle du document \"{DOC_KEYS[1]}\"\n")
    return root


def missing_docs(**extra) -> dict:
    """Résultat de lot « non testable » : clés de documents dites non vérifiées, fixtures non disponibles."""
    return {"id": "sp-1", "statut": "non_couvert",
            "raison": f"Documents {DOC_KEYS[0]} et {DOC_KEYS[1]} produits par d'autres processus, fixtures non "
                      f"disponibles, clés de document non vérifiées.", **extra}


def test_data_only_in_project_init_makes_task_testable(cache, worktree):
    profile = {**cp.empty_profile(), "init_features": [INIT_REL]}
    index = cte.known_values(CLIENT, profile, {}, worktree)
    assert index[DOC_KEYS[0]][0]["source"] == "init" and index[DOC_KEYS[0]][0]["ou"].startswith(INIT_REL)
    assert index[DOC_KEYS[1]][0]["source"] == "depot"
    evaluation = cte.assess(missing_docs(recherches=ALL_SOURCES[:1]), index, profile)
    assert evaluation["etat"] == "testable" and set(evaluation["trouvees"]) == set(DOC_KEYS)
    assert any("trouvée(s)" in r for r in evaluation["rejets"])
    # Raison au format ancien (texte libre) : à confirmer, valeurs trouvées jointes
    legacy = cte.assess(missing_docs(), index, profile)
    assert legacy["etat"] == "a_confirmer" and set(legacy["trouvees"]) == set(DOC_KEYS) and legacy["questions"]


def test_data_only_in_initializer_makes_task_testable(cache):
    inventory = {"initialiseurs": [{"depot": "app", "chemin": "x/DocInit.java", "classe": "DocInit",
                                    "creations": [{"entite": "RefReportDoc", "code": DOC_KEYS[0], "expression": "C"},
                                                  {"entite": "RefReportDoc", "code": DOC_KEYS[1], "expression": "D"}]}]}
    index = cte.known_values(CLIENT, cp.empty_profile(), inventory)
    assert index[DOC_KEYS[0]] == [{"source": "initializers", "ou": "app/x/DocInit.java"}]
    assert cte.assess(missing_docs(resume="Clés des documents à vérifier."), index)["etat"] == "testable"


def test_initializer_and_configuration_values_extracted(cache, tmp_path):
    config = str(tmp_path / "copies" / "SpecDocConfig.java")
    write(config, "static final String KEY = \"cle-doc\";\nboolean enabled = true;\n// \"commentaire\"\n")
    inventory = {"specifiques": [{"id": "sp-1", "fichiers": [{"repo": "app", "path": "a/SpecDocConfig.java",
                                                              "local": config}]}]}
    index = cte.known_values(CLIENT, cp.empty_profile(), inventory)
    assert index["cle-doc"][0]["source"] == "initializers" and "enabled=true" in index
    assert "commentaire" not in index


def test_reason_without_sources_is_rejected(cache):
    result = missing_docs(blocage={"type": "donnee_impossible", "preuve": "aucun endpoint de création"},
                          recherches=ALL_SOURCES[:2])
    evaluation = cte.assess(result, {})
    assert evaluation["etat"] == "a_confirmer" and evaluation["questions"]
    assert any(r.startswith("sources non citées") and "Cucumber Standard aligné" in r for r in evaluation["rejets"])


def test_not_testable_requires_proven_blocker(cache):
    # Absence d'information : jamais un blocage
    assert cte.assess(missing_docs(recherches=ALL_SOURCES), {})["etat"] == "a_confirmer"
    # Type de blocage sans preuve : rejeté
    no_proof = missing_docs(blocage={"type": "non_deploye", "preuve": ""}, recherches=ALL_SOURCES)
    assert cte.assess(no_proof, {})["etat"] == "a_confirmer"
    # Blocage prouvé, 6 sources citées, aucune valeur dite manquante connue : non testable
    proven = missing_docs(blocage={"type": "non_deploye", "preuve": "run n° 4 : échec « déploiement »"},
                          recherches=ALL_SOURCES, resume="Le processus n'est pas déployé.")
    evaluation = cte.assess(proven, {})
    assert evaluation["etat"] == "non_testable" and evaluation["resume"] == "Le processus n'est pas déployé."
    assert cte.blocker_line(evaluation) == "code ou personnalisation non déployé — preuve : run n° 4 : échec « déploiement »"
    # Même blocage, mais une valeur dite manquante est connue : rejeté
    assert cte.assess(proven, {DOC_KEYS[0]: [{"source": "init", "ou": "x"}]})["etat"] == "testable"


def test_without_own_behaviour_requires_proof(cache):
    assert cte.assess({"statut": "non_couvert", "raison": "DTO", "sans_objet": {"preuve": "A.java:3 class A {}"}},
                      {})["etat"] == "sans_objet"
    assert cte.assess({"statut": "non_couvert", "raison": "DTO", "sans_objet": {"preuve": ""}}, {})["etat"] == \
        "a_confirmer"
    assert cte.assess({"statut": "non_couvert", "raison": ct.TECHNICAL_REASON}, {})["etat"] == "sans_objet"


def test_user_answer_reused_without_new_question(cache):
    question = f"Quelle est la clé du document {DOC_KEYS[0]} ?"
    result = missing_docs(questions=[question, "Quel paramétrage d'impression utiliser ?"])
    first = cte.assess(result, {})
    assert first["etat"] == "a_confirmer" and question in first["questions"]
    cte.save_answer(CLIENT, question, f"{DOC_KEYS[0]}-KEY", "sp-1")
    cte.save_answer(CLIENT, question, f"{DOC_KEYS[0]}-KEY2", "sp-1")
    profile = cp.effective_profile(cp.load_profile(CLIENT))
    assert len(profile["reponses"]) == 1 and profile["reponses"][0]["reponse"] == f"{DOC_KEYS[0]}-KEY2"
    # Même question (casse, ponctuation) pour une autre tâche : jamais reposée
    other = {**result, "id": "sp-2", "questions": [question.upper().rstrip(" ?") + " ?"]}
    assert cte.question_key(other["questions"][0]) == cte.question_key(question)
    reused = cte.assess(other, {}, profile)
    assert reused["questions"] == [] and reused["etat"] == "testable"
    assert question not in cte.assess(result, {}, profile)["questions"]
    block = cte.answers_block(profile)
    assert "ne repose jamais ces questions" in block and f"→ {DOC_KEYS[0]}-KEY2" in block
    # La réponse rejoint l'index des valeurs connues (source profil)
    assert cte.known_values(CLIENT, profile, {})[f"{DOC_KEYS[0]}-KEY2"][0]["source"] == "profil"


NEGATIVE = (f"{CLIENT_TAG}\nFeature: Rendu\n"
            "  Scenario: Document exclu non rendu\n"
            "    Given un document \"DOC_FICTIF_A\"\n"
            "    When je demande le rendu du document \"DOC_FICTIF_A\"\n"
            "    Then aucune instance du document n'est générée\n")
WITNESS = ("  Scenario: Document non standard rendu\n"
           "    Given un document \"DOC_FICTIF_C\"\n"
           "    When je demande le rendu du document \"DOC_FICTIF_C\"\n"
           "    Then \"1\" instance du document est générée\n")


def test_negative_scenario_without_witness_is_lot_error(tmp_path):
    root = str(tmp_path / "wt")
    rel = f"{RES}/features/{CLIENT}/Rendu.feature"
    write(os.path.join(root, *rel.split("/")), NEGATIVE)
    errors = cv.check_positive_witness(root, [rel])
    assert len(errors) == 1 and errors[0].startswith(f"{rel}:3 scénario négatif « Document exclu non rendu »")
    # Témoin dans les mêmes conditions (même step d'action, autre valeur) : plus d'erreur
    write(os.path.join(root, *rel.split("/")), NEGATIVE + WITNESS)
    assert cv.check_positive_witness(root, [rel]) == []
    # Un « témoin » sur une autre action ne compte pas
    other = WITNESS.replace("je demande le rendu du document", "j'imprime l'étiquette")
    write(os.path.join(root, *rel.split("/")), NEGATIVE + other)
    assert len(cv.check_positive_witness(root, [rel])) == 1


def test_retro_analysis_reclassifies_and_counts(cache, worktree):
    profile = {**cp.empty_profile(), "init_features": [INIT_REL]}
    index = cte.known_values(CLIENT, profile, {}, worktree)
    state = {"lots": [{"numero": 1, "ids": ["sp-1", "sp-2", "sp-3", "sp-4"], "resultats": [
        missing_docs(),
        {"id": "sp-2", "statut": "non_couvert", "raison": "Aucun step pour cet écran."},
        {"id": "sp-3", "statut": "non_couvert", "raison": ct.TECHNICAL_REASON},
        {"id": "sp-4", "statut": "couvert"}]}]}
    document = cte.retro(CLIENT, state, index, profile)
    assert document["taches"] == {"sp-1": "a_confirmer", "sp-2": "a_confirmer", "sp-3": "sans_objet"}
    assert document["reclassees"] == 3
    assert cte.retro_line(document) == ("3 tâche(s) reclassée(s) : 0 testable(s), 2 à confirmer, 1 sans objet ; "
                                        "0 reste(nt) non testable(s) (blocage prouvé)")


def test_coverage_rows_use_assessment_and_warning_shows_proof(cache, monkeypatch):
    monkeypatch.setattr(cov, "import_results", lambda client: {})
    inventory = {"specifiques": [{"id": i, "libelle": i, "type": "processus", "nature": "", "depot": "d",
                                  "fichiers": [{"path": "a/A.java", "kind": "extension"}]}
                                 for i in ("sp-1", "sp-2", "sp-3")]}
    cte.save_index(CLIENT, {DOC_KEYS[0]: [{"source": "init", "ou": INIT_REL}]})
    state = {"lots": [{"numero": 1, "ids": ["sp-1", "sp-2", "sp-3"], "resultats": [
        missing_docs(recherches=ALL_SOURCES),
        {**missing_docs(), "id": "sp-2", "raison": "Processus absent.", "recherches": ALL_SOURCES,
         "blocage": {"type": "non_deploye", "preuve": "run n° 2 : processus introuvable"}},
        {"id": "sp-3", "statut": "non_couvert", "raison": "DTO.", "sans_objet": {"preuve": "A.java:1"}}]}]}
    rows = {r["id"]: r for r in cov.coverage_rows(CLIENT, inventory, state)}
    assert rows["sp-1"]["etat"] == "non_couvert" and rows["sp-1"]["testabilite"] == "testable"
    assert rows["sp-2"]["etat"] == "non_testable" and rows["sp-2"]["testabilite"] == "non testable"
    assert rows["sp-3"]["etat"] == "sans_objet"
    warnings = cov.selection_warnings(list(rows.values()), ["sp-1", "sp-2"])
    assert warnings == ["sp-2 : non testable aujourd'hui — code ou personnalisation non déployé — preuve : run n° 2 : "
                        "processus introuvable"]


def test_initializer_data_found_in_init_is_usable_in_lot_prompt():
    data = [{"code": DOC_KEYS[0], "entite": "RefReportDoc", "classe": "DocInit", "statut": "inconnu"}]
    assert "ne t'y fie pas" in cp.initializers_block(data, "")
    block = cp.initializers_block(data, "", present_codes={DOC_KEYS[0]})
    assert "ne t'y fie pas" not in block and "recréées par l'init du projet), utilisables" in block
