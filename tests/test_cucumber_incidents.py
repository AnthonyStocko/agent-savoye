"""Registre des incidents résolus : signature reconnue et corrigée sans Claude, report au changement de version,
relance unique sur conflit de verrouillage."""
import json
import os
import subprocess
from datetime import datetime
from types import SimpleNamespace
import importlib
import io
import sys

import pytest

from tools import client_version, cucumber_failures, cucumber_incidents, cucumber_profile, cucumber_tools

CLEAR_STEP = 'INIT- Purge de l\'environnement par le clear "{}"'
NO_ROUTE = "java.lang.AssertionError: DELETE clear/wms/all : 404 - No route found"
STALE = ("java.lang.AssertionError: DELETE clear/wms/all : 500 - org.hibernate.StaleStateException: Batch update "
         "returned unexpected row count from update [0]; statement executed: delete from wms_device_identifier")


def load_main():
    """main.py rouvre sys.stdout à l'import : importé derrière des flux temporaires (comme test_cucumber_import)."""
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


def git(cwd, *args):
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cucumber_tools, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(cucumber_tools, "STD_CACHE_DIR", str(tmp_path / "cache" / "_standard"))
    monkeypatch.setattr(client_version, "KIS_REGISTRY", str(tmp_path / "cache" / "_standard" / "kis.json"))
    monkeypatch.setenv("AGENT_KNOWLEDGE_DIR", str(tmp_path / "partage"))
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@t")
    git(work, "config", "user.name", "t")
    (work / "Init.feature").write_text("Feature: init\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")
    return tmp_path, str(work)


def write_profile(client, clear="init-data-rest-clear-main-wms", version="1.3.0", kis="26.16", standard="26.22"):
    document = cucumber_profile.load_profile(client)
    document.setdefault("detecte", {})["clear_name"] = clear
    document["version_client"] = {"choix": {"id": version, "type": "tag", "ref": f"app-{version}"},
                                  "deduction": {"version_client": version, "standard": {"version": standard},
                                                "jenkins": {"kis_version": kis}, "kis": {"kis": kis}}}
    cucumber_tools._write_json(cucumber_profile.profile_path(client), document)


def profile(client):
    return cucumber_profile.effective_profile(cucumber_profile.load_profile(client))


def failed(step, message, build="b/1"):
    return {"ok": False, "build": build, "message": message, "init_en_echec": {"step": step, "message": message}}


