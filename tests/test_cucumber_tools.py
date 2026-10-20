"""Livrable du worktree Cucumber client et steps déclarés par constante."""
import os

from tests.conftest import git, write
from tools import cucumber_tools as ct
from tools import cucumber_verify as cv

FEATURE_REL = "src/test/resources/cucumber/features/apparel/commande.feature"
BASE_FEATURE = "Feature: Commandes\n\n  @existant\n  Scenario: Existant\n    Then le champ \"a\" vaut \"b\"\n"


def _lot(repo, number, title):
    path = os.path.join(repo, *FEATURE_REL.split("/"))
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"\n  Scenario: {title}\n    Then le champ \"statut\" vaut \"VALIDE\"\n")
    new = [s for s in cv.parse_scenarios(open(path, encoding="utf-8").read()) if s["titre"] == title]
    for s in new:
        s["fichier"] = FEATURE_REL
    tags = cv.tag_scenarios(repo, new, "apparel", number)
    assert ct.commit_lot(repo, "apparel", number)
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
    patch = str(tmp_path / "apparel-cucumber.patch")
    assert ct.write_patch(repo, base, patch, "apparel")
    text = open(patch, encoding="utf-8").read()
    assert "@agent_" not in text
    assert text.count("Subject: [PATCH") == 2 and "Statut validé" in text and "Statut refusé" in text
    # Le worktree garde les tags pour les reprises
    assert "@agent_apparel_2_1" in open(os.path.join(repo, *FEATURE_REL.split("/")), encoding="utf-8").read()
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
    assert ct.write_patch(repo, base, patch, "apparel")
    assert "@agent_apparel_1_1" in open(patch, encoding="utf-8").read()


def test_strip_agent_tags_keeps_other_tags():
    text = "  @agent_apparel_3 @agent_apparel_3_2 @smoke\n  Scenario: x\n  @agent_apparel_3 @agent_apparel_3_1\n"
    assert ct.strip_agent_tags(text, "apparel") == "  @smoke\n  Scenario: x\n"


def test_discard_lot_restores_clean_worktree(repo):
    write(os.path.join(repo, "a.txt"), "a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    write(os.path.join(repo, "a.txt"), "modifié\n")
    write(os.path.join(repo, "src", "nouveau.feature"), "Feature: x\n")
    ct.discard_lot(repo)
    assert git(repo, "status", "--porcelain") == ""
    assert open(os.path.join(repo, "a.txt"), encoding="utf-8").read() == "a\n"


STEPS_JAVA = '''package cucumber.features.apparel;

public class CommandeSteps {
    static final String VALIDER = "^je valide la commande \\"([^\\"]*)\\"$";
    private static final String PREFIXE = "APPAREL-";

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
    write(os.path.join(root, "cucumber", "features", "apparel", "CommandeSteps.java"), STEPS_JAVA)
    write(os.path.join(root, "cucumber", "util", "Libelles.java"), LIBELLES_JAVA)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "steps")
    catalogue = ct.extract_cucumber(repo, "HEAD")
    expressions = {s["expr"] for s in catalogue["steps"]}
    assert "^une commande litterale$" in expressions
    assert '^je valide la commande \\"([^\\"]*)\\"$' in expressions
    assert '^le statut vaut \\"([^\\"]*)\\"$' in expressions
    assert "APPAREL-le montant vaut (\\\\d+)$" in expressions
    assert [u["argument"] for u in catalogue["steps_non_resolus"]] == ["Inconnue.ABSENTE"]
    ct.report_unresolved_steps("test", catalogue)
    assert "1 step(s) par constante non résolu(s)" in capsys.readouterr().out
    # Extraction Java sur disque (steps client, endpoints) : mêmes résolutions
    found = cv._disk_steps(repo)
    assert '^le statut vaut "([^"]*)"$' in found and "APPAREL-le montant vaut (\\\\d+)$" in found
    methods = ct._java_methods(STEPS_JAVA, ct.disk_class_constants(root))
    assert [m["steps_non_resolus"] for m in methods if m["name"] == "absente"] == [["Inconnue.ABSENTE"]]
