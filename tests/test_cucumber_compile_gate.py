"""
Aucune livraison sans « mvn test-compile » réussi sur le dépôt client, avec la bibliothèque de SON pom ; tout appel
Java vers la bibliothèque est vérifié contre les classes de cette version (cas réel : step d'init appelant
KISEndPoints.disableAllJobs / enableAllJobs, absents de la version du client, livré sans compiler).
"""
import os
import shutil
import subprocess
import sys
import zipfile

import pytest

from tests.conftest import git, write
from tools import cucumber_compile, cucumber_init as ci, cucumber_library as cl, cucumber_tools as ct

POM = """<project>
    <properties><cucumber.lib.version>26.16</cucumber.lib.version></properties>
    <dependencies>
        <!-- <dependency><groupId>a-sis</groupId><artifactId>Cucumber</artifactId><version>1.0</version></dependency> -->
        <dependency><groupId>io.cucumber</groupId><artifactId>cucumber-java</artifactId><version>7.13.0</version></dependency>
        <dependency>
            <groupId>a-sis</groupId>
            <artifactId>Cucumber</artifactId>
            <version>${cucumber.lib.version}</version>
            <type>test-jar</type>
        </dependency>
    </dependencies>
</project>
"""

# API de la version du client : ni disableAllJobs ni enableAllJobs (ajoutés plus tard au Standard)
API = {"version": "26.16", "classes": {
    "cucumber.endPoints.EndPoints": {"super": "java.lang.Object", "methodes": {
        "sendDeleteRest": [[1, True, False], [2, True, False], [4, True, False], [5, True, False]]}},
    "cucumber.endPoints.KIS.KISEndPoints": {"super": "cucumber.endPoints.EndPoints", "methodes": {
        "clear": [[1, True, False]], "disableJob": [[1, True, False]], "enableJob": [[1, True, False]]}},
    "cucumber.features.BaseStep": {"super": "java.lang.Object", "methodes": {}},
    "cucumber.features.kis.QAFileIntegrationStep": {"super": "cucumber.features.BaseStep", "methodes": {
        "enableInitData": [[0, False, False]], "integrationFile": [[1, False, False]]}},
    "cucumber.util.context.TestContext": {"super": "java.lang.Object", "methodes": {}},
}}
NEWER = {**API, "version": "26.24", "classes": {**API["classes"], "cucumber.endPoints.KIS.KISEndPoints": {
    "super": "cucumber.endPoints.EndPoints", "methodes": {
        "clear": [[1, True, False]], "disableAllJobs": [[0, True, False]], "enableAllJobs": [[0, True, False]]}}}}

# Step livré chez le client (anonymisé) : appels des sources récentes du Standard, absents de la version du pom
DELIVERED = """package cucumber.features.client;

import cucumber.endPoints.KIS.KISEndPoints;
import cucumber.features.BaseStep;
import cucumber.features.kis.QAFileIntegrationStep;
import cucumber.util.context.TestContext;

public class InitClientSteps extends BaseStep {
    private final QAFileIntegrationStep fileIntegration;

    public InitClientSteps(TestContext testContext) {
        super(testContext);
        fileIntegration = new QAFileIntegrationStep(testContext);
    }

    public void purgeParClear(String clearName) throws Exception {
        fileIntegration.enableInitData();
        // KISEndPoints.unknownInComment();
        String text = "KISEndPoints.unknownInString()";
        KISEndPoints.disableAllJobs();
        try {
            KISEndPoints.clear(clearName);
        } finally {
            KISEndPoints.enableAllJobs();
        }
        KISEndPoints.clear(clearName, "all");
        fileIntegration.integrationFile(java.util.List.of(1, 2));
    }
}
"""


def test_library_version_is_read_from_the_client_pom():
    assert cl.pom_library_version(POM) == "26.16"
    assert cl.pom_library_version("<project><dependencies/></project>") == ""


def test_calls_are_checked_against_the_client_version():
    errors = cl.check_java(DELIVERED, API, "src/test/java/InitClientSteps.java")
    assert len(errors) == 3, errors
    assert errors[0].startswith("src/test/java/InitClientSteps.java:20 KISEndPoints.disableAllJobs() n'existe pas "
                                "dans la bibliothèque 26.16")
    assert "disableJob(1)" in errors[0] and "utilise une méthode ou un step existant" in errors[0]
    assert ":24 KISEndPoints.enableAllJobs()" in errors[1]
    assert ":26 KISEndPoints.clear : 2 argument(s) donné(s), la bibliothèque 26.16 attend 1" in errors[2]
    # Même code, version plus récente : seule l'arité reste fausse ; méthode héritée (EndPoints) reconnue
    assert len(cl.check_java(DELIVERED, NEWER)) == 1
    assert cl.check_java("import cucumber.endPoints.KIS.KISEndPoints;\nclass A { void a() { "
                         "KISEndPoints.sendDeleteRest(\"u\"); KISEndPoints.toString(); } }", API) == []
    assert "disableJob(1 arg.)" in cl.describe(API, "KISEndPoints") and "absente" in cl.describe(API, "Inconnue")


