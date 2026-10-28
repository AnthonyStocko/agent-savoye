"""Livrable du worktree Cucumber client et steps déclarés par constante."""
import os

from tests.conftest import git, write
from tools import cucumber_tools as ct
from tools import cucumber_verify as cv

FEATURE_REL = "src/test/resources/cucumber/features/clientx/commande.feature"
BASE_FEATURE = "Feature: Commandes\n\n  @existant\n  Scenario: Existant\n    Then le champ \"a\" vaut \"b\"\n"


def _lot(repo, number, title):
    path = os.path.join(repo, *FEATURE_REL.split("/"))
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"\n  Scenario: {title}\n    Then le champ \"statut\" vaut \"VALIDE\"\n")
    new = [s for s in cv.parse_scenarios(open(path, encoding="utf-8").read()) if s["titre"] == title]
    for s in new:
        s["fichier"] = FEATURE_REL
    tags = cv.tag_scenarios(repo, new, "clientx", number)
    assert ct.commit_lot(repo, "clientx", number)
    return tags


def _generation(repo):
    write(os.path.join(repo, *FEATURE_REL.split("/")), BASE_FEATURE)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    _lot(repo, 1, "Statut validé")
    _lot(repo, 2, "Statut refusé")
    return base


def test_final_patch_has_no_agent_tags(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("CUCUMBER_KEEP_AGENT_TAGS", raising=False)
    base = _generation(repo)
    patch = str(tmp_path / "clientx-cucumber.patch")
    assert ct.write_patch(repo, base, patch, "clientx")
    text = open(patch, encoding="utf-8").read()
    assert "@agent_" not in text
    assert text.count("Subject: [PATCH") == 1 and "Statut validé" in text and "Statut refusé" in text
    # Le worktree garde les tags pour les reprises
    assert "@agent_clientx_2_1" in open(os.path.join(repo, *FEATURE_REL.split("/")), encoding="utf-8").read()
    assert git(repo, "status", "--porcelain") == ""
    # Le patch s'applique sur la base
    git(repo, "checkout", "-q", "--detach", base)
    git(repo, "am", "-q", patch)
    applied = open(os.path.join(repo, *FEATURE_REL.split("/")), encoding="utf-8").read()
    assert "Statut refusé" in applied and "@agent_" not in applied and "@existant" in applied


def test_keep_agent_tags_option(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CUCUMBER_KEEP_AGENT_TAGS", "1")
    base = _generation(repo)
    patch = str(tmp_path / "p.patch")
    assert ct.write_patch(repo, base, patch, "clientx")
    assert "@agent_clientx_1_1" in open(patch, encoding="utf-8").read()


def test_strip_agent_tags_keeps_other_tags():
    text = "  @agent_clientx_3 @agent_clientx_3_2 @smoke\n  Scenario: x\n  @agent_clientx_3 @agent_clientx_3_1\n"
    assert ct.strip_agent_tags(text, "clientx") == "  @smoke\n  Scenario: x\n"


def test_discard_lot_restores_clean_worktree(repo):
    write(os.path.join(repo, "a.txt"), "a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    write(os.path.join(repo, "a.txt"), "modifié\n")
    write(os.path.join(repo, "src", "nouveau.feature"), "Feature: x\n")
    ct.discard_lot(repo)
    assert git(repo, "status", "--porcelain") == ""
    assert open(os.path.join(repo, "a.txt"), encoding="utf-8").read() == "a\n"


STEPS_JAVA = '''package cucumber.features.clientx;

public class CommandeSteps {
    static final String VALIDER = "^je valide la commande \\"([^\\"]*)\\"$";
    private static final String PREFIXE = "CLIENTX-";

    @Given("^une commande litterale$")
    public void litteral() {}

    @When(VALIDER)
    public void valider(String code) {}

    @Then(Libelles.STATUT)
    public void statut() {}

    @Then(PREFIXE + "le montant vaut (\\\\d+)$")
    public void montant(int m) {}

    @Then(Inconnue.ABSENTE)
    public void absente() {}
}
'''
LIBELLES_JAVA = '''package cucumber.util;

public final class Libelles {
    public static final String STATUT = "^le statut vaut \\"([^\\"]*)\\"$";
}
'''


def test_steps_declared_by_constant(repo, capsys):
    root = os.path.join(repo, "src", "test", "java")
    write(os.path.join(root, "cucumber", "features", "clientx", "CommandeSteps.java"), STEPS_JAVA)
    write(os.path.join(root, "cucumber", "util", "Libelles.java"), LIBELLES_JAVA)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "steps")
    catalogue = ct.extract_cucumber(repo, "HEAD")
    expressions = {s["expr"] for s in catalogue["steps"]}
    assert "^une commande litterale$" in expressions
    assert '^je valide la commande \\"([^\\"]*)\\"$' in expressions
    assert '^le statut vaut \\"([^\\"]*)\\"$' in expressions
    assert "CLIENTX-le montant vaut (\\\\d+)$" in expressions
    assert [u["argument"] for u in catalogue["steps_non_resolus"]] == ["Inconnue.ABSENTE"]
    ct.report_unresolved_steps("test", catalogue)
    assert "1 step(s) par constante non résolu(s)" in capsys.readouterr().out
    # Extraction Java sur disque (steps client, endpoints) : mêmes résolutions
    found = cv._disk_steps(repo)
    assert '^le statut vaut "([^"]*)"$' in found and "CLIENTX-le montant vaut (\\\\d+)$" in found
    methods = ct._java_methods(STEPS_JAVA, ct.disk_class_constants(root))
    assert [m["steps_non_resolus"] for m in methods if m["name"] == "absente"] == [["Inconnue.ABSENTE"]]


def test_squashed_patch_never_deletes_existing_files_and_resolves_case(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("CUCUMBER_PATCH_MODE", raising=False)
    write(os.path.join(repo, "src/test/resources/cucumber/features/clientx/WMS/Product/a.feature"), "Feature: A\n")
    write(os.path.join(repo, "src/test/resources/cucumber/features/clientx/existant.feature"), "Feature: E\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "rm", "-q", "src/test/resources/cucumber/features/clientx/existant.feature")
    write(os.path.join(repo, "src/test/resources/cucumber/features/clientx/WMS/product/a.feature"),
          "Feature: A minuscule\n")
    write(os.path.join(repo, "src/test/resources/cucumber/features/clientx/WMS/Product/a.feature"), "Feature: A2\n")
    write(os.path.join(repo, "src/test/resources/cucumber/features/clientx/nouveau.feature"), "Feature: N\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "lot 1")
    patch = str(tmp_path / "p.patch")
    assert ct.write_patch(repo, base, patch, "clientx")
    text = open(patch, encoding="utf-8").read()
    assert text.count("Subject: [PATCH") == 1 and "nouveau.feature" in text
    assert "deleted file" not in text and "existant.feature" not in text      # jamais de suppression
    assert "WMS/Product/a.feature" in text and "WMS/product/a.feature" not in text   # un seul chemin par casse
    assert git(repo, "status", "--porcelain") == ""


def test_squashed_patch_uses_the_existing_case_of_a_path(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("CUCUMBER_PATCH_MODE", raising=False)
    write(os.path.join(repo, "src/test/resources/cucumber/features/clientx/WMS/product/b.feature"), "Feature: B\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "rm", "-q", "--cached", "src/test/resources/cucumber/features/clientx/WMS/product/b.feature")
    git(repo, "commit", "-qm", "retrait")
    write(os.path.join(repo, "src/test/resources/cucumber/features/clientx/WMS/Product/b.feature"), "Feature: B2\n")
    git(repo, "add", "src/test/resources/cucumber/features/clientx/WMS/Product/b.feature")
    git(repo, "commit", "-qm", "lot")
    patch = str(tmp_path / "p.patch")
    assert ct.write_patch(repo, base, patch, "clientx")
    text = open(patch, encoding="utf-8").read()
    assert "WMS/product/b.feature" in text and "WMS/Product/b.feature" not in text and "Feature: B2" in text
