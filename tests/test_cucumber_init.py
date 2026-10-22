"""Initialisation Cucumber d'un client : détection des modules, filtrage, renommage et patch applicable."""
import os
import subprocess

import pytest

from tests.conftest import CLEAR, git, write

from tools import cucumber_init as ci
from tools import cucumber_tools as ct

RES = ci.RESOURCES_DIR
PROFILE = {"clear_name": CLEAR}
INIT_FEATURE = ("@INIT_{m} @{m} @PERFORMANCE\n"
                "Feature: 01 - Initialisation des données\n\n"
                "  Scenario:  Intégration des données\n"
                "    Given INIT- Purge de l'environnement\n"
                "    And INIT- Initialisation globale par fichiers via l'écran\n")
# Données non UTF-8 et binaires : doivent traverser le patch intactes
LATIN1_CSV = "code;libellé\nVDA;Entrepôt\n".encode("latin-1")
XLSM = bytes(range(256)) * 4


def _write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


@pytest.fixture
def std_dir(tmp_path):
    """Cucumber Standard avec les initialisations WMS, TMS et OMS, et une feature hors initialisation."""
    root = str(tmp_path / "std")
    for module in ci.INIT_MODULES:
        write(os.path.join(root, RES, "features", module, "01-Contexte", "01-Initialisation.feature"),
              INIT_FEATURE.format(m=module))
        _write_bytes(os.path.join(root, RES, "files", module, "01-Contexte", f"01-{module}SiteDraft.csv"), LATIN1_CSV)
        _write_bytes(os.path.join(root, RES, "files", module, "01-Contexte", f"02-{module}BayDraft.xlsm"), XLSM)
    write(os.path.join(root, RES, "features", "WMS", "03-Preparation", "Autre.feature"), "@WMS\nFeature: Autre\n")
    return root


@pytest.fixture
def client_cucumber(repo):
    """Dépôt <client>-cucumber sur integration, avec une feature client existante."""
    write(os.path.join(repo, RES, "features", "clientx", "WMS", "Content", "SPEC-1.feature"),
          "@WMS @CLIENTX\nFeature: Clientx - Spec\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Worktree et patch dans tmp_path, pas dans le workspace."""
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))


def test_client_modules_from_repos_and_inventory(tmp_path):
    root = tmp_path / "client_CLIENTX"
    for name in ("wms-content-backend", "odatio-clientx", "clientx-cucumber"):
        os.makedirs(root / name / ".git")
    assert ci.client_modules(str(root), {}) == {"WMS"}
    # Module client dans un dépôt au nom neutre : détecté par le dossier de premier niveau des fichiers
    inventory = {"specifiques": [{"depot": "odatio-clientx", "fichiers": [
        {"repo": "odatio-clientx", "path": "clientx-tms-provider/src/main/java/a/Provider.java"},
        {"repo": "odatio-clientx", "path": "clientx-tls-util/src/main/java/oms/Ignored.java"}]}]}
    assert ci.client_modules(str(root), inventory) == {"WMS", "TMS"}
    assert ci.client_modules("", {}) == set()


def test_std_init_files_by_module(std_dir):
    files = ci.std_init_files(std_dir)
    assert set(files) == {"WMS", "TMS", "OMS"}
    assert files["WMS"][0] == f"{RES}/features/WMS/01-Contexte/01-Initialisation.feature"
    assert all("/WMS/01-Contexte/" in p for p in files["WMS"])
    assert not any("03-Preparation" in p for p in files["WMS"])


def test_wms_only_client_gets_nothing_from_tms_oms(std_dir, client_cucumber, isolated_cache):
    result = ci.build_client_init(client_cucumber, std_dir, "clientx", "integration", {"WMS"}, PROFILE)
    assert result["modules"] == ["WMS"]
    data = [p for p in result["fichiers"] if not p.endswith(".java")]
    assert data and all("/clientx/01-Contexte/WMS/" in p for p in data)
    assert [p for p in result["fichiers"] if p.endswith(".java")] == [ci.clear_step_path("clientx")]
    changed = git(ci.init_worktree_dir("clientx"), "diff", "--name-only", "integration..HEAD").splitlines()
    assert sorted(changed) == sorted(result["fichiers"])
    assert not any("/TMS/" in p or "/OMS/" in p for p in changed)


