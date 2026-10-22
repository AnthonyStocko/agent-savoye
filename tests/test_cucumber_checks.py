"""Contrôles déterministes des fichiers d'un lot : fichiers de données, emplacement des features, casse du tag client."""
import importlib
import io
import os
import sys

import pytest

from tests.conftest import git, write
from tools import cucumber_checks as cc

RES = "src/test/resources/cucumber"
EXISTING = ("@WMS @APPAREL @STOCK\n"
            "Feature: Stock : Apparel - existant\n\n"
            "  Scenario: Contexte\n"
            "    Given INIT- Intégration des fichiers via l'écran\n"
            "      | fileName       |\n"
            "      | CIN-EXIST.xml  |\n")
LOT = ("@WMS @APPAREL\n"
       "Feature: Stock : Apparel - nouveau\n"
       "  Description qui cite customized.xml sans le lire\n\n"
       "  Scenario: Contexte\n"
       "    # Adapté de WMS/07-Stock/13-Interactions_avec_le_WCS/TOW-1130.feature, voir aussi notes.txt\n"
       "    Given INIT- Intégration des fichiers via l'écran\n"
       "      | fileName                 | withTransformation |\n"
       "      | TOW-1130-Draft.xlsm      | true               |\n"
       "    Given INIT- Intégration des règles\n"
       "      | fileName                 |\n"
       "      | TOW-1130-rule-Storage    |\n"
       "    When j'envoie le message \"MSG-1.xml\"\n"
       "    Then le contrôle \"OK\" est fait\n")


@pytest.fixture
def std_dir(tmp_path):
    """Cucumber Standard : un fichier de données, une règle, un nom présent dans deux dossiers avec deux contenus."""
    root = str(tmp_path / "std")
    folder = "WMS/07-Stock/13-Interactions_avec_le_WCS"
    write(os.path.join(root, RES, "features", folder, "TOW-1130.feature"), "@WMS\nFeature: Std\n")
    write(os.path.join(root, RES, "files", folder, "TOW-1130-Draft.xlsm"), "xlsm du Standard")
    write(os.path.join(root, RES, "rules", folder, "TOW-1130-rule-Storage.json"), "{}")
    write(os.path.join(root, RES, "files", "WMS", "A", "MSG-1.xml"), "<a/>")
    write(os.path.join(root, RES, "files", "WMS", "B", "MSG-1.xml"), "<b/>")
    return root


@pytest.fixture
def client_repo(repo):
    """Dépôt apparel-cucumber à sa base : une feature existante sous features/apparel/ et son fichier de données."""
    write(os.path.join(repo, RES, "features", "apparel", "WMS", "Content", "Existant.feature"), EXISTING)
    write(os.path.join(repo, RES, "files", "apparel", "WMS", "Content", "CIN-EXIST.xml"), "<x/>")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def test_data_references_reads_steps_and_tables_only():
    refs = cc.data_references(LOT)
    assert [(r["nom"], r["regle"]) for r in refs] == [
        ("TOW-1130-Draft.xlsm", False), ("TOW-1130-rule-Storage", True), ("MSG-1.xml", False)]
    # Description et commentaires ignorés ; en-têtes et valeurs sans extension non plus
    assert not any(r["nom"] in ("customized.xml", "notes.txt", "true", "OK") for r in refs)


def test_data_references_reads_examples_and_skips_variables_and_doc_strings():
    text = ("  Scenario Outline: Envoi\n    When j'envoie le fichier \"<fichier>\"\n"
            "    And le corps est :\n      \"\"\"\n      {\"file\": \"corps.json\"}\n      \"\"\"\n"
            "    Examples:\n      | fichier   |\n      | a.csv     |\n      | ${b}.csv  |\n")
    assert [r["nom"] for r in cc.data_references(text)] == ["a.csv"]


def test_resolved_path_follows_feature_folder():
    feature = f"{RES}/features/apparel/WMS/Content/X.feature"
    assert cc.resolved_path(feature, {"nom": "a.xml", "regle": False}) == f"{RES}/files/apparel/WMS/Content/a.xml"
    assert cc.resolved_path(feature, {"nom": "r", "regle": True}) == f"{RES}/rules/apparel/WMS/Content/r.json"


def test_missing_file_present_in_standard_is_copied(client_repo, std_dir):
    rel = f"{RES}/features/apparel/WMS/Content/Nouveau.feature"
    write(os.path.join(client_repo, *rel.split("/")), LOT)
    inspired = ["src/test/resources/cucumber/features/WMS/07-Stock/13-Interactions_avec_le_WCS/TOW-1130.feature / Ctx"]
    result = cc.check_data_files(client_repo, [rel], std_dir, inspired=inspired)
    copied = dict(result["copies"])
    data = f"{RES}/files/apparel/WMS/Content/TOW-1130-Draft.xlsm"
    rule = f"{RES}/rules/apparel/WMS/Content/TOW-1130-rule-Storage.json"
    assert set(copied) == {data, rule}
    with open(os.path.join(client_repo, *data.split("/")), encoding="utf-8") as f:
        assert f.read() == "xlsm du Standard"
    # Même nom dans deux dossiers du Standard, contenus différents : pas de copie au hasard
    assert len(result["erreurs"]) == 1 and "MSG-1.xml" in result["erreurs"][0]
    assert "plusieurs fichiers du Standard" in result["erreurs"][0]
    # Une fois copiés, les fichiers comptent comme présents dans le dépôt client
    assert cc.check_data_files(client_repo, [rel], std_dir)["copies"] == []


