"""
Tests existants (client fictif) : les features écrites à la main dans le dépôt client sont reconnues et rattachées
aux tâches, la couverture réelle est « non mesurée » tant qu'aucun log n'est importé (jamais 0 %), les chiffres du
résumé se recoupent, l'action suivante tient compte de l'existant, une feature non rattachée est proposée.
"""
import json
import os
import re

import pytest
from streamlit.testing.v1 import AppTest

from tests.conftest import CLIENT, CLIENT_TAG, git, write
from tools import cucumber_coverage as cov
from tools import cucumber_detection as cd
from tools import cucumber_import
from tools import cucumber_journey as cj
from tools import cucumber_links as cl
from tools import cucumber_profile as cp
from tools import cucumber_tools as ct

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEATURES = f"src/test/resources/cucumber/features/{CLIENT}/"
BY_CLASS = FEATURES + "WMS/Regle_controle.feature"
BY_TICKET = FEATURES + "WMS/FIC-12-Dossier.feature"
ORPHAN = FEATURES + "WMS/Divers.feature"


def spec(spec_id: str, name: str, java: str, **extra) -> dict:
    return {"id": spec_id, "libelle": f"app · {name}", "type": "extension", "nature": "objet métier", "depot": "app",
            "dossier": name, "nb_fichiers": 1, "mots_cles": [], "couverture": "non_couvert",
            "fichiers": [{"repo": "app", "path": f"src/{java}.java", "blob": "b"}], **extra}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path / "ws"))
    os.makedirs(tmp_path / "ws")
    monkeypatch.setattr(cd, "start", lambda *args, **kwargs: False)
    monkeypatch.setattr(cd, "running", lambda *args, **kwargs: False)
    # Dépôt <client>-cucumber : features écrites à la main, hors agent, sur la branche de base
    origin = str(tmp_path / "origin")
    os.makedirs(origin)
    git(origin, "init", "-q", "-b", "integration")
    git(origin, "config", "user.name", "t")
    git(origin, "config", "user.email", "t@example.com")
    write(os.path.join(origin, *BY_CLASS.split("/")),
          f"{CLIENT_TAG}\nFeature: Contrôle de la règle\n  # Source : classe FicRegleControleServiceImpl\n"
          "  Scenario: La règle refuse le dossier\n    When un contrôle\n    Then un refus\n"
          "  Scenario: La règle accepte le dossier\n    When un contrôle\n    Then une acceptation\n")
    write(os.path.join(origin, *BY_TICKET.split("/")),
          f"{CLIENT_TAG} @FIC-12\nFeature: Dossier FIC-12\n  Scenario: Le dossier est validé\n    When x\n    Then y\n")
    write(os.path.join(origin, *ORPHAN.split("/")),
          f"{CLIENT_TAG}\nFeature: Divers\n  Scenario: Étiquette transport imprimée\n    When x\n    Then y\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "tests écrits à la main")
    repo = str(tmp_path / "client" / f"{CLIENT}-cucumber")
    os.makedirs(os.path.dirname(repo))
    import subprocess
    subprocess.run(["git", "clone", "-q", origin, repo], check=True, capture_output=True)
    specifics = [spec("sp-a", "regle", "FicRegleControleServiceImpl"), spec("sp-b", "dossier", "FicDossier"),
                 spec("sp-c", "etiquette", "FicEtiquette", mots_cles=["etiquette", "transport", "imprimee"]),
                 spec("sp-d", "autre", "FicAutre")]
    inventory = {"genere_le": "2026-10-27T10:00:00", "specifiques": specifics}
    ct.save_inventory(CLIENT, inventory)
    cd.save_project(CLIENT, {"client_repo": str(tmp_path / "client"), "client_name": CLIENT, "db_schema": "",
                             "application": "", "std_repo": str(tmp_path / "client")})
    cp.save_user_fields(CLIENT, {"tag_client": CLIENT_TAG, "features_root": FEATURES})
    profile = cp.effective_profile(cp.load_profile(CLIENT))
    document = cl.refresh(CLIENT, repo, profile, inventory, {"sp-b": ["FIC-12"]})
    return {"repo": repo, "document": document, "inventory": inventory}


def import_run(numero: int, results: dict) -> None:
    folder = os.path.join(cucumber_import.imports_dir(CLIENT), str(numero))
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "analyse.json"), "w", encoding="utf-8") as f:
        json.dump({"numero": numero, "non_analyse": "", "resultats": results, "propositions": []}, f)
    with open(os.path.join(cucumber_import.imports_dir(CLIENT), "index.json"), "w", encoding="utf-8") as f:
        json.dump({"imports": [{"numero": numero, "date": "2026-10-26T10:00:00", "fichiers": []}]}, f)


