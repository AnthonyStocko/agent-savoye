"""
Onglet « Cucumber » simplifié : remarques sur les étapes 1 et 2 (prérequis, détail lisible, jeton d'écriture) et
étapes 4 (clear déduit des routes, init du Standard adaptée ou comparée, run de l'init) et 5 (résultat par
fonctionnalité). Client fictif.
"""
import importlib
import io
import json
import os
import sys

import pytest

from tests.conftest import CLIENT, git, write

from tools import cucumber_profile as cp
from tools import cucumber_repo_setup as rs
from tools import cucumber_simple as cs
from tools import cucumber_simple_init as si
from tools import cucumber_tools as ct

CLIENT_BUNDLE = """#### Standard
import:a-sis/wms-contrib-x@26.22-SNAPSHOT
## import:a-sis/tms-contrib-x@26.22-SNAPSHOT
runtime:client/spec-module@1.3-SNAPSHOT
"""
CONTRIBS = {
    "wms-contrib-x": "import:a-sis/socle-x@3.12\nruntime:a-sis/init-data-rest-clear-main-wms@26.22-SNAPSHOT\n",
    "tms-contrib-x": "runtime:a-sis/init-data-rest-clear-main-tms@26.22-SNAPSHOT\n",
    "socle-x": "runtime:a-sis/kis-core@3.12\n",
}
WMS_REST = """@RestResource
public class WmsClearRest {
    @DELETE("/clear/wms/all")
    public void all() {}
    @DELETE("/clear/wms/data")
    public void data() {}
}
"""
STD_INIT = """@INIT_WMS @WMS
Feature: 01 - Initialisation des données

  Scenario: Intégration des données
    Given INIT- Purge de l'environnement
    And KIS- Activation du toggle "A"
    And INIT- Initialisation globale par fichiers via l'écran

  Scenario: Paramétrage
    Given CONFIG- Choix de la langue
"""


# ---------------------------------------------------------------------------
# Remarques 1 à 3 : prérequis dès l'étape 1, détail lisible, jeton d'écriture distinct
# ---------------------------------------------------------------------------

def test_prerequisites_checked_at_step_one():
    full = {"MAVEN_REPOSITORY_URL": "https://nexus", "MAVEN_REPOSITORY_USER": "u", "MAVEN_REPOSITORY_PASSWORD": "p",
            "BITBUCKET_WRITE_TOKEN": "w"}
    assert cs.missing_prerequisites(full) == []
    missing = cs.missing_prerequisites({**full, "MAVEN_REPOSITORY_PASSWORD": " ", "BITBUCKET_WRITE_TOKEN": ""})
    assert len(missing) == 2 and "MAVEN_REPOSITORY_PASSWORD" in missing[0] and "BITBUCKET_WRITE_TOKEN" in missing[1]
    deduction = {"version_client": "1.3.1", "standard": {"version": "26.22"}}
    step = cs.project_step(deduction, True, missing)
    assert step["etat"] == "echec" and "Configuration" in step["resultat"] and "1.3.1" in step["resultat"]
    assert cs.project_step(deduction, True, [])["etat"] == "ok"
    blocked = {s["cle"]: s for s in cs.states(step, {})}
    assert blocked["depot"]["etat"] == "bloquee"                                    # plus d'échec à l'étape 2


def test_technical_detail_is_readable_text():
    lines = cs.detail_lines({"depot": "x-cucumber", "branche": "v1.3", "commit": "abc1234567 poussé sur v1.3",
                             "bibliotheque": "26.22", "compilation": "réussie (mvn test-compile)",
                             "erreurs": ["A.java:1 erreur", "B.java:2 erreur"], "plan": {"action": "x"},
                             "inconnu_cle": {"a": 1}})
    labels = [label for label, _, _ in lines]
    assert labels[:5] == ["Dépôt", "Branche", "Commit", "Bibliothèque Cucumber", "Compilation"]
    assert "Plan" not in labels and ("Inconnu cle", "a : 1", None) in lines
    errors = next(item for item in lines if item[0] == "Erreurs")
    assert errors[1] == "2 élément(s)" and errors[2] == ["A.java:1 erreur", "B.java:2 erreur"]
    assert all(isinstance(text, str) and "{" not in text for _, text, _ in lines)


