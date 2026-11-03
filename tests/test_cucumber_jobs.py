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


SUSPEND = """@INIT_CLIENTX_WMS
Feature: Init

  Scenario: Intégration des données
    Given KIS- Désactivation du job
      | name              |
      # TODO-JOB: job « absent » : aucune définition chez le client
      # | absent            |
      | clean-up-tmp      |
    And INIT- Purge de l'environnement par le clear "wms/all"
    And KIS- Activation du job
      | name              |
      | clean-up-tmp      |
    # TODO-VERSION: step absent de la bibliothèque du client (bibliothèque 26.16) : défini dans le Standard aligné
    # And KIS- Désactivation de tous les jobs
    And INIT- Initialisation globale par fichiers via l'écran

  Scenario: Purge des fichiers d'intégration
    # TODO-VERSION: step absent de la bibliothèque du client (bibliothèque 26.16) : défini dans le Standard aligné
    # Given KIS- Activation de tous les jobs
    And KIS- Lancement du job
      | name         |
      | clean-up-tmp |
    And KIS- Désactivation du job
      | name              |
      | Job précubing     |
"""


def test_job_steps_switch_to_project_steps_when_library_lacks_all_jobs():
    steps = cj.project_job_steps(CLIENT)
    prefix = steps[cj.ALL_OFF].split("- ", 1)[0]
    assert prefix.isupper() and prefix != "KIS" and all(s.startswith(prefix + "- ") for s in steps.values())
    text, notes = cj.use_project_job_steps(SUSPEND, CLIENT, "26.16")
    lines = text.splitlines()
    # Désactivation / activation par job autour de la purge : suspension / réactivation de tous les jobs
    assert f"    Given {steps[cj.ALL_OFF]}" == lines[5] and f"    And {steps[cj.ALL_ON]}" in lines
    assert "      # | clean-up-tmp      |" in lines and "      # | absent            |" in lines
    # « tous les jobs » commenté TODO-VERSION : rétabli en step du projet, une seule note TODO-VERSION au-dessus
    index = lines.index(f"    And {steps[cj.ALL_OFF]}")
    assert "POST jobs/suspend" in lines[index - 1] and "TODO-VERSION" not in lines[index - 2]
    assert f"    Given {steps[cj.ALL_ON]}" in lines
    # Désactivation isolée : step du projet par job, table active conservée
    last = lines.index(f"    And {steps[cj.ONE_OFF]}")
    assert lines[last + 2] == "      | Job précubing     |"
    assert not any(line.strip().startswith(("Given KIS- D", "And KIS- D", "And KIS- A")) for line in lines)
    assert len(notes) == 5
    # Jobs toujours contrôlés sous le step du projet ; texte inchangé au second passage pour les steps du projet
    assert [u["nom"] for u in cj.usages(text)] == ["clean-up-tmp", "Job précubing"]
    java = cj.jobs_java(CLIENT, "26.16")
    assert all(f'@Given("{s}")' in java for s in steps.values())
    assert 'sendPostRest(URL_JOBS + "/suspend")' in java and 'sendPostRest(URL_JOBS + "/unsuspend")' in java


def test_library_with_all_jobs_steps_is_left_unchanged(tmp_path):
    std = str(tmp_path / "std")
    os.makedirs(std)
    git(std, "init", "-q", "-b", "development")
    git(std, "config", "user.name", "t")
    git(std, "config", "user.email", "t@example.com")
    step = os.path.join(std, "src", "test", "java", "cucumber", "features", "kis", "KISJobStep.java")
    write(step, '@When("KIS- Désactivation du job")')
    git(std, "add", "-A")
    git(std, "commit", "-qm", "26.16")
    git(std, "tag", "v26.16")
    write(step, f'@Given("{cj.ALL_OFF}")')
    git(std, "commit", "-qam", "26.22")
    git(std, "tag", "v26.22")
    assert not cj.library_has_all_jobs(std, "refs/tags/v26.16") and cj.library_has_all_jobs(std, "refs/tags/v26.22")
    worktree = str(tmp_path / "wt")
    rel = "src/test/resources/cucumber/features/x/Init.feature"
    for version, changed in (("26.22", False), ("26.16", True)):
        write(os.path.join(worktree, "pom.xml"), "<project><dependencies><dependency><groupId>a-sis</groupId>"
              f"<artifactId>Cucumber</artifactId><version>{version}</version><type>test-jar</type></dependency>"
              "</dependencies></project>")
        write(os.path.join(worktree, *rel.split("/")), SUSPEND)
        out = cj.apply_project_steps(worktree, [rel], CLIENT, std)
        assert bool(out["fichiers"]) is changed, out
    assert out["fichiers"] == [rel, cj.jobs_java_path(CLIENT)]
    assert os.path.exists(os.path.join(worktree, *cj.jobs_java_path(CLIENT).split("/")))


