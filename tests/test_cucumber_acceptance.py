"""
Critères d'acceptation de la stabilisation (client fictif, de bout en bout dans l'IHM rendue sans navigateur) :
a. tâche avec logique → test écrit, diff visible, patch téléchargeable ;
b. tâche couverte par une autre → « couverte par … » avec lien, jamais « non couverte » ;
c. déclaration sans logique → « sans objet », sans lot Claude ;
d. Maven indisponible → « Génération interrompue », aucun lot lancé, état inchangé ;
e. .env avec une ligne invalide → message de démarrage avec le numéro de ligne, aucune trace dans les cartes ;
f. scénarios anciens sans tag client → correction proposée en un clic (puis appliquée).
"""
import importlib
import io
import os
import subprocess
import sys

import pytest
from streamlit.testing.v1 import AppTest

from tests.conftest import CLIENT, CLIENT_TAG, git, write
from tools import cucumber_coverage as cov
from tools import cucumber_detection as cd
from tools import cucumber_preflight as cpf
from tools import cucumber_profile as cp
from tools import cucumber_results as cr
from tools import cucumber_tools as ct
from tools import env_tools

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = "src/test/resources/cucumber"
FEATURES = f"{RES}/features/{CLIENT}/"
FEATURE_A = FEATURES + "WMS/Regle.feature"
OLD_FEATURE = FEATURES + "WMS/Ancienne.feature"
EVENT = "fic-wms-goodsin-event-controle-dossier"
LISTENER = "fic-wms-goodsin-listener-controle-dossier"
STANDARD_PROCESS = "wms-goodsin-process-standard"


@pytest.fixture
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


def spec(spec_id: str, name: str, **extra) -> dict:
    return {"id": spec_id, "libelle": f"app · {name}", "type": "extension", "nature": "objet métier", "depot": "app",
            "dossier": name, "nb_fichiers": 1, "mots_cles": [], "couverture": "non_couvert",
            "fichiers": [{"repo": "app", "path": f"{name}/Regle.java", "blob": "b-" + spec_id}], **extra}


