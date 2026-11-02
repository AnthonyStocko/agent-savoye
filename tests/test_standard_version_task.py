"""
Tâche de détection de la version du Standard (client fictif, clones locaux, Jenkins simulé) : sources rapides
d'abord (résultat provisoire), confirmation par le code, délai maximal, erreurs avec cause précise, journal, et
jamais d'état « en cours » sans fin.
"""
import os
import time
from datetime import datetime, timedelta

import pytest

from tests.conftest import CLIENT
from tests.test_standard_version import BOM, PARENT, STD_GROUP, pom, repo_with
from tools import cucumber_tools, standard_version_task as task

LIBRARY = pom("<dependencies><dependency><groupId>x</groupId><artifactId>cucumber</artifactId>"
              "<version>26.16</version><type>test-jar</type></dependency></dependencies>")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cucumber_tools, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(task, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(task, "STD_CACHE_DIR", str(tmp_path / "cache" / "_standard"))
    monkeypatch.setattr(task, "JOURNAL", str(tmp_path / "journal_technique.log"))
    monkeypatch.setenv("CUCUMBER_VERIFY_JOB", "VERIFY")
    # Tâches lancées par les tests de l'IHM pour le même client fictif : sans effet ici
    monkeypatch.setattr(task, "_PROCESSES", {})
    std = str(tmp_path / "std")
    repo_with(std, "std-core", {"pom.xml": pom("", group=STD_GROUP, version="26.22.0")}, branch="development")
    client = str(tmp_path / "client")
    repo_with(client, f"{CLIENT}-odatio-app", {"pom.xml": PARENT})
    repo_with(client, f"{CLIENT}-cucumber", {"pom.xml": LIBRARY})
    return {"std": std, "client": client, "journal": str(tmp_path / "journal_technique.log")}


def builds(version: str):
    return lambda job: [{"numero": 7, "parametres": {"REPO": f"{CLIENT}-cucumber",
                                                     "CUCUMBER_STANDARD_BRANCH": version}}]


def test_fast_sources_first_then_code_confirms(env):
    state = task.run(CLIENT, env["client"], env["std"], builds_reader=builds("v26.22"))
    assert state["statut"] == "ok" and state["resultat"]["version"] == "26.22"
    # Premier résultat (Jenkins, bibliothèque) avant la lecture du code
    assert state["provisoire"]["version"] == "26.22" and state["provisoire"]["source"] == "jenkins"
    assert state["resultat"]["source"] == "code"
    with open(env["journal"], encoding="utf-8") as f:
        log = f.read()
    for expected in ("build(s) lu(s), 1 pour", f"{CLIENT}-cucumber/pom.xml : bibliothèque Cucumber 26.16",
                     f"groupIds du Standard (clone local) : {STD_GROUP}",
                     f"{CLIENT}-odatio-app/pom.xml : std-parent 26.22.3 (parent)", "résultat : Standard 26.22"):
        assert expected in log, expected
    assert log.index("Jenkins") < log.index("odatio-app/pom.xml")


def test_conflict_between_code_and_jenkins(env):
    state = task.run(CLIENT, env["client"], env["std"], builds_reader=builds("v26.21"))
    assert state["statut"] == "conflit"


def test_no_source_gives_precise_causes(env, tmp_path):
    empty = str(tmp_path / "vide")
    repo_with(empty, f"{CLIENT}-odatio-app", {"pom.xml": BOM.replace(STD_GROUP, "org.autre")})
    state = task.run(CLIENT, empty, env["std"], builds_reader=lambda job: [])
    assert state["statut"] == "non_detectee"
    assert "aucun pom avec une version du Standard (1 pom lu(s) sur 1 dépôt(s))" in state["cause"]
    assert "aucun build Jenkins lu" in state["cause"]


def test_unreachable_repo_and_deadline(env, tmp_path):
    state = task.run(CLIENT, str(tmp_path / "absent"), env["std"], builds_reader=lambda job: [])
    assert state["statut"] == "non_detectee" and "inaccessible" in state["cause"]
    state = task.run(CLIENT, env["client"], env["std"], deadline_s=-1, builds_reader=builds("v26.22"))
    assert state["statut"] == "non_detectee" and "délai de" in state["cause"]


def test_stale_running_state_never_stays_running(env):
    old = (datetime.now() - timedelta(seconds=task.DEADLINE_S + 60)).isoformat(timespec="seconds")
    cucumber_tools._write_json(task.state_path(CLIENT), {"statut": "en_cours", "etape": "lecture des pom du "
                                                         "projet client", "progression": "3/8", "debut": old})
    state = task.view(CLIENT)
    assert state["statut"] == "non_detectee" and "délai de 5 min dépassé" in state["cause"] and "3/8" in state["cause"]
    assert task.load_state(CLIENT)["statut"] == "non_detectee"
    # État final récent : relancé seulement après l'intervalle de détection
    assert task.due(state, 300) is False and task.due(state, 0) is True
    assert task.due({}, 300) is True and task.due({"statut": "en_cours"}, 300) is False
