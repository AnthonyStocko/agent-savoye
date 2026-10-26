"""Point 5 : recréation, par l'init du client, des données d'initializers effacées par le clear."""
import os

import pytest

from tests.conftest import CLEAR, git, write
from tools import cucumber_init as ci
from tools import cucumber_recreation as cr
from tools import cucumber_tools as ct

RES = ci.RESOURCES_DIR
DATA = [
    {"depot": "app", "classe": "SiteInitializer", "entite": "RefSite", "code": "S1", "expression": "",
     "mode": "initByNaturalId", "certitude": "certain", "statut": "effacee"},
    {"depot": "app", "classe": "ReasonInitializer", "entite": "BlockingReason", "code": "DECANT", "expression": "",
     "mode": "new", "certitude": "certain", "statut": "inconnu"},
    {"depot": "app", "classe": "SiteInitializer", "entite": "RefSite", "code": "S2", "expression": "",
     "mode": "initByNaturalId", "certitude": "certain", "statut": "survit"},
    {"depot": "app", "classe": "StockInitializer", "entite": "Stock", "code": "", "expression": "svc.create()",
     "mode": "service", "certitude": "partiel", "statut": "effacee"},
]
CANDIDATES = [{"expression": "OMS- Création d'un site", "entete": ["code", "libelle", "actif"],
               "ligne": ["VDA", "Entrepôt", "true"]},
              {"expression": "PREP- Création d'un panier de campagne", "entete": ["code"], "ligne": ["P1"]}]
INIT = ("@INIT_WMS @WMS\nFeature: 01 - Initialisation\n\n  Scenario: Init\n"
        "    Given INIT- Purge de l'environnement\n    And INIT- Initialisation globale par fichiers via l'écran\n")


def test_only_erased_coded_data_is_recreated():
    profile = {"server_initialized_data": DATA, "initializers_survive_clear": "faux"}
    assert [d["code"] for d in cr.to_recreate(profile)] == ["S1", "DECANT"]
    # Sort inconnu après le clear : seules les données marquées « effacée »
    assert [d["code"] for d in cr.to_recreate({**profile, "initializers_survive_clear": ""})] == ["S1"]


def test_standard_step_first_otherwise_project_step():
    profile = {"server_initialized_data": DATA, "initializers_survive_clear": "faux"}
    items = cr.plan(profile, "clientx", CANDIDATES)
    site, reason = items
    assert site["mode"] == "standard" and site["ligne"] == ["S1", "Entrepôt", "true"]
    assert reason["mode"] == "projet" and reason["expression"] == "INIT- Clientx recréation de BlockingReason {string}"
    lines = "".join(cr.feature_lines(items, "    ", "\n"))
    assert "    # RefSite S1 : initializer SiteInitializer (app)\n    And OMS- Création d'un site\n" in lines
    assert "      | S1 | Entrepôt | true |\n" in lines
    assert '    And INIT- Clientx recréation de BlockingReason "DECANT"' in lines


def test_std_creation_steps_need_a_table_example():
    steps = {"OMS- Création d'un site": {"params": "DataTable table"},
             "OMS- Création du site {string}": {"params": "String c, DataTable t"},
             "OMS- Contrôle du site": {"params": "DataTable table"}}
    text = ("Feature: F\n  Scenario: S\n    Given OMS- Création d'un site\n      | code | libelle |\n"
            "      | VDA  | Entrepôt |\n")
    assert cr.std_creation_steps(steps, [text]) == [{"expression": "OMS- Création d'un site",
                                                    "entete": ["code", "libelle"], "ligne": ["VDA", "Entrepôt"]}]


RECREATION_JAVA = """package cucumber.features.clientx;

public class InitClientxInitializerSteps extends BaseStep {
    /** Recrée BlockingReason comme l'initializer ReasonInitializer (app). */
    @Given("INIT- Clientx recréation de BlockingReason {string}")
    public void recreer(String code) throws Exception {
        if (!ReasonEndPoints.exists(code)) {
            ReasonEndPoints.create(code, true);
        }
    }
}
"""