@pytest.fixture
def project(tmp_path, monkeypatch):
    """
    Projet fictif généré une fois : sp-a (test écrit), sp-b (couverte par sp-c, pas encore testée), sp-c, sp-d
    (déclaration d'événement sans logique, process du Standard : tri sans lot), sp-e (jamais générée).
    """
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path / "ws"))
    os.makedirs(tmp_path / "ws")
    monkeypatch.setattr(cd, "start", lambda *args, **kwargs: False)
    monkeypatch.setattr(cd, "running", lambda *args, **kwargs: False)
    repo = str(tmp_path / "client")
    os.makedirs(repo)
    cd.save_project(CLIENT, {"client_repo": repo, "client_name": CLIENT, "db_schema": "", "application": "",
                             "std_repo": repo})
    event_xml = str(tmp_path / "copies" / EVENT / "customized.xml")
    write(event_xml, f'<execution-event-definition xmlns="x" name="{EVENT}"><variable name="id"/>'
                     f'</execution-event-definition>\n')
    listener_xml = str(tmp_path / "copies" / LISTENER / "customized.xml")
    write(listener_xml, f'<execution-event-listener-definition xmlns="x" on-event="a/{EVENT}" '
                        f'execute="a/{STANDARD_PROCESS}"><mappings><input>$event.id</input></mappings>'
                        f'</execution-event-listener-definition>\n')
    studio = {"type": "extension", "nature": "processus", "depot": "studio", "nb_fichiers": 1}
    specifics = [spec("sp-a", "regle"), spec("sp-b", "dossier"), spec("sp-c", "controle"), spec("sp-e", "autre"),
                 {**studio, "id": "sp-d", "libelle": f"studio · {EVENT}", "dossier": EVENT, "mots_cles": [],
                  "fichiers": [{"repo": "studio", "path": f"{EVENT}/customized.xml", "local": event_xml}]},
                 {**studio, "id": "sp-l", "libelle": f"studio · {LISTENER}", "dossier": LISTENER, "mots_cles": [],
                  "fichiers": [{"repo": "studio", "path": f"{LISTENER}/customized.xml", "local": listener_xml}]}]
    ct.save_inventory(CLIENT, {"genere_le": "2026-10-27T10:00:00", "specifiques": specifics})
    cp.save_user_fields(CLIENT, {"tag_client": CLIENT_TAG, "features_root": FEATURES})
    # Worktree de la génération : base, puis le lot 1 qui écrit le test de sp-a
    worktree = ct.worktree_dir(CLIENT)
    os.makedirs(worktree)
    git(worktree, "init", "-q", "-b", "integration")
    git(worktree, "config", "user.name", "t")
    git(worktree, "config", "user.email", "t@example.com")
    write(os.path.join(worktree, "README.md"), "base\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "base")
    base = git(worktree, "rev-parse", "HEAD")
    write(os.path.join(worktree, *FEATURE_A.split("/")),
          f"{CLIENT_TAG}\nFeature: Règle\n  Scenario: La règle s'applique\n    When une action\n    Then un effet\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "Cucumber coverage - lot 1")
    # Génération : sp-a écrit, sp-b couverte par sp-c ; tri sans token pour la déclaration sp-d et le listener sp-l
    specifics_by_id = {s["id"]: s for s in specifics}
    remaining, sorted_out = ct.presort(["sp-d", "sp-l"], specifics_by_id)
    assert remaining == []                                       # (c) aucun lot Claude pour eux
    lot = {"numero": 1, "ids": ["sp-a", "sp-b", "sp-c"], "statut": "termine", "commit": True,
           "fin": "2026-10-27T10:30:00", "fichiers": [FEATURE_A], "resultats": [
               {"id": "sp-a", "statut": "couvert", "fichiers": [FEATURE_A], "classes_java": [],
                "scenarios": [{"nom": "Règle / La règle s'applique", "fichier": FEATURE_A}]},
               {"id": "sp-b", "statut": "couvert_indirectement", "couvert_par": "sp-c",
                "resume": "Le contrôle du dossier couvre cette tâche."},
               {"id": "sp-c", "statut": "non_couvert", "raison": "Pas encore écrit.", "questions": [
                   "Quel code de dossier utiliser pour le scénario de contrôle ?"]}]}
    state = {"client": CLIENT, "base": base, "lots": [lot, ct.presort_lot(sorted_out, 2)]}
    cr.record_lot(state, state["lots"][0])
    cr.record_lot(state, state["lots"][1])
    ct.save_state(CLIENT, state)
    return {"tmp": tmp_path, "worktree": worktree, "base": base}


def app_at(card: str = "", step: str = "couverture") -> AppTest:
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=120)
    at.session_state[f"cucumber_step_{CLIENT}"] = step
    if card:
        at.session_state[f"cucumber_card_{CLIENT}"] = card
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def text_of(at) -> str:
    return " ".join([m.value for m in at.markdown] + [c.value for c in at.caption] + [w.value for w in at.warning]
                    + [e.value for e in at.error] + [s.value for s in at.success])


def no_raw_output(at) -> None:
    """Aucune sortie brute dans la page hors du journal technique replié (traces, avertissements de bibliothèques)."""
    shown = text_of(at) + " ".join(c.value for c in at.code if "=====" not in c.value)
    for raw in ("Traceback", "python-dotenv could not parse", "[CUCUMBER ENVIRONNEMENT]", "[CUCUMBER DÉTECTION]",
                "[LOT "):
        assert raw not in shown


# ---------------------------------------------------------------------------
# a. Test écrit : diff visible, patch téléchargeable
# ---------------------------------------------------------------------------

def test_a_task_with_logic_test_written_diff_and_patch(project):
    rows = {r["id"]: r for r in cov.coverage_rows(CLIENT, ct.load_inventory(CLIENT))}
    assert rows["sp-a"]["etat"] == "non_execute" and rows["sp-a"]["generation"]["type"] == "test_ecrit"
    at = app_at("sp-a")
    assert "Test écrit" in text_of(at) and "1 scénario(s) dans 1 feature(s)" in text_of(at)
    downloads = [b for b in at.get("download_button") if str(b.proto.label) == "Télécharger"]
    assert downloads
    [t for t in at.toggle if t.label == "Voir les modifications"][0].set_value(True).run()
    diff = [c.value for c in at.code if FEATURE_A in c.value]
    assert diff and "+Feature: Règle" in diff[0]
    assert any("Patch global de la livraison" in str(b.proto.label) for b in at.get("download_button")) is False
    no_raw_output(at)


# ---------------------------------------------------------------------------
# b. Couverte par une autre tâche ; c. déclaration sans logique
# ---------------------------------------------------------------------------

def test_b_covered_by_another_task_never_not_covered(project):
    rows = {r["id"]: r for r in cov.coverage_rows(CLIENT, ct.load_inventory(CLIENT))}
    assert rows["sp-b"]["generation"]["type"] == "couverte_par"
    assert rows["sp-b"]["etat"] == "en_attente" and rows["sp-b"]["couvert_par"] == "sp-c"
    at = app_at("sp-b")
    assert "Pas de test : couverte par une autre fonctionnalité" in text_of(at)
    # Une seule action sur une carte en attente : ouvrir la tâche qui la couvre
    opener = [b for b in at.button if (b.key or "").startswith(("cucumber_card_open_", "cucumber_result_open_"))]
    assert len(opener) == 1 and opener[0].label.startswith("Ouvrir « ")
    opener[0].click().run()
    assert at.session_state[f"cucumber_card_{CLIENT}"] == "sp-c"
    no_raw_output(at)


def test_c_declaration_without_logic_without_claude_lot(project):
    rows = {r["id"]: r for r in cov.coverage_rows(CLIENT, ct.load_inventory(CLIENT))}
    assert rows["sp-l"]["generation"]["type"] == "sans_logique" and rows["sp-l"]["etat"] == "sans_objet"
    state = ct.load_state(CLIENT)
    assert state["lots"][1]["tri"] and not state["lots"][1].get("tokens")      # lot du tri : aucun appel Claude
    at = app_at("sp-l")
    assert "Pas de test : sans logique propre" in text_of(at)
    no_raw_output(at)


# ---------------------------------------------------------------------------
# d. Maven indisponible : génération interrompue, aucun lot lancé, état inchangé
# ---------------------------------------------------------------------------

REAL_POPEN = subprocess.Popen


class FakePopen:
    """main.py simulé : contrôle d'environnement en échec (Maven indisponible), aucun lot, code 3. Les autres
    commandes (git) restent réelles."""
    def __new__(cls, cmd, **kwargs):
        if "main.py" not in cmd:
            return REAL_POPEN(cmd, **kwargs)
        return super().__new__(cls)

    def __init__(self, cmd, **kwargs):
        problem = cpf.maven_problem("commande-maven-introuvable -q test-compile")
        self.stdout = iter([f"[CUCUMBER ENVIRONNEMENT] ❌ {problem}\n",
                            "[CUCUMBER] ⛔ Génération interrompue avant tout lot : 1 problème(s) d'environnement\n"])
        self.returncode = 3

    def wait(self):
        return self.returncode


def test_d_maven_unavailable_interrupts_without_lot(project, monkeypatch):
    problem = cpf.maven_problem("commande-maven-introuvable -q test-compile")
    assert problem.startswith("Maven indisponible : la commande « commande-maven-introuvable » est introuvable")
    before = ct.load_state(CLIENT)["lots"]
    etat_before = {r["id"]: r["etat"] for r in cov.coverage_rows(CLIENT, ct.load_inventory(CLIENT))}
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    at = app_at("sp-e")
    [b for b in at.button if (b.key or "").startswith("cucumber_card_generate_")][0].click().run()
    assert any("L'écriture des tests n'a pas pu commencer" in e.value and "Maven indisponible" in e.value
               for e in at.error)
    state = ct.load_state(CLIENT)
    assert state["lots"] == before                                    # aucun lot lancé
    assert state["generations"]["sp-e"]["type"] == "interrompue"
    assert state["generations"]["sp-e"]["message"].startswith("Maven indisponible")
    rows = {r["id"]: r for r in cov.coverage_rows(CLIENT, ct.load_inventory(CLIENT))}
    assert {i: r["etat"] for i, r in rows.items()} == etat_before     # état des tâches inchangé
    at = app_at("sp-e")
    assert any("Interrompu" in w.value and "L'état de la fonctionnalité n'a pas changé" in w.value
               for w in at.warning)
    no_raw_output(at)
    # Aucune génération ne se termine sans résultat enregistré
    assert cr.missing_results(state, ["sp-e"], "2026-01-01T00:00:00") == []


def test_d_preflight_blocked_clone_is_never_modified(tmp_path):
    root = str(tmp_path / "client")
    repo = os.path.join(root, "app")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "integration")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.com")
    write(os.path.join(repo, "a.txt"), "1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-q", "-b", "travail")
    write(os.path.join(repo, "a.txt"), "travail\n")
    git(repo, "commit", "-qam", "travail")
    git(repo, "checkout", "-q", "integration")
    write(os.path.join(repo, "a.txt"), "integration\n")
    git(repo, "commit", "-qam", "integration")
    subprocess.run(["git", "-C", repo, "merge", "travail"], capture_output=True)    # fusion en conflit
    status = git(repo, "status", "--porcelain")
    problems = cpf.check(root, "")
    assert len(problems) == 1 and problems[0].startswith("Dépôt app : fusion inachevée sur la branche integration")
    assert "l'agent n'y touche pas" in problems[0]
    assert git(repo, "status", "--porcelain") == status                            # rien modifié