def test_client_renaming_in_tags_title_and_paths():
    text = INIT_FEATURE.format(m="TMS").replace("\n", "\r\n")
    adapted = ci.adapt_feature(text, "TMS", "Clientx-2", CLEAR)
    first, second = adapted.splitlines()[:2]
    assert first == "@INIT_CLIENTX2_TMS @CLIENTX2 @TMS @PERFORMANCE"
    assert second == "Feature: Clientx2 - 01 - Initialisation des données"
    assert "\r\n" in adapted
    # Purge Standard (clear « full ») remplacée par le clear nommé, par le step client
    assert f'Given {ci.CLEAR_STEP} "{CLEAR}"\r\n' in adapted
    assert "Given INIT- Purge de l'environnement\r\n" not in adapted
    # Sous <client>/01-Contexte/<MODULE>/ : trié avant les autres features du client (Cucumber trie par chemin)
    assert ci.client_path(f"{RES}/files/TMS/01-Contexte/a.csv", "Clientx-2") == f"{RES}/files/clientx2/01-Contexte/TMS/a.csv"
    assert ci.client_path(f"{RES}/features/WMS/01-Contexte/x.feature", "clientx") < f"{RES}/features/clientx/EXC/a.feature"


def test_clear_name_from_profile_then_environment(monkeypatch):
    monkeypatch.delenv("CUCUMBER_CLEAR_NAME", raising=False)
    assert ci.clear_name({}) == "" and ci.clear_name(PROFILE) == CLEAR
    monkeypatch.setenv("CUCUMBER_CLEAR_NAME", "mon-clear")
    assert ci.clear_name({}) == "mon-clear" and ci.clear_name(PROFILE) == CLEAR
    adapted = ci.adapt_feature(INIT_FEATURE.format(m="WMS"), "WMS", "clientx", "mon-clear", "@ClientX")
    assert f'Given {ci.CLEAR_STEP} "mon-clear"' in adapted and adapted.startswith("@INIT_ClientX_WMS @ClientX")
    java = ci.clear_step_java("clientx")
    assert "package cucumber.features.clientx;" in java and "public class InitClientxSteps extends BaseStep" in java
    assert f'@Given("{ci.CLEAR_STEP} {{string}}")' in java and "KISEndPoints.clear(clearName)" in java


def test_patch_applies_on_base(std_dir, client_cucumber, isolated_cache, tmp_path):
    result = ci.build_client_init(client_cucumber, std_dir, "clientx", "integration", {"WMS", "OMS"}, PROFILE)
    assert result["patch"] == ct.init_patch_path("clientx") and os.path.exists(result["patch"])
    assert result["modules"] == ["OMS", "WMS"]
    # Le Standard n'est pas modifié
    with open(os.path.join(std_dir, RES, "features", "WMS", "01-Contexte", "01-Initialisation.feature"),
              encoding="utf-8") as f:
        assert f.read().startswith("@INIT_WMS @WMS")

    clone = str(tmp_path / "clone")
    subprocess.run(["git", "clone", "-q", "-b", "integration", client_cucumber, clone], check=True)
    git(clone, "config", "user.name", "test")
    git(clone, "config", "user.email", "test@example.com")
    git(clone, "config", "core.autocrlf", "false")
    git(clone, "apply", "--check", result["patch"])
    git(clone, "am", "-q", result["patch"])
    csv = os.path.join(clone, RES, "files", "clientx", "01-Contexte", "WMS", "01-WMSSiteDraft.csv")
    xlsm = os.path.join(clone, RES, "files", "clientx", "01-Contexte", "OMS", "02-OMSBayDraft.xlsm")
    with open(csv, "rb") as f:
        assert f.read() == LATIN1_CSV
    with open(xlsm, "rb") as f:
        assert f.read() == XLSM
    assert os.path.exists(os.path.join(clone, *ci.clear_step_path("clientx").split("/")))
    with open(os.path.join(clone, RES, "features", "clientx", "01-Contexte", "WMS", "01-Initialisation.feature"),
              encoding="utf-8") as f:
        assert f.readline().strip() == "@INIT_CLIENTX_WMS @CLIENTX @WMS @PERFORMANCE"

    # Deuxième génération : rien de nouveau par rapport à une base qui contient déjà l'initialisation
    git(client_cucumber, "pull", "-q", clone, "integration")
    again = ci.build_client_init(client_cucumber, std_dir, "clientx", "integration", {"WMS", "OMS"}, PROFILE)
    assert again["patch"] == "" and not os.path.exists(ct.init_patch_path("clientx"))


