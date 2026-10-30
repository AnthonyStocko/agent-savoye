"""
Onglet Cucumber simplifié (client fictif), sans ouvrir le mode expert :
- comprendre l'état de la couverture en lisant le résumé « Où en est-on » ;
- choisir deux fonctionnalités et écrire leurs tests (« Écrire les tests de la sélection ») ;
- voir le résultat de chacune, puis voir et télécharger les modifications ;
- importer un résultat Jenkins et voir les fonctionnalités passer « vérifié, vert » ou « en échec » ;
- aucun mot du vocabulaire interne (lot, worktree, dry-run, glue, runner, inventaire, profil, catalogue, tokens,
  patch) dans les libellés affichés hors du mode expert et du « Détail technique » replié.
"""
import json
import os
import re
import subprocess
from datetime import datetime

import pytest
from streamlit.testing.v1 import AppTest

from tests.conftest import CLIENT, CLIENT_TAG, git, write
from tools import cucumber_detection as cd
from tools import cucumber_import
from tools import cucumber_profile as cp
from tools import cucumber_results as cr
from tools import cucumber_tools as ct

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEATURES = f"src/test/resources/cucumber/features/{CLIENT}/"
FORBIDDEN = re.compile(r"(?i)\b(lots?|worktrees?|dry-?runs?|glue|runners?|inventaires?|profils?|catalogues?|tokens?|"
                       r"patch(es)?)\b")
REAL_POPEN = subprocess.Popen


