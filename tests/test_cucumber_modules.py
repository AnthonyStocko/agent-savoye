"""Modules testés d'un client fictif à deux modules (WMS, TMS) : TMS exclu partout, retrait du tag client réversible."""
import json
import os

import pytest

from tests.conftest import CLEAR, CLIENT, CLIENT_TAG, git, write

from tools import cucumber_checks as cc
from tools import cucumber_coverage as cov
from tools import cucumber_failures as cf
from tools import cucumber_import as cimp
from tools import cucumber_init as ci
from tools import cucumber_jobs as cjobs
from tools import cucumber_journey as cj
from tools import cucumber_modules as cm
from tools import cucumber_profile as cp
from tools import cucumber_tools as ct

RES = ci.RESOURCES_DIR
FEATURES = f"{RES}/features"
STD_INIT = ("@INIT_{m} @{m}\n"
            "Feature: 01 - Initialisation des données\n\n"
            "  Scenario: Intégration des données\n"
            "    Given INIT- Purge de l'environnement\n"
            "    And INIT- Initialisation globale par fichiers via l'écran\n"
            "    And INIT- Purge de l'environnement par le clear \"tms/all\"\n"
            "    And INIT- Configuration TMS :\n"
            "      | cle | valeur |\n"
            "      | a   | b      |\n")
CLIENT_WMS = f"@WMS {CLIENT_TAG}\nFeature: Clientx - Préparation\n\n  @agent_clientx_1\n  Scenario: Prépare\n    Given x\n"
CLIENT_TMS = (f"@TMS {CLIENT_TAG}\nFeature: Clientx - Transport\n\n  {CLIENT_TAG} @agent_clientx_2\n"
              f"  Scenario: Expédie\n    Given y\n")
INIT_TMS = f"@INIT_CLIENTX_TMS {CLIENT_TAG} @TMS\nFeature: Clientx - 01 - Initialisation des données\n"
INIT_WMS = f"@INIT_CLIENTX_WMS {CLIENT_TAG} @WMS\nFeature: Clientx - 01 - Initialisation des données\n"
WMS_FEATURE = f"{FEATURES}/clientx/WMS/03-Preparation/SPEC-1.feature"
TMS_FEATURE = f"{FEATURES}/clientx/TMS/02-Transport/SPEC-2.feature"
INIT_WMS_PATH = f"{FEATURES}/clientx/01-Contexte/WMS/01-Initialisation.feature"
INIT_TMS_PATH = f"{FEATURES}/clientx/01-Contexte/TMS/01-Initialisation.feature"


def _spec(spec_id, depot, path):
    return {"id": spec_id, "libelle": f"{depot} · {spec_id}", "type": "extension", "nature": "objet métier",
            "depot": depot, "dossier": spec_id, "nb_fichiers": 1, "mots_cles": [], "couverture": "non_couvert",
            "fichiers": [{"repo": depot, "path": path}]}


