"""
Onglet « Cucumber » (5 étapes), client fictif, option « Configuration copiée, sans purge » : réglage et
avertissement à l'étape 1, copie de référence obligatoire à l'étape 3, étape 4 bloquée sans référence.
"""
import os

import pytest
from streamlit.testing.v1 import AppTest

from tools import client_version, cucumber_copied_config as cc, cucumber_detection as cd, cucumber_profile, \
    cucumber_tools as ct

CLIENT = "acme"
DEDUCTION = {"version_client": "2.0", "standard": {"version": "26.24"}, "kis": {"kis": "4.0"}}


@pytest.fixture
def screen(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(cd, "start", lambda *args, **kwargs: False)
    monkeypatch.setattr(cd, "running", lambda *args, **kwargs: False)
    monkeypatch.setattr(client_version, "saved", lambda client: {"deduction": DEDUCTION})
    for name in ("MAVEN_REPOSITORY_URL", "MAVEN_REPOSITORY_USER", "MAVEN_REPOSITORY_PASSWORD",
                 "BITBUCKET_WRITE_TOKEN"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("CUCUMBER_DB_SCHEMA", "cucumber_customer_1")
    cd.save_project(CLIENT, {"client_repo": "https://git.example/projects/ACME", "client_name": CLIENT,
                             "db_schema": "", "application": "", "std_repo": "std", "source_schema": "acme_prod"})
    ct.save_state(CLIENT, {"client": CLIENT, "simple": {"depot": {"etat": "ok", "resultat": "ok", "detail": {}}}})
    return tmp_path


def run_app() -> AppTest:
    at = AppTest.from_file(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"), default_timeout=120)
    at.query_params["page"] = "cucumber"
    at.session_state["cs_project"] = CLIENT
    at.run()
    assert not at.exception, at.exception
    return at


def texts(at: AppTest) -> str:
    return " ".join([m.value for m in at.markdown] + [c.value for c in at.caption] + [w.value for w in at.warning]
                    + [b.label for b in at.button] + [c.label for c in at.checkbox])


def test_option_off_by_default_and_step_3_optional(screen):
    at = run_app()
    assert any(c.label == cc.LABEL and not c.value for c in at.checkbox)
    assert cc.WARNING not in texts(at)
    assert "Recopier la configuration du client (essais locaux)" in texts(at)


def test_option_on_shows_warning_and_requires_the_reference(screen):
    cc.save_option(CLIENT, True)
    at = run_app()
    page = texts(at)
    assert cc.WARNING in page
    assert "Copier la configuration du client (référence des runs)" in page
    assert "aucune copie de référence" in page
    assert "4. Initialisation — bloquée" in page
    assert cucumber_profile.effective_profile(cucumber_profile.load_profile(CLIENT))[cc.OPTION] is True
