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
    assert "      # TODO-JOB: job « job-du-standard » : aucune définition chez le client" in text
    assert "      # | job-du-standard   |" in lines and "      | clean-up-tmp      |" in lines
    # Table sans plus aucune ligne : le step entier est commenté, rien n'est supprimé
    assert "    # And KIS- Activation du job" in lines and "    # | name              |" in lines
    assert "      | Nouveau job    | 60      | com.example.client.JobClient |" in lines
    assert "      # | Job inconnu    | 60      | com.example.absent.JobAbsent |" in lines
    assert len([line for line in lines if line.strip()]) > len([line for line in INIT.splitlines() if line.strip()])
    # Relu après commentaire : plus aucun usage non prouvé
    assert not cj.proven([{**u, "fichier": "init.feature"} for u in cj.usages(text)], known)[1]


ORDER = """@INIT_CLIENTX_WMS
Feature: Init

  Scenario: Intégration des données
    Given KIS- Désactivation du job
      | name              |
      | clean-up-tmp      |
      | Job créé plus bas |
    And INIT- Initialisation globale par fichiers via l'écran
    And MESS- Configuration du partenaire "PART_FICHIER"
    And MESS- Configuration du partenaire "PART_INCONNU"

  Scenario: Initialisation du paramétrage
    Given KIS- Création du job
      | name              | type                         |
      | Job créé plus bas | com.example.client.JobClient |
    And KIS- Activation du job
      | name              |
      | Job créé plus bas |
"""


def test_job_referenced_before_its_creation_is_removed_even_if_defined_elsewhere():
    items = [{**u, "fichier": "init.feature"} for u in cj.usages(ORDER)]
    known = {"noms": {"clean-up-tmp": "socle KIS kis-io", "Job créé plus bas": "Standard x"},
             "classes": {"com.example.client.JobClient": "app"}}
    ok, missing = cj.proven(items, known)
    assert [(m["nom"], m["ligne"]) for m in missing] == [("Job créé plus bas", 8)]
    assert "avant sa création par l'init (l. 16)" in missing[0]["raison"]
    activation = next(o for o in ok if o["step"] == "KIS- Activation du job")
    assert activation["source"] == "créé par l'init (l. 16)"


def test_definitions_from_kis_socle_and_deployed_standard_only(tmp_path):
    std = str(tmp_path / "std")
    for repo, module, deployed_bundle in (("wes-backend", "wes-core", False), ("tariff-workflow", "tariff-event", True)):
        path = os.path.join(std, repo)
        os.makedirs(path)
        git(path, "init", "-q", "-b", "development")
        git(path, "config", "user.name", "t")
        git(path, "config", "user.email", "t@example.com")
        write(os.path.join(path, module, "src", "main", "java", "Job.java"), f'String NAME = "job-{module}";')
        if deployed_bundle:
            # Module bibliothèque embarqué par un module déployé du même dépôt
            write(os.path.join(path, "tariff-listener", "pom.xml"), "<project><dependencies><dependency>"
                  "<artifactId>tariff-event</artifactId></dependency></dependencies></project>")
        git(path, "add", "-A")
        git(path, "commit", "-qm", "std")
    found, elsewhere = cj.names_in_standard(std, "", ["job-wes-core", "job-tariff-event"],
                                            {"tariff-listener-activate-request"})
    assert list(found) == ["job-tariff-event"] and "tariff-workflow" in found["job-tariff-event"]
    assert elsewhere == {"job-wes-core": "wes-backend/wes-core/src/main/java/Job.java"}
    kis = cj.names_in_kis(["clean-up-tmp", "absent"], search=lambda q: [("kis-io", "kis-io-job/JobModule.java")]
                          if "clean-up-tmp" in q else [])
    assert kis == {"clean-up-tmp": "socle KIS kis-io : kis-io-job/JobModule.java"}


def test_temporary_diagnostic_step_and_console_lines():
    text = cj.with_diagnostic(ORDER)
    lines = text.splitlines()
    assert lines[4] == f"    Given {cj.DIAGNOSTIC_STEP}" and lines[5] == "    And KIS- Désactivation du job"
    java = cj.diagnostic_java(CLIENT)
    assert f'@Given("{cj.DIAGNOSTIC_STEP}")' in java and "sendGetRestToDisplayList(\"jobs\", KisJob.class)" in java
    assert "post" not in java.lower().replace("system.out.println", "")
    console = "[2026-11-03T08:00:00Z] [DIAGNOSTIC JOBS] 2 job(s)\n[x] [DIAGNOSTIC JOBS] clean-up-tmp | T | actif=true\n"
    assert cj.diagnostic_lines(console) == ["2 job(s)", "clean-up-tmp | T | actif=true"]


def test_references_must_be_created_before_or_proven():
    found = cj.reference_order({"init.feature": ORDER}, {"01-ExcPartnerDraft.xlsm": "code PART_FICHIER"})
    assert [(r["code"], r["ligne"]) for r in found] == [("PART_INCONNU", 11)]
    assert cj.reference_order({"init.feature": ORDER}, {}, {"PART_FICHIER": "base", "PART_INCONNU": "base"}) == []