def test_write_token_is_distinct_from_read_token(monkeypatch):
    monkeypatch.setenv("BITBUCKET_HTTP_TOKEN", "read")
    monkeypatch.delenv("BITBUCKET_WRITE_TOKEN", raising=False)
    assert "BITBUCKET_WRITE_TOKEN" in rs.write_token_missing(f"{CLIENT}-cucumber")
    monkeypatch.setenv("BITBUCKET_WRITE_TOKEN", "write")
    assert rs.write_token_missing() == "" and rs.write_token() == "write"


# ---------------------------------------------------------------------------
# Étape 4 : clear déduit des routes de l'application à sa version
# ---------------------------------------------------------------------------

def test_deployed_clears_follow_active_imports_only():
    deployed = si.deployed_clears([("app@v1.3 bundle.list", CLIENT_BUNDLE)], lambda a: CONTRIBS.get(a, ""))
    assert set(deployed) == {"wms"}                                                 # import TMS commenté
    assert "wms-contrib-x" in deployed["wms"]


def test_clear_decision_is_deterministic_from_routes():
    deployed = {"wms": "p1", "tms": "p2"}
    routes = si.clear_routes({"wms": [WMS_REST], "tms": ['@DELETE("/clear/tms/all")'], "full": []})
    assert routes["wms"] == ["wms/all", "wms/data"]
    decision = si.decide_clear(deployed, routes, ["TMS", "WMS"])
    assert decision["mode"] == "client" and decision["routes"] == ["wms/all", "tms/all"]     # ordre WMS, TMS
    assert si.decide_clear(deployed, routes, ["WMS"])["routes"] == ["wms/all"]
    standard = si.decide_clear({**deployed, "full": "p"}, {**routes, "full": ["full/all"]}, ["WMS"])
    assert standard["mode"] == "standard" and si.expected_steps(standard) == [si.STD_PURGE_STEP]
    missing = si.decide_clear({"wms": "p"}, routes, ["WMS", "OMS"])
    assert missing["mode"] == "" and "OMS" in missing["erreur"]
    unproven = si.decide_clear({"wms": "p"}, {"wms": ["wms/data"]}, ["WMS"])
    assert unproven["mode"] == "" and "/clear/wms/all" in unproven["erreur"]
    assert si.decide_clear(deployed, routes, [])["erreur"]


def test_apply_clear_replaces_only_when_it_differs():
    decision = {"mode": "client", "routes": ["wms/all", "tms/all"], "motif": "m"}
    fixed, notes = si.apply_clear(STD_INIT, decision)
    assert 'Given INIT- Purge de l\'environnement par le clear "wms/all"' in fixed
    assert 'And INIT- Purge de l\'environnement par le clear "tms/all"' in fixed
    assert "TODO-INIT:" in fixed and notes == ["clear full/all → wms/all, tms/all"]
    assert [r for _, r in si.purges(fixed)] == ["wms/all", "tms/all"]
    assert si.apply_clear(fixed, decision) == (fixed, [])                          # déjà conforme : inchangé
    legacy = STD_INIT.replace("INIT- Purge de l'environnement",
                              'INIT- Purge de l\'environnement par le clear "init-data-rest-clear-main-wms"')
    assert si.apply_clear(legacy, {"mode": "client", "routes": ["wms/all"], "motif": ""})[1] == []


def test_standard_init_adapted_with_decided_clear():
    text = si.client_feature(STD_INIT, "WMS", CLIENT, {"mode": "client", "routes": ["wms/all"]})
    assert f"@INIT_{CLIENT.upper()}_WMS" in text and f"@{CLIENT.upper()}" in text
    assert 'Given INIT- Purge de l\'environnement par le clear "wms/all"' in text
    assert "INIT- Purge de l'environnement\n" not in text
    assert si.init_tags({"a": text}) == f"@INIT_{CLIENT.upper()}_WMS"
    assert si.init_tags({"a": text}, {"WMS"}) == ""
    java = si.clear_java(CLIENT, ["wms/all"], ["wms : preuve"])
    assert '"wms/all"' in java and si.defines_clear_step(java) and si.missing_routes(java, ["wms/all"]) == []
    assert si.missing_routes(java, ["tms/all"]) == ["tms/all"]


