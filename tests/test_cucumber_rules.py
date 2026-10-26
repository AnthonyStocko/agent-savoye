"""Phase 5 : runner dans le rapport, enseignements du point 9 (échecs connus, longueurs, environnement dans l'init)."""
import json
import os

import pytest

from tests.conftest import CLIENT, git, write
from tools import cucumber_environment as ce
from tools import cucumber_init as ci
from tools import cucumber_rules as cr
from tools import cucumber_tools as ct
from tools import cucumber_verify as cv

RES = "src/test/resources/cucumber"


@pytest.mark.parametrize("message, nature, hint", [
    ("Unable to find the custom field champLibre on entity X", "environnement", "champ personnalisé non déclaré"),
    ("La taille de la valeur ABCDEFGHIJKLMNOPQRSTUV est trop grande (max 20)", "technique", "longueur maximale"),
    ("La propriété refSite est requise", "technique", "brouillon"),
    ("Erreur : liste de choix vide pour le champ motif", "environnement", "liste de choix vide"),
    ("L'ordre OP-1 ne peut pas être lancé : contrôle du client", "technique", "règle active du client"),
    ("Timeout: no websocket message received on channel form", "technique", "diagnostic temporaire d'écran"),
])
def test_known_failures_have_a_nature_and_a_hint(message, nature, hint):
    assert cv.failure_kind(message)[0] == nature and hint in cv.failure_hint(message)


def test_unknown_failures_keep_their_classification():
    assert cv.failure_kind("expected:<A> but was:<B>")[0] == "assertion" and cv.failure_hint("x") == ""


def test_code_lengths_from_entity_models():
    texts = {
        "RefFictif": "@Entity\nclass RefFictif {\n  public final static int LNG_CODE = 12;\n"
                     "  @Column(name = COLUMN_CODE, length = LNG_CODE, nullable = false)\n  private String code;\n}",
        "WmsBac": "@Entity\nclass WmsBac {\n  @Size(max = 8)\n  private String code;\n}",
        "RefLongueurs": "class RefLongueurs {\n  public static final int CODE = 30;\n}",
        "RefAutre": "@Entity\nclass RefAutre {\n  @Column(length = RefLongueurs.CODE)\n  private String code;\n}",
        "PasEntite": "class PasEntite {\n  @Size(max = 3)\n  private String code;\n}",
    }
    assert cr.model_lengths(texts) == {"RefFictif": 12, "WmsBac": 8, "RefAutre": 30}


def test_created_codes_longer_than_their_entity_are_lot_errors(tmp_path):
    rel = f"{RES}/features/{CLIENT}/Bacs.feature"
    write(os.path.join(str(tmp_path), rel),
          "Feature: Bacs\n  Background:\n    Given ADMIN- Création des bacs WmsBac\n      | code      | libelle |\n"
          "      | BAC-123456 | bac     |\n    And ADMIN- Création des sites\n      | code |\n"
          "      | SITE-LONG-DE-PLUS-DE-VINGT |\n    And ADMIN- Création des entrepôts\n      | code |\n"
          "      | ENT-1 |\n  Scenario: S\n    Then le bac \"BAC-123456\" existe\n      | code |\n"
          "      | CODE-TRES-TRES-TRES-LONG |\n")
    lengths = {"WmsBac": 8, "RefSiteFictif": 30}
    errors = cr.check_code_lengths(str(tmp_path), [rel], lengths)
    assert len(errors) == 1 and "« BAC-123456 » (10 caractères) au-delà de 8" in errors[0]
    # Sans modèle trouvé pour l'entité : longueur par défaut (20)
    errors = cr.check_code_lengths(str(tmp_path), [rel], {})
    assert any("SITE-LONG-DE-PLUS-DE-VINGT" in e and "par défaut" in e for e in errors)
    assert not any("CODE-TRES" in e for e in errors)          # step de contrôle (Then), pas de création


