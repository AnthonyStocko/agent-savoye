"""
Connexion OIDC (AGENT_AUTH) : accès refusé sans connexion, liste AGENT_AUTH_ALLOWED, auteur tracé = utilisateur
connecté (décisions, connaissances partagées, exécutions de main.py), secrets des comptes de service jamais
renvoyés au navigateur, configuration écrite par tools.auth_setup sans secret commité.
"""
import os
import shutil

import pytest
from streamlit.testing.v1 import AppTest

from tools import auth_setup, auth_tools, cucumber_decisions, knowledge_tools

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH = {"redirect_uri": "http://localhost:8501/oauth2callback", "cookie_secret": "x" * 32,
        "client_id": "id-app", "client_secret": "secret-app",
        "server_metadata_url": "https://idp.example.invalid/.well-known/openid-configuration"}
PERSON = {"nom": "Camille Martin", "adresse": "camille.martin@example.com", "groupes": ["grp-qa"]}


def _protected_page():
    import streamlit as st
    from tools import auth_tools
    auth_tools.require_login(st)
    st.write("CONTENU PROTÉGÉ")


def _texts(at) -> str:
    return " ".join([m.value for m in at.markdown] + [m.value for m in at.error] + [m.value for m in at.info]
                    + [b.label for b in at.button])


def _page(monkeypatch, auth=None, secrets=None):
    if auth is None:
        monkeypatch.delenv("AGENT_AUTH", raising=False)
    else:
        monkeypatch.setenv("AGENT_AUTH", auth)
    at = AppTest.from_function(_protected_page)
    if secrets is not None:
        at.secrets["auth"] = secrets
    return at.run(timeout=30)


def test_access_refused_without_login_when_auth_is_active(monkeypatch):
    at = _page(monkeypatch, "1", AUTH)
    assert "CONTENU PROTÉGÉ" not in _texts(at) and "Se connecter" in _texts(at)


def test_access_unchanged_when_auth_is_off(monkeypatch):
    for value in (None, "", "0"):
        at = _page(monkeypatch, value)
        assert "CONTENU PROTÉGÉ" in _texts(at) and "Se connecter" not in _texts(at)


def test_incomplete_provider_configuration_blocks_access(monkeypatch):
    at = _page(monkeypatch, "1", {**AUTH, "client_secret": ""})
    assert "CONTENU PROTÉGÉ" not in _texts(at) and "client_secret" in _texts(at)
    assert "secret-app" not in _texts(at)


