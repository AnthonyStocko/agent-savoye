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


def test_data_in_project_init_becomes_proposed_answer(cache, worktree):
    profile = {**cp.empty_profile(), "init_features": [INIT_REL]}
    index = cte.known_values(CLIENT, profile, {}, worktree)
    assert index[DOC_KEYS[0]][0]["source"] == "init" and index[DOC_KEYS[0]][0]["ou"].startswith(INIT_REL)
    assert index[DOC_KEYS[1]][0]["source"] == "depot"
    # Toutes les valeurs dites manquantes sont trouvées : directement testable, réponses proposées avec leur source
    evaluation = cte.assess(missing_docs(), index, profile, "Rendu d'un document")
    assert evaluation["etat"] == "testable" and evaluation["attendues"] == 0 and evaluation["questions"] == []
    proposed = {v: p for p in evaluation["proposees"] for v in p["valeurs"]}
    assert set(proposed) == set(DOC_KEYS)
    assert proposed[DOC_KEYS[0]]["source"].startswith(f"init du projet ({INIT_REL}:")
    # Une valeur introuvable : une seule question précise, avec la donnée, le scénario et les candidats proches
    result = missing_docs(raison=f"Documents {DOC_KEYS[0]} et DOC_FICTIF_Z exclus du rendu, clés non vérifiées.")
    evaluation = cte.assess(result, index, profile, "Rendu d'un document")
    assert evaluation["etat"] == "a_confirmer" and evaluation["attendues"] == 1
    assert [v for p in evaluation["proposees"] for v in p["valeurs"]] == [DOC_KEYS[0]]
    question = evaluation["questions"][0]
    assert question["texte"].startswith("Pour tester « Rendu d'un document » : DOC_FICTIF_Z est cité mais "
                                        "introuvable dans le profil, l'init")
    assert set(question["candidats"]) == set(DOC_KEYS)


def test_data_in_initializer_becomes_proposed_answer_grouped_by_entity(cache):
    inventory = {"initialiseurs": [{"depot": "app", "chemin": "x/DocInit.java", "classe": "DocInit",
                                    "creations": [{"entite": "RefReportDoc", "code": DOC_KEYS[0], "expression": "C"},
                                                  {"entite": "RefReportDoc", "code": DOC_KEYS[1], "expression": "D"}]}]}
    index = cte.known_values(CLIENT, cp.empty_profile(), inventory)
    assert index[DOC_KEYS[0]] == [{"source": "initializers", "ou": "app/x/DocInit.java", "entite": "RefReportDoc"}]
    evaluation = cte.assess(missing_docs(), index)
    assert evaluation["etat"] == "testable"
    assert evaluation["proposees"] == [{"cle": cte.question_key("proposition " + " ".join(sorted(DOC_KEYS))),
                                        "libelle": "RefReportDoc présent(s) sur l'environnement",
                                        "valeurs": list(DOC_KEYS), "source": "initializer (app/x/DocInit.java)",
                                        "reponse": None}]


def test_no_generic_question_nor_copy_of_the_reason(cache):
    reason = "Les documents sont produits par d'autres processus, ce qui demande des fixtures non disponibles."
    generic = ["Quelle donnée, clé ou valeur faut-il pour tester cette tâche ?",
               "Les documents sont produits par d'autres processus, ce qui demande des fixtures non disponibles ?"]
    evaluation = cte.assess({"statut": "non_couvert", "raison": reason, "questions": generic}, {})
    assert evaluation["questions"] == [] and evaluation["etat"] == "testable"
    # Raison sans aucune valeur identifiable : rien de précis à demander, jamais de question générique
    assert cte.assess({"statut": "non_couvert", "raison": reason}, {})["questions"] == []
    # Références de tickets ou de scénarios : pas des données
    assert cte.assess({"statut": "non_couvert", "raison": "Voir CLE-2021 et TOW-1037-4."}, {})["questions"] == []
    precise = "Quel est le code du paramétrage d'impression du document DOC_FICTIF_A pour le scénario de rendu ?"
    kept = cte.assess({"statut": "non_couvert", "raison": reason, "questions": [precise]}, {})
    assert [q["texte"] for q in kept["questions"]] == [precise] and kept["etat"] == "a_confirmer"


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
    # Blocage prouvé, 6 sources citées, aucune valeur dite manquante dans le projet : non testable
    proven = missing_docs(blocage={"type": "non_deploye", "preuve": "run n° 4 : échec « déploiement »"},
                          recherches=ALL_SOURCES, resume="Le processus n'est pas déployé.")
    evaluation = cte.assess(proven, {})
    assert evaluation["etat"] == "non_testable" and evaluation["resume"] == "Le processus n'est pas déployé."
    assert cte.blocker_line(evaluation) == "code ou personnalisation non déployé — preuve : run n° 4 : échec « déploiement »"
    # Même blocage, mais les valeurs dites manquantes sont dans le projet : rejeté, testable
    known = {v: [{"source": "init", "ou": "x"}] for v in DOC_KEYS}
    assert cte.assess(proven, known)["etat"] == "testable"
    # Trouvées seulement dans le Standard : exemples d'usage, le blocage prouvé tient
    assert cte.assess(proven, {v: [{"source": "standard", "ou": "x"}] for v in DOC_KEYS})["etat"] == "non_testable"


def test_without_own_behaviour_requires_proof(cache):
    assert cte.assess({"statut": "non_couvert", "raison": "DTO", "sans_objet": {"preuve": "A.java:3 class A {}"}},
                      {})["etat"] == "sans_objet"
    assert cte.assess({"statut": "non_couvert", "raison": "DTO", "sans_objet": {"preuve": ""}}, {})["etat"] != \
        "sans_objet"
    assert cte.assess({"statut": "non_couvert", "raison": ct.TECHNICAL_REASON}, {})["etat"] == "sans_objet"


