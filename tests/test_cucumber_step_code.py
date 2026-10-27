"""Code des steps du Standard injecté dans les lots, sources lisibles par l'agent et compilation par l'agent."""
import io
import importlib
import os
import sys

import pytest

from tests.conftest import git, write
from tools import cucumber_compile, cucumber_tools as ct

STEPS = '''package cucumber.features.goodsin;

public class GoodsInFolderStep extends BaseStep {
    private static final String CREATE = "REC- Création dossier d'arrivage";

    @Given(CREATE)
    public void createFolder(DataTable table) {
        Map<String, String> row = table.asMaps().get(0);
        String code = row.get("code");
        String supplier = row.get("fournisseur");
        Context().put("GOODS_IN_FOLDER", GoodsInEndPoints.create(code, supplier));
    }

    @Then("REC- Le dossier d'arrivage {string} a le statut {string}")
    public void checkStatus(String code, String status) {
        assertEquals(status, GoodsInEndPoints.get(code).getStatus());
    }
}
'''


@pytest.fixture
def std_repo(repo, tmp_path, monkeypatch):
    write(os.path.join(repo, "src/test/java/cucumber/features/goodsin/GoodsInFolderStep.java"), STEPS)
    write(os.path.join(repo, "src/test/resources/cucumber/features/goodsin/REC-1.feature"),
          "Feature: Arrivage\n  Scenario: Création\n")
    write(os.path.join(repo, "src/main/java/App.java"), "class App {}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "std")
    monkeypatch.setattr(ct, "STD_CACHE_DIR", str(tmp_path / "cache"))
    return repo


def test_std_step_code_resolves_constants_and_exports_sources(std_repo):
    meta = ct.std_step_code(std_repo, "8.1")
    create = meta["steps"]["REC- Création dossier d'arrivage"]
    assert create["fichier"].endswith("GoodsInFolderStep.java") and create["ligne"] == 7
    assert create["code"].startswith("public void createFolder(DataTable table) {")
    assert 'row.get("fournisseur")' in create["code"] and "GOODS_IN_FOLDER" in create["code"]
    assert "REC- Le dossier d'arrivage {string} a le statut {string}" in meta["steps"]
    sources = meta["sources"]
    assert os.path.exists(os.path.join(sources, "src/test/java/cucumber/features/goodsin/GoodsInFolderStep.java"))
    assert os.path.exists(os.path.join(sources, "src/test/resources/cucumber/features/goodsin/REC-1.feature"))
    assert not os.path.exists(os.path.join(sources, "src/main/java/App.java"))
    # En cache tant que le HEAD du Standard ne change pas
    assert ct.std_step_code(std_repo, "8.1") == meta


def test_step_code_truncated():
    method = {"signature": "public void f()", "body": "{\n" + "    x();\n" * 60 + "}"}
    code = ct._step_code(method, "A.java", max_lines=10)
    assert code.count("\n") == 10 and code.endswith("ligne(s) de plus : 'A.java'") and "\n    x();" in code


def test_lot_block_injects_candidate_code(std_repo):
    meta = ct.std_step_code(std_repo, "8.1")
    spec = {"id": "s1", "libelle": "Arrivage", "type": "override", "nature": "processus", "mots_cles": [],
            "nb_fichiers": 0, "fichiers": [], "couverture": "non_couvert",
            "steps_candidats": [{"expr": "REC- Création dossier d'arrivage", "classe": "GoodsInFolderStep",
                                 "endpoints": []},
                                {"expr": "REC- Inconnu", "classe": "X", "endpoints": []}]}
    block = ct.lot_block([spec], step_code=meta)
    assert "`REC- Création dossier d'arrivage` — GoodsInFolderStep (code injecté plus bas)" in block
    assert "`REC- Inconnu` — X\n" in block + "\n"
    assert "### Code des steps candidats" in block and 'row.get("fournisseur")' in block
    assert "### Code des steps candidats" not in ct.lot_block([spec])
    assert "### Code des steps candidats" not in ct.lot_block([spec], step_code=meta, step_code_max=50)
    # Pas de doublon entre spécifiques du lot
    assert ct.lot_block([spec, {**spec, "id": "s2"}], step_code=meta).count("```java") == 1


def test_agent_compile_script(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path))
    assert cucumber_compile.main(["acme"]) == 0 and "contexte absent" in capsys.readouterr().out
    worktree = tmp_path / "wt"
    write(str(worktree / "pom.xml"), "<project/>")
    cucumber_compile.write_context("acme", str(worktree), "", "mvn -q -o test-compile")
    calls = []
    monkeypatch.setattr(ct, "verify_build", lambda build, cmd: calls.append((build, cmd)) or {
        "ok": False, "etape": "compilation", "erreurs": ["src/test/java/A.java:3:5 cannot find symbol"],
        "environnement": False})
    assert cucumber_compile.main(["ACME"]) == 1
    out = capsys.readouterr().out
    assert calls == [(str(worktree), "mvn -q -o test-compile")]
    assert "COMPILATION KO : 1 erreur(s)" in out and "- src/test/java/A.java:3:5 cannot find symbol" in out
    monkeypatch.setattr(ct, "verify_build", lambda build, cmd: {"ok": True, "erreurs": [], "environnement": False})
    assert cucumber_compile.main(["acme"]) == 0 and "COMPILATION OK" in capsys.readouterr().out