# ---------------------------------------------------------------------------
# e. .env avec une ligne invalide
# ---------------------------------------------------------------------------

def test_e_invalid_env_line_message_with_line_number(project, monkeypatch, tmp_path):
    workdir = tmp_path / "poste"
    workdir.mkdir()
    (workdir / ".env").write_text("A=1\n   texte collé par erreur\nB='\\\\\\\\serveur\\\\partage'\n", encoding="utf-8")
    monkeypatch.chdir(workdir)
    problems = env_tools.check_env(".env")
    assert [p["ligne"] for p in problems] == [2]
    at = app_at("sp-a")
    assert any("ligne(s) 2" in w.value and "Fichier .env" in w.value for w in at.warning)
    no_raw_output(at)
    # Neutralisation : mise en commentaire, copie gardée, plus aucune ligne en défaut
    assert env_tools.neutralize_invalid_lines(".env") == 1 and os.path.exists(".env.bak")
    assert env_tools.check_env(".env") == []


def test_e_single_env_writer_one_line_and_unc_paths(tmp_path):
    path = str(tmp_path / ".env")
    write(path, "# commentaire\nA=ancien\n")
    unc = "\\\\serveur\\partage\\dossier"
    env_tools.write_env(path, {"A": "nouveau", "UNC": unc, "TEXTE": "ligne 1\nligne 2", "Q": 'a "b" # c'})
    from dotenv import dotenv_values
    values = dotenv_values(path)
    assert values["A"] == "nouveau" and values["UNC"] == unc and values["TEXTE"] == "ligne 1 ligne 2"
    assert values["Q"] == 'a "b" # c' and env_tools.check_env(path) == []
    assert len(open(path, encoding="utf-8").read().splitlines()) == 5


