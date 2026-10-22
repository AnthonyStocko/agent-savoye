"""Initialisation du client : détection, codes créés, contrôles des lots (codes, ordre, purge) et runner."""
import io
import os
import zipfile

import pytest

from tests.conftest import git, write
from tools import cucumber_init_data as cid
from tools import cucumber_tools as ct

RES = "src/test/resources/cucumber"
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
INIT = (f"@INIT_APPAREL_WMS @APPAREL @WMS\n"
        "Feature: Apparel - 01 - Initialisation des données\n\n"
        "  Scenario: Intégration des données\n"
        "    Given INIT- Purge de l'environnement par le clear \"init-data-rest-clear-main-wms\"\n"
        "    And INIT- Initialisation globale par fichiers via l'écran\n"
        "    And CARTO- Création des secteurs\n"
        "      | refSiteCode | code      |\n"
        "      | VDA         | PTS_STOCK |\n")
INIT_REL = f"{RES}/features/apparel/01-Contexte/WMS/01-Initialisation.feature"


def xlsx(rows: list, sheet: str = "data") -> bytes:
    """Classeur xlsx minimal : cellules inlineStr, une cellule vide omise comme le fait Excel."""
    def cell(ref, value):
        return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>' if value else ""
    body = "".join(f'<row r="{i}">' + "".join(cell(f"{chr(65 + j)}{i}", v) for j, v in enumerate(row)) + "</row>"
                   for i, row in enumerate(rows, 1))
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{MAIN}" xmlns:r="http://schemas.openxmlformats.org/'
                                      f'officeDocument/2006/relationships"><sheets><sheet name="enums" sheetId="2" '
                                      f'r:id="rId2"/><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets>'
                                      f'</workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
                                                 '2006/relationships"><Relationship Id="rId1" Target="worksheets/'
                                                 'sheet1.xml"/><Relationship Id="rId2" Target="worksheets/'
                                                 'sheet2.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{MAIN}"><sheetData>{body}</sheetData></worksheet>')
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet xmlns="{MAIN}"><sheetData/></worksheet>')
    return data.getvalue()


def _bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