def test_existing_init_compared_not_copied():
    client = si.client_feature(STD_INIT, "WMS", CLIENT, {"mode": "client", "routes": ["wms/all"]}).replace(
        "    And INIT- Initialisation globale par fichiers via l'écran\n",
        "    # TODO-VERSION: attente dépassée avec la bibliothèque 26.16\n"
        "    And X- Initialisation globale avec un délai de 900 secondes\n")
    gaps = si.step_gaps(client, si.client_feature(STD_INIT, "WMS", CLIENT, {"mode": "client", "routes": ["wms/all"]}))
    assert gaps == {"standard_seulement": ["INIT- Initialisation globale par fichiers via l'écran"],
                    "client_seulement": ["X- Initialisation globale avec un délai de 900 secondes"]}
    outdated = si.outdated_workarounds(client, "26.22")
    assert len(outdated) == 1 and "26.16" in outdated[0]
    assert si.outdated_workarounds(client, "26.16") == []


# ---------------------------------------------------------------------------
# Étape 4 de bout en bout : dépôt local, Standard fictif, run simulé
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


@pytest.fixture
def init_workspace(tmp_path, monkeypatch, main_module):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.delenv("AGENT_AUTH_USER", raising=False)
    os.makedirs(os.path.dirname(cp.profile_path(CLIENT)), exist_ok=True)
    profile = cp.empty_profile()
    profile["modules"] = [{"code": "WMS", "tag": "@WMS", "sources": ["test"]}]
    with open(cp.profile_path(CLIENT), "w", encoding="utf-8") as f:
        json.dump({"detecte": profile, "saisi": {}}, f)
    std = str(tmp_path / "std")
    write(os.path.join(std, "src/test/resources/cucumber/features/WMS/01-Contexte/01-Initialisation.feature"), STD_INIT)
    write(os.path.join(std, "src/test/resources/cucumber/files/WMS/01-Contexte/01-Data.csv"), "a;b\n")
    root = str(tmp_path / "client_root")
    origin = str(tmp_path / "origin.git")
    os.makedirs(origin)
    git(origin, "init", "-q", "--bare", "-b", "integration")
    local = os.path.join(root, f"{CLIENT}-cucumber")
    os.makedirs(local)
    git(local, "init", "-q", "-b", "v1.3")
    for key, value in (("user.name", "t"), ("user.email", "t@example.com"), ("core.autocrlf", "false")):
        git(local, "config", key, value)
    for rel, text in rs.scaffold_files(CLIENT, "26.22", "@A_REVOIR").items():
        write(os.path.join(local, *rel.split("/")), text)
    git(local, "add", "-A")
    git(local, "commit", "-qm", "base")
    git(local, "remote", "add", "origin", origin)
    git(local, "push", "-q", "origin", "v1.3")
    state = ct.load_state(CLIENT)
    cs.record_step(state, "depot", "ok", "ok", {"depot": f"{CLIENT}-cucumber", "branche": "v1.3"})
    ct.save_state(CLIENT, state)
    return {"root": root, "std": std, "origin": origin}


AGENT_BRANCH = "feature/agent-cucumber-init-v1.3"
NO_STEP = lambda worktree: {"erreur": "test : step non écrit"}  # noqa: E731
STD_FILES_JAVA = ("package cucumber.features.clientx;\npublic class InitClientxFichiersStandardSteps {\n"
                  "    @Given(\"INIT- Initialisation globale par fichiers du Standard {string}\")\n"
                  "    public void init(String module) { fileIntegration.integrateStandard(module); }\n}\n")
STD_FILES_STEP = lambda worktree: {"chemin": si.std_files_java_path(CLIENT), "texte": STD_FILES_JAVA}  # noqa: E731


