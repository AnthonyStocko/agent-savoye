"""Initialisation Cucumber d'un client : détection des modules, filtrage, renommage et patch applicable."""
import os
import subprocess

import pytest

from tests.conftest import git, write
from tools import cucumber_init as ci
from tools import cucumber_tools as ct

RES = ci.RESOURCES_DIR
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
    write(os.path.join(repo, RES, "features", "apparel", "WMS", "Content", "SPEC-1.feature"),
          "@WMS @APPAREL\nFeature: Apparel - Spec\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Worktree et patch dans tmp_path, pas dans le workspace."""
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))


def test_client_modules_from_repos_and_inventory(tmp_path):
    root = tmp_path / "client_APPAREL"
    for name in ("wms-content-backend", "odatio-apparel", "apparel-cucumber"):
        os.makedirs(root / name / ".git")
    assert ci.client_modules(str(root), {}) == {"WMS"}
    # Module client dans un dépôt au nom neutre : détecté par le dossier de premier niveau des fichiers
    inventory = {"specifiques": [{"depot": "odatio-apparel", "fichiers": [
        {"repo": "odatio-apparel", "path": "apparel-tms-provider/src/main/java/a/Provider.java"},
        {"repo": "odatio-apparel", "path": "apparel-tls-util/src/main/java/oms/Ignored.java"}]}]}
    assert ci.client_modules(str(root), inventory) == {"WMS", "TMS"}
    assert ci.client_modules("", {}) == set()


def test_std_init_files_by_module(std_dir):
    files = ci.std_init_files(std_dir)
    assert set(files) == {"WMS", "TMS", "OMS"}
    assert files["WMS"][0] == f"{RES}/features/WMS/01-Contexte/01-Initialisation.feature"
    assert all("/WMS/01-Contexte/" in p for p in files["WMS"])
    assert not any("03-Preparation" in p for p in files["WMS"])


def test_wms_only_client_gets_nothing_from_tms_oms(std_dir, client_cucumber, isolated_cache):
    result = ci.build_client_init(client_cucumber, std_dir, "apparel", "integration", {"WMS"})
    assert result["modules"] == ["WMS"]
    assert result["fichiers"] and all("/apparel/WMS/01-Contexte/" in p for p in result["fichiers"])
    changed = git(ci.init_worktree_dir("apparel"), "diff", "--name-only", "integration..HEAD").splitlines()
    assert sorted(changed) == sorted(result["fichiers"])
    assert not any("/TMS/" in p or "/OMS/" in p for p in changed)


def test_client_renaming_in_tags_title_and_paths():
    text = INIT_FEATURE.format(m="TMS").replace("\n", "\r\n")
    adapted = ci.adapt_feature(text, "TMS", "Apparel-2")
    first, second = adapted.splitlines()[:2]
    assert first == "@INIT_APPAREL2_TMS @APPAREL2 @TMS @PERFORMANCE"
    assert second == "Feature: Apparel2 - 01 - Initialisation des données"
    assert "\r\n" in adapted and "Given INIT- Purge de l'environnement" in adapted
    # Le dossier client (segment de package Java) précède le module, comme les features client existantes
    assert ci.client_path(f"{RES}/files/TMS/01-Contexte/a.csv", "Apparel-2") == f"{RES}/files/apparel2/TMS/01-Contexte/a.csv"


def test_patch_applies_on_base(std_dir, client_cucumber, isolated_cache, tmp_path):
    result = ci.build_client_init(client_cucumber, std_dir, "apparel", "integration", {"WMS", "OMS"})
    assert result["patch"] == ct.init_patch_path("apparel") and os.path.exists(result["patch"])
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
    csv = os.path.join(clone, RES, "files", "apparel", "WMS", "01-Contexte", "01-WMSSiteDraft.csv")
    xlsm = os.path.join(clone, RES, "files", "apparel", "OMS", "01-Contexte", "02-OMSBayDraft.xlsm")
    with open(csv, "rb") as f:
        assert f.read() == LATIN1_CSV
    with open(xlsm, "rb") as f:
        assert f.read() == XLSM
    with open(os.path.join(clone, RES, "features", "apparel", "WMS", "01-Contexte", "01-Initialisation.feature"),
              encoding="utf-8") as f:
        assert f.readline().strip() == "@INIT_APPAREL_WMS @APPAREL @WMS @PERFORMANCE"

    # Deuxième génération : rien de nouveau par rapport à une base qui contient déjà l'initialisation
    git(client_cucumber, "pull", "-q", clone, "integration")
    again = ci.build_client_init(client_cucumber, std_dir, "apparel", "integration", {"WMS", "OMS"})
    assert again["patch"] == "" and not os.path.exists(ct.init_patch_path("apparel"))


def test_unknown_std_module_is_reported(std_dir, client_cucumber, isolated_cache):
    os.remove(os.path.join(std_dir, RES, "features", "TMS", "01-Contexte", "01-Initialisation.feature"))
    result = ci.build_client_init(client_cucumber, std_dir, "apparel", "integration", {"TMS"})
    assert result == {"modules": [], "fichiers": [], "patch": "", "absents": ["TMS"]}