@pytest.fixture
def client_repo(repo, tmp_path, monkeypatch):
    """Dépôt client avec son initialisation sous features/apparel/01-Contexte/WMS/ et ses fichiers de données."""
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    write(os.path.join(repo, *INIT_REL.split("/")), INIT)
    data = os.path.join(repo, RES, "files", "apparel", "01-Contexte", "WMS")
    _bytes(os.path.join(data, "04-RefSiteDraft-v22.10.0.xlsm"),
           xlsx([["Veuillez sélectionner"], ["Données d'entête"], ["code", "name"], ["VDA", "Villeneuve"],
                 ["STE", ""]]))
    _bytes(os.path.join(data, "08-WmsSectorDraft.xlsm"),
           xlsx([["refSiteCode", "action", "code"], ["VDA", "CREATE", "PTS_MVT"], ["VDA", "DELETE", "OLD"],
                 ["VDA", "", "GARE_1"]]))
    write(os.path.join(data, "03-RefStockOwnerDraft.csv"), "code;name\nSO-1;Name-SO-1\nSO-2;Name-SO-2\n")
    _bytes(os.path.join(data, "05-RefSiteDraft-casse.xlsx"), b"pas un classeur")
    write(os.path.join(repo, RES, "features", "apparel", "WMS", "Content", "Existant.feature"),
          "@APPAREL\nFeature: F\n  Scenario: S\n    Given un step\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def test_init_detected_with_files_and_codes(client_repo):
    init = cid.client_init(client_repo, "apparel")
    assert cid.init_paths(init) == [INIT_REL]
    feature = init["features"][0]
    assert "@INIT_APPAREL_WMS" in feature["tags"] and len(feature["fichiers"]) == 4
    assert init["codes"]["sites"] == ["STE", "VDA"]
    # Ligne DELETE exclue ; table de step de création comprise
    assert init["codes"]["secteurs"] == ["GARE_1", "PTS_MVT", "PTS_STOCK"]
    assert init["codes"]["donneurs_ordres"] == ["SO-1", "SO-2"]
    # Fichier illisible : avertissement, pas d'échec
    assert len(init["avertissements"]) == 1 and "05-RefSiteDraft-casse.xlsx" in init["avertissements"][0]
    assert os.path.exists(cid.init_path("apparel"))


def test_init_tags_override_detection(client_repo):
    assert cid.init_paths(cid.client_init(client_repo, "apparel", "@AUTRE_INIT")) == []
    assert cid.init_paths(cid.client_init(client_repo, "apparel", "@INIT_APPAREL_WMS and not @WIP")) == [INIT_REL]


def test_init_detected_by_context_folder_without_tag():
    assert cid.is_init_feature(f"{RES}/features/apparel/01-Contexte/01-Initialisation.feature", "Feature: X", set())
    assert not cid.is_init_feature(f"{RES}/features/apparel/WMS/Content/A.feature", "Feature: X", set())


def test_xlsx_keeps_column_positions_with_missing_cells():
    rows = cid._xlsx_rows(xlsx([["code", "name", "x"], ["A", "", "z"]]))
    assert rows == [["code", "name", "x"], ["A", "", "z"]]


def test_init_block_lists_codes_and_forbids_purge(client_repo):
    block = cid.init_block(cid.client_init(client_repo, "apparel"), "init-data-rest-clear-main-wms")
    assert "sites [STE, VDA]" in block and "donneurs d'ordres [SO-1, SO-2]" in block
    assert "Utilise UNIQUEMENT ces codes" in block and INIT_REL in block and "init-data-rest-clear-main-wms" in block
    many = {"features": [{"chemin": INIT_REL}], "codes": {"sites": [f"S{i}" for i in range(40)]}}
    assert "(10 autres)" in cid.init_block(many, "c", max_codes=30)


def test_unknown_site_or_owner_is_an_error(client_repo):
    init = cid.client_init(client_repo, "apparel")
    rel = f"{RES}/features/apparel/WMS/Content/Nouveau.feature"
    write(os.path.join(client_repo, *rel.split("/")),
          "@APPAREL\nFeature: F\n  Scenario: S\n    Then CONTENT- Contrôle contenu de stock\n"
          "      | wmsLocation.refLocation.refSite.code | refStockOwner.code | quantity |\n"
          "      | VDA                                  | SO-1               | 2        |\n"
          "      | LYON                                 | SO-9               | 1        |\n"
          "      | ${site}                              | SO-1               | 1        |\n")
    errors = cid.check_init_codes(client_repo, [rel], init)
    assert len(errors) == 2
    assert any("site « LYON »" in e and "VDA" in e for e in errors)
    assert any("donneur d'ordres « SO-9 »" in e for e in errors)


def test_code_created_in_feature_is_accepted(client_repo):
    init = cid.client_init(client_repo, "apparel")
    rel = f"{RES}/features/apparel/WMS/Content/Nouveau.feature"
    write(os.path.join(client_repo, *rel.split("/")),
          "Feature: F\n  Background:\n    Given REF- Création des sites\n      | code | name |\n"
          "      | LYON | Lyon |\n  Scenario: S\n    Then contrôle\n      | refSiteCode |\n      | LYON        |\n")
    assert cid.check_init_codes(client_repo, [rel], init) == []


def test_feature_running_before_init_is_rejected(client_repo):
    init = cid.client_init(client_repo, "apparel")
    before = f"{RES}/features/WMS/07-Stock/apparel/X.feature"
    also_before = f"{RES}/features/apparel/01-Contexte/A-test.feature"
    after = f"{RES}/features/apparel/EXC/Y.feature"
    errors = cid.check_execution_order([before, also_before, after, INIT_REL], init)
    assert [e.split(" : ", 1)[0] for e in errors] == [before, also_before]
    assert "Cucumber trie les features par chemin" in errors[0]


def test_purge_or_init_in_generated_feature_is_rejected(client_repo):
    init = cid.client_init(client_repo, "apparel")
    rel = f"{RES}/features/apparel/WMS/Content/Nouveau.feature"
    write(os.path.join(client_repo, *rel.split("/")),
          "Feature: F\n  Scenario: Contexte\n    Given INIT- Purge de l'environnement\n"
          "    And INIT- Intégration des fichiers via l'écran\n      | fileName |\n      | a.xml |\n")
    errors = cid.check_no_init_rewrite(client_repo, {rel}, init)
    assert len(errors) == 1 and ":3 " in errors[0] and "ne réécris jamais de purge" in errors[0]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

RUNNER = """package cucumber;

import io.cucumber.junit.Cucumber;
import io.cucumber.junit.CucumberOptions;
import org.junit.runner.RunWith;

@RunWith(Cucumber.class)
@CucumberOptions(
        plugin = {{{plugins}}},
        glue = {{"cucumber"}},
        tags = "{tags}",
        features = {{"{features}"}})
public class RunnerTest {{
}}
"""
CONVENTIONS = {"features_root": f"{RES}/features/apparel/", "client_tag": "@APPAREL", "init": [INIT_REL]}
STD_RUNNER = {"path": "src/test/java/cucumber/RunCucumberTest.java", "fqcn": "cucumber.RunCucumberTest",
              "glue": ["cucumber"], "features": [f"{RES}/features/"], "tags": [],
              "plugins": ["pretty", "json:target/cucumber-reports/Cucumber.json"]}


def runner(features=f"{RES}/features/", tags="@APPAREL", plugins='"pretty", "json:target/cucumber.json"'):
    text = RUNNER.format(features=features, tags=tags, plugins=plugins)
    files = {"src/test/java/cucumber/RunnerTest.java": text}
    return ct.runner_entries(ct.java_classes(files), files)[0]


def test_runner_options_are_read():
    entry = runner()
    assert entry["fqcn"] == "cucumber.RunnerTest" and entry["glue"] == ["cucumber"]
    assert entry["features"] == [f"{RES}/features/"] and entry["tags"] == ["@APPAREL"]
    assert "json:target/cucumber.json" in entry["plugins"]


def test_tags_match():
    assert ct.tags_match("@APPAREL or @WMS", {"@APPAREL"})
    assert not ct.tags_match("@WMS and not @APPAREL", {"@APPAREL", "@WMS"})
    assert ct.tags_match("(@A or @B) and not @C", {"@B"}) and ct.tags_match("", {"@X"})


def test_runner_without_gap():
    assert ct.runner_gaps(runner(), "cucumber.features.apparel", CONVENTIONS) == []


def test_runner_gaps_path_tags_json():
    gaps = ct.runner_gaps(runner(features=f"{RES}/features/WMS", tags="@WMS", plugins='"pretty"'),
                          "cucumber.features.apparel", CONVENTIONS)
    assert len(gaps) == 3
    assert "chemin de features" in gaps[0] and f"{RES}/features/apparel/" in gaps[0]
    assert "« @WMS » ne sélectionne pas @APPAREL" in gaps[1]
    assert "json:target/cucumber-reports/Cucumber.json" in gaps[2]
    # classpath: rapporté à src/test/resources/
    assert ct.runner_gaps(runner(features="classpath:cucumber/features"), "cucumber.features.apparel",
                          CONVENTIONS) == []


def test_existing_runner_is_completed_never_doubled():
    incomplete = runner(plugins='"pretty"')
    status, target = ct.runner_status_for("cucumber.features.apparel", [incomplete], [STD_RUNNER], "Apparel",
                                          CONVENTIONS)
    assert ct.runner_to_complete(status) and not ct.needs_runner(status) and target == incomplete
    assert "N'en crée JAMAIS un second" in status
    # Runner client dont la glue ne couvre pas les steps : complété (glue), pas de second runner
    other = dict(runner(), glue=["autre.pkg"])
    status, _ = ct.runner_status_for("cucumber.features.apparel", [other], [], "Apparel", CONVENTIONS)
    assert ct.runner_to_complete(status) and "ajoute la glue 'cucumber.features.apparel'" in status


def test_overlay_uses_standard_runner():
    status, target = ct.runner_status_for("cucumber.features.apparel", [], [STD_RUNNER], "Apparel", CONVENTIONS)
    assert target == STD_RUNNER and not ct.needs_runner(status) and not ct.runner_to_complete(status)
    assert "surcouche du Cucumber Standard" in status and ct.runner_block("x.java", status, 1000) == ""


def test_no_runner_creation_includes_init_and_tags():
    status, target = ct.runner_status_for("cucumber.features.apparel", [], [], "Apparel", CONVENTIONS)
    assert target is None and ct.needs_runner(status)
    assert INIT_REL in status and "@APPAREL" in status and "json:" in status


def test_client_with_std_test_jar_and_no_runner_runs_in_overlay(client_repo, tmp_path, monkeypatch):
    write(os.path.join(client_repo, "pom.xml"), "<project><dependency><type>test-jar</type></dependency></project>")
    std_java = {"runners": [STD_RUNNER], "steps_root": "cucumber.features"}
    java = ct.client_java_context("apparel", client_repo, std_java, CONVENTIONS)
    assert java["overlay_run"] and not java["overlay"] and not ct.needs_runner(java["runner_status"])


def test_second_runner_written_by_lot_is_rejected(client_repo):
    rel = "src/test/java/cucumber/features/apparel/RunApparelCucumberTest.java"
    write(os.path.join(client_repo, *rel.split("/")),
          RUNNER.format(features=f"{RES}/features/", tags="@APPAREL", plugins='"json:a.json"')
          .replace("package cucumber;", "package cucumber.features.apparel;")
          .replace("RunnerTest", "RunApparelCucumberTest"))
    java = {"overlay_run": True, "runner": STD_RUNNER, "package_steps": "cucumber.features.apparel"}
    errors = ct.lot_runner_errors(client_repo, "HEAD", [rel], java, CONVENTIONS)
    assert len(errors) == 1 and "second runner" in errors[0] and "cucumber.RunCucumberTest" in errors[0]
    # Aucun runner existant, pas de surcouche : le runner créé est seulement contrôlé (ici sans écart)
    java = {"overlay_run": False, "runner": None, "package_steps": "cucumber.features.apparel"}
    assert ct.lot_runner_errors(client_repo, "HEAD", [rel], java, CONVENTIONS) == []