def _push(worktree, branch):
    done = __import__("subprocess").run(["git", "-C", worktree, "push", "-q", "origin", f"HEAD:refs/heads/{branch}"],
                                        capture_output=True, text=True)
    return "" if done.returncode == 0 else done.stderr


def _init_record():
    return ct.load_state(CLIENT)["simple"]["init"]


def test_init_generated_validated_pushed_then_run(init_workspace, main_module):
    w = init_workspace
    runs = []

    def run(profile, worktree):
        runs.append(profile)
        return {"ok": True, "message": "initialisation : 2 scénario(s) OK", "build": "https://jenkins/job/1/"}
    helpers = {"clear": lambda: {"mode": "client", "routes": ["wms/all"], "motif": "m", "preuves": ["wms : p"],
                                 "erreur": ""},
               "compile": lambda wt: {"ok": True, "erreurs": []}, "push": _push, "run": run, "std_files": NO_STEP}
    call = lambda action: main_module.run_simple_init(CLIENT, w["root"], w["std"], None, "26.22", action,  # noqa: E731
                                                      helpers)
    assert call("verifier") == 0
    record = _init_record()
    assert record["etat"] == "a_faire" and record["detail"]["plan"]["action"] == "appliquer"
    diff = record["detail"]["plan"]["diff"]
    assert 'par le clear "wms/all"' in diff and "01-Data.csv (fichier de données du Standard)" in diff
    assert "InitClientxSteps.java" in diff or f"Init{CLIENT.capitalize()}Steps.java" in diff
    assert "01-Contexte" not in git(w["origin"], "ls-tree", "-r", "--name-only", "v1.3")   # rien poussé avant validation
    assert call("appliquer") == 0 and _init_record()["detail"]["plan"]["action"] == "lancer"
    # L'agent n'écrit QUE sur sa branche ; la branche de la version reçoit un patch, appliqué par l'utilisateur
    pushed = git(w["origin"], "ls-tree", "-r", "--name-only", AGENT_BRANCH)
    assert f"src/test/resources/cucumber/features/{CLIENT}/01-Contexte/WMS/01-Initialisation.feature" in pushed
    assert f"src/test/resources/cucumber/files/{CLIENT}/01-Contexte/WMS/01-Data.csv" in pushed
    assert "01-Contexte" not in git(w["origin"], "ls-tree", "-r", "--name-only", "v1.3")
    patch = _init_record()["detail"]["patch"]
    assert os.path.exists(patch) and "01-Initialisation.feature" in open(patch, encoding="utf-8").read()
    assert call("verifier") == 0 and _init_record()["detail"]["plan"]["action"] == "lancer"   # rien à refaire
    assert call("lancer") == 0 and _init_record()["etat"] == "ok"
    assert runs[0]["verify_branch"] == AGENT_BRANCH and runs[0]["init_tags"] == f"@INIT_{CLIENT.upper()}_WMS"
    assert _init_record()["detail"]["build"] == "https://jenkins/job/1/"
    assert patch in _init_record()["resultat"]


def test_init_failure_proposes_known_correction(init_workspace, main_module):
    w = init_workspace
    timeout = ("org.awaitility.core.ConditionTimeoutException: Condition with cucumber.endPoints.excGs1.GS1EndPoints "
               "was not fulfilled within 6 minutes.")
    outcome = {"run": {"ok": False, "message": "environnement non initialisé", "build": "b",
                       "echecs": [{"fichier": "", "step": "INIT- Initialisation globale par fichiers via l'écran",
                                   "message": timeout}]}}
    helpers = {"clear": lambda: {"mode": "client", "routes": ["wms/all"], "motif": "m", "preuves": [], "erreur": ""},
               "compile": lambda wt: {"ok": True, "erreurs": []}, "push": _push, "std_files": NO_STEP,
               "run": lambda profile, worktree: outcome["run"]}
    call = lambda action: main_module.run_simple_init(CLIENT, w["root"], w["std"], None, "26.22", action,  # noqa: E731
                                                      helpers)
    assert call("appliquer") == 0
    assert call("lancer") == 1
    record = _init_record()
    assert record["etat"] == "echec" and record["detail"]["plan"]["action"] == "corriger"
    assert "délai" in record["resultat"]
    outcome["run"] = {"ok": True, "message": "OK", "build": "b2"}
    assert call("corriger") == 0 and _init_record()["etat"] == "ok"
    feature = git(w["origin"], "show", f"{AGENT_BRANCH}:src/test/resources/cucumber/features/{CLIENT}/01-Contexte/WMS/"
                                       f"01-Initialisation.feature")
    assert "avec un délai de" in feature and "TODO-VERSION" in feature


