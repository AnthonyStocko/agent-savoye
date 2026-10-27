"""
Masquage des secrets, commun à tout l'agent (import de rapports Cucumber, dépôt des connaissances partagées) :
paramètres -D*password*, -DuserName, jetons, en-têtes Authorization, mots de passe d'URL ou de JSON, leurs formes
base64, les valeurs secrètes de la configuration (.env : *_TOKEN, *_SECRET, *_PASSWORD, *_API_KEY) et celles lues
pendant l'exécution (register_secrets : identifiants des arguments d'un job Jenkins, gardés en mémoire seulement).

`remaining_secrets` contrôle un texte déjà masqué : tout motif de secret qui subsiste interdit son dépôt.
"""
import base64
import binascii
import os
import re

MASK = "****"
_VALUE = r"(\"[^\"]*\"|'[^']*'|[^\s\"',;&}]+)"
SECRET_PATTERNS = (
    ("mot de passe", re.compile(r"(-D[\w.-]*pass(?:word|wd)?[\w.-]*=)" + _VALUE, re.I)),
    ("utilisateur", re.compile(r"(-D[\w.-]*user(?:name)?[\w.-]*=)" + _VALUE, re.I)),
    ("jeton", re.compile(r"(-D[\w.-]*(?:token|secret|api[_-]?key)[\w.-]*=)" + _VALUE, re.I)),
    ("jeton", re.compile(r"(Authorization\s*:\s*(?:Bearer|Basic)\s+)(\S+)", re.I)),
    ("mot de passe", re.compile(r"([?&;](?:password|passwd|pwd|token)=)([^&\s\"']+)", re.I)),
    ("mot de passe", re.compile(r"(\"(?:password|passwd|pwd|token)\"\s*:\s*\")([^\"]+)", re.I)),
)
# Secrets reconnaissables à leur seule forme (contrôle avant dépôt, en plus des motifs ci-dessus)
TELLTALE_PATTERNS = (
    ("clé privée", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jeton JWT", re.compile(r"\beyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]{10,}")),
    ("jeton Atlassian", re.compile(r"\bATATT[\w-]{20,}")),
    ("jeton GitHub", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("clé AWS", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("clé Anthropic", re.compile(r"\bsk-ant-[\w-]{20,}")),
)
# Variables de configuration dont la valeur est un secret
_SECRET_ENV_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY)$", re.I)
MIN_ENV_SECRET_LEN = 6
# Valeurs secrètes lues pendant l'exécution (arguments d'un job Jenkins) : en mémoire seulement, jamais écrites
_RUNTIME_SECRETS = set()


def register_secrets(values) -> None:
    """Valeurs secrètes connues de ce processus (ex. identifiants des arguments d'un job), masquées partout ensuite."""
    for value in values or ():
        if value and len(value.strip()) >= 4:
            _RUNTIME_SECRETS.update(_variants(value.strip()))


def _variants(value: str) -> set:
    """Valeur secrète, sa forme base64 et, si elle est en base64, sa forme décodée."""
    out = {value, base64.b64encode(value.encode()).decode()}
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
        if decoded.isprintable() and len(decoded) >= 4:
            out.add(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        pass
    return {v for v in out if len(v) >= 4}


def mask_secrets(text: str) -> tuple:
    """(texte masqué, {type de secret: nombre}) : paramètres -D*password*, -DuserName, jetons, en-têtes
    Authorization, mots de passe d'URL ou de JSON, et toute autre occurrence de ces valeurs (base64 comprise)."""
    counts, values = {}, set()
    for label, pattern in SECRET_PATTERNS:
        def replace(match, label=label):
            value = match.group(2).strip("\"'")
            if value and value != MASK:
                counts[label] = counts.get(label, 0) + 1
                values.update(_variants(value))
            quote = match.group(2)[0] if match.group(2)[:1] in "\"'" and len(match.group(2)) > 1 else ""
            return f"{match.group(1)}{quote}{MASK}{quote}"
        text = pattern.sub(replace, text)
    for value in sorted(values | _RUNTIME_SECRETS, key=len, reverse=True):
        if value in text:
            text = text.replace(value, MASK)
            if value in _RUNTIME_SECRETS:
                counts["argument du job"] = counts.get("argument du job", 0) + 1
    return text, counts


def env_secrets(environ=None) -> dict:
    """{variable: valeur} des secrets de la configuration (*_TOKEN, *_SECRET, *_PASSWORD, *_API_KEY non vides)."""
    environ = os.environ if environ is None else environ
    return {name: value.strip() for name, value in environ.items()
            if _SECRET_ENV_NAME.search(name) and len((value or "").strip()) >= MIN_ENV_SECRET_LEN}


def mask_all(text: str, environ=None) -> tuple:
    """`mask_secrets` puis masquage des valeurs secrètes de la configuration (et de leur forme base64)."""
    text, counts = mask_secrets(text or "")
    for name, value in env_secrets(environ).items():
        for variant in sorted(_variants(value), key=len, reverse=True):
            if variant in text:
                text = text.replace(variant, MASK)
                counts[name] = counts.get(name, 0) + 1
    return text, counts


def remaining_secrets(text: str, environ=None) -> list:
    """
    Motifs de secret encore présents dans un texte supposé masqué : [(type, extrait tronqué)]. Liste vide = rien
    ne s'oppose au dépôt. L'extrait ne montre que le début de la valeur.
    """
    found = []
    for label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text or ""):
            if match.group(2).strip("\"'") not in ("", MASK):
                found.append((label, match.group(1).strip()[:40]))
    for label, pattern in TELLTALE_PATTERNS:
        found.extend((label, match.group(0)[:8] + "…") for match in pattern.finditer(text or ""))
    for name, value in env_secrets(environ).items():
        if any(variant in (text or "") for variant in _variants(value)):
            found.append(("configuration", name))
    if any(value in (text or "") for value in _RUNTIME_SECRETS):
        found.append(("argument du job", "valeur lue dans la configuration du job"))
    return found