def test_init_refused_without_clear(std_dir, client_cucumber, isolated_cache, monkeypatch):
    monkeypatch.delenv("CUCUMBER_CLEAR_NAME", raising=False)
    with pytest.raises(RuntimeError, match="clear_name vide"):
        ci.build_client_init(client_cucumber, std_dir, "clientx", "integration", {"WMS"}, {})


def test_unknown_std_module_is_reported(std_dir, client_cucumber, isolated_cache):
    os.remove(os.path.join(std_dir, RES, "features", "TMS", "01-Contexte", "01-Initialisation.feature"))
    result = ci.build_client_init(client_cucumber, std_dir, "clientx", "integration", {"TMS"}, PROFILE)
    assert {k: result[k] for k in ("modules", "fichiers", "patch", "absents")} == {
        "modules": [], "fichiers": [], "patch": "", "absents": ["TMS"]}


def test_init_in_three_steps_from_profile(std_dir, client_cucumber, isolated_cache):
    """Clear du profil, initialisation globale du Standard, puis fichiers du module spécifique dans l'ordre."""
    spec_dir = f"{RES}/files/clientx/WMS/Spec"
    write(os.path.join(client_cucumber, *spec_dir.split("/"), "b-Statuts.csv"), "code\n")
    write(os.path.join(client_cucumber, *spec_dir.split("/"), "a-Documents.xlsm"), "x")
    git(client_cucumber, "add", "-A")
    git(client_cucumber, "commit", "-q", "-m", "fichiers spécifiques")
    profile = {**PROFILE, "spec_files": [f"{spec_dir}/b-Statuts.csv", f"{spec_dir}/a-Documents.xlsm"]}
    result = ci.build_client_init(client_cucumber, std_dir, "clientx", "integration", {"WMS"}, profile)
    assert result["avertissements"] == []
    feature = os.path.join(ci.init_worktree_dir("clientx"), RES, "features", "clientx", "01-Contexte", "WMS",
                           "01-Initialisation.feature")
    with open(feature, encoding="utf-8") as f:
        steps = [line.strip() for line in f if line.strip().startswith(("Given", "And", "|"))]
    assert steps[0] == f'Given {ci.CLEAR_STEP} "{CLEAR}"'
    assert steps[1] == f"And {ci.GLOBAL_INIT_STEP}"
    # Un step par fichier, dans l'ordre du profil, chemin relatif au dossier de données de l'init
    assert steps[2:8] == [f"And {ci.SPEC_STEP}", "| fileName |", "| ../../WMS/Spec/b-Statuts.csv |",
                          f"And {ci.SPEC_STEP}", "| fileName |", "| ../../WMS/Spec/a-Documents.xlsm |"]


def test_init_without_spec_files_warns(std_dir, client_cucumber, isolated_cache):
    result = ci.build_client_init(client_cucumber, std_dir, "clientx", "integration", {"WMS"}, PROFILE)
    assert result["patch"] and "aucun fichier du module spécifique" in result["avertissements"][0]


def test_spec_file_name_relative_to_init_data_folder():
    feature = f"{RES}/features/clientx/01-Contexte/WMS/01-Initialisation.feature"
    assert ci.spec_file_name(feature, f"{RES}/files/clientx/01-Contexte/WMS/x.csv") == "x.csv"
    assert ci.spec_file_name(feature, f"{RES}/files/clientx/Spec/y.xml") == "../../Spec/y.xml"