STANDARD_CLEAR = {"mode": "standard", "routes": ["full/all"], "motif": "full déployée", "preuves": ["full : p"],
                  "erreur": ""}


def test_standard_init_used_as_is_without_copy(init_workspace, main_module):
    """Purge du Standard et aucun écart pour ce client : init du Standard lancée telle quelle, aucune copie."""
    w = init_workspace
    runs = []
    helpers = {"clear": lambda: STANDARD_CLEAR, "compile": lambda wt: {"ok": True, "erreurs": []}, "push": _push,
               "run": lambda profile, worktree: runs.append(profile) or {"ok": True, "message": "OK", "build": "b1"}}
    call = lambda action: main_module.run_simple_init(CLIENT, w["root"], w["std"], None, "26.22", action,  # noqa: E731
                                                      helpers)
    assert call("verifier") == 0
    record = _init_record()
    assert record["detail"]["plan"]["action"] == "lancer" and "diff" not in record["detail"]["plan"]
    assert any("utilisée telle quelle" in r for r in record["detail"]["remarques"])
    assert call("lancer") == 0 and _init_record()["etat"] == "ok"
    assert runs == [{**runs[0], "verify_branch": AGENT_BRANCH, "init_tags": "@INIT_WMS"}]
    assert "01-Contexte" not in git(w["origin"], "ls-tree", "-r", "--name-only", AGENT_BRANCH)   # aucune copie
    assert "patch" not in _init_record()["detail"]


def test_standard_init_not_run_by_the_job_falls_back_to_adapted_copy(init_workspace, main_module):
    w = init_workspace
    outcome = {"run": {"ok": False, "message": "environnement non initialisé (@INIT_WMS) : aucun scénario pour "
                                               "@INIT_WMS", "build": "b"}}
    helpers = {"clear": lambda: STANDARD_CLEAR, "compile": lambda wt: {"ok": True, "erreurs": []}, "push": _push,
               "run": lambda profile, worktree: outcome["run"], "std_files": NO_STEP}
    call = lambda action: main_module.run_simple_init(CLIENT, w["root"], w["std"], None, "26.22", action,  # noqa: E731
                                                      helpers)
    assert call("lancer") == 1
    assert _init_record()["detail"]["standard_indisponible"] == ["WMS"]
    assert call("verifier") == 0
    record = _init_record()
    assert record["detail"]["plan"]["action"] == "appliquer" and "le job n'exécute pas" in record["resultat"]
    assert "Purge de l'environnement" in record["detail"]["plan"]["diff"]          # copie avec la purge du Standard