def test_code_length_index_is_cached(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    write(os.path.join(repo, "dom/src/main/java/x/datamodel/WmsBac.java"),
          "@Entity\nclass WmsBac {\n  @Size(max = 8)\n  private String code;\n}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "modele")
    assert cr.code_length_index(CLIENT, [repo]) == {"WmsBac": 8}
    os.remove(os.path.join(repo, "dom/src/main/java/x/datamodel/WmsBac.java"))   # même commit : cache
    assert cr.code_length_index(CLIENT, [repo]) == {"WmsBac": 8}


def test_runner_change_is_explained_in_the_report(repo):
    runner = "src/test/java/cucumber/RunFictifCucumberTest.java"
    write(os.path.join(repo, runner), "@CucumberOptions(tags = \"@x\")\nclass RunFictifCucumberTest {}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    write(os.path.join(repo, runner), "@CucumberOptions(tags = \"not @A_REVOIR\", plugin = \"json:x\")\n"
                                      "class RunFictifCucumberTest {}\n")
    git(repo, "commit", "-qam", "runner")
    lines = ct.runner_report(repo, base, {"consigne": "complète le runner : plugin JSON absent"})
    assert "## Runner" in lines and any(f"`{runner}` (+1 / -1 ligne(s)) — raison : complète le runner" in line
                                        for line in lines)
    assert ct.runner_report(repo, base, {}) == []


def test_environment_dependencies_from_profile_and_runs(tmp_path):
    imports = tmp_path / "imports"
    (imports / "3").mkdir(parents=True)
    (imports / "3" / "analyse.json").write_text(json.dumps({"numero": 3, "resultats": {
        "a::b": {"message": "Unable to find the custom field champLibre"}}}), encoding="utf-8")
    profile = {"environment_checks": [{"type": "document", "entite": "Dossier", "code": "BL"},
                                      {"type": "inconnu", "code": "x"},
                                      {"type": "champ_personnalise", "entite": "", "code": "champLibre"}]}
    deps = ce.dependencies(profile, str(imports))
    assert [(d["type"], d["code"], d["source"]) for d in deps] == [("document", "BL", "profil"),
                                                                   ("champ_personnalise", "champLibre", "profil")]
    assert ce.imported_dependencies(str(imports))[0]["source"] == "run importé n° 3"


def _init_worktree(repo):
    rel = f"{RES}/features/{CLIENT}/01-Contexte/WMS/01-Initialisation.feature"
    other = f"{RES}/features/{CLIENT}/01-Contexte/WMS/02-Autre.feature"
    write(os.path.join(repo, rel), f"Feature: Init\n  Scenario: Init\n    Given {ci.GLOBAL_INIT_STEP}\n")
    write(os.path.join(repo, other), "Feature: Autre\n  Scenario: A\n    Given x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return rel, other


def test_environment_scenarios_written_or_commented(repo):
    rel, other = _init_worktree(repo)
    base = git(repo, "rev-parse", "HEAD")
    env = {"controles": [{"type": "champ_personnalise", "entite": "WmsContent", "code": "champLibre"}],
           "recreations": ["statut BLOQUE | ajouté à la main"]}
    calls = []
    env["writer"] = lambda worktree, checks, manual: calls.append((checks, manual)) or []
    out = ci._environment_steps(repo, base, CLIENT, [rel, other], env)
    text = open(os.path.join(repo, rel), encoding="utf-8").read()
    assert out["livres"] and calls and ce.check_expression(CLIENT) in text and ce.recreation_expression(CLIENT) in text
    assert text.index("Recréation des corrections manuelles") < text.index("Contrôle de l'environnement")
    assert "| champ_personnalise | WmsContent | champLibre |" in text and "| statut BLOQUE / ajouté à la main |" in text
    assert ce.check_expression(CLIENT) not in open(os.path.join(repo, other), encoding="utf-8").read()
    git(repo, "checkout", "--", ".")
    env["writer"] = lambda worktree, checks, manual: ["compilation : symbole introuvable"]
    out = ci._environment_steps(repo, base, CLIENT, [rel, other], env)
    text = open(os.path.join(repo, rel), encoding="utf-8").read()
    assert not out["livres"] and f"# {ce.TODO_MARKER} compilation" in text
    assert f"#     Then {ce.check_expression(CLIENT)}" in text or f"# Then {ce.check_expression(CLIENT)}" in text


def test_environment_check_must_be_read_only(tmp_path):
    worktree = str(tmp_path)
    write(os.path.join(worktree, ce.java_path(CLIENT)),
          "class S {\n  @Then(\"" + ce.check_expression(CLIENT) + "\")\n  public void controle(DataTable t) {\n"
          "    endPoints.post(\"/x\", t);\n  }\n}\n")
    errors = ce.check_steps(worktree, CLIENT, [{"type": "document", "code": "BL"}], ["x"], [])
    assert any("lecture seule" in e for e in errors)
    assert any(ce.recreation_expression(CLIENT) in e and "0 fois" in e for e in errors)
