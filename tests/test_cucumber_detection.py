"""
Détection automatique du projet Cucumber (client fictif) : état de l'init absente, incomplète (un cas par type
d'élément manquant) ou complète, preuves, détection relancée au changement de commit seulement, aucune écriture
dans les dépôts, lot d'initialisation proposé selon l'état et compléments sans fichier protégé modifié.
"""
import hashlib
import json
import os
import sys

import pytest

from tests.conftest import CLEAR, CLIENT, CLIENT_TAG, git, write
from tools import cucumber_detection as cd
from tools import cucumber_init as ci
from tools import cucumber_journey as cj
from tools import cucumber_profile as cp
from tools import cucumber_tools as ct

RES = "src/test/resources/cucumber"
INIT_DIR = f"{RES}/features/{CLIENT}/01-Contexte/WMS"
INIT_REL = f"{INIT_DIR}/01-Initialisation.feature"
DATA_DIR = f"{RES}/files/{CLIENT}/01-Contexte/WMS"
FEATURE_REL = f"{RES}/features/{CLIENT}/WMS/Goodsin/SPEC-1.feature"
PURGE = f"    Given INIT- Purge de l'environnement par le clear \"{CLEAR}\"\n"
GLOBAL = "    And INIT- Initialisation globale par fichiers via l'écran\n"


def init_text(purge: str = PURGE, extra: str = "") -> str:
    return (f"@INIT_{CLIENT.upper()}_WMS {CLIENT_TAG}\nFeature: Initialisation\n\n"
            f"  Scenario: Intégration des données\n{purge}{GLOBAL}{extra}")


FEATURE = f"{CLIENT_TAG}\nFeature: Arrivage\n  Scenario: S\n    Given un step\n      | code | customString4 |\n"


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("CUCUMBER_CLEAR_NAME", CLEAR)
    monkeypatch.delenv("CUCUMBER_INIT_TAGS", raising=False)
    cd._PROCESSES.clear()


@pytest.fixture
def std_dir(tmp_path):
    root = str(tmp_path / "std")
    write(os.path.join(root, RES, "features", "WMS", "01-Contexte", "01-Initialisation.feature"),
          "@INIT_WMS @WMS\nFeature: Init\n")
    return root


