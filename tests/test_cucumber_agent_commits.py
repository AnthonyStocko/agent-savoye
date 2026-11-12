"""Commits reconnus comme écrits par l'agent lors de l'intégration de la branche distante."""
from tools.cucumber_checks import _AGENT_COMMIT


def test_agent_commit_subjects_recognised():
    for subject in ("Cucumber coverage of canson specifics - lot 1 (vérification)",
                    "Cucumber initialisation complements of canson",
                    "Cucumber canson : init de la version v1.3",
                    "Cucumber verification (EPO-2412)",
                    "Intégration de origin/cucumber_canson_agent avant la vérification"):
        assert _AGENT_COMMIT.match(subject), subject


def test_human_commit_not_recognised():
    for subject in ("Clear wms/all commenté pour tester sans purge", "Pull request #12: V1.5"):
        assert not _AGENT_COMMIT.match(subject), subject
