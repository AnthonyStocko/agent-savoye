"""
Authentification OIDC de l'application (fournisseur d'identité de l'entreprise), par l'authentification native de
Streamlit (st.login / st.user / st.logout, Authlib).

- AGENT_AUTH : vide (ou 0) = désactivée, comportement inchangé ; activée (1) = page de connexion avant tout accès.
- AGENT_AUTH_ALLOWED : liste optionnelle (virgules ou points-virgules) d'adresses (prenom.nom@domaine), de domaines
  (@domaine) ou de groupes (nom ou identifiant, lus dans les revendications « groups » / « roles » du jeton) ;
  vide = toute personne authentifiée par le fournisseur.
- Configuration du fournisseur : .streamlit/secrets.toml, section [auth] (redirect_uri, cookie_secret, client_id,
  client_secret, server_metadata_url) — jamais commitée ; modèle : .streamlit/secrets.toml.example ; écrite par
  `python -m tools.auth_setup`.
- Auteur tracé (décisions, bilans, connaissances partagées, journal) : l'identité connectée « Nom <adresse> ».
  Dans l'application, lue sur la session ; dans les exécutions de main.py lancées par l'application, transmise par
  la variable AGENT_AUTH_USER (subprocess_env).
"""
import os
import re

AUTH_KEYS = ("redirect_uri", "cookie_secret", "client_id", "client_secret", "server_metadata_url")
USER_ENV = "AGENT_AUTH_USER"
SECRETS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".streamlit",
                            "secrets.toml")


def enabled() -> bool:
    """AGENT_AUTH renseigné et différent de 0 / false / non."""
    value = os.getenv("AGENT_AUTH", "").strip().lower()
    return bool(value) and value not in ("0", "false", "non", "no", "off")


def allowed_rules() -> list:
    return [r.strip() for r in re.split(r"[,;]", os.getenv("AGENT_AUTH_ALLOWED", "")) if r.strip()]


def missing_settings(secrets) -> list:
    """Clés de la section [auth] absentes ou vides de `secrets` (st.secrets ou dict)."""
    try:
        section = dict(secrets.get("auth") or {})
    except Exception:  # noqa: BLE001 — secrets.toml absent ou illisible : tout manque
        section = {}
    return [k for k in AUTH_KEYS if not str(section.get(k, "")).strip()]


def identity(user) -> dict:
    """{nom, adresse, groupes} d'un utilisateur connecté (st.user ou dict de revendications) ; {} sinon."""
    try:
        claims = dict(user.to_dict()) if hasattr(user, "to_dict") else dict(user or {})
    except Exception:  # noqa: BLE001
        return {}
    if not claims.get("is_logged_in", False):
        return {}
    email = str(claims.get("email") or claims.get("preferred_username") or claims.get("upn") or "").strip()
    name = str(claims.get("name") or "").strip()
    groups = []
    for key in ("groups", "roles"):
        value = claims.get(key) or []
        groups += [str(v) for v in (value if isinstance(value, (list, tuple)) else [value])]
    return {"nom": name, "adresse": email, "groupes": groups} if (name or email) else {}


def logged_in(user) -> bool:
    """La session est connectée (revendication is_logged_in ; absente = non connectée)."""
    try:
        return bool(user.get("is_logged_in")) if hasattr(user, "get") else bool(getattr(user, "is_logged_in", False))
    except (AttributeError, KeyError):
        return False


def is_allowed(person: dict, rules: list = None) -> bool:
    """Personne autorisée par AGENT_AUTH_ALLOWED : adresse exacte, domaine (@…) ou groupe ; liste vide = oui."""
    rules = allowed_rules() if rules is None else rules
    if not person:
        return False
    if not rules:
        return True
    email, groups = person.get("adresse", "").lower(), {g.lower() for g in person.get("groupes") or []}
    for rule in (r.lower() for r in rules):
        if rule.startswith("@") and email.endswith(rule):
            return True
        if "@" in rule[1:] and email == rule:
            return True
        if rule in groups:
            return True
    return False


def label(person: dict) -> str:
    """Auteur tracé : « Nom <adresse> », à défaut l'un des deux."""
    name, email = (person or {}).get("nom", ""), (person or {}).get("adresse", "")
    return f"{name} <{email}>" if name and email else name or email


def session_identity() -> dict:
    """Identité connectée de la session Streamlit en cours ({} hors de l'application ou sans connexion)."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        if get_script_run_ctx() is None:
            return {}
        import streamlit as st
        return identity(st.user)
    except Exception:  # noqa: BLE001 — Streamlit absent ou session sans authentification configurée
        return {}


def current_author() -> str:
    """Auteur connecté : session de l'application, sinon AGENT_AUTH_USER (exécution lancée par l'application)."""
    if not enabled():
        return ""
    return label(session_identity()) or os.getenv(USER_ENV, "").strip()


def subprocess_env(base: dict = None) -> dict:
    """Environnement d'une exécution de main.py lancée par l'application : l'auteur connecté y est transmis."""
    env = dict(os.environ if base is None else base)
    author = current_author()
    if author:
        env[USER_ENV] = author
    else:
        env.pop(USER_ENV, None)
    return env


def require_login(st) -> dict:
    """
    Garde de l'application, appelée avant tout affichage : sans AGENT_AUTH, rien (renvoie {}). Sinon, configuration
    [auth] incomplète → page d'explication ; personne non connectée → page de connexion ; connectée mais non
    autorisée → refus et déconnexion. Arrête le script (st.stop) dans ces trois cas. Renvoie l'identité connectée.
    """
    if not enabled():
        return {}
    missing = missing_settings(st.secrets)
    if missing:
        st.title("Connexion non configurée")
        st.error("AGENT_AUTH est activé mais la configuration du fournisseur d'identité est incomplète "
                 f"(.streamlit/secrets.toml, section [auth] : {', '.join(missing)}).")
        st.markdown("Sur le poste qui héberge l'application : `python -m tools.auth_setup` (voir README, "
                    "« Connexion OIDC »), puis redémarrer l'application.")
        st.stop()
    if not logged_in(st.user):
        st.title("Scrum Master IA")
        st.info("Connectez-vous avec votre compte de l'entreprise pour accéder à l'application.")
        st.button("Se connecter", type="primary", on_click=st.login, icon=":material/login:")
        st.stop()
    person = identity(st.user)
    if not is_allowed(person):
        st.title("Accès refusé")
        st.error(f"{label(person) or 'Ce compte'} n'est pas autorisé à utiliser cette application "
                 f"(liste AGENT_AUTH_ALLOWED).")
        st.button("Se déconnecter", on_click=st.logout, icon=":material/logout:")
        st.stop()
    return person


def secret_field(st, name: str, label: str = "", **kwargs) -> str:
    """
    Champ d'un secret du .env (jeton, mot de passe des comptes de service) : connexion active, la valeur enregistrée
    n'est jamais renvoyée au navigateur — champ vide = valeur gardée, saisie = remplacement. Sinon, champ masqué
    pré-rempli (comportement historique). Renvoie la valeur à enregistrer.
    """
    current = os.getenv(name, "")
    if not enabled():
        return st.text_input(label or name, value=current, type="password", **kwargs)
    entered = st.text_input(label or name, value="", type="password", key=f"secret_{name}",
                            placeholder="renseigné — non affiché (saisir pour remplacer)" if current else "", **kwargs)
    return entered or current