@pytest.fixture
def init_setup(tmp_path, repo, monkeypatch):
    std = str(tmp_path / "std")
    write(os.path.join(std, RES, "features", "WMS", "01-Contexte", "01-Initialisation.feature"), INIT)
    write(os.path.join(repo, RES, "features", "clientx", "A.feature"), "@CLIENTX\nFeature: A\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(ci.cucumber_recreation, "std_creation_steps", lambda steps, texts: CANDIDATES)
    profile = {"clear_name": CLEAR, "server_initialized_data": DATA, "initializers_survive_clear": "faux",
               "spec_files": [f"{RES}/files/clientx/01-Contexte/WMS/spec.csv"]}
    return std, repo, profile


def _init_text(result):
    rel = next(f for f in result["fichiers"] if f.endswith(".feature"))
    with open(os.path.join(ci.init_worktree_dir("clientx"), *rel.split("/")), encoding="utf-8") as f:
        return f.read()


def test_init_recreates_between_global_init_and_spec_files(init_setup):
    std, repo, profile = init_setup
    calls = []

    def recreate(worktree, items):
        calls.append([e["expression"] for e in items if e["mode"] == "projet"])
        write(os.path.join(worktree, *cr.java_path("clientx").split("/")), RECREATION_JAVA)
        return cr.check_project_steps(worktree, items, ["OMS- Création d'un site"])
    result = ci.build_client_init(repo, std, "clientx", "integration", {"WMS"}, profile, recreate)
    text = _init_text(result)
    assert calls == [["INIT- Clientx recréation de BlockingReason {string}"]]
    order = [text.index(s) for s in (ci.GLOBAL_INIT_STEP, "And OMS- Création d'un site",
                                     'And INIT- Clientx recréation de BlockingReason "DECANT"', ci.SPEC_STEP)]
    assert order == sorted(order) and cr.TODO_MARKER not in text
    assert cr.java_path("clientx") in result["fichiers"] and result["recreation"]["non_livrees"] == []
    assert "S2" not in text and "svc.create" not in text


def test_failed_project_recreation_stays_commented(init_setup):
    std, repo, profile = init_setup

    def recreate(worktree, items):
        write(os.path.join(worktree, *cr.java_path("clientx").split("/")), "class X {}\n")
        return ["step « INIT- Clientx recréation de BlockingReason {string} » défini 0 fois dans le projet"]
    result = ci.build_client_init(repo, std, "clientx", "integration", {"WMS"}, profile, recreate)
    text = _init_text(result)
    assert f"# {cr.TODO_MARKER} BlockingReason DECANT : initializer ReasonInitializer (app) — step" in text
    assert '    # And INIT- Clientx recréation de BlockingReason "DECANT"' in text
    assert "    And OMS- Création d'un site" in text  # step Standard reconnu : livré
    assert cr.java_path("clientx") not in result["fichiers"]
    assert not os.path.exists(os.path.join(ci.init_worktree_dir("clientx"), *cr.java_path("clientx").split("/")))
    assert result["recreation"]["non_livrees"] == ["BlockingReason DECANT"]
    assert any("non livrée" in w for w in result["avertissements"])


def test_project_step_checks(tmp_path):
    items = cr.plan({"server_initialized_data": DATA[1:2], "initializers_survive_clear": "faux"}, "clientx", [])
    worktree = str(tmp_path)
    path = os.path.join(worktree, *cr.java_path("clientx").split("/"))
    write(path, RECREATION_JAVA)
    assert cr.check_project_steps(worktree, items, []) == []
    write(path, RECREATION_JAVA.replace("ReasonInitializer", "X").replace(
        "if (!ReasonEndPoints.exists(code)) {\n            ReasonEndPoints.create(code, true);\n        }",
        "throw new PendingException();"))
    errors = cr.check_project_steps(worktree, items, ["INIT- Clientx recréation de BlockingReason {string}"])
    assert len(errors) == 3
    assert any("déjà défini par le Standard" in e for e in errors) and any("non cité" in e for e in errors)
    assert any("PendingException" in e for e in errors)