def test_client_specifics_go_to_separate_complements(init_workspace, main_module, tmp_path):
    """Sujets propres au client : compléments à part (patch validé), lancés après l'init du Standard."""
    w = init_workspace
    findings = {"constats": [{"type": "champ_personnalise", "message": "1 champ personnalisé : customBoolean1"}]}
    runs = []

    def complements(what, worktree, plan):
        if what == "constats":
            return findings["constats"]
        rel = f"src/test/resources/cucumber/features/{CLIENT}/01-Contexte/WMS/99-Complements-initialisation.feature"
        path = os.path.join(worktree, *rel.split("/"))
        write(path, f"@INIT_{CLIENT.upper()}_WMS_COMPLEMENT\nFeature: Compléments\n\n  Scenario: Champ\n"
                    f"    Given CONFIG- Choix de la langue\n")
        git(worktree, "add", "-A")
        git(worktree, "commit", "-qm", "complements")
        patch = str(tmp_path / "complements.patch")
        with open(patch, "w", encoding="utf-8") as f:
            f.write(git(worktree, "format-patch", "-1", "--stdout"))
        git(worktree, "reset", "-q", "--hard", "HEAD~1")
        return {"patch": patch, "fichiers": [rel]}
    helpers = {"clear": lambda: STANDARD_CLEAR, "compile": lambda wt: {"ok": True, "erreurs": []}, "push": _push,
               "complements": complements,
               "run": lambda profile, worktree: runs.append(profile["init_tags"]) or {"ok": True, "message": "OK",
                                                                                     "build": "b"}}
    call = lambda action: main_module.run_simple_init(CLIENT, w["root"], w["std"], None, "26.22", action,  # noqa: E731
                                                      helpers)
    assert call("verifier") == 0
    record = _init_record()
    assert record["detail"]["plan"]["action"] == "completer" and "99-Complements" in record["detail"]["plan"]["diff"]
    assert "l'init n'est pas modifiée" in record["resultat"]
    findings["constats"] = []                                    # couverts une fois les compléments présents
    assert call("completer") == 0 and _init_record()["detail"]["plan"]["action"] == "lancer"
    assert "99-Complements" in git(w["origin"], "ls-tree", "-r", "--name-only", AGENT_BRANCH)
    assert call("lancer") == 0
    assert runs == ["@INIT_WMS", f"@INIT_{CLIENT.upper()}_WMS_COMPLEMENT"]     # Standard, puis compléments