def test_generated_purge_step_uses_only_the_client_version_api():
    java = ci.clear_step_java("client", API)
    assert "AllJobs" not in java and cl.check_java(java, API) == []
    assert 'EndPoints.sendDeleteRest(URL_CLEAR + module + "/all", null, null, null, true);' in java
    newer = ci.clear_step_java("client", NEWER)
    assert "KISEndPoints.disableAllJobs();" in newer and cl.check_java(newer, NEWER) == []
    with pytest.raises(RuntimeError, match="sendDeleteRest"):
        ci.clear_step_java("client", {"version": "1.0", "classes": {"x.Y": {"super": "", "methodes": {}}}})


@pytest.mark.skipif(not shutil.which("javac"), reason="JDK absent")
def test_api_is_read_from_the_jar_classes(tmp_path, monkeypatch):
    source = tmp_path / "src" / "cucumber" / "endPoints" / "KIS"
    write(str(source / "KISEndPoints.java"), "package cucumber.endPoints.KIS;\npublic class KISEndPoints {\n"
          "  public static void clear(String type) {}\n  public static void launch(String... jobs) {}\n"
          "  protected long size(long a, double[] b, String c) { return 0; }\n  private void hidden() {}\n}\n")
    subprocess.run(["javac", "-d", str(tmp_path / "classes"), str(source / "KISEndPoints.java")], check=True)
    jar = tmp_path / "lib-tests.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.write(tmp_path / "classes" / "cucumber" / "endPoints" / "KIS" / "KISEndPoints.class",
                      "cucumber/endPoints/KIS/KISEndPoints.class")
    monkeypatch.setattr(ct, "STD_CACHE_DIR", str(tmp_path / "cache"))
    api = cl.load_api("9.9", str(jar))
    methods = api["classes"]["cucumber.endPoints.KIS.KISEndPoints"]["methodes"]
    assert methods == {"clear": [[1, True, False]], "launch": [[1, True, True]], "size": [[3, False, False]]}
    code = "import cucumber.endPoints.KIS.KISEndPoints;\nclass A { void a() { KISEndPoints.launch(\"a\", \"b\"); " \
           "KISEndPoints.launch(); KISEndPoints.hidden(); } }"
    assert [e.split(" ", 2)[2][:30] for e in cl.check_java(code, api)] == ["KISEndPoints.hidden() n'existe"]


# ---------------------------------------------------------------------------
# Contrôle bloquant des livraisons (format_patch → require_compiled)
# ---------------------------------------------------------------------------

