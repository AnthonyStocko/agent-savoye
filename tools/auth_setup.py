"""
Mise en place de la connexion OIDC, sans rien demander à l'infrastructure : écrit la section [auth] de
.streamlit/secrets.toml (jamais commité), génère le cookie_secret, vérifie que le fournisseur répond, et active
AGENT_AUTH dans le .env.

    python -m tools.auth_setup --tenant <id ou domaine Entra ID> --client-id <id> [--client-secret <secret>]
                               [--redirect-uri http://localhost:8501/oauth2callback] [--allowed "@domaine.com"]
                               [--activer]

Autre fournisseur OIDC : --metadata-url <…/.well-known/openid-configuration> au lieu de --tenant. Sans
--client-secret, il est demandé sans écho. Voir README, « Connexion OIDC ».
"""
import argparse
import getpass
import os
import re
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import auth_tools, env_tools  # noqa: E402

DEFAULT_REDIRECT = "http://localhost:8501/oauth2callback"
GITIGNORE_LINE = ".streamlit/secrets.toml"


def entra_metadata_url(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant.strip()}/v2.0/.well-known/openid-configuration"


def _toml_string(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def current_auth(path: str) -> dict:
    """Valeurs [auth] déjà présentes dans `path` ({} si absent ou illisible)."""
    try:
        import tomllib
        with open(path, "rb") as f:
            return dict(tomllib.load(f).get("auth") or {})
    except (OSError, ValueError):
        return {}


def with_auth_section(text: str, values: dict) -> str:
    """Texte de secrets.toml où la section [auth] (et ses sous-sections [auth.*]) est remplacée par `values`."""
    kept, skipping = [], False
    for line in text.splitlines():
        header = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if header:
            skipping = header.group(1).strip() == "auth" or header.group(1).strip().startswith("auth.")
        if not skipping:
            kept.append(line)
    body = "\n".join(kept).strip()
    section = "[auth]\n" + "".join(f"{k} = {_toml_string(values[k])}\n" for k in auth_tools.AUTH_KEYS)
    return (body + "\n\n" if body else "") + section


def ensure_gitignore(root: str = ROOT) -> bool:
    """Ajoute .streamlit/secrets.toml au .gitignore s'il n'y est pas ; True si ajouté."""
    path = os.path.join(root, ".gitignore")
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    if GITIGNORE_LINE in text.splitlines():
        return False
    with open(path, "a", encoding="utf-8") as f:
        f.write(("" if text.endswith("\n") or not text else "\n")
                + "# Connexion OIDC : identifiants du fournisseur d'identité (modèle : .streamlit/secrets.toml.example)\n"
                + GITIGNORE_LINE + "\n")
    return True


def check_provider(url: str) -> str:
    """'' si le document de découverte OIDC répond avec un issuer, sinon la raison."""
    try:
        import requests
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            return f"HTTP {response.status_code}"
        return "" if response.json().get("issuer") else "réponse sans « issuer »"
    except Exception as e:  # noqa: BLE001
        return str(e)[:200]


def setup(metadata_url: str, client_id: str, client_secret: str, redirect_uri: str = DEFAULT_REDIRECT,
          path: str = auth_tools.SECRETS_PATH) -> dict:
    """Écrit la section [auth] de `path` (cookie_secret existant gardé) ; renvoie les valeurs écrites."""
    previous = current_auth(path)
    values = {"redirect_uri": redirect_uri, "cookie_secret": previous.get("cookie_secret") or secrets.token_urlsafe(32),
              "client_id": client_id, "client_secret": client_secret, "server_metadata_url": metadata_url}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(with_auth_section(text, values))
    return values


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Connexion OIDC de l'application (voir README).")
    provider = parser.add_mutually_exclusive_group(required=True)
    provider.add_argument("--tenant", help="Entra ID : identifiant de l'annuaire (tenant) ou domaine")
    provider.add_argument("--metadata-url", help="Autre fournisseur : URL .well-known/openid-configuration")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", default="")
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT)
    parser.add_argument("--allowed", default=None, help="AGENT_AUTH_ALLOWED : adresses, @domaines ou groupes")
    parser.add_argument("--activer", action="store_true", help="AGENT_AUTH=1 dans le .env")
    args = parser.parse_args(argv)
    url = args.metadata_url or entra_metadata_url(args.tenant)
    secret = args.client_secret or getpass.getpass("Secret client (non affiché) : ")
    if not secret.strip():
        print("❌ Secret client vide : rien n'est écrit.")
        return 2
    problem = check_provider(url)
    print(f"Fournisseur : {url} — " + (f"⚠️ injoignable ({problem}) ; configuration écrite quand même"
                                       if problem else "répond"))
    setup(url, args.client_id.strip(), secret.strip(), args.redirect_uri.strip())
    print(f"✅ {auth_tools.SECRETS_PATH} : section [auth] écrite (cookie_secret généré ou conservé).")
    if ensure_gitignore():
        print(f"✅ {GITIGNORE_LINE} ajouté au .gitignore.")
    env = {}
    if args.activer:
        env["AGENT_AUTH"] = "1"
    if args.allowed is not None:
        env["AGENT_AUTH_ALLOWED"] = args.allowed.strip()
    if env:
        env_tools.write_env(os.path.join(ROOT, ".env"), env)
        print("✅ .env : " + ", ".join(f"{k}={v}" for k, v in env.items()))
    print(f"URL de retour à déclarer dans l'enregistrement de l'application : {args.redirect_uri}")
    print("Redémarrez l'application (streamlit run app.py) pour prendre en compte la configuration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