def test_complements_with_standard_init_get_their_own_tag(init_workspace, main_module, monkeypatch, tmp_path):
    """Chemin réel : compléments construits à côté de l'emplacement client de l'init du Standard, tag _COMPLEMENT."""
    w = init_workspace
    from tools import cucumber_init
    seen = {}

    def fake_build(local, std, client, base, profile, status, anchors, imports, recreate, writer):
        seen.update(base=base, anchors=anchors)
        wt = cucumber_init.add_worktree(local, cucumber_init.init_worktree_dir(client), cucumber_init.init_branch(client),
                                        base, reset=True)
        rel = anchors[0].rsplit("/", 1)[0] + "/99-Complements-initialisation.feature"
        write(os.path.join(wt, *rel.split("/")), f"@INIT_{CLIENT.upper()}\nFeature: Compléments\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "c")
        patch = str(tmp_path / "c.patch")
        cucumber_init.format_patch(wt, f"{base}..HEAD", patch)
        return {"patch": patch, "fichiers": [rel], "avertissements": []}
    monkeypatch.setattr(cucumber_init, "build_init_complements", fake_build)
    local = os.path.join(w["root"], f"{CLIENT}-cucumber")
    worktree = ct.add_worktree(local, str(tmp_path / "wt"), f"cucumber_{CLIENT}_version", "v1.3", reset=True)
    std_rel = "src/test/resources/cucumber/features/WMS/01-Contexte/01-Initialisation.feature"
    plan = {"present": {}, "standard": {"WMS": {"tags": "@INIT_WMS", "textes": {std_rel: STD_INIT}}}}
    built = main_module.simple_init_build_complements(CLIENT, local, worktree, w["root"], w["std"], {}, plan,
                                                      [{"type": "champ_personnalise"}], ["WMS"])
    assert seen["base"] == f"cucumber_{CLIENT}_version"
    assert seen["anchors"] == [f"src/test/resources/cucumber/features/{CLIENT}/01-Contexte/WMS/01-Initialisation.feature"]
    text = open(built["patch"], encoding="utf-8").read()
    assert f"+@INIT_{CLIENT.upper()}_WMS_COMPLEMENT" in text and f"+@INIT_{CLIENT.upper()}\n" not in text


CLIENT_CLEAR = {"mode": "client", "routes": ["wms/all"], "motif": "m", "preuves": ["wms : p"], "erreur": ""}
DATA = f"src/test/resources/cucumber/files/{CLIENT}/01-Contexte/WMS/01-Data.csv"
FEATURE = f"src/test/resources/cucumber/features/{CLIENT}/01-Contexte/WMS/01-Initialisation.feature"


def test_data_files_read_from_the_standard_not_copied(init_workspace, main_module):
    """Init adaptée (clear du client) : fichiers de données lus dans le Standard par un step du projet, aucune copie."""
    w = init_workspace
    runs = []
    helpers = {"clear": lambda: CLIENT_CLEAR, "compile": lambda wt: {"ok": True, "erreurs": []}, "push": _push,
               "std_files": STD_FILES_STEP,
               "run": lambda profile, worktree: runs.append(profile["init_tags"]) or {"ok": True, "message": "OK",
                                                                                     "build": "b"}}
    call = lambda action: main_module.run_simple_init(CLIENT, w["root"], w["std"], None, "26.22", action,  # noqa: E731
                                                      helpers)
    assert call("verifier") == 0
    diff = _init_record()["detail"]["plan"]["diff"]
    assert 'INIT- Initialisation globale par fichiers du Standard "WMS"' in diff and "01-Data.csv" not in diff
    assert "FichiersStandardSteps.java" in diff
    assert call("appliquer") == 0
    pushed = git(w["origin"], "ls-tree", "-r", "--name-only", AGENT_BRANCH)
    assert FEATURE in pushed and DATA not in pushed and si.std_files_java_path(CLIENT) in pushed
    patch = open(_init_record()["detail"]["patch"], encoding="utf-8").read()
    assert "01-Data.csv" not in patch and "fichiers du Standard" in patch
    assert call("lancer") == 0 and runs == [f"@INIT_{CLIENT.upper()}_WMS"]


def test_agent_copy_replaced_by_standard_files_without_force(init_workspace, main_module):
    """Copie déjà poussée par l'agent (pas sur la branche de la version) : régénérée, copies retirées, sans forcer."""
    w = init_workspace
    base = {"clear": lambda: CLIENT_CLEAR, "compile": lambda wt: {"ok": True, "erreurs": []}, "push": _push}
    call = lambda action, extra: main_module.run_simple_init(  # noqa: E731
        CLIENT, w["root"], w["std"], None, "26.22", action, {**base, **extra})
    assert call("appliquer", {"std_files": NO_STEP}) == 0                       # ancienne version : copie
    assert DATA in git(w["origin"], "ls-tree", "-r", "--name-only", AGENT_BRANCH)
    first = git(w["origin"], "rev-parse", AGENT_BRANCH).strip()
    assert call("verifier", {"std_files": STD_FILES_STEP}) == 0
    record = _init_record()
    assert record["detail"]["plan"]["action"] == "appliquer" and f"- {DATA} (retiré" in record["detail"]["plan"]["diff"]
    assert call("appliquer", {"std_files": STD_FILES_STEP}) == 0
    pushed = git(w["origin"], "ls-tree", "-r", "--name-only", AGENT_BRANCH)
    assert DATA not in pushed and FEATURE in pushed
    assert git(w["origin"], "merge-base", "--is-ancestor", first, AGENT_BRANCH) == ""     # historique conservé
    patch = open(_init_record()["detail"]["patch"], encoding="utf-8").read()
    assert "01-Data.csv" not in patch                                                       # patch net vs v1.3


def test_standard_files_unreadable_at_runtime_fall_back_to_copy(init_workspace, main_module):
    w = init_workspace
    outcome = {"run": {"ok": False, "message": "environnement non initialisé", "build": "b",
                       "echecs": [{"fichier": FEATURE, "step": 'INIT- Initialisation globale par fichiers du Standard "WMS"',
                                   "message": "dossier files/WMS/01-Contexte introuvable"}]}}
    helpers = {"clear": lambda: CLIENT_CLEAR, "compile": lambda wt: {"ok": True, "erreurs": []}, "push": _push,
               "std_files": STD_FILES_STEP, "run": lambda profile, worktree: outcome["run"]}
    call = lambda action: main_module.run_simple_init(CLIENT, w["root"], w["std"], None, "26.22", action,  # noqa: E731
                                                      helpers)
    assert call("appliquer") == 0 and call("lancer") == 1
    assert _init_record()["detail"]["fichiers_copies"] == ["WMS"]
    assert call("verifier") == 0
    diff = _init_record()["detail"]["plan"]["diff"]
    assert "01-Data.csv (fichier de données du Standard)" in diff
    assert "+    And INIT- Initialisation globale par fichiers via l'écran" in diff


def test_std_files_java_checks():
    assert si.check_std_files_java(STD_FILES_JAVA) == []
    assert si.check_std_files_java("") and si.check_std_files_java(STD_FILES_JAVA.replace(
        "fileIntegration.integrateStandard(module);", "Files.copy(a, b);"))
    extract = STD_FILES_JAVA.replace("fileIntegration.integrateStandard(module);",
                                     'Path d = Paths.get("target", "fichiers-standard"); Files.copy(in, d.resolve(n));')
    assert si.check_std_files_java(extract) == []                      # extraction temporaire sous target/ permise
    assert si.check_std_files_java(extract.replace("Path d =", 'Files.write(Paths.get("src/test/resources/x"), b); Path d ='))
    assert si.check_std_files_java(STD_FILES_JAVA.replace("{ fileIntegration.integrateStandard(module); }", "{ }"))


def test_library_jar_must_embed_the_standard_data(tmp_path):
    import zipfile
    folder = tmp_path / "a-sis" / "Cucumber" / "26.22"
    folder.mkdir(parents=True)
    jar = folder / "Cucumber-26.22-tests.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("cucumber/files/WMS/01-Contexte/01-RefSiteDraft.xlsm", b"x")
    assert si.library_jar("26.22", str(tmp_path)) == str(jar)
    assert si.jar_has_data(str(jar), "wms") and not si.jar_has_data(str(jar), "TMS")
    assert si.library_jar("26.24", str(tmp_path)) == ""


def test_init_stops_without_proven_clear(init_workspace, main_module):
    w = init_workspace
    code = main_module.run_simple_init(CLIENT, w["root"], w["std"], None, "26.22", "verifier",
                                       {"clear": lambda: {"mode": "", "routes": [], "preuves": [], "motif": "",
                                                          "erreur": "aucune ressource de clear déployée pour WMS"}})
    assert code == 1 and "Clear non déterminé" in _init_record()["resultat"]


# ---------------------------------------------------------------------------
# Étape 5 : résultat par fonctionnalité
# ---------------------------------------------------------------------------

def test_feature_result_green_only_when_executed_and_relevant():
    rel = "src/test/resources/cucumber/features/x/A.feature"
    lot = {"ids": ["a"], "statut": "termine",
           "resultats": [{"id": "a", "statut": "couvert", "fichiers": [rel]}],
           "execution": {"message": "", "scenarios": {
               "@t1": {"id": f"{rel}::S1", "statut": "pertinent", "build": "https://jenkins/b/3/"},
               "@t2": {"id": f"{rel}::S2", "statut": "pertinent", "build": "https://jenkins/b/3/"}}}}
    green = cs.feature_result({"lots": [lot]}, "a", "WMS — A")
    assert green["vert"] and "2 scénario(s)" in green["resultat"]
    lot["execution"]["scenarios"]["@t2"]["statut"] = "non_pertinent"
    assert not cs.feature_result({"lots": [lot]}, "a")["vert"]
    lot["execution"]["scenarios"] = {}
    assert "non vérifiés" in cs.feature_result({"lots": [lot]}, "a")["resultat"]
    lot["resultats"][0]["statut"] = "non_couvert"
    lot["resultats"][0]["resume"] = "pas de donnée"
    assert cs.feature_result({"lots": [lot]}, "a")["resultat"] == "Non couverte : pas de donnée"
    assert not cs.feature_result({"lots": []}, "b")["vert"]
    merged = cs.merge_features([{"id": "a", "vert": False}, {"id": "b", "vert": True}], [{"id": "a", "vert": True}])
    assert merged == [{"id": "b", "vert": True}, {"id": "a", "vert": True}]