def test_whole_application_is_closed_without_login(monkeypatch, tmp_path):
    """L'application elle-même : rien de ses onglets n'est rendu sans connexion."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AGENT_AUTH=1\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_AUTH", "1")
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=120)
    at.secrets["auth"] = AUTH
    at.run()
    assert not at.exception and "Se connecter" in _texts(at)
    assert not at.tabs and "Tableau de bord" not in " ".join(t.value for t in at.title)


def test_allowed_list():
    assert auth_tools.is_allowed(PERSON, [])
    assert auth_tools.is_allowed(PERSON, ["@example.com"])
    assert auth_tools.is_allowed(PERSON, ["Camille.Martin@example.com"])
    assert auth_tools.is_allowed(PERSON, ["grp-qa"])
    assert not auth_tools.is_allowed(PERSON, ["@autre.com", "grp-dev", "x@example.com"])
    assert not auth_tools.is_allowed({}, [])


def test_identity_from_claims():
    claims = {"is_logged_in": True, "name": "Camille Martin", "preferred_username": "camille.martin@example.com",
              "groups": ["grp-qa"]}
    assert auth_tools.identity(claims) == PERSON
    assert auth_tools.identity({"is_logged_in": False}) == {}
    assert auth_tools.label(PERSON) == "Camille Martin <camille.martin@example.com>"


@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH", "1")
    monkeypatch.setenv("AGENT_KNOWLEDGE_AUTHOR", "compte-fixe")
    monkeypatch.delenv(auth_tools.USER_ENV, raising=False)
    monkeypatch.setattr(auth_tools, "session_identity", lambda: PERSON)
    return auth_tools.label(PERSON)


def test_traced_author_is_the_connected_user(connected):
    assert knowledge_tools.author() == connected
    assert cucumber_decisions._event("acceptée", "ok")["auteur"] == connected
    # Décision de l'agent : reste « agent »
    assert cucumber_decisions._event("prise par l'agent", author="agent")["auteur"] == "agent"


def test_connected_user_is_passed_to_main_runs(connected, monkeypatch):
    env = auth_tools.subprocess_env({"PATH": "x", auth_tools.USER_ENV: "ancien"})
    assert env[auth_tools.USER_ENV] == connected and env["PATH"] == "x"
    # Dans l'exécution de main.py (hors session Streamlit) : l'auteur vient de l'environnement transmis
    monkeypatch.setattr(auth_tools, "session_identity", lambda: {})
    monkeypatch.setenv(auth_tools.USER_ENV, connected)
    assert knowledge_tools.author() == connected


def test_system_user_when_auth_is_off(monkeypatch):
    monkeypatch.delenv("AGENT_AUTH", raising=False)
    monkeypatch.setenv(auth_tools.USER_ENV, "usurpateur")
    monkeypatch.setenv("AGENT_KNOWLEDGE_AUTHOR", "compte-fixe")
    assert knowledge_tools.author() == "compte-fixe"
    assert auth_tools.USER_ENV not in auth_tools.subprocess_env({auth_tools.USER_ENV: "usurpateur"})


def _secret_page():
    import streamlit as st
    from tools import auth_tools
    st.session_state["valeur"] = auth_tools.secret_field(st, "JENKINS_API_TOKEN")


def test_service_tokens_are_never_sent_to_the_connected_user(monkeypatch):
    monkeypatch.setenv("JENKINS_API_TOKEN", "jeton-de-service-123")
    monkeypatch.setenv("AGENT_AUTH", "1")
    at = AppTest.from_function(_secret_page).run(timeout=30)
    field = at.text_input[0]
    assert field.value == "" and "jeton-de-service-123" not in str(field.proto)
    assert at.session_state["valeur"] == "jeton-de-service-123"   # champ vide : valeur enregistrée gardée
    field.input("nouveau-jeton").run(timeout=30)
    assert at.session_state["valeur"] == "nouveau-jeton"
    monkeypatch.delenv("AGENT_AUTH")
    assert AppTest.from_function(_secret_page).run(timeout=30).text_input[0].value == "jeton-de-service-123"


def test_setup_writes_secrets_outside_git(tmp_path):
    path = str(tmp_path / ".streamlit" / "secrets.toml")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write('[autre]\ncle = "v"\n\n[auth]\nclient_id = "ancien"\ncookie_secret = "garde"\n[auth.microsoft]\nx = 1\n')
    values = auth_setup.setup(auth_setup.entra_metadata_url("mon-annuaire"), "id-app", 'se"cret', path=path)
    assert values["cookie_secret"] == "garde"
    written = auth_setup.current_auth(path)
    assert written["client_secret"] == 'se"cret' and written["client_id"] == "id-app"
    assert written["server_metadata_url"] == ("https://login.microsoftonline.com/mon-annuaire/v2.0/"
                                              ".well-known/openid-configuration")
    assert "microsoft" not in written and '[autre]\ncle = "v"' in open(path, encoding="utf-8").read()
    assert auth_tools.missing_settings({"auth": written}) == []
    new = auth_setup.setup("u", "c", "s", path=str(tmp_path / "neuf" / "secrets.toml"))
    assert len(new["cookie_secret"]) >= 32
    shutil.copy(os.path.join(ROOT, ".gitignore"), tmp_path / ".gitignore")
    assert auth_setup.ensure_gitignore(str(tmp_path)) is False   # déjà ignoré dans le dépôt
    assert auth_setup.ensure_gitignore(str(tmp_path / "neuf")) is True


def test_no_real_secrets_file_is_tracked():
    import subprocess
    tracked = subprocess.run(["git", "-C", ROOT, "ls-files", ".streamlit"], capture_output=True, text=True).stdout
    assert ".streamlit/secrets.toml.example" in tracked or tracked == ""
    assert ".streamlit/secrets.toml\n" not in tracked + "\n" or tracked.strip() == ".streamlit/secrets.toml.example"
