"""Profil client : détection, priorité dépôt > saisie > détection, champs vides, et aucun littéral client dans le code."""
import glob
import json
import os

import pytest

from tests.conftest import CLEAR, CLIENT, CLIENT_TAG, FICTIVE_VALUES, OWNERS, SITES, git, write
from tools import cucumber_profile as cp
from tools import cucumber_tools as ct

RES = "src/test/resources/cucumber"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INIT_REL = f"{RES}/features/{CLIENT}/01-Contexte/WMS/01-Initialisation.feature"
INIT = (f"@INIT_{CLIENT.upper()}_WMS {CLIENT_TAG}\n"
        "Feature: Initialisation\n\n"
        "  Scenario: Intégration des données\n"
        f"    Given INIT- Purge de l'environnement par le clear \"{CLEAR}\"\n"
        "    And INIT- Initialisation globale par fichiers via l'écran\n"
        "    And INIT- Intégration des fichiers via l'écran\n"
        "      | fileName           |\n"
        "      | 20-SpecDocument.xlsm |\n"
        "    And INIT- Intégration des fichiers via l'écran\n"
        "      | fileName           |\n"
        "      | 10-SpecStatus.csv  |\n")
POM = ("<project><dependencies><dependency><groupId>g</groupId><artifactId>Cucumber</artifactId>"
       "<version>26.21</version><type>test-jar</type></dependency></dependencies></project>")
STD_RUNNER = {"path": "src/test/java/cucumber/RunCucumberTest.java", "fqcn": "cucumber.RunCucumberTest",
              "glue": ["cucumber"], "features": [f"{RES}/features/"], "tags": [],
              "plugins": ["json:target/cucumber-reports/Cucumber.json"]}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.delenv("CUCUMBER_INIT_TAGS", raising=False)


@pytest.fixture
def std_dir(tmp_path):
    """Cucumber Standard : une initialisation WMS et son fichier de données."""
    root = str(tmp_path / "std")
    write(os.path.join(root, RES, "features", "WMS", "01-Contexte", "01-Initialisation.feature"),
          "@INIT_WMS @WMS\nFeature: Init\n")
    write(os.path.join(root, RES, "files", "WMS", "01-Contexte", "01-RefSiteDraft.csv"), "code\n")
    return root


