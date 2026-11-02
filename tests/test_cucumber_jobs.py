"""
Jobs KIS de l'init (client fictif) : chaque job désactivé, activé, modifié ou lancé est prouvé présent chez le client
(base, code de son application, création plus haut dans l'init), la classe de chaque job créé aussi (application ou
Standard à sa version) ; un job non prouvé est commenté sous TODO-JOB, jamais supprimé.
"""
import os

from tests.conftest import CLIENT, git, write
from tools import cucumber_jobs as cj

INIT = """@INIT_CLIENTX_WMS
Feature: Init

  Scenario: Intégration des données
    Given KIS- Désactivation du job
      | name              |
      | job-du-standard   |
      | clean-up-tmp      |
    And KIS- Activation du job
      | name              |
      | job-du-standard   |
    And KIS- Création du job
      | name           | timeout | type                         |
      | Nouveau job    | 60      | com.example.client.JobClient |
      | Job inconnu    | 60      | com.example.absent.JobAbsent |
    And KIS- Lancement du job
      | name        |
      | Nouveau job |
    And INIT- Initialisation globale par fichiers via l'écran
"""


def test_usages_read_names_classes_and_lines():
    found = cj.usages(INIT)
    assert [(u["step"].split("- ")[1], u["nom"], u["ligne"]) for u in found] == [
        ("Désactivation du job", "job-du-standard", 7), ("Désactivation du job", "clean-up-tmp", 8),
        ("Activation du job", "job-du-standard", 11), ("Création du job", "Nouveau job", 14),
        ("Création du job", "Job inconnu", 15), ("Lancement du job", "Nouveau job", 18)]
    assert found[3]["classe"] == "com.example.client.JobClient" and found[3]["preuve"] == "classe"


def test_proof_sources_and_comments(tmp_path):
    # Application du client : la classe du job créé y est présente
    app = str(tmp_path / "client" / f"{CLIENT}-odatio-app")
    os.makedirs(app)
    git(app, "init", "-q", "-b", "integration")
    git(app, "config", "user.name", "t")
    git(app, "config", "user.email", "t@example.com")
    write(os.path.join(app, "core", "src", "main", "java", "com", "example", "client", "JobClient.java"), "class X {}")
    git(app, "add", "-A")
    git(app, "commit", "-qm", "app")
    items = [{**u, "fichier": "init.feature"} for u in cj.usages(INIT)]
    code = cj.from_code(str(tmp_path / "client"), [], ["com.example.client.JobClient", "com.example.absent.JobAbsent"])
    known = {"noms": {"clean-up-tmp": "base clientxd"}, "classes": code["classes"]}
    ok, missing = cj.proven(items, known)
    assert {(m["nom"], m["preuve"]) for m in missing} == {("job-du-standard", "nom"), ("Job inconnu", "classe")}
    # Lancement d'un job créé plus haut par l'init : prouvé par la création
    launch = next(o for o in ok if o["step"] == "KIS- Lancement du job")
    assert launch["source"] == "créé par l'init (l. 14)"

    text = cj.comment_unproven(INIT, missing)
    lines = text.splitlines()
    # Ligne retirée d'une table qui garde des lignes : seule la ligne est commentée
    assert "      # TODO-JOB: job « job-du-standard » absent chez le client" in text
    assert "      # | job-du-standard   |" in lines and "      | clean-up-tmp      |" in lines
    # Table sans plus aucune ligne : le step entier est commenté, rien n'est supprimé
    assert "    # And KIS- Activation du job" in lines and "    # | name              |" in lines
    assert "      | Nouveau job    | 60      | com.example.client.JobClient |" in lines
    assert "      # | Job inconnu    | 60      | com.example.absent.JobAbsent |" in lines
    assert len([line for line in lines if line.strip()]) > len([line for line in INIT.splitlines() if line.strip()])
    # Relu après commentaire : plus aucun usage non prouvé
    assert not cj.proven([{**u, "fichier": "init.feature"} for u in cj.usages(text)], known)[1]