INVENTORY = {"specifiques": [
    _spec("sp-wms", "wms-content-backend", "wms-content-core/src/main/java/a/Stock.java"),
    _spec("sp-tms", "odatio-clientx", "clientx-tms-organization/src/main/java/b/Carrier.java"),
    _spec("sp-neutre", "odatio-clientx", "clientx-util/src/main/java/c/Util.java")]}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(cc, "WORKSPACE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def std_dir(tmp_path):
    """Standard : init WMS (qui purge aussi le TMS) et TMS, OMS non utilisé par le client."""
    root = str(tmp_path / "std")
    for module in ("WMS", "TMS", "OMS"):
        write(os.path.join(root, RES, "features", module, "01-Contexte", "01-Initialisation.feature"),
              STD_INIT.format(m=module))
        write(os.path.join(root, RES, "files", module, "01-Contexte", f"01-{module}Site.csv"), "code\nA\n")
    return root


@pytest.fixture
def client_repo(repo):
    """Dépôt clientx-cucumber : features WMS et TMS, init par module, sur integration."""
    for rel, text in ((WMS_FEATURE, CLIENT_WMS), (TMS_FEATURE, CLIENT_TMS), (INIT_WMS_PATH, INIT_WMS),
                      (INIT_TMS_PATH, INIT_TMS)):
        write(os.path.join(repo, *rel.split("/")), text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def _profile(cache, client_repo, std_dir, excluded=("TMS",)):
    """Profil enregistré : modules détectés dans le dépôt, init des deux modules, TMS décoché."""
    ct.save_inventory(CLIENT, INVENTORY)
    detected = {**cp.empty_profile(), "tag_client": CLIENT_TAG, "clear_name": CLEAR,
                "init_features": [INIT_TMS_PATH, INIT_WMS_PATH],
                "init_tags": "@INIT_CLIENTX_TMS or @INIT_CLIENTX_WMS",
                "modules": cm.detect_in_worktree(client_repo, CLIENT, std_dir, CLIENT_TAG)}
    with open(cp.profile_path(CLIENT), "w", encoding="utf-8") as f:
        json.dump({"detecte": detected, "saisi": {}}, f)
    cm.save_excluded(CLIENT, excluded)
    return cp.effective_profile(cp.load_profile(CLIENT))


def test_modules_detected_from_features_and_inventory_without_hard_list(cache, client_repo, std_dir):
    ct.save_inventory(CLIENT, INVENTORY)
    found = {m["code"]: m for m in cm.detect_in_worktree(client_repo, CLIENT, std_dir, CLIENT_TAG)}
    assert set(found) == {"WMS", "TMS"}                  # OMS : au Standard, absent chez le client ; CLIENTX : tag client
    assert found["TMS"]["tag"] == "@TMS"
    assert any("inventaire" in s for s in found["TMS"]["sources"])
    # Module inconnu du code : détecté par le seul couple dossier / tag
    texts = {f"{FEATURES}/clientx/WES/a.feature": f"@WES {CLIENT_TAG}\nFeature: a\n"}
    assert [m["code"] for m in cm.detect(texts, {}, {}, CLIENT_TAG, CLIENT)] == ["WES"]
    assert cm.std_codes(std_dir) == {"WMS": "@WMS", "TMS": "@TMS", "OMS": "@OMS"}


def test_excluded_module_has_no_init_and_no_trace_in_other_inits(cache, client_repo, std_dir):
    profile = _profile(cache, client_repo, std_dir)
    assert profile["init_features"] == [INIT_WMS_PATH]
    assert profile["init_tags"] == "@INIT_CLIENTX_WMS"
    assert profile["init_features_tous"] == [INIT_TMS_PATH, INIT_WMS_PATH]
    result = ci.build_client_init(client_repo, std_dir, CLIENT, "integration", {"WMS", "TMS"}, profile)
    assert result["modules"] == ["WMS"] and result["exclus"] == ["TMS"] and not result["absents"]
    assert not any("/TMS/" in p for p in result["fichiers"])
    rel = ci.client_path(f"{RES}/features/WMS/01-Contexte/01-Initialisation.feature", CLIENT)
    text = open(os.path.join(ci.init_worktree_dir(CLIENT), *rel.split("/")), encoding="utf-8").read()
    # Retirés, pas commentés : plus aucune trace du module décoché
    assert "tms/all" not in text and "Configuration TMS" not in text and "| a   | b      |" not in text
    assert "INIT- Initialisation globale par fichiers via l'écran" in text
    assert any("retirée (module TMS décoché)" in w for w in result["avertissements"])
    assert cm.traces({rel: text}, profile) == []
    # Correction de l'init (apply_environment_rules) : mêmes règles
    write(os.path.join(client_repo, "init", "WMS", "x.feature"), STD_INIT.format(m="WMS"))
    changed = cjobs.apply_environment_rules(client_repo, ["init/WMS/x.feature"], {}, profile=profile)
    assert changed["fichiers"] == ["init/WMS/x.feature"] and "tms/all" not in open(
        os.path.join(client_repo, "init", "WMS", "x.feature"), encoding="utf-8").read()


def test_clear_of_a_module_recognized_by_name_and_by_the_profile_mapping(cache, client_repo, std_dir):
    profile = {**_profile(cache, client_repo, std_dir),
               "clears_application": {"init-data-rest-clear-main-tms": ["odatio-clientx/bundle.list"]}}
    assert cm.module_clears(profile) == {"TMS": {"init-data-rest-clear-main-tms"}}
    purge = "INIT- Purge de l'environnement par le clear"
    assert cm.step_modules(f'    And {purge} "tms/all"', profile) == {"TMS"}
    assert cm.step_modules(f'    And {purge} "init-data-rest-clear-main-tms"', profile) == {"TMS"}
    assert cm.step_modules(f'    Given {purge} "{CLEAR}"', profile) == set()
    assert cm.step_modules(f'    # And {purge} "tms/all"', profile) == set()


def test_existing_init_cleaned_when_unchecked_and_restored_when_rechecked(cache, client_repo, std_dir):
    """Init existante (écrite à la main) : clear TMS retiré de l'init WMS, init TMS désactivée ; recoché : remis."""
    wms_init = INIT_WMS + ("\n  Scenario: Init\n    Given INIT- Purge de l'environnement\n"
                           "    And INIT- Purge de l'environnement par le clear \"tms/all\"\n"
                           "    And INIT- Initialisation globale par fichiers via l'écran\n")
    write(os.path.join(client_repo, *INIT_WMS_PATH.split("/")), wms_init)
    git(client_repo, "commit", "-qam", "init à la main")
    profile = _profile(cache, client_repo, std_dir)
    assert len(cm.traces(cm.worktree_features(client_repo), profile)) == 2   # clear dans l'init WMS, tag d'init TMS
    plan = {p["fichier"]: p for p in cm.retag_plan(client_repo, profile, "TMS")}
    assert plan[INIT_WMS_PATH]["raison"] == "module TMS décoché"
    assert [line.strip() for line in plan[INIT_WMS_PATH]["avant"]] == [
        "And INIT- Purge de l'environnement par le clear \"tms/all\""]
    result = cm.apply_retag(client_repo, CLIENT, "integration", profile, "TMS")
    cm.record_retag(CLIENT, "TMS", False, result)
    git(client_repo, "am", "-q", result["patch"])
    assert cm.traces(cm.worktree_features(client_repo), profile) == []
    assert "tms/all" not in open(os.path.join(client_repo, *INIT_WMS_PATH.split("/")), encoding="utf-8").read()
    # Recoché : steps et tag client remis, à l'identique
    cm.save_excluded(CLIENT, [])
    active = cp.effective_profile(cp.load_profile(CLIENT))
    back = cm.apply_retag(client_repo, CLIENT, "integration", active, "TMS", restore=True)
    git(client_repo, "am", "-q", back["patch"])
    for rel, text in ((INIT_WMS_PATH, wms_init), (INIT_TMS_PATH, INIT_TMS), (TMS_FEATURE, CLIENT_TMS)):
        assert open(os.path.join(client_repo, *rel.split("/")), encoding="utf-8").read() == text
    cm.record_retag(CLIENT, "TMS", True, back)
    assert cm.retag_state(cp.load_profile(CLIENT), "TMS")["retires"] == []


def test_init_facts_ignore_the_excluded_module(cache, client_repo, std_dir):
    from tools import cucumber_detection
    profile = _profile(cache, client_repo, std_dir)
    facts = cucumber_detection.init_facts(client_repo, CLIENT, profile, INVENTORY)
    assert [f["chemin"] for f in facts["features"]] == [INIT_WMS_PATH]


def test_generation_and_coverage_leave_the_excluded_module_out(cache, client_repo, std_dir):
    profile = _profile(cache, client_repo, std_dir)
    assert {s["id"]: cm.task_excluded(s, profile) for s in INVENTORY["specifiques"]} == {
        "sp-wms": "", "sp-tms": "TMS", "sp-neutre": ""}
    sources = {"imports": {}, "verifies": {}, "desactives": {}, "mis_de_cote": set(), "audit": {}, "existants": {}}
    rows = cov.coverage_rows(CLIENT, INVENTORY, {"lots": []}, sources)
    by_id = {r["id"]: r for r in rows}
    assert by_id["sp-tms"]["etat"] == "hors_perimetre"
    assert cov.STATES["hors_perimetre"][0] == "hors périmètre (module exclu)"
    assert "hors_perimetre" not in cov.SELECTABLE_STATES
    rates = cov.rates(rows)
    assert rates["total"] == 2 and rates["hors_perimetre"] == 1
    sentences = [t for t, _ in cov.status_sentences(rows, {})]
    assert sentences[0] == "2 fonctionnalités spécifiques."
    assert "2 restent à tester." in sentences
    assert any("1 hors périmètre (module exclu)" in t for t in sentences)
    assert not cov.in_filter(by_id["sp-tms"], "a_tester") and cov.in_filter(by_id["sp-tms"], "hors_perimetre")
    assert cov.summary_sentence(rows, {})[0][0] == "2 tâches."
    assert "sur 2" in cj.overview_line(CLIENT, "init complète", rows, {})


def test_run_expression_excludes_the_module_and_its_init(cache, client_repo, std_dir, monkeypatch):
    profile = _profile(cache, client_repo, std_dir)
    assert cm.run_expression(CLIENT_TAG, profile, CLIENT) == \
        f"{CLIENT_TAG} and not @TMS and not @INIT_CLIENTX_TMS"
    assert cm.run_expression("@agent_clientx_1 or @agent_clientx_2", profile, CLIENT) == \
        "(@agent_clientx_1 or @agent_clientx_2) and not @TMS and not @INIT_CLIENTX_TMS"
    assert cm.run_expression("@INIT_CLIENTX_TMS or @INIT_CLIENTX_WMS", profile, CLIENT) == \
        "@INIT_CLIENTX_WMS and not @TMS and not @INIT_CLIENTX_TMS"
    assert cm.run_expression("@INIT_CLIENTX_TMS", profile, CLIENT) == ""      # rien à lancer
    # Sans module exclu : inchangée
    assert cm.run_expression(CLIENT_TAG, {**profile, "modules_exclus": []}, CLIENT) == CLIENT_TAG
    # Expression du run réellement envoyée au job (run lancé par l'agent)
    from tools import cucumber_verify
    seen = {}

    def fake_run(worktree, tags, *args, **kwargs):
        seen["tags"] = tags
        return {"ok": True, "message": "", "build": "", "resultats": {}}
    monkeypatch.setattr(cucumber_verify, "run_on_branch", fake_run)
    cucumber_verify.run_tags("branche", client_repo, "", None, CLIENT_TAG, "", {}, "x", {"profil": profile})
    assert seen["tags"] == f"{CLIENT_TAG} and not @TMS and not @INIT_CLIENTX_TMS"
    assert not cucumber_verify.run_tags("branche", client_repo, "", None, "@INIT_CLIENTX_TMS", "", {}, "x",
                                        {"profil": profile})["ok"]


def test_import_classifies_failures_of_the_excluded_module_out_of_scope(cache, client_repo, std_dir):
    profile = _profile(cache, client_repo, std_dir)
    check = cimp.scope_checker(profile, client_repo)
    assert check(TMS_FEATURE) == "TMS" and check(INIT_TMS_PATH) == "TMS" and check(WMS_FEATURE) == ""
    results = {f"{TMS_FEATURE}::Expédie": {"statut": "echec", "message": "Expected 1 but was 2", "step": "y"},
               f"{WMS_FEATURE}::Prépare": {"statut": "echec", "message": "Expected 1 but was 2", "step": "x"}}
    items = {i["id"]: i for i in cf.failure_results(results, out_of_scope=check)}
    out = items[f"{TMS_FEATURE}::Expédie"]
    assert out["categorie"] == "hors_perimetre" and out["categorie"] not in cf.TO_WRITE
    assert items[f"{WMS_FEATURE}::Prépare"]["categorie"] != "hors_perimetre"
    assert cf.missing({"resultats": results, "echecs": list(items.values())}) == []
    assert all(e["id"] != out["id"] for e in cf.pending({"echecs": list(items.values())}))


def test_client_tag_swap_is_reversible(cache, client_repo, std_dir):
    profile = _profile(cache, client_repo, std_dir)
    plan = cm.retag_plan(client_repo, profile, "TMS")
    assert sorted(p["fichier"] for p in plan) == [INIT_TMS_PATH, TMS_FEATURE]
    assert {p["fichier"]: p["tags"] for p in plan}[TMS_FEATURE] == 2          # feature et scénario
    result = cm.apply_retag(client_repo, CLIENT, "integration", profile, "TMS")
    assert sorted(result["fichiers"]) == [INIT_TMS_PATH, TMS_FEATURE] and os.path.exists(result["patch"])
    worktree = cm.retag_worktree_dir(CLIENT)
    excluded_text = open(os.path.join(worktree, *TMS_FEATURE.split("/")), encoding="utf-8").read()
    assert CLIENT_TAG not in excluded_text and excluded_text.count(cm.EXCLUDED_TAG) == 2
    assert excluded_text.startswith(f"@TMS {cm.EXCLUDED_TAG}\n")
    wms = open(os.path.join(worktree, *WMS_FEATURE.split("/")), encoding="utf-8").read()
    assert wms == CLIENT_WMS                                                   # autre module : intact
    # Patch appliqué au dépôt client, puis module réactivé : retour exact au texte d'origine
    git(client_repo, "am", "-q", result["patch"])
    cm.save_excluded(CLIENT, [])
    active = cp.effective_profile(cp.load_profile(CLIENT))
    assert [p["fichier"] for p in cm.retag_plan(client_repo, active, "TMS", restore=True)] == sorted(
        [INIT_TMS_PATH, TMS_FEATURE])
    back = cm.apply_retag(client_repo, CLIENT, "integration", active, "TMS", restore=True)
    git(client_repo, "am", "-q", back["patch"])
    for rel, text in ((TMS_FEATURE, CLIENT_TMS), (INIT_TMS_PATH, INIT_TMS), (WMS_FEATURE, CLIENT_WMS)):
        assert open(os.path.join(client_repo, *rel.split("/")), encoding="utf-8").read() == text
    # Plus rien à remettre
    assert cm.apply_retag(client_repo, CLIENT, "integration", active, "TMS", restore=True)["patch"] == ""


def test_summary_line_and_saved_choice(cache, client_repo, std_dir):
    profile = _profile(cache, client_repo, std_dir)
    assert cm.summary_line(profile) == "Modules testés : WMS — TMS exclu"
    assert cp.load_profile(CLIENT)["saisi"]["modules_exclus"] == ["TMS"]
    cm.save_excluded(CLIENT, [])
    active = cp.effective_profile(cp.load_profile(CLIENT))
    assert cm.summary_line(active) == "Modules testés : TMS, WMS"
    assert active["init_features"] == [INIT_TMS_PATH, INIT_WMS_PATH]
    assert "modules_exclus" not in cp.load_profile(CLIENT)["saisi"]