@pytest.fixture(scope="module")
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


def test_prompts_carry_compilation_and_sources(main_module):
    block = main_module.agent_compilation_block('"py" "c.py" acme')
    assert 'Commande : "py" "c.py" acme' in block and main_module.agent_compilation_block("") == ""
    assert main_module.cucumber_agent_tools({}) == main_module.CUCUMBER_TOOLS
    assert main_module.cucumber_agent_tools({"outils_agent": ["Read", "Bash(py:*)"]}) == ["Read", "Bash(py:*)"]
    for name in ("cucumber_client_fix", "cucumber_client_review"):
        text = main_module.render_prompt(name, **{k: k for k in (
            "etape client_name lot_label token_saving_rules worktree erreurs fichiers java_std java_client "
            "package_steps scenarios_signales steps_std endpoints_std sources_std").split()},
            compilation_agent=block)
        assert "sources_std" in text and 'Commande : "py" "c.py" acme' in text


def _git_repo(path):
    os.makedirs(path)
    git(path, "init", "-q", "-b", "dev")
    git(path, "config", "user.name", "t")
    git(path, "config", "user.email", "t@e.x")
    git(path, "config", "core.autocrlf", "false")


def test_override_standard_version_exported(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "STD_CACHE_DIR", str(tmp_path / "cache" / "_standard"))
    java = "src/main/java/com/acme/PriceService.java"
    std, client = str(tmp_path / "std" / "wms"), str(tmp_path / "client" / "wms")
    for repo, body in ((std, "return 10;"), (client, "return 12;")):
        _git_repo(repo)
        write(os.path.join(repo, java), f"public class PriceService {{ int price() {{ {body} }} }}\n")
        write(os.path.join(repo, "src/main/java/com/acme/NewThing.java"), "class NewThing {}\n") \
            if repo == client else None
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "init")
    inventory = ct.build_inventory("acme", str(tmp_path / "client"), str(tmp_path / "std"))
    ct.export_sources(inventory)
    files = {f["path"]: f for s in inventory["specifiques"] for f in s["fichiers"]}
    override = files[java]
    assert override["kind"] == "override"
    with open(override["local_standard"], encoding="utf-8") as f:
        assert "return 10;" in f.read()
    with open(override["local"], encoding="utf-8") as f:
        assert "return 12;" in f.read()
    assert "local_standard" not in files["src/main/java/com/acme/NewThing.java"]
    spec = next(s for s in inventory["specifiques"] if any(f["path"] == java for f in s["fichiers"]))
    assert f"Version Standard (avant override" in ct.lot_block([{**spec, "couverture": "non_couvert"}])


def test_followup_resumes_lot_session(main_module, monkeypatch, tmp_path):
    system = tmp_path / "sys.md"
    system.write_text("regles")
    calls = []

    def fake(prompt, model, tools, turns, run_dir, effort, system_file="", resume=""):
        calls.append((model, effort, system_file, resume))
        if resume == "dead":
            return 1, "No conversation found", {}, False
        return 0, "{}", {"session_id": "s2"}, False
    monkeypatch.setattr(main_module, "run_cucumber_claude", fake)
    monkeypatch.delenv("CUCUMBER_RESUME_SESSION", raising=False)
    lot = {"session": "s1", "modele": "opus", "effort": "high", "systeme": str(system)}
    out = main_module.cucumber_followup(lot, "p", "haiku", "low", ["Read"], "10", str(tmp_path))
    assert calls[-1] == ("opus", "high", str(system), "s1") and out[4:] == ("opus", "high")
    assert lot["session"] == "s2"
    lot["session"] = "dead"
    out = main_module.cucumber_followup(lot, "p", "haiku", "low", ["Read"], "10", str(tmp_path))
    assert calls[-1] == ("haiku", "low", "", "") and "session" not in lot and out[4:] == ("haiku", "low")
    monkeypatch.setenv("CUCUMBER_RESUME_SESSION", "0")
    main_module.cucumber_followup({"session": "s1"}, "p", "haiku", "low", ["Read"], "10", str(tmp_path))
    assert calls[-1][3] == ""


def test_session_id_parsed(main_module):
    text, usage = main_module.parse_claude_output('{"type": "result", "result": "ok", "session_id": "abc"}')
    assert text == "ok" and usage["session_id"] == "abc"