def test_missing_file_without_copy_reports_standard_path(client_repo, std_dir):
    rel = f"{RES}/features/apparel/WMS/Content/Nouveau.feature"
    write(os.path.join(client_repo, *rel.split("/")), LOT)
    errors = cc.check_data_files(client_repo, [rel], std_dir, copy=False)["erreurs"]
    assert any("fichier à copier depuis src/test/resources/cucumber/files/WMS/07-Stock/13-Interactions_avec_le_WCS/"
               "TOW-1130-Draft.xlsm" in e for e in errors)


def test_file_unknown_everywhere_is_an_error(client_repo, std_dir):
    rel = f"{RES}/features/apparel/WMS/Content/Nouveau.feature"
    write(os.path.join(client_repo, *rel.split("/")), "Feature: F\n  Scenario: S\n    When j'envoie \"absent.csv\"\n")
    errors = cc.check_data_files(client_repo, [rel], std_dir)["erreurs"]
    assert len(errors) == 1 and "introuvable au Standard" in errors[0]
    assert f"{RES}/files/apparel/WMS/Content/absent.csv" in errors[0]


def test_existing_client_file_is_accepted(client_repo, std_dir):
    rel = f"{RES}/features/apparel/WMS/Content/Existant.feature"
    assert cc.check_data_files(client_repo, [rel], std_dir) == {"copies": [], "erreurs": []}


def test_features_root_detected_from_client_features(client_repo, std_dir):
    assert cc.client_features_root(client_repo, "HEAD", "apparel", std_dir) == f"{RES}/features/apparel/"


def test_features_root_defaults_to_client_segment(repo, std_dir):
    git(repo, "commit", "-q", "--allow-empty", "-m", "vide")
    assert cc.client_features_root(repo, "HEAD", "Apparel", std_dir) == f"{RES}/features/apparel/"


def test_new_feature_outside_client_root_is_rejected(client_repo):
    root = f"{RES}/features/apparel/"
    misplaced = f"{RES}/features/WMS/07-Stock/02-Fabrication/apparel/SPEC-x.feature"
    existing = f"{RES}/features/apparel/WMS/Content/Existant.feature"
    errors = cc.check_locations([misplaced, existing], root, "apparel", {misplaced})
    assert len(errors) == 1 and errors[0].startswith(misplaced)
    # Le dossier du Standard est reproduit SOUS la racine du client
    assert f"{RES}/features/apparel/WMS/07-Stock/02-Fabrication/SPEC-x.feature" in errors[0]


def test_client_tag_convention_and_wrong_case(client_repo):
    assert cc.client_tag(client_repo, "HEAD", "apparel") == "@APPAREL"
    rel = f"{RES}/features/apparel/WMS/Content/Nouveau.feature"
    write(os.path.join(client_repo, *rel.split("/")), "@WMS @apparel @agent_apparel_1\nFeature: F\n")
    errors = cc.check_tags(client_repo, [rel], "@APPAREL")
    assert len(errors) == 1 and "@apparel" in errors[0] and "@APPAREL" in errors[0]
    assert cc.check_tags(client_repo, [f"{RES}/features/apparel/WMS/Content/Existant.feature"], "@APPAREL") == []


def test_client_tag_defaults_to_upper_case(repo):
    git(repo, "commit", "-q", "--allow-empty", "-m", "vide")
    assert cc.client_tag(repo, "HEAD", "apparel") == "@APPAREL"


def test_check_lot_files_skips_data_of_misplaced_feature(client_repo, std_dir):
    misplaced = f"{RES}/features/WMS/07-Stock/apparel/X.feature"
    write(os.path.join(client_repo, *misplaced.split("/")), LOT.replace("@APPAREL", "@apparel"))
    result = cc.check_lot_files(client_repo, "HEAD", [misplaced], "apparel", std_dir)
    assert result["copies"] == []
    assert any("hors de la racine" in e for e in result["erreurs"])
    assert any("tag @apparel" in e for e in result["erreurs"])
    assert not any("absent" in e for e in result["erreurs"])


def test_feature_dirs_are_minimal():
    assert cc.feature_dirs(["a/b/x.feature", "a/b/c/y.feature", "a/d/z.feature"]) == ["a/b", "a/d"]


@pytest.fixture(scope="module")
def main_module():
    """main.py, importé sans qu'il remplace sys.stdout (ce qui fermerait la capture de pytest)."""
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


def test_lot_check_copies_then_reports_file_errors(main_module, client_repo, std_dir):
    """Sans compilation : copie des fichiers du Standard, puis erreur « fichiers » pour la boucle de correction."""
    rel = f"{RES}/features/apparel/WMS/Content/Nouveau.feature"
    write(os.path.join(client_repo, *rel.split("/")), LOT.replace("MSG-1.xml", "TOW-1130-Draft.xlsm"))
    check = main_module.check_cucumber_lot("apparel", "1/1", client_repo, "HEAD", std_dir, [rel], "", "")
    assert check == {"ok": True, "etape": "fichiers", "erreurs": [], "environnement": False}
    assert os.path.exists(os.path.join(client_repo, RES, "files", "apparel", "WMS", "Content", "TOW-1130-Draft.xlsm"))
    write(os.path.join(client_repo, *rel.split("/")), LOT.replace("@APPAREL", "@apparel"))
    check = main_module.check_cucumber_lot("apparel", "1/1", client_repo, "HEAD", std_dir, [rel], "", "")
    assert not check["ok"] and check["etape"] == "fichiers" and not check["environnement"]
    assert any("@apparel" in e for e in check["erreurs"]) and any("MSG-1.xml" in e for e in check["erreurs"])