# ---------------------------------------------------------------------------
# f. Scénarios anciens sans tag client : correction proposée en un clic
# ---------------------------------------------------------------------------

def test_f_old_scenarios_without_client_tag(project, main_module):
    worktree = project["worktree"]
    write(os.path.join(worktree, *OLD_FEATURE.split("/")),
          "Feature: Ancienne\n  Scenario: écrit avant la règle\n    Given x\n\n"
          f"  {ct.a_revoir_tag()}\n  Scenario: désactivé\n    Given y\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", "ancienne feature")
    profile = cp.effective_profile(cp.load_profile(CLIENT))
    state = ct.load_state(CLIENT)
    # Contrôle de livraison : règle rétroactive, la livraison n'échoue pas
    assert main_module.check_cucumber_selection(CLIENT, worktree, "", profile, state, "")
    assert len(ct.load_state(CLIENT)["selection"]["retroactif"]) == 1
    at = app_at()
    # Bandeau « À faire » : une phrase et un bouton
    assert [b for b in at.button if b.label == "Corriger automatiquement"]
    assert any("1 ancien test ne porte pas le tag du client" in m.value for m in at.markdown)
    # Le clic lance --fix-client-tags : ajout déterministe (scénario désactivé exclu), commit, nouveau contrôle
    assert main_module.fix_client_tags(CLIENT, "") == 0
    text = open(os.path.join(worktree, *OLD_FEATURE.split("/")), encoding="utf-8").read()
    assert f"  {CLIENT_TAG}\n  Scenario: écrit avant la règle" in text
    assert f"{ct.a_revoir_tag()} {CLIENT_TAG}" not in text and text.count(CLIENT_TAG) == 1
    assert ct.load_state(CLIENT)["selection"]["retroactif"] == []
    assert "client tag" in git(worktree, "log", "-1", "--format=%s")


def test_d_interruption_gives_a_single_action(project):
    cause = cpf.maven_problem("commande-maven-introuvable -q test-compile")
    cr.record_interruption(CLIENT, ["sp-e"], cause, "relancer")
    generation = ct.load_state(CLIENT)["generations"]["sp-e"]
    assert generation["action"] == "" and generation["message"].count("Action :") == 1
    cr.record_interruption(CLIENT, ["sp-e"], "erreur de l'agent pendant le lot", "relancer la génération")
    assert ct.load_state(CLIENT)["generations"]["sp-e"]["action"] == "relancer la génération"


def test_generation_section_with_init_patch_and_patch_renders_once(project):
    """Patch d'initialisation et patch livré présents, section Génération ouverte : aucun élément en double."""
    write(ct.init_patch_path(CLIENT), "diff --git a/x b/x\n")
    write(ct.patch_path(CLIENT), "diff --git a/y b/y\n")
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=120)
    at.session_state[f"cucumber_section_expert_{CLIENT}"] = True
    at.session_state[f"cucumber_section_expert_{CLIENT}_vu"] = True
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    labels = [str(b.proto.label) for b in at.get("download_button")]
    assert labels.count("Télécharger le patch d'initialisation") == 1
