"""Génération propre : scénarios réécrits vérifiés, génération sans run = erreur de l'agent, branche recréée."""
import os
import subprocess

import pytest

from tools import cucumber_bilan, cucumber_checks, cucumber_delivery, cucumber_verify

FEATURE = "src/test/resources/cucumber/features/acme/WMS/Appairage.feature"
INIT = "src/test/resources/cucumber/features/acme/01-Contexte/WMS/01-Initialisation.feature"


def git(cwd, *args):
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def write(root, rel, text):
    path = os.path.join(str(root), *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def feature(then: str, title: str = "Appairage BL en fin de prélèvement") -> str:
    return (f"@WMS @ACME\nFeature: Appairage\n\n  Scenario: {title}\n    Given l'OP CANSON-APPBL-1\n"
            f"    Then {then}\n")


def repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "v1.3")
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    return path


def test_rewritten_or_declared_scenario_with_an_existing_title_is_a_lot_scenario(tmp_path):
    work = repo(tmp_path / "w")
    write(work, FEATURE, feature("le besoin d'appairage est assigné") + feature("x", "Autre scénario"))
    git(work, "add", "-A")
    git(work, "commit", "-qm", "essai précédent")
    # Même titre, steps réécrits par le lot : vérifié comme un nouveau ; l'autre, inchangé, reste existant
    write(work, FEATURE, feature("le besoin d'appairage est assigné à l'UP") + feature("x", "Autre scénario"))
    new, existing = cucumber_verify._lot_scenarios(str(work), [FEATURE])
    assert [s["titre"] for s in new] == ["Appairage BL en fin de prélèvement"]
    assert "Autre scénario" in [s["titre"] for s in existing]
    # Inchangé mais déclaré par le JSON du lot : du lot
    results = [{"id": "sp-1", "statut": "couvert", "fichiers": [FEATURE],
                "scenarios": [{"nom": "Autre scénario", "fichier": FEATURE}]}]
    new, _ = cucumber_verify._lot_scenarios(str(work), [FEATURE], "HEAD", results)
    assert {s["titre"] for s in new} == {"Appairage BL en fin de prélèvement", "Autre scénario"}


def test_written_test_without_run_is_an_agent_error():
    lot = {"execution": {"mode": "aucun", "erreur_agent": True, "message": cucumber_bilan.NO_RUN_ERROR}}
    out = cucumber_bilan.lot_outcome(lot, [{"id": "sp-1", "statut": "couvert"}], [FEATURE], True, 1)
    assert out["erreur_agent"] and out["ligne"].startswith("❌")
    assert "NON vérifié(s) (aucune exécution : erreur de l'agent)" in out["ligne"]
    plain = cucumber_bilan.lot_outcome({}, [{"id": "sp-1", "statut": "couvert"}], [FEATURE], True, 1)
    assert plain["ligne"].startswith("⚠️") and "(aucune exécution) —" in plain["ligne"]
    with open(os.path.join(os.path.dirname(__file__), "..", "main.py"), encoding="utf-8") as f:
        main = f.read()
    assert '"erreur_agent": True, "message": cucumber_bilan.NO_RUN_ERROR' in main


@pytest.fixture
def remote_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(cucumber_delivery, "check_push", lambda *a, **k: "")
    events = []
    monkeypatch.setattr(cucumber_delivery, "log_event", lambda client, kind, **data: events.append((kind, data)))
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "v1.3", str(origin)], check=True)
    seed = repo(tmp_path / "seed")
    write(seed, INIT, "Feature: init\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "init v1.3")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "v1.3")
    # Essais précédents sur la branche de travail : trois versions du même test, anciens fichiers de données
    git(seed, "checkout", "-q", "-b", "cucumber_acme_agent")
    for name in ("SPEC-APPAIRAGE-BL.feature", "Appairage_non_mecanise.feature", "Appairage.feature"):
        write(seed, FEATURE.rsplit("/", 1)[0] + "/" + name, feature("essai"))
    write(seed, "src/test/resources/cucumber/files/acme/WOI-SPEC-APB.xml", "<x/>")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "Cucumber coverage of acme specifics - lot 1 (vérification)")
    git(seed, "push", "-q", "origin", "cucumber_acme_agent")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", "-b", "v1.3", str(origin), str(work)], check=True)
    return seed, work, origin, events


def test_new_generation_recreates_the_work_branch_from_the_base(remote_setup):
    seed, work, origin, events = remote_setup
    old = git(origin, "rev-parse", "cucumber_acme_agent")
    done, line = cucumber_checks.replace_verify_branch(str(work), "cucumber_acme_agent", ("v1.3", ""), [INIT])
    assert done and "recréée depuis la branche de base" in line
    assert git(origin, "rev-parse", "cucumber_acme_agent") == git(work, "rev-parse", "HEAD")
    files = git(origin, "ls-tree", "-r", "--name-only", "cucumber_acme_agent").splitlines()
    assert files == [INIT]  # aucun essai précédent ni ancien fichier de données
    assert events[-1][0] == "push" and events[-1][1]["ancien"] == old
    # Déjà propre : rien à faire
    assert cucumber_checks.replace_verify_branch(str(work), "cucumber_acme_agent", ("v1.3",), [INIT])[0]


def test_branch_is_not_recreated_when_it_carries_an_init_missing_from_the_base(remote_setup):
    seed, work, origin, _ = remote_setup
    write(seed, INIT, "Feature: init verte (délai 900 s)\n")
    write(seed, "src/test/java/cucumber/features/acme/InitAcmeSteps.java", "class InitAcmeSteps {}\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "init verte")
    git(seed, "push", "-q", "origin", "cucumber_acme_agent")
    before = git(origin, "rev-parse", "cucumber_acme_agent")
    done, line = cucumber_checks.replace_verify_branch(str(work), "cucumber_acme_agent", ("v1.3",), [INIT])
    assert not done and "init absente de la branche de base" in line and "PR" in line
    assert INIT in line and "InitAcmeSteps.java" in line
    assert git(origin, "rev-parse", "cucumber_acme_agent") == before


def test_protected_branch_is_never_recreated(remote_setup):
    _, work, origin, _ = remote_setup
    before = git(origin, "rev-parse", "v1.3")
    assert cucumber_checks.replace_verify_branch(str(work), "v1.3", ("v1.3", "integration"), [INIT]) == (
        False, "branche v1.3 protégée : jamais recréée")
    assert not cucumber_checks.replace_verify_branch(str(work), "", ("v1.3",))[0]
    assert git(origin, "rev-parse", "v1.3") == before