@pytest.fixture
def client_repo(repo):
    """Dépôt <client>-cucumber : init en trois étapes, ses fichiers, une feature client, pom test-jar, pas de runner."""
    write(os.path.join(repo, "pom.xml"), POM)
    write(os.path.join(repo, *INIT_REL.split("/")), INIT)
    data = os.path.join(repo, RES, "files", CLIENT, "01-Contexte", "WMS")
    write(os.path.join(data, "01-RefSiteDraft.csv"), "code;name\n" + "".join(f"{s};x\n" for s in SITES))
    write(os.path.join(data, "03-RefStockOwnerDraft.csv"), "code;name\n" + "".join(f"{o};x\n" for o in OWNERS))
    write(os.path.join(data, "04-ExcPartnerDraft.csv"), "code;name\nPART-X;x\n")
    write(os.path.join(data, "20-SpecDocument.xlsm"), "binaire")
    write(os.path.join(data, "10-SpecStatus.csv"), "code\nST-X\n")
    write(os.path.join(repo, RES, "files", CLIENT, "WMS", "Content", "SPEC-1.xml"), "<a/>")
    write(os.path.join(repo, RES, "features", CLIENT, "WMS", "Content", "SPEC-1.feature"),
          f"@WMS {CLIENT_TAG}\nFeature: F\n  Scenario: S\n    Given un step\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def test_profile_detected_from_repository(client_repo, std_dir, cache):
    creation = {"entite": "RefReportDoc", "code": "DOC1", "expression": "DOC_CODE", "mode": "initByNaturalId",
                "certitude": "certain"}
    ct.save_inventory(CLIENT, {"specifiques": [], "initialiseurs": [
        {"depot": "app", "chemin": "A.java", "classe": "DocInitializer", "creations": [creation]}]})
    profile, suggestions, warnings = cp.detect_profile(CLIENT, client_repo, "HEAD", std_dir, [STD_RUNNER], "26.21")
    assert profile["features_root"] == f"{RES}/features/{CLIENT}/" and profile["tag_client"] == CLIENT_TAG
    assert profile["init_features"] == [INIT_REL] and profile["init_tags"] == f"@INIT_{CLIENT.upper()}_WMS"
    assert profile["clear_name"] == CLEAR
    # Fichiers intégrés après l'initialisation globale, dans l'ordre de la feature
    folder = f"{RES}/files/{CLIENT}/01-Contexte/WMS/"
    assert profile["spec_files"] == [folder + "20-SpecDocument.xlsm", folder + "10-SpecStatus.csv"]
    # Fichiers protégés : la feature d'init et tout ce qu'elle intègre (dossier de l'init globale compris)
    assert set(profile["protected_files"]) == {INIT_REL, *(folder + n for n in (
        "01-RefSiteDraft.csv", "03-RefStockOwnerDraft.csv", "04-ExcPartnerDraft.csv", "20-SpecDocument.xlsm",
        "10-SpecStatus.csv"))}
    assert profile["reference_codes"]["sites"] == list(SITES)
    assert profile["reference_codes"]["donneurs_ordres"] == list(OWNERS)
    assert profile["reference_codes"]["partenaires"] == ["PART-X"]
    assert profile["library_version"] == "26.21" and profile["server_version"] == "26.21"
    assert profile["server_initialized_data"] == [{"depot": "app", "classe": "DocInitializer", **creation,
                                                   "statut": "inconnu"}]
    # Pas de runner propre, test-jar du Standard : runner du Standard en surcouche
    assert profile["runner"]["classe"] == "cucumber.RunCucumberTest" and profile["runner"]["surcouche"]
    assert profile["runner"]["plugin_json"].startswith("json:")
    # Suggestions : données du client hors init Standard (même nom exclu) et hors fichiers déjà retenus
    assert f"{RES}/files/{CLIENT}/WMS/Content/SPEC-1.xml" in suggestions["spec_files"]
    assert not any(s.endswith("01-RefSiteDraft.csv") or s.endswith("20-SpecDocument.xlsm")
                   for s in suggestions["spec_files"])
    assert warnings == []


def test_undetected_fields_stay_empty_with_warnings(repo, std_dir, cache):
    git(repo, "commit", "-q", "--allow-empty", "-m", "vide")
    profile, _, warnings = cp.detect_profile(CLIENT, repo, "HEAD", std_dir, server_version="dev")
    assert profile["server_version"] == "" and profile["clear_name"] == "" and profile["init_features"] == [] and profile["spec_files"] == []
    assert profile["library_version"] == "" and not profile["runner"]["classe"]
    effective = cp.effective_profile({"detecte": profile, "avertissements_detection": warnings})
    assert any("clear de l'environnement vide" in w for w in effective["avertissements"])
    assert any("racine des features non détectée" in w for w in effective["avertissements"])


def test_priority_repo_over_user_over_detection(client_repo, std_dir, cache):
    write(os.path.join(client_repo, cp.REPO_PROFILE), json.dumps({"clear_name": "clear-depot", "inconnu": 1}))
    git(client_repo, "add", "-A")
    git(client_repo, "commit", "-qm", "profil versionné")
    document = cp.refresh_profile(CLIENT, client_repo, "HEAD", std_dir, [STD_RUNNER])
    assert document["depot"] == {"clear_name": "clear-depot"}
    detected = document["detecte"]
    cp.save_user_fields(CLIENT, {**detected, "clear_name": "clear-saisi", "init_tags": "@INIT_SAISI"})
    saved = cp.load_profile(CLIENT)
    # Seuls les champs modifiés sont gardés dans la saisie
    assert saved["saisi"] == {"clear_name": "clear-saisi", "init_tags": "@INIT_SAISI"}
    effective = cp.effective_profile(saved)
    assert effective["clear_name"] == "clear-depot" and effective["sources"]["clear_name"] == "depot"
    assert effective["init_tags"] == "@INIT_SAISI" and effective["sources"]["init_tags"] == "saisi"
    assert effective["features_root"] == detected["features_root"] and effective["sources"]["features_root"] == "detecte"
    # La saisie survit à une nouvelle détection
    assert cp.effective_profile(cp.refresh_profile(CLIENT, client_repo, "HEAD", std_dir))["init_tags"] == "@INIT_SAISI"


def test_invalid_repo_profile_is_ignored_with_warning(client_repo, std_dir, cache):
    write(os.path.join(client_repo, cp.REPO_PROFILE), "{pas du json")
    git(client_repo, "add", "-A")
    git(client_repo, "commit", "-qm", "profil cassé")
    document = cp.refresh_profile(CLIENT, client_repo, "HEAD", std_dir)
    assert document["depot"] == {} and any("illisible" in w for w in document["avertissements_detection"])


def test_views_for_checks(client_repo, std_dir, cache):
    effective = cp.client_profile(CLIENT, client_repo, "HEAD", std_dir, server_version="26.30")
    assert cp.init_view(effective)["features"] == [{"chemin": INIT_REL}]
    assert cp.init_view(effective)["codes"]["sites"] == list(SITES)
    assert {k: v for k, v in cp.conventions(effective).items() if k not in ("runner", "protected_files")} == {
        "features_root": f"{RES}/features/{CLIENT}/", "client_tag": CLIENT_TAG, "init": [INIT_REL],
        "library_version": "26.21", "server_version": "26.30", "server_initialized_data": [],
        "initializers_survive_clear": "", "workarounds": [], "verify_branch": "",
        "verify_branch_param": "CUCUMBER_SPEC_BRANCH"}
    assert INIT_REL in cp.conventions(effective)["protected_files"]


def guarded_files() -> list:
    """Fichiers du code de l'agent où aucun littéral propre à un client ne doit apparaître."""
    patterns = ["tools/*.py", "prompts/*.md", "main.py", "app.py", "webhook_server.py", "jenkins/*", ".env.example"]
    return [p for pattern in patterns for p in glob.glob(os.path.join(ROOT, pattern)) if os.path.isfile(p)]


def test_no_client_literals():
    """Les valeurs du client fictif des tests (conftest) n'apparaissent jamais dans le code : rien n'est en dur."""
    found = []
    for path in guarded_files():
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        found += [f"{os.path.relpath(path, ROOT)} : {value}" for value in FICTIVE_VALUES if value in text]
    assert guarded_files() and found == []


def test_profile_block_in_lot_prompt(client_repo, std_dir, cache):
    block = cp.profile_block(cp.client_profile(CLIENT, client_repo, "HEAD", std_dir))
    assert f"'{RES}/features/{CLIENT}/'" in block and f"Tag du client (casse exacte) : {CLIENT_TAG}" in block
    assert f"sites [{', '.join(SITES)}]" in block and "Utilise UNIQUEMENT ces codes" in block
    assert "Ne réécris jamais de purge ni d'init" in block and CLEAR in block
    assert "N'utilise QUE des steps existants" in block and "Background" in block
    assert "Fichiers protégés de l'init globale (6" in block and f"{RES}/files/{CLIENT}/01-Contexte/WMS/" in block
    assert "ne les modifie JAMAIS" in block
    assert "TODO-VERSION" not in block
    gap = cp.profile_block({**cp.empty_profile(), "library_version": "26.16", "server_version": "26.21-SNAPSHOT"})
    assert "bibliothèque de steps 26.16" in gap and "`# TODO-VERSION: <step, colonne ou champ> — à vérifier en 26.16`" in gap
    assert cp.version_block("26.21", "26.21-SNAPSHOT") == "" and cp.version_block("", "26.21") == ""
    assert "step du projet de nom unique" in gap and "Ne modifie JAMAIS la bibliothèque partagée" in gap
    with_file = cp.profile_block({**cp.empty_profile(), "library_version": "26.16", "server_version": "26.21"},
                                 gap_file="/cache/ecarts.md")
    assert "cherche-le par Grep dans '/cache/ecarts.md'" in with_file
    empty = cp.profile_block(cp.effective_profile({}))
    assert "(non renseignée" in empty and "aucune feature d'initialisation détectée" in empty
    assert "aucun détecté" in empty


def test_lot_prompts_take_profile_from_variables(monkeypatch):
    """Prompts de lot : aucune valeur de profil en dur, le bloc profil vient de la partie variable."""
    with open(os.path.join(ROOT, "prompts", "cucumber_client_lot.md"), encoding="utf-8") as f:
        assert "${profil_block}" in f.read()
    with open(os.path.join(ROOT, "prompts", "cucumber_client_lot_systeme.md"), encoding="utf-8") as f:
        system = f.read()
    assert "sans aucun repli" in system and "PROFIL CLIENT" in system and "Background" in system


def test_generated_init_spec_files_detected_back(client_repo, std_dir, cache):
    """Fichiers spécifiques d'une init générée (chemins en ../) relus à l'identique par la détection."""
    from tools import cucumber_init as ci
    spec = f"{RES}/files/{CLIENT}/WMS/Content/SPEC-1.xml"
    text = ci.adapt_feature(INIT.replace(f"    And INIT- Intégration des fichiers", "    # ", 1), "WMS", CLIENT, CLEAR,
                            CLIENT_TAG, [spec], INIT_REL)
    assert cp.init_spec_files(INIT_REL, text)[0] == spec


def test_initializer_statuses_and_profile_block():
    data = [{"depot": "app", "classe": "C", "entite": "RefReportDoc", "code": code, "expression": code,
             "mode": "initByNaturalId", "certitude": "certain", "statut": "inconnu"} for code in ("DOC1", "DOC2")]
    document = {"detecte": {**cp.empty_profile(), "server_initialized_data": data},
                "saisi": {"server_initialized_data": [{**data[0], "statut": "recreee"},
                                                      {"classe": "Manuel", "code": "M1", "statut": "survit"}]}}
    effective = cp.effective_profile(document)
    assert [(d["code"], d["statut"]) for d in effective["server_initialized_data"]] == [
        ("DOC1", "recreee"), ("DOC2", "inconnu"), ("M1", "survit")]
    block = cp.initializers_block(effective["server_initialized_data"], "")
    assert "utilisables : DOC1 (RefReportDoc, C), M1 (?, Manuel)" in block and "incertaine : DOC2 (RefReportDoc, C)" in block
    assert "effacées par le clear : DOC2" in cp.initializers_block(effective["server_initialized_data"], "faux")
    assert "incertaine" not in cp.initializers_block(effective["server_initialized_data"], "vrai")
    assert cp.initializers_block([], "") == "" and "initializers serveur" in cp.profile_block(effective)
    assert "aucun test de leur seule existence" in block and "DOC1 (RefReportDoc, C)" in block


def test_workarounds_detected_and_optional(client_repo, std_dir, cache):
    rel = f"{RES}/features/{CLIENT}/Contournement.feature"
    write(os.path.join(client_repo, rel), "Feature: X\n  # TODO-VERSION: step réécrit, bibliothèque ancienne\n")
    git(client_repo, "add", "-A")
    git(client_repo, "commit", "-qm", "contournement")
    profile = cp.client_profile(CLIENT, client_repo, "HEAD", std_dir)
    assert profile["workarounds"] == [{"fichier": rel, "ligne": 2, "texte": "step réécrit, bibliothèque ancienne"}]
    assert not any("contournements" in w or "initializers serveur vide" in w for w in profile["avertissements"])
    assert profile["initializers_survive_clear"] == "" and cp.SURVIVE_CLEAR[""] == "inconnu"