def test_hand_written_features_recognised_and_linked(project):
    document = project["document"]
    assert document["branche"] == "origin/integration" and document["branche_profil"] == ""
    assert {f["chemin"]: f["scenarios"] for f in document["features"]} == {BY_CLASS: 2, BY_TICKET: 1, ORPHAN: 1}
    links = cl.current_links(CLIENT)
    assert links["par_tache"] == {"sp-a": [BY_CLASS], "sp-b": [BY_TICKET]}
    assert links["rattachees"][BY_CLASS][0]["raisons"] == ["classe FicRegleControleServiceImpl citée"]
    assert links["rattachees"][BY_TICKET][0]["raisons"] == ["ticket FIC-12 lié à la tâche (lien vérifié)"]
    # Non rattachée : proposée (mots-clés), à confirmer
    [orphan] = links["non_rattachees"]
    assert orphan["feature"] == ORPHAN and orphan["proposition"]["spec"] == "sp-c"
    assert orphan["proposition"]["force"] == "proposé"


def test_covered_and_not_measured_without_run(project):
    rows = {r["id"]: r for r in cov.coverage_rows(CLIENT, project["inventory"])}
    assert rows["sp-a"]["etat"] == "non_execute" and len(rows["sp-a"]["scenarios"]) == 2
    assert rows["sp-b"]["etat"] == "non_execute" and rows["sp-d"]["etat"] == "non_couvert"
    groups = cov.summary_groups(list(rows.values()))
    assert sum(len(v) for v in groups.values()) == len(rows) == 4
    assert (len(groups["test"]), len(groups["a_couvrir"])) == (2, 2)
    sentence = " ".join(t for t, _ in cov.summary_sentence(list(rows.values()), cov.last_run(CLIENT)))
    assert sentence == ("4 tâches. 2 ont un test (aucun run importé : couverture réelle non mesurée). 2 restent à "
                        "couvrir.")
    # Action suivante : importer d'abord, puis rattacher, puis générer
    links = cl.current_links(CLIENT)
    assert cj.next_action(CLIENT, list(rows.values()), links)["cle"] == "import"
    import_run(1, {f"{BY_CLASS}::La règle refuse le dossier": {"statut": "passe"},
                   f"{BY_CLASS}::La règle accepte le dossier": {"statut": "passe"},
                   f"{BY_TICKET}::Le dossier est validé": {"statut": "echoue", "message": "attendu VALIDE"}})
    rows = cov.coverage_rows(CLIENT, project["inventory"])
    assert {r["id"]: r["etat"] for r in rows}["sp-a"] == "vert"
    assert " ".join(t for t, _ in cov.summary_sentence(rows, cov.last_run(CLIENT))).startswith(
        "4 tâches. 2 ont un test (dont 1 vert au run du 26/10).")
    assert cj.next_action(CLIENT, rows, links)["cle"] == "rattacher"
    cl.confirm(CLIENT, ORPHAN, "sp-c")
    links = cl.current_links(CLIENT)
    assert links["non_rattachees"] == [] and links["par_tache"]["sp-c"] == [ORPHAN]
    rows = cov.coverage_rows(CLIENT, project["inventory"])
    assert cj.next_action(CLIENT, rows, links)["cle"] == "generer"
    assert len(cov.summary_groups(rows)["test"]) == 3


def test_screen_shows_tests_as_covered_and_not_measured(project):
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=120)
    at.session_state[f"cucumber_section_detail_{CLIENT}"] = True  # détail du projet ouvert
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    buttons = [b.label for b in at.button]
    # Zone « Où en est-on » : sans résultat importé, « pas encore vérifié », jamais 0 %
    assert "2 ont un test, pas encore vérifié." in buttons and "2 restent à tester." in buttons
    assert "Importer un résultat Jenkins" in [p.proto.popover.label for p in at.get("popover")]
    # Onglet Cucumber seul (les autres onglets affichent les tickets et la mémoire du poste) ; « 100 % » n'est pas 0 %
    text = " ".join(m.value for m in at.tabs[2].markdown)
    assert "4 fonctionnalités spécifiques." in text
    assert not re.search(r"(?<![\d,.])0 %", " ".join(buttons) + text)
    # Liste : les tests écrits à la main apparaissent « test écrit »
    assert text.count("✍ test écrit") == 2
    # Un nombre cliquable filtre la liste
    [b for b in at.button if b.label.startswith("2 ont un test")][0].click().run()
    assert "Fonctionnalités qui ont un test — afficher tout" in [b.label for b in at.button]
    assert " ".join(m.value for m in at.markdown).count("⚪ à tester") == 0
    # Test non rattaché : proposition, confirmation en un clic
    assert any("Proposition : « Etiquette » — mots-clés communs" in c.value for c in at.caption)
    [b for b in at.button if b.label == "Confirmer" and "cucumber_link_ok_" in (b.key or "")][0].click().run()
    assert cl.current_links(CLIENT)["non_rattachees"] == []