def learn_clear_incident(client, work):
    """Échec du clear (404), clear corrigé dans le profil et step du projet ajouté, puis init verte."""
    write_profile(client, clear="init-data-rest-clear-main-autre")
    cucumber_incidents.observe(client, profile(client), failed(CLEAR_STEP.format("init-data-rest-clear-main-autre"),
                                                               NO_ROUTE), work)
    write_profile(client, clear="init-data-rest-clear-main-wms")
    os.makedirs(os.path.join(work, "steps"), exist_ok=True)
    with open(os.path.join(work, "steps", "ClearSteps.java"), "w", encoding="utf-8") as f:
        f.write("class ClearSteps {}\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "clear du client")
    return cucumber_incidents.observe(client, profile(client), {"ok": True, "build": "b/2", "numero": 7}, work)


def test_resolved_incident_is_recorded_with_its_correction_and_proof(env):
    _, work = env
    out = learn_clear_incident("acme", work)
    assert out["enregistres"] == [1]
    entry = cucumber_incidents.load("acme")["incidents"][0]
    assert entry["signature"] == {"step": "init- purge de l'environnement par le clear \"…\"",
                                  "motif": "No route found"}
    kinds = [a["type"] for a in entry["correction"]["actions"]]
    assert kinds == ["profil", "step_projet"]
    assert entry["correction"]["actions"][0]["champs"] == {"clear_name": "init-data-rest-clear-main-wms"}
    assert entry["preuves"][-1]["build"] == "b/2" and entry["versions"] == ["1.3.0"]
    # Réglages prouvés de la version, et registre global partagé (fichier versionné)
    data = cucumber_incidents.load("acme")
    assert data["reglages"]["1.3.0"]["reglages"]["clear_name"] == "init-data-rest-clear-main-wms"
    assert data["reglages"]["1.3.0"]["steps"] == [1] and data["format"] == 1 and data["revision"] >= 2
    shared = cucumber_incidents.load_global()
    assert shared["incidents"][0]["clients"] == ["acme"] and shared["revision"] >= 1


def test_known_signature_is_applied_without_claude_then_verified(env, monkeypatch):
    main = load_main()
    tmp_path, work = env
    learn_clear_incident("acme", work)
    # Nouvelle copie de l'init (autre version) : clear d'un autre client revenu, step du projet absent
    write_profile("acme", clear="init-data-rest-clear-main-autre")
    git(work, "reset", "-q", "--hard", "HEAD~1")
    analysis = {"init_en_echec": {"id": "Init.feature::init", "step": CLEAR_STEP.format("x-y"), "message": NO_ROUTE}}
    monkeypatch.setattr(main.cucumber_import, "load_analysis", lambda client, numero: analysis)
    monkeypatch.setattr(main, "init_workspace", lambda args, repo, prof: (work, "origin/v1.3"))
    monkeypatch.setattr(main, "run_cucumber_claude", lambda *a, **k: pytest.fail("appel à Claude"))
    delivered = {}

    def deliver(args, prof, worktree, start, changed, message, init, action, lock_retry=True, applied=None):
        delivered.update(changed=changed, action=action, applied=applied, clear=prof["clear_name"])
        return 0
    monkeypatch.setattr(main, "deliver_init_correction", deliver)
    args = SimpleNamespace(client_name="acme", cucumber_repo="", cucumber_base_branch="")
    assert main.fix_init_known_incident(args, 3, "") == 0
    assert delivered["changed"] == ["steps/ClearSteps.java"] and delivered["clear"] == "init-data-rest-clear-main-wms"
    assert delivered["action"].startswith("incident connu (n° 1), correction appliquée")
    assert delivered["applied"]["numero"] == 1
    assert os.path.exists(os.path.join(work, "steps", "ClearSteps.java"))
    # Bilan : incident connu ; autre échec : incident nouveau, analyse habituelle (None)
    assert cucumber_incidents.describe("acme", CLEAR_STEP.format("z"), NO_ROUTE)["libelle"].startswith(
        "incident connu (n° 1)")
    analysis["init_en_echec"] = {"step": "INIT- Autre", "message": "NullPointerException"}
    assert main.fix_init_known_incident(args, 4, "") is None
    assert cucumber_incidents.describe("acme", "INIT- Autre", "NullPointerException")["libelle"] == "incident nouveau"
    # Autre client : incident du registre global, donné en contexte (valeurs propres au client non appliquées)
    other = cucumber_incidents.lookup("globex", CLEAR_STEP.format("q"), NO_ROUTE)
    assert other["numero"] == "G1" and not other["applicable"]
    assert "n° G1 (global)" in cucumber_incidents.context_block("globex", [{"step": CLEAR_STEP.format("q"),
                                                                             "message": NO_ROUTE}])


def test_proven_settings_are_carried_over_and_revalidated_before_generation(env, monkeypatch):
    main = load_main()
    _, work = env
    learn_clear_incident("acme", work)
    old = cucumber_profile.load_profile("acme")["version_client"]["deduction"]
    # Nouvelle version : KIS inconnue, clear détecté différent
    write_profile("acme", clear="init-data-rest-clear-main-autre", version="1.4.0", kis="", standard="26.24")
    new = cucumber_profile.load_profile("acme")["version_client"]["deduction"]
    revalidation = cucumber_incidents.carry_over("acme", old, new)
    assert revalidation["statut"] == "a_faire" and (revalidation["de"], revalidation["vers"]) == ("1.3.0", "1.4.0")
    reports = {r["reglage"]: r for r in revalidation["reports"]}
    assert reports["kis_version"]["valeur"] == "26.16" and reports["kis_version"]["preuve"] == "b/2"
    assert reports["clear_name"]["applique"] and reports["steps"]["valeur"] == ["n° 1"]
    applied = profile("acme")
    assert applied["clear_name"] == "init-data-rest-clear-main-wms"
    assert applied["version_client"]["deduction"]["jenkins"]["kis_version"] == "26.16"
    # Revalidation avant toute génération : rouge → écart de version, avec l'ancien réglage et sa preuve
    git(work, "reset", "-q", "--hard", "HEAD~1")
    runs = []

    def red(client, prof, worktree, repository, mode, lock_retry=True, applied=None):
        runs.append(worktree)
        return failed(CLEAR_STEP.format("init-data-rest-clear-main-wms"), NO_ROUTE, "b/9")
    monkeypatch.setattr(main, "run_init_and_import", red)
    assert main.revalidate_version("acme", profile("acme"), work, "", "branche") is False
    assert os.path.exists(os.path.join(work, "steps", "ClearSteps.java")), "step de contournement reporté"
    gap = cucumber_incidents.revalidation("acme")
    assert gap["statut"] == "ecart" and gap["ecart"][0].startswith("Écart de version 1.3.0 → 1.4.0")
    assert any("clear de la version 1.3.0 : init-data-rest-clear-main-wms — preuve : init verte, build b/2" in line
               for line in gap["ecart"])
    # Vert : réglages prouvés pour la nouvelle version, KIS reportée enregistrée pour son Standard
    def green(client, prof, worktree, repository, mode, lock_retry=True, applied=None):
        return cucumber_incidents.observe(client, prof, {"ok": True, "build": "b/10"}, worktree) and {"ok": True}
    monkeypatch.setattr(main, "run_init_and_import", green)
    assert main.revalidate_version("acme", profile("acme"), work, "", "branche") is True
    assert cucumber_incidents.revalidation("acme")["statut"] == "ok"
    assert "1.4.0" in cucumber_incidents.load("acme")["reglages"]
    assert client_version.load_kis_registry()["26.24"]["kis"] == "26.16"
    assert main.revalidate_version("acme", profile("acme"), work, "", "branche") is True


def test_version_choice_carries_settings_over(env, monkeypatch):
    _, work = env
    learn_clear_incident("acme", work)
    monkeypatch.setattr(client_version, "library_at_version", lambda *a: {})
    monkeypatch.setattr(client_version, "cucumber_std_refs_for", lambda *a: [])
    monkeypatch.setattr(client_version, "compute", lambda *a, **k: {"version_client": "1.4.0", "jenkins": {},
                                                                   "standard": {"version": "26.24"}})
    deduction = client_version.choose("acme", {"id": "1.4.0"}, "", "", "")
    assert deduction["jenkins"]["kis_version"] == "26.16" and deduction["kis"]["source"] == "report"
    assert cucumber_incidents.pending("acme", "1.4.0")["statut"] == "a_faire"


def test_lock_conflict_is_retried_once_then_concurrency_is_checked():
    calls, checked = [], []

    def run_fn(results):
        def run():
            calls.append(1)
            return results[len(calls) - 1]
        return run
    lock = {"ok": False, "build": "b/1", "message": "init", "echecs": [{"message": STALE}]}
    out = cucumber_incidents.run_with_lock_retry(run_fn([lock, lock, lock]), lambda r: checked.append(r) or {
        "message": "schéma x utilisé par b/0"}, log=lambda *_: None)
    assert len(calls) == 2 and len(checked) == 1 and out["relance"] == 1
    assert out["concurrence"]["message"] == "schéma x utilisé par b/0"
    calls.clear()
    out = cucumber_incidents.run_with_lock_retry(run_fn([lock, {"ok": True, "build": "b/2"}]), log=lambda *_: None)
    assert len(calls) == 2 and out["ok"] and "concurrence" not in out
    calls.clear()
    other = {"ok": False, "message": "NullPointerException", "echecs": []}
    assert cucumber_incidents.run_with_lock_retry(run_fn([other]), log=lambda *_: None)["relance"] == 0
    assert len(calls) == 1
    # Classement : environnement, conflit de verrouillage, jamais un défaut du test
    got = cucumber_failures.classify({"message": STALE, "step": CLEAR_STEP.format("init-data-rest-clear-main-wms")})
    assert (got["categorie"], got["nature"]) == ("environnement", "conflit_verrouillage")
    assert "wms_device_identifier" in got["cause"] and "copie de base" in got["action"]


def test_concurrency_names_builds_and_db_copies_on_the_same_schema():
    start = datetime(2026, 11, 10, 8, 0).timestamp() * 1000
    build = {"url": "j/1", "debut": start, "duree": 600_000,
             "parametres": {"ADDITIONAL_ARGLINE": "-Ddb.schema=cucumber_acme -Dx=1"}}
    builds = [build,
              {"url": "j/2", "debut": start + 120_000, "duree": 300_000, "parametres": {"DB_SCHEMA": "cucumber_acme"}},
              {"url": "j/3", "debut": start + 60_000, "duree": 300_000, "parametres": {"DB_SCHEMA": "autre"}},
              {"url": "j/4", "debut": start - 7_200_000, "duree": 60_000, "parametres": {"DB_SCHEMA": "cucumber_acme"}}]
    copies = [{"date": datetime(2026, 11, 10, 8, 5).isoformat(), "duree_s": 120, "cible": "pg / cucumber_acme"},
              {"date": datetime(2026, 11, 10, 6, 0).isoformat(), "duree_s": 120, "cible": "pg / cucumber_acme"}]
    found = cucumber_incidents.concurrency("acme", build, builds, copies)
    assert found["schema"] == "cucumber_acme"
    assert [b.split(" ")[0] for b in found["builds"]] == ["j/2"] and len(found["copies"]) == 1
    assert "utilisé en même temps" in found["message"]
    alone = cucumber_incidents.concurrency("acme", build, [build], [])
    assert "aucun autre build ni aucune copie de base" in alone["message"]


def test_signature_names_the_known_motifs():
    assert cucumber_incidents.motif("java.lang.UnsupportedClassVersionError: x has been compiled by a more recent "
                                    "version") == "UnsupportedClassVersionError"
    assert cucumber_incidents.motif("org.awaitility.core.ConditionTimeoutException: Condition ... within 360 "
                                    "seconds.") == "ConditionTimeoutException 360 seconds"
    assert cucumber_incidents.motif("Adresse de livraison incohérente") == "Adresse de livraison incohérente"
    assert cucumber_incidents.motif('Class X has no field "additionalProperties"') == \
        "has no field additionalProperties"
    assert cucumber_incidents.motif(STALE) == "conflit de verrouillage"
    assert cucumber_incidents.step_key(CLEAR_STEP.format("a")) == cucumber_incidents.step_key(CLEAR_STEP.format("b"))


def test_registry_is_wired_in_runs_imports_and_generation():
    root = os.path.join(os.path.dirname(__file__), "..")
    text = open(os.path.join(root, "main.py"), encoding="utf-8").read()
    assert "cucumber_incidents.observe(client, profile, result, worktree, applied)" in text
    assert "code = fix_init_known_incident(args, args.fix_init_from_import, cucumber_repo_dir)" in text
    assert "not revalidate_version(" in text and text.count("cucumber_incidents.context_block(") >= 3
    assert "cucumber_incidents.lookup(client, failure.get(\"step\", \"\")" in text
    imported = open(os.path.join(root, "tools", "cucumber_import.py"), encoding="utf-8").read()
    assert "cucumber_incidents.describe(" in imported
    json.dumps(cucumber_incidents._empty("x"))


def test_proven_settings_are_bootstrapped_from_green_imports(env):
    write_profile("acme")
    document = cucumber_profile.load_profile("acme")
    document["detecte"]["job_roles"] = {"composant_version": "COMPONENT_VERSION", "kis_version": "KIS_VERSION"}
    cucumber_tools._write_json(cucumber_profile.profile_path("acme"), document)
    params = {"http://j/317": {"COMPONENT_VERSION": "1.3.0", "KIS_VERSION": "3.12"},
              "http://j/290": {"COMPONENT_VERSION": "1.3.0", "KIS_VERSION": "3.11"},
              "http://j/363": {"COMPONENT_VERSION": "1.5-SNAPSHOT", "KIS_VERSION": "4.0"}}
    imports = [{"numero": 14, "build": "http://j/290", "analyse": {"tests": 4}},
               {"numero": 17, "build": "http://j/317", "analyse": {"tests": 4}},
               {"numero": 20, "build": "http://j/363", "analyse": {"tests": 4, "init_en_echec": {"step": "x"}}}]
    assert cucumber_incidents.bootstrap("acme", params.get, imports) == ["1.3.0"]
    proven = cucumber_incidents.load("acme")["reglages"]["1.3.0"]
    assert proven["build"] == "http://j/317" and proven["reglages"]["kis_version"] == "3.12"
    assert cucumber_incidents.bootstrap("acme", params.get, imports) == []