def _fake_maven(tmp_path, output: str, code: int) -> str:
    script = tmp_path / f"mvn_{code}.py"
    script.write_text(f"import pathlib, sys\nlog = pathlib.Path(r'{tmp_path / 'appels.txt'}')\n"
                      f"log.write_text((log.read_text() if log.exists() else '') + 'x')\n"
                      f"print({output!r})\nsys.exit({code})\n", encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


@pytest.fixture
def client_repo(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "COMPILED_DIR", str(tmp_path / "compilation"))
    monkeypatch.setattr(cl, "load_api", lambda version, jar="": API if version == "26.16" else {})
    write(os.path.join(repo, "pom.xml"), POM)
    write(os.path.join(repo, "README.md"), "base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    return repo


def _deliver(repo, java: str, tmp_path):
    write(os.path.join(repo, "src", "test", "java", "cucumber", "features", "client", "InitClientSteps.java"), java)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    output = str(tmp_path / "init.patch")
    with open(output, "w") as f:
        f.write("ancien patch")
    return output


@pytest.mark.real_compile
def test_delivery_blocked_when_a_library_call_is_absent(client_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CUCUMBER_COMPILE_CMD", _fake_maven(tmp_path, "[INFO] BUILD SUCCESS", 0))
    output = _deliver(client_repo, DELIVERED, tmp_path)
    with pytest.raises(ct.DeliveryBlocked, match="bibliothèque 26.16 de son pom") as blocked:
        ct.format_patch(client_repo, "integration~1..integration", output)
    assert "KISEndPoints.disableAllJobs()" in str(blocked.value) and not os.path.exists(output)


@pytest.mark.real_compile
def test_delivery_blocked_on_compile_errors(client_repo, tmp_path, monkeypatch):
    error = ("[ERROR] /w/src/test/java/cucumber/features/client/InitClientSteps.java:[3,5] cannot find symbol\n"
             "  symbol:   method disableAllJobs()")
    monkeypatch.setenv("CUCUMBER_COMPILE_CMD", _fake_maven(tmp_path, error, 1))
    output = _deliver(client_repo, ci.clear_step_java("client", API), tmp_path)
    with pytest.raises(ct.DeliveryBlocked, match="ne compile pas") as blocked:
        ct.format_patch(client_repo, "integration~1..integration", output)
    assert "InitClientSteps.java:3:5 cannot find symbol" in str(blocked.value) and not os.path.exists(output)


@pytest.mark.real_compile
def test_maven_unavailable_stops_delivery_never_skipped(client_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CUCUMBER_COMPILE_CMD", "mvn-introuvable-xyz -q test-compile")
    output = _deliver(client_repo, ci.clear_step_java("client", API), tmp_path)
    with pytest.raises(ct.DeliveryBlocked, match="aucun contrôle n'est sauté"):
        ct.format_patch(client_repo, "integration~1..integration", output)
    # Commande vide : la commande par défaut est utilisée, jamais aucune compilation
    monkeypatch.setenv("CUCUMBER_COMPILE_CMD", "")
    monkeypatch.setattr(ct, "DEFAULT_COMPILE_CMD", _fake_maven(tmp_path, "[INFO] BUILD SUCCESS", 0))
    assert ct.format_patch(client_repo, "integration~1..integration", output)
    assert (tmp_path / "appels.txt").read_text() == "x"


@pytest.mark.real_compile
def test_delivery_compiled_once_per_tree_with_the_client_pom(client_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CUCUMBER_COMPILE_CMD", _fake_maven(tmp_path, "[INFO] BUILD SUCCESS", 0))
    output = _deliver(client_repo, ci.clear_step_java("client", API), tmp_path)
    assert ct.format_patch(client_repo, "integration~1..integration", output)
    assert open(output, encoding="utf-8").read().startswith("From ")
    assert ct.format_patch(client_repo, "integration~1..integration", output)
    assert (tmp_path / "appels.txt").read_text() == "x"   # même arbre : pas de nouvelle compilation
    # Sans pom (version de la bibliothèque inconnue) : arrêt
    git(client_repo, "rm", "-q", "pom.xml")
    git(client_repo, "commit", "-qm", "sans pom")
    with pytest.raises(ct.DeliveryBlocked, match="pas de pom.xml"):
        ct.format_patch(client_repo, "integration~2..integration", output)


def test_agent_compile_tool_stops_on_maven_failure_and_lists_the_api(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path))
    worktree = tmp_path / "wt"
    write(str(worktree / "pom.xml"), POM)
    write(str(worktree / "src" / "test" / "java" / "InitClientSteps.java"), DELIVERED)
    cucumber_compile.write_context("acme", str(worktree), "", "")
    monkeypatch.setattr(cl, "load_api", lambda version, jar="": API)
    monkeypatch.setattr(ct, "verify_build", lambda build, cmd: {"ok": False, "erreurs": ["mvn absent"],
                                                                "environnement": True})
    assert cucumber_compile.main(["acme"]) == cucumber_compile.STOPPED
    assert "ARRÊT" in capsys.readouterr().out
    monkeypatch.setattr(ct, "verify_build", lambda build, cmd: {"ok": True, "erreurs": [], "environnement": False})
    assert cucumber_compile.main(["acme"]) == 1   # compile, mais appels absents de la version du pom
    out = capsys.readouterr().out
    assert "bibliothèque 26.16 du pom du client" in out and "KISEndPoints.disableAllJobs()" in out
    assert cucumber_compile.main(["acme", "--api", "KISEndPoints"]) == 0
    assert "static disableJob(1 arg.)" in capsys.readouterr().out


def test_build_output_is_never_taken_for_repository_files(repo):
    """Dépôt client neuf sans .gitignore : les copies de features sous target/ (compilation) ne sont pas livrées."""
    from tools import cucumber_checks
    write(os.path.join(repo, "src", "test", "resources", "cucumber", "features", "client", "A.feature"), "Feature: A\n")
    write(os.path.join(repo, "target", "test-classes", "cucumber", "features", "client", "A.feature"), "Feature: A\n")
    write(os.path.join(repo, "src", "test", "resources", "cucumber", "files", "target", "test-classes", "x.xml"), "x")
    assert cucumber_checks.repo_files(repo) == {"src/test/resources/cucumber/features/client/A.feature"}


@pytest.mark.real_compile
def test_compile_copy_follows_the_worktree_head(client_repo, tmp_path, monkeypatch):
    # Copie de compilation déjà préparée sur un ancien commit : « HEAD » est celui de l'espace de travail, jamais
    # celui de la copie (sinon l'ancienne version est recompilée et ses erreurs bloquent la livraison)
    monkeypatch.setattr(ct, "COMPILED_DIR", str(tmp_path / "compilation"))
    monkeypatch.setenv("CUCUMBER_COMPILE_CMD", _fake_maven(tmp_path, "[INFO] BUILD SUCCESS", 0))
    output = _deliver(client_repo, DELIVERED, tmp_path)
    with pytest.raises(ct.DeliveryBlocked):
        ct.format_patch(client_repo, "integration~1..HEAD", output)
    _deliver(client_repo, ci.clear_step_java("client", API), tmp_path)
    assert ct.format_patch(client_repo, "integration~1..HEAD", output)