CONNECTED = """@INIT_CLIENTX_WMS
Feature: Init

  Scenario: Initialisation du paramètrage
    Given INIT- Initialisation globale des règles
    And INIT- Configuration du colisage

  Scenario: Purge des fichiers d'intégration
    And CLIENTX- Désactivation du job
      | name                                  |
      | Schedule preparation orders precubing |
"""


def test_rules_step_dropped_without_rules_folder_and_connected_job_kept_active(tmp_path):
    rel = f"src/test/resources/cucumber/features/{CLIENT}/01-Contexte/WMS/01-Initialisation.feature"
    assert cj.rules_folder(rel) == f"src/test/resources/cucumber/rules/{CLIENT}/01-Contexte/WMS/"
    props = cj.arg_properties("-DuserCubing='ZGV2' -DpasswordCubing=x -DurlCubing='https://cubing' -Dk=\"v w\"")
    assert props == {"userCubing": "ZGV2", "passwordCubing": "x", "urlCubing": "https://cubing", "k": "v w"}
    text, notes = cj.drop_rules_without_folder(CONNECTED, rel, str(tmp_path))
    assert "    # Given INIT- Initialisation globale des règles" in text.splitlines() and len(notes) == 1
    # Dossier de règles présent : le step est gardé
    os.makedirs(tmp_path / "src/test/resources/cucumber/rules" / CLIENT / "01-Contexte" / "WMS")
    assert cj.drop_rules_without_folder(CONNECTED, rel, str(tmp_path)) == (CONNECTED, [])
    # Connexion cubing fournie par le job Jenkins et configurée plus haut : précubing laissé actif
    kept, reasons = cj.keep_connected_jobs_active(CONNECTED, props)
    assert "    # And CLIENTX- Désactivation du job" in kept.splitlines() and "-DurlCubing" in reasons[0]
    assert not cj.usages(kept)
    # Sans connexion (propriété absente ou vide) : la désactivation reste
    assert cj.keep_connected_jobs_active(CONNECTED, {"urlCubing": ""}) == (CONNECTED, [])


def test_failed_proof_search_is_undetermined_never_absent(tmp_path):
    def down(query):
        raise cj.SearchUnavailable("recherche de code Bitbucket injoignable : Max retries exceeded")
    errors = []
    assert cj.names_in_kis(["clean-up-tmp"], search=down, errors=errors) == {}
    assert errors == [{"nom": "clean-up-tmp", "erreur": "recherche de code Bitbucket injoignable : Max retries exceeded"}]
    rel = "src/test/resources/cucumber/features/x/Init.feature"
    write(os.path.join(str(tmp_path), *rel.split("/")), INIT)
    result = cj.apply(str(tmp_path), [rel], "", [], kis_search=down)
    # Aucun fichier réécrit : un job dont la preuve n'a pas pu être cherchée n'est jamais commenté
    assert result["fichiers"] == [] and {m["nom"] for m in result["indetermines"]} >= {"clean-up-tmp"}
    with open(os.path.join(str(tmp_path), *rel.split("/")), encoding="utf-8") as f:
        assert f.read() == INIT


def test_recent_standard_fetch_is_reused(tmp_path, monkeypatch):
    import importlib
    import io
    import sys
    import time
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        main = importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert main.fetch_age_minutes(str(repo)) is None
    (repo / ".git" / "FETCH_HEAD").write_text("x")
    assert main.fetch_age_minutes(str(repo)) < 1
    old = time.time() - 2 * 3600
    os.utime(repo / ".git" / "FETCH_HEAD", (old, old))
    assert 119 < main.fetch_age_minutes(str(repo)) < 121
    monkeypatch.setenv("STD_FETCH_MAX_AGE_MINUTES", "0")
    assert main.std_fetch_max_age() == 0
    monkeypatch.setenv("STD_FETCH_MAX_AGE_MINUTES", "abc")
    assert main.std_fetch_max_age() == 60