def commit(repo, files: dict, message: str = "base") -> None:
    for rel, text in files.items():
        write(os.path.join(repo, *rel.split("/")), text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


def complete_repo(repo, init: str = None, feature: str = FEATURE) -> str:
    """Init complète : clear de l'environnement, intégration globale, déclaration du champ personnalisé utilisé."""
    commit(repo, {INIT_REL: init or init_text(), f"{DATA_DIR}/01-RefSiteDraft.csv": "code\nS1\n",
                  f"{DATA_DIR}/02-RefCustomFieldDraft.csv": "entity;attribute\nWmsGoodsInFolder;customString4\n",
                  FEATURE_REL: feature})
    return repo


def detect(repo, std_dir, inventory: dict = None, user: dict = None, analysis: dict = None) -> tuple:
    """Profil puis faits relevés à la base `integration` ; état de l'init. Renvoie (faits, état)."""
    ct.save_inventory(CLIENT, inventory or {"specifiques": [], "initialiseurs": []})
    cp.detect_in_base(CLIENT, repo, "integration", std_dir)
    if user:
        cp.save_user_fields(CLIENT, user)
    profile = cp.effective_profile(cp.load_profile(CLIENT))
    facts = cd.init_facts(cp.base_worktree_dir(CLIENT), CLIENT, profile, ct.load_inventory(CLIENT))
    return facts, cd.init_status(facts, profile, CLIENT, analysis or {}, os.path.join(ct.CACHE_ROOT, "aucun"))


def kinds(status: dict) -> list:
    return [f["type"] for f in status["constats"]]


# ---------------------------------------------------------------------------
# État de l'init
# ---------------------------------------------------------------------------

def test_init_absent(repo, std_dir, cache):
    commit(repo, {FEATURE_REL: FEATURE})
    facts, status = detect(repo, std_dir)
    assert status["etat"] == "absente" and kinds(status) == ["absente"]
    assert "aucune feature taguée @INIT_*" in cd.proof_line(status["constats"][0]["preuves"][0])
    assert cd.state_label(status) == "init absente"
    lot = cd.init_lot(status)
    assert lot["mode"] == "init" and lot["titre"] == "Initialisation des données"
    # Tous les tests dépendent du lot d'initialisation
    assert cd.lot_dependency(["sp-1"], cd.init_dependencies(status, facts)).startswith("dépend du lot d'init")


def test_init_complete(repo, std_dir, cache):
    facts, status = detect(complete_repo(repo), std_dir)
    assert status == {"etat": "complete", "constats": []}
    assert cd.init_lot(status) == {} and cd.state_label(status) == "init complète"
    assert cd.lot_dependency(["sp-1"], cd.init_dependencies(status, facts)) == ""


def test_incomplete_clear_not_matching_environment(repo, std_dir, cache):
    facts, status = detect(complete_repo(repo, init_text("    Given INIT- Purge de l'environnement par le clear "
                                                         "\"clear-autre\"\n")), std_dir)
    assert status["etat"] == "incomplete" and kinds(status) == ["clear"]
    proof = status["constats"][0]["preuves"][0]
    assert proof["fichier"] == INIT_REL and proof["ligne"] == 5 and "clear-autre" in proof["extrait"]
    assert f"clear attendu « {CLEAR} »" in status["constats"][0]["message"]
    # Purge Standard sans clear nommé : non conforme non plus
    _, status = detect(complete_repo(repo, init_text("    Given INIT- Purge de l'environnement\n")), std_dir)
    assert kinds(status) == ["clear"] and status["constats"][0]["preuves"][0]["clear"] == ""
    # Lot proposé : compléments ; clear dans un fichier protégé -> à traiter à la main, tous les tests dépendent
    lot = cd.init_lot(status)
    assert lot["mode"] == "complements" and not lot["constats"][0]["livrable"]
    assert cd.init_dependencies(status, facts)["global"] == cd.FINDINGS["clear"]


def test_incomplete_initializer_data_erased_and_not_recreated(repo, std_dir, cache):
    inventory = {"specifiques": [], "initialiseurs": [{"depot": "app", "chemin": "A.java", "classe": "DocInit",
                                                      "creations": [{"entite": "RefReportDoc", "code": "DOC1",
                                                                     "expression": "DOC_CODE", "mode": "new",
                                                                     "certitude": "certain"}]}]}
    complete_repo(repo)
    _, status = detect(repo, std_dir, inventory, user={"initializers_survive_clear": "faux"})
    assert kinds(status) == ["initializer"] and status["constats"][0]["codes"] == ["DOC1"]
    assert "DocInit" in cd.proof_line(status["constats"][0]["preuves"][0])
    assert status["constats"][0]["livrable"]
    # Recréée par une ligne active de l'init : plus rien à signaler
    complete_repo(repo, init_text(extra="    And OMS- Création d'un document\n      | code |\n      | DOC1 |\n"))
    _, status = detect(repo, std_dir, inventory, user={"initializers_survive_clear": "faux"})
    assert status["etat"] == "complete"


def test_incomplete_custom_field_used_but_not_declared(repo, std_dir, cache, tmp_path):
    source = str(tmp_path / "copies" / "Process__customized.xml")
    write(source, "<field name=\"customDate2\"/>\n")
    java = str(tmp_path / "copies" / "Repository.java")
    write(java, "query.setNull(entity.customLong1);\n")
    inventory = {"specifiques": [{"id": "sp-1", "libelle": "Arrivage", "fichiers": [
        {"repo": "app", "path": "studio/Process__customized.xml", "local": source},
        {"repo": "app", "path": "src/Repository.java", "local": java}]}], "initialiseurs": []}
    # Ligne commentée d'une feature et code Java : pas des usages par les tests ou les processus
    commit(repo, {INIT_REL: init_text(), FEATURE_REL: FEATURE + "    # customString9 vide au Standard\n"})
    facts, status = detect(repo, std_dir, inventory)
    assert kinds(status) == ["champ_personnalise"]
    finding = status["constats"][0]
    assert finding["codes"] == ["customDate2", "customString4"] and finding["livrable"]
    proofs = [cd.proof_line(p) for p in finding["preuves"]]
    assert proofs[0].startswith("app/studio/Process__customized.xml:1") and f"{FEATURE_REL}:5" in proofs[1]
    # Le lot de tests du spécifique qui utilise le champ dépend du lot d'initialisation, pas les autres
    dependencies = cd.init_dependencies(status, facts)
    assert cd.lot_dependency(["sp-1"], dependencies) == "dépend du lot d'initialisation (customDate2)"
    assert cd.lot_dependency(["sp-2"], dependencies) == ""


def test_incomplete_environment_checks_missing(repo, std_dir, cache):
    complete_repo(repo)
    check = {"type": "document", "entite": "RefReportDoc", "code": "DOCX"}
    _, status = detect(repo, std_dir, user={"environment_checks": [check]})
    assert kinds(status) == ["controle_environnement"] and status["constats"][0]["codes"] == ["DOCX"]
    assert "document RefReportDoc DOCX" in cd.proof_line(status["constats"][0]["preuves"][0])


def test_incomplete_todo_version_steps(repo, std_dir, cache):
    complete_repo(repo, init_text(extra="    # TODO-VERSION: step absent de la bibliothèque 26.16\n"
                                        "    # And INIT- Intégration des règles\n"))
    _, status = detect(repo, std_dir)
    assert kinds(status) == ["todo_version"] and not status["constats"][0]["livrable"]
    proof = status["constats"][0]["preuves"][0]
    assert proof["fichier"] == INIT_REL and proof["ligne"] == 7 and proof["extrait"].startswith("# TODO-VERSION:")


def test_incomplete_init_failed_at_last_imported_run(repo, std_dir, cache):
    analysis = {"numero": 3, "init_en_echec": {"id": f"{INIT_REL}::Intégration des données",
                                               "step": "INIT- Initialisation globale par fichiers via l'écran",
                                               "message": "INTEGRATED_WITH_ERROR"}}
    facts, status = detect(complete_repo(repo), std_dir, analysis=analysis)
    assert kinds(status) == ["init_en_echec"]
    line = cd.proof_line(status["constats"][0]["preuves"][0])
    assert line.startswith("run importé n° 3") and "INTEGRATED_WITH_ERROR" in line
    assert cd.init_dependencies(status, facts)["global"]
    assert cd.state_label(status) == "init incomplète (1 élément)"


# ---------------------------------------------------------------------------
# Détection en arrière-plan : cache par empreinte, relance au changement de commit, lecture seule
# ---------------------------------------------------------------------------

def repo_digest(path: str) -> dict:
    """Empreinte de chaque fichier du dépôt, .git compris : toute écriture la change."""
    out = {}
    for folder, _, names in os.walk(path):
        for name in names:
            full = os.path.join(folder, name)
            with open(full, "rb") as f:
                out[os.path.relpath(full, path)] = hashlib.sha1(f.read()).hexdigest()
    return out


def test_detection_writes_nothing_in_repositories(repo, std_dir, cache):
    complete_repo(repo)
    before = repo_digest(repo)
    detect(repo, std_dir)
    cd.repo_commits(repo, repo, "integration", std_dir)
    detect(repo, std_dir)
    assert repo_digest(repo) == before


def test_detection_cached_by_commit_fingerprint(repo, std_dir, cache):
    complete_repo(repo)
    commits = cd.repo_commits(repo, repo, "integration", "")
    facts, _ = detect(repo, std_dir)
    cd.save_result(CLIENT, commits, facts)
    same = cd.repo_commits(repo, repo, "integration", "")
    assert cd.up_to_date(CLIENT, cd.fingerprint(same))
    commit(repo, {FEATURE_REL: FEATURE + "  Scenario: T\n    Given un step\n"}, "nouveau commit")
    changed = cd.repo_commits(repo, repo, "integration", "")
    assert cd.fingerprint(changed) != cd.fingerprint(commits) and not cd.up_to_date(CLIENT, cd.fingerprint(changed))
    # Détection en échec : jamais considérée à jour, dernier résultat conservé
    cd.save_error(CLIENT, "dépôt inaccessible")
    assert not cd.up_to_date(CLIENT, cd.fingerprint(commits)) and cd.load_detection(CLIENT)["faits"] == facts


def test_background_start_only_when_due_or_forced(cache):
    cmd = [sys.executable, "-c", "pass"]
    assert cd.start(CLIENT, cmd)
    cd._PROCESSES[CLIENT].wait()
    cd.save_result(CLIENT, {"cucumber": "abc"}, {"features": []})
    assert not cd.start(CLIENT, cmd)
    assert cd.start(CLIENT, cmd, force=True)
    cd._PROCESSES[CLIENT].wait()


def test_background_failure_gives_short_cause_without_blocking(cache):
    cmd = [sys.executable, "-c", "print('[CUCUMBER] ❌ Dépôt client inaccessible'); raise SystemExit(2)"]
    assert cd.start(CLIENT, cmd, force=True)
    cd._PROCESSES[CLIENT].wait()
    cd.finish(CLIENT)
    document = cd.load_detection(CLIENT)
    assert document["statut"] == "erreur" and document["cause"] == "[CUCUMBER] Dépôt client inaccessible"
    indicator = cd.profile_indicator(document, cp.empty_profile())
    assert indicator["couleur"] == "rouge" and "inaccessible" in indicator["cause"]


def test_profile_indicator_colors(cache):
    detection = {"statut": "ok", "faits": {"features": []}}
    profile = {**cp.empty_profile(), "features_root": "r/", "tag_client": CLIENT_TAG, "init_features": ["i"],
               "init_tags": "@I", "clear_name": CLEAR, "spec_files": ["s"], "protected_files": ["p"],
               "runner": {"classe": "R"}, "reference_codes": {"sites": ["S"]}, "library_version": "1",
               "server_version": "1", "initializers_survive_clear": "vrai"}
    assert cd.profile_indicator(detection, profile)["couleur"] == "vert"
    orange = cd.profile_indicator(detection, {**profile, "tag_client": "", "runner": {"classe": ""}})
    assert orange["couleur"] == "orange" and orange["manquants"] == ["tag_client", "runner"]
    assert cd.profile_indicator({}, profile)["couleur"] == "attente"


def test_known_projects_and_overview_line(cache):
    cd.save_project(CLIENT, {"client_repo": "https://git/projects/X", "client_name": CLIENT})
    os.makedirs(os.path.join(ct.CACHE_ROOT, "_standard"))
    assert list(cd.known_projects(ct.CACHE_ROOT)) == [CLIENT]
    rows = [{"id": "a", "etat": "vert", "scenarios": [{}]}, {"id": "b", "etat": "non_couvert", "scenarios": []},
            {"id": "c", "etat": "vert", "scenarios": [{}]}]
    analysis = {"numero": 1, "resultats": {"a": {"statut": "passe"}, "b": {"statut": "echoue"}}}
    line = cj.overview_line(CLIENT, "init incomplète (3 éléments)", rows, analysis)
    assert line == f"{CLIENT.capitalize()} · init incomplète (3 éléments) · 2 tâches avec un test sur 3 · dernier " \
                   f"run : 1 vert / 1 échec"
    assert cj.overview_line(CLIENT, "init complète", [], {}).endswith(
        "aucune tâche inventoriée · aucun run importé : couverture réelle non mesurée")
    assert cj.overview_line(CLIENT, "init complète", rows, {"numero": 2, "non_analyse": "écart"}).endswith(
        "dernier run non analysé")


# ---------------------------------------------------------------------------
# Lot « Compléments d'initialisation »
# ---------------------------------------------------------------------------

def test_complements_lot_adds_only_missing_elements_in_new_feature(repo, std_dir, cache):
    commit(repo, {INIT_REL: init_text(), FEATURE_REL: FEATURE})
    facts, status = detect(repo, std_dir)
    assert kinds(status) == ["champ_personnalise"]
    profile = cp.effective_profile(cp.load_profile(CLIENT))
    written = []

    def writer(worktree, checks, manual):
        written.append([c["code"] for c in checks])
        return []
    result = ci.build_init_complements(repo, std_dir, CLIENT, "integration", profile, status,
                                       [f["chemin"] for f in facts["features"]], writer=writer)
    target = f"{INIT_DIR}/{ci.COMPLEMENTS_NAME}.feature"
    assert result["fichiers"] == [target] and result["patch"] and written == [["customString4"]]
    with open(result["patch"], encoding="utf-8") as f:
        patch = f.read()
    # Une seule feature ajoutée, taguée comme l'init ; l'init existante et ses fichiers ne sont pas touchés
    assert f"b/{target}" in patch and f"b/{INIT_REL}" not in patch and "new file mode" in patch
    assert f"@INIT_{CLIENT.upper()}_WMS {CLIENT_TAG}" in patch and "| champ_personnalise |  | customString4 |" in patch
    assert not set(profile["protected_files"]) & {line[6:] for line in patch.splitlines() if line.startswith("+++ b/")}


def test_complements_lot_keeps_manual_findings_and_never_rewrites_init(repo, std_dir, cache):
    complete_repo(repo, init_text("    Given INIT- Purge de l'environnement par le clear \"clear-autre\"\n"))
    facts, status = detect(repo, std_dir)
    profile = cp.effective_profile(cp.load_profile(CLIENT))
    result = ci.build_init_complements(repo, std_dir, CLIENT, "integration", profile, status,
                                       [f["chemin"] for f in facts["features"]])
    # Clear dans l'init (fichier protégé) : rien de livrable, constat rendu avec sa preuve
    assert result["patch"] == "" and [f["type"] for f in result["manuels"]] == ["clear"]


def test_complements_path_never_reuses_an_existing_feature():
    first = f"{INIT_DIR}/{ci.COMPLEMENTS_NAME}.feature"
    assert ci.complements_path(INIT_REL, set()) == first
    assert ci.complements_path(INIT_REL, {first}) == f"{INIT_DIR}/{ci.COMPLEMENTS_NAME}-2.feature"


def test_detection_file_is_json(cache):
    cd.save_result(CLIENT, {"cucumber": "abc"}, {"features": []})
    with open(cd.detection_path(CLIENT), encoding="utf-8") as f:
        assert json.load(f)["empreinte"] == cd.fingerprint({"cucumber": "abc"})


def test_complements_without_verifiable_check_add_no_feature(repo, std_dir, cache):
    """Seul complément : un champ personnalisé que l'API ne permet pas de vérifier -> aucune feature ajoutée,
    avertissement non bloquant (jamais un contrôle qui fait échouer l'init)."""
    commit(repo, {INIT_REL: init_text(), FEATURE_REL: FEATURE})
    facts, status = detect(repo, std_dir)
    profile = cp.effective_profile(cp.load_profile(CLIENT))
    result = ci.build_init_complements(
        repo, std_dir, CLIENT, "integration", profile, status, [f["chemin"] for f in facts["features"]],
        writer=lambda worktree, checks, manual: {"erreurs": [], "non_verifiables": [c["code"] for c in checks]})
    assert result["fichiers"] == [] and result["patch"] == ""
    assert any("non vérifiable par l'API : customString4" in w for w in result["avertissements"])