def spec(spec_id: str, name: str) -> dict:
    return {"id": spec_id, "libelle": f"app · {name}", "type": "extension", "nature": "objet métier", "depot": "app",
            "dossier": name, "nb_fichiers": 1, "mots_cles": [], "couverture": "non_couvert",
            "fichiers": [{"repo": "app", "path": f"wms-{name}/src/{name}.java", "blob": "b-" + spec_id}]}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path / "ws"))
    os.makedirs(tmp_path / "ws")
    monkeypatch.setattr(cd, "start", lambda *args, **kwargs: False)
    monkeypatch.setattr(cd, "running", lambda *args, **kwargs: False)
    repo = str(tmp_path / "client")
    os.makedirs(repo)
    cd.save_project(CLIENT, {"client_repo": repo, "client_name": CLIENT, "db_schema": "", "application": "",
                             "std_repo": repo})
    ct.save_inventory(CLIENT, {"genere_le": "2026-10-27T10:00:00", "depot_client": repo, "specifiques": [
        spec("sp-a", "reception"), spec("sp-b", "colisage"), spec("sp-c", "expedition")]})
    cp.save_user_fields(CLIENT, {"tag_client": CLIENT_TAG, "features_root": FEATURES})
    worktree = ct.worktree_dir(CLIENT)
    os.makedirs(worktree)
    git(worktree, "init", "-q", "-b", "integration")
    git(worktree, "config", "user.name", "t")
    git(worktree, "config", "user.email", "t@example.com")
    write(os.path.join(worktree, "README.md"), "base\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "base")
    ct.save_state(CLIENT, {"client": CLIENT, "base": git(worktree, "rev-parse", "HEAD"), "lots": []})
    return {"worktree": worktree}


class FakeWriter:
    """main.py simulé : écrit un test par fonctionnalité demandée (commit dans l'espace de travail, résultats)."""
    def __new__(cls, cmd, **kwargs):
        if "main.py" not in cmd:
            return REAL_POPEN(cmd, **kwargs)
        return super().__new__(cls)

    def __init__(self, cmd, **kwargs):
        ids = cmd[cmd.index("--specifics") + 1].split(",")
        worktree, state = ct.worktree_dir(CLIENT), ct.load_state(CLIENT)
        results = []
        for spec_id in ids:
            feature = f"{FEATURES}WMS/{spec_id}.feature"
            write(os.path.join(worktree, *feature.split("/")),
                  f"{CLIENT_TAG}\nFeature: Test de {spec_id}\n  Scenario: Effet de {spec_id}\n    When x\n    Then y\n")
            results.append({"id": spec_id, "statut": "couvert", "fichiers": [feature],
                            "scenarios": [{"nom": f"Test de {spec_id} / Effet de {spec_id}", "fichier": feature}]})
        git(worktree, "add", "-A")
        git(worktree, "commit", "-qm", "tests")
        lot = {"numero": len(state["lots"]) + 1, "ids": ids, "statut": "termine", "commit": True,
               "fin": datetime.now().isoformat(timespec="seconds"), "fichiers": [r["fichiers"][0] for r in results], "resultats": results}
        state["lots"].append(lot)
        cr.record_lot(state, lot)
        ct.save_state(CLIENT, state)
        self.stdout = iter([f"[LOT 1/1] {' ; '.join('app · ' + i for i in ids)}\n"])
        self.returncode = 0

    def wait(self):
        return 0


def visible_texts(at) -> list:
    """Libellés affichés hors du mode expert et des « Détail technique » repliés (contenu de fichiers exclu)."""
    texts = []

    def walk(node):
        proto = getattr(node, "proto", None)
        if getattr(node, "type", "") == "expandable" and proto is not None and \
                proto.expandable.label == "Détail technique":
            return
        if getattr(node, "type", "") == "code":
            return
        for attribute in ("label", "value", "placeholder", "help"):
            value = getattr(node, attribute, None)
            if isinstance(value, str):
                texts.append(value)
        if getattr(node, "type", "") == "popover" and proto is not None:
            texts.append(proto.popover.label)
        for option in getattr(node, "options", None) or []:
            texts.append(str(option))
        for child in (getattr(node, "children", None) or {}).values():
            walk(child)
    walk(at.tabs[2] if len(at.tabs) > 2 else at.main)
    return texts


def open_app() -> AppTest:
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_no_internal_vocabulary_outside_expert_mode(project):
    at = open_app()
    found = sorted({m.group(0) for t in visible_texts(at) for m in FORBIDDEN.finditer(t)})
    assert found == [], found


def test_full_path_without_expert_mode(project, monkeypatch):
    """Écran minimal : bouton « Lancer », tâches choisies, résultat Jenkins, modifications à appliquer."""
    at = open_app()
    markdown = " ".join(m.value for m in at.markdown)
    assert "3 fonctionnalités spécifiques." in markdown and "Aucune n'a encore de test." in markdown
    # Bouton 1 : rien d'écrit encore → première couverture des 3 tâches
    assert "Écrire la première couverture (3)" in [b.label for b in at.button]
    # Bouton 2 : deux tâches choisies dans la liste déroulante
    at.multiselect(key=f"cucumber_simple_taches_{CLIENT}").set_value(["sp-a", "sp-b"]).run()
    assert "Écrire les tests (2)" in [b.label for b in at.button]
    monkeypatch.setattr(subprocess, "Popen", FakeWriter)
    [b for b in at.button if b.label == "Écrire les tests (2)"][0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Écriture terminée" in s.value for s in at.success)
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)
    at = open_app()
    markdown = " ".join(m.value for m in at.markdown)
    assert "2 ont un test, pas encore vérifié." in markdown
    # Tests déjà écrits, aucun résultat importé : rien à lancer ; la tâche restante est proposée au bouton 2
    go = [b for b in at.button if b.key == f"cucumber_simple_go_{CLIENT}"][0]
    assert go.disabled
    assert len(at.multiselect(key=f"cucumber_simple_taches_{CLIENT}").options) == 1
    # Résultat Jenkins importé avec un échec corrigeable → le bouton 1 propose la correction
    folder = os.path.join(cucumber_import.imports_dir(CLIENT), "1")
    os.makedirs(folder)
    failing = f"{FEATURES}WMS/sp-b.feature::Effet de sp-b"
    with open(os.path.join(folder, "analyse.json"), "w", encoding="utf-8") as f:
        json.dump({"numero": 1, "non_analyse": "", "propositions": [{"spec": "sp-b", "scenarios": [failing]}],
                   "resultats": {f"{FEATURES}WMS/sp-a.feature::Effet de sp-a": {"statut": "passe"},
                                 failing: {"statut": "echoue", "message": "attendu X"}}}, f)
    with open(os.path.join(cucumber_import.imports_dir(CLIENT), "index.json"), "w", encoding="utf-8") as f:
        json.dump({"imports": [{"numero": 1, "date": "2026-10-27T12:00:00", "fichiers": []}]}, f)
    at = open_app()
    assert "Corriger les 1 test(s) en échec" in [b.label for b in at.button]
    markdown = " ".join(m.value for m in at.markdown)
    assert "dont 1 vérifié vert le 27/10" in markdown and "et 1 en échec." in markdown
    # Modifications à appliquer : téléchargeables sur l'écran minimal
    found = sorted({m.group(0) for t in visible_texts(at) for m in FORBIDDEN.finditer(t)})
    assert found == [], found


def test_imported_console_with_failures_gives_one_result_per_failure(project):
    """
    Console Jenkins réelle (extraits du jeu de référence) avec trois échecs, importée comme depuis l'écran : un
    résultat par échec sur l'écran simple (catégorie, cause, action), les modifications téléchargeables pour celui
    qui en a dès l'import (désactivation proposée d'un défaut du produit), sans vocabulaire interne.
    """
    reference = os.path.join(ROOT, "tests", "reference")
    names = ["02-champ-controle-null", "09-additional-properties", "10-liste-choix-vide-studio"]
    console = "\n".join(open(os.path.join(reference, f"{n}.console.txt"), encoding="utf-8").read().replace(
        "/features/client/", f"/features/{CLIENT}/") for n in names)
    worktree = project["worktree"]
    # Features des scénarios en échec présentes dans le dépôt de l'agent
    for title, feature in re.findall(r"Scenario: (.+?)\s+# (src/\S+?\.feature):\d+", console):
        write(os.path.join(worktree, *feature.split("/")),
              f"Feature: F\n\n  {CLIENT_TAG}\n  Scenario: {title}\n    When x\n    Then y\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "features")
    imported = cucumber_import.import_files(CLIENT, {"console.txt": console.encode("utf-8")})
    analysis = cucumber_import.analyse(CLIENT, imported["numero"], ct.load_state(CLIENT), worktree)
    failures = analysis["echecs"]
    assert [(e["categorie"], e["nature"]) for e in failures] == [
        ("correction", "champ_non_renvoye"), ("environnement", "version"), ("produit", "liste_choix_vide")]
    assert all(e["cause"] and e["action"] for e in failures)
    at = open_app()
    markdown = " ".join(m.value for m in at.markdown)
    assert f"Échecs du run importé n° {imported['numero']} : 3" in markdown
    assert all(e["titre"] in markdown for e in failures)
    captions = " ".join(c.value for c in at.caption)
    assert all(e["cause"][:40] in captions for e in failures)
    downloads = [b for b in at.get("download_button") if b.proto.label == "Télécharger les modifications"]
    assert len(downloads) == 1                                           # désactivation proposée du défaut produit
    assert "Modifications écrites par « Corriger »" in captions           # la correction : par le bouton 1
    found = sorted({m.group(0) for t in visible_texts(at) for m in FORBIDDEN.finditer(t)})
    assert found == [], found