def test_answers_saved_automatically_and_reused_without_new_question(cache):
    question = f"Quelle est la clé du paramétrage du document {DOC_KEYS[0]} pour le scénario de rendu ?"
    result = missing_docs(questions=[question])
    first = cte.assess(result, {})
    assert first["etat"] == "a_confirmer" and question in [q["texte"] for q in first["questions"]]
    cte.save_answer(CLIENT, question, f"{DOC_KEYS[0]}-KEY", "sp-1")
    cte.save_answer(CLIENT, question, f"{DOC_KEYS[0]}-KEY2", "sp-1")
    profile = cp.effective_profile(cp.load_profile(CLIENT))
    assert len(profile["reponses"]) == 1 and profile["reponses"][0]["reponse"] == f"{DOC_KEYS[0]}-KEY2"
    # Même question (casse, ponctuation) pour une autre tâche : déjà répondue, jamais reposée
    other = {**result, "id": "sp-2", "questions": [question.upper().rstrip(" ?") + " ?"]}
    known = {v: [{"source": "init", "ou": "x"}] for v in DOC_KEYS}
    reused = cte.assess(other, known, profile)
    assert reused["attendues"] == 0 and reused["etat"] == "testable"
    block = cte.answers_block(profile)
    assert "ne repose jamais ces questions" in block and f"→ {DOC_KEYS[0]}-KEY2" in block
    # Réponse proposée confirmée ou corrigée : enregistrée sous sa clé, avec son statut
    proposal = {"cle": "proposition x", "libelle": "RefReportDoc présent(s) sur l'environnement"}
    cte.save_answer(CLIENT, proposal["libelle"], DOC_KEYS[0], "sp-1", key=proposal["cle"], status="confirmee")
    cte.save_answer(CLIENT, proposal["libelle"], DOC_KEYS[1], "sp-1", key=proposal["cle"], status="corrigee")
    saved = cte.answers(cp.effective_profile(cp.load_profile(CLIENT)))
    assert saved["proposition x"]["reponse"] == DOC_KEYS[1] and saved["proposition x"]["statut"] == "corrigee"
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
    # Valeurs trouvées dans le projet, rien de précis à demander : testables, jamais de question générique
    assert document["taches"] == {"sp-1": "testable", "sp-2": "testable", "sp-3": "sans_objet"}
    assert document["reclassees"] == 3
    assert cte.retro_line(document) == ("3 tâche(s) reclassée(s) : 2 testable(s), 0 à confirmer, 1 sans objet ; "
                                        "0 reste(nt) non testable(s) (blocage prouvé)")


def test_coverage_rows_use_assessment_and_warning_shows_proof(cache, monkeypatch):
    monkeypatch.setattr(cov, "import_results", lambda client: {})
    inventory = {"specifiques": [{"id": i, "libelle": i, "type": "processus", "nature": "", "depot": "d",
                                  "fichiers": [{"path": "a/A.java", "kind": "extension"}]}
                                 for i in ("sp-1", "sp-2", "sp-3")]}
    cte.save_index(CLIENT, {v: [{"source": "init", "ou": INIT_REL}] for v in DOC_KEYS})
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


OUTLINE_NEGATIVE = (f"{CLIENT_TAG}\nFeature: Exclusion du rendu\n"
                    "  Scenario Outline: La réédition d'un document exclu n'est pas rendue\n"
                    "    When PARAM- Réédition du document avec les caractéristiques suivantes\n"
                    "      | refReportDocCode | key   |\n"
                    "      | <doc>            | <key> |\n"
                    "    Then REPORT- <X> documents générés répondant aux caractéristiques\n"
                    "      | X | refReportDoc.code |\n"
                    "      | 0 | <doc>             |\n\n"
                    "    Examples:\n      | doc          | key |\n      | DOC_FICTIF_A | K1  |\n")
TITLED_WITNESS = ("  Scenario: Témoin - la réédition d'un document non exclu déclenche le rendu\n"
                  "    When PARAM- Réédition du document avec les caractéristiques suivantes\n"
                  "      | refReportDocCode | key |\n"
                  "      | DOC_FICTIF_C     | K2  |\n"
                  "    Then REPORT- <X> documents générés répondant aux caractéristiques\n"
                  "      | X | refReportDoc.code |\n"
                  "      | 1 | DOC_FICTIF_C      |\n")


def test_witness_titled_not_excluded_and_negative_outline(tmp_path):
    """Un témoin intitulé « … non exclu … » n'est pas négatif ; un plan de scénario à 0 instance l'est."""
    root = str(tmp_path / "wt")
    rel = f"{RES}/features/{CLIENT}/Exclusion.feature"
    write(os.path.join(root, *rel.split("/")), OUTLINE_NEGATIVE + "\n" + TITLED_WITNESS)
    assert cv.check_positive_witness(root, [rel]) == []
    scenarios = cv.parse_scenarios(OUTLINE_NEGATIVE + "\n" + TITLED_WITNESS)
    assert [cv.is_negative(s) for s in scenarios] == [True, False]
    # Le plan de scénario négatif seul : erreur de lot
    write(os.path.join(root, *rel.split("/")), OUTLINE_NEGATIVE)
    errors = cv.check_positive_witness(root, [rel])
    assert len(errors) == 1 and "« La réédition d'un document exclu n'est pas rendue » sans témoin" in errors[0]
