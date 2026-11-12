"""
Serveurs MCP des appels `claude` de l'agent : LISTE FERMÉE. Par défaut, aucun serveur (--strict-mcp-config) ; seul
Groundcover (lecture des logs serveur de l'environnement de test) peut être ajouté, et seulement pour les étapes de
diagnostic (DIAGNOSTIC_STEPS) : jamais pour l'analyse d'un ticket ni la génération d'un patch.

Configuration (onglet ⚙️, section « Serveurs MCP ») :
- GROUNDCOVER_MCP_URL : adresse du serveur MCP, donnée par « Connect to MCP » dans Groundcover (la commande ou la
  configuration JSON copiée y est aussi acceptée : l'adresse et les en-têtes non secrets en sont extraits) ;
- GROUNDCOVER_MCP_HEADERS : en-têtes non secrets éventuels (« Nom: valeur ; Nom: valeur »), jamais de jeton ;
- GROUNDCOVER_MCP_ENABLED : 1 pour activer ;
- GROUNDCOVER_MCP_TOOLS : (facultatif) outils autorisés, à défaut ceux de lecture relevés par le bouton « Tester ».

Garde-fous :
- le fichier de configuration MCP écrit pour un appel ne contient QUE le serveur Groundcover, et --strict-mcp-config
  reste passé : aucun autre serveur (ni Jira, ni connecteur claude.ai : ENABLE_CLAUDEAI_MCP_SERVERS=false) ;
- seuls les outils de lecture sont dans --allowedTools ; les autres outils relevés sont dans --disallowedTools ;
- aucun jeton : la connexion (OAuth, par navigateur) est conservée par Claude Code dans son dossier de configuration
  (CLAUDE_CONFIG_DIR) ; une adresse ou un en-tête porteur d'un secret est refusé ;
- lignes de log masquées (mots de passe, jetons) avant tout stockage (réponse de l'appel, état du test).
"""
import json
import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

from tools.groundcover_tools import mask

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_DIR = os.path.join(AGENT_DIR, ".tmp", "mcp")
SERVER = "groundcover"
# Seuls serveurs que l'agent peut charger, jamais étendue par la configuration
ALLOWED_SERVERS = (SERVER,)
TOOL_PREFIX = f"mcp__{SERVER}__"
# Étapes (noms de report_token_usage) qui reçoivent Groundcover : analyse des échecs d'un run importé, correction de
# l'init, revue d'un run de vérification (correction après exécution)
DIAGNOSTIC_STEPS = ("cucumber-import-fix", "cucumber-init-fix", "cucumber-verify-fix")
URL_ENV, HEADERS_ENV, ENABLED_ENV, TOOLS_ENV = ("GROUNDCOVER_MCP_URL", "GROUNDCOVER_MCP_HEADERS",
                                                "GROUNDCOVER_MCP_ENABLED", "GROUNDCOVER_MCP_TOOLS")
WINDOW_SECONDS = 30
ACCESS_MARKER = "GROUNDCOVER_ACCES=echec"
STATUS_LABELS = {"non configuré": "non configuré", "désactivé": "désactivé", "non testé": "non testé",
                 "connecté": "connecté", "expiré": "connexion expirée ou absente", "erreur": "erreur"}

_FORBIDDEN_HOST = re.compile(r"atlassian|jira", re.I)
_SECRET_NAME = re.compile(r"authorization|token|secret|api[-_]?key|password|passwd|pwd|cookie|credential|bearer", re.I)
_READ_VERBS = {"get", "list", "query", "search", "describe", "fetch", "find", "read", "show", "explain", "count",
               "analyze", "analyse", "lookup", "retrieve", "summarize", "aggregate", "inspect", "view"}
_WRITE_WORDS = {"create", "update", "delete", "remove", "write", "set", "put", "post", "patch", "edit", "add", "mute",
                "silence", "ack", "acknowledge", "resolve", "enable", "disable", "assign", "close", "send", "trigger",
                "run", "execute", "exec", "modify", "insert", "upsert", "drop", "deploy", "restart", "scale", "save",
                "import", "upload", "apply", "rollback", "kill", "stop", "start", "invite", "grant", "revoke"}
_AUTH_ERROR = re.compile(r"\b401\b|\b403\b|unauthori[sz]ed|forbidden|expired?|expir|needs[- ]auth|re-?authenticat"
                         r"|authenticat|oauth|invalid[_ ]token|login", re.I)
_POD_INFO = re.compile(r"\[PodInfo\]\s+([\w.-]+)/([\w.-]+)")
_CONTAINER = re.compile(r"\bContainer \[([\w.-]+)\]")
_RUNNING_ON = re.compile(r"\bRunning on ([\w.-]+) in /")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def parse_headers(text: str) -> dict:
    """En-têtes « Nom: valeur » séparés par « ; » ou des retours à la ligne."""
    out = {}
    for item in re.split(r"[;\n]", text or ""):
        name, sep, value = item.partition(":")
        if sep and name.strip():
            out[name.strip()] = value.strip()
    return out


def format_headers(headers: dict) -> str:
    return " ; ".join(f"{k}: {v}" for k, v in (headers or {}).items())


def url_error(url: str) -> str:
    """'' si l'adresse est acceptable : http(s), sans identifiants ni jeton, jamais un serveur Jira / Atlassian."""
    if not url:
        return "adresse vide"
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return "adresse http(s) attendue (ex. celle donnée par « Connect to MCP » dans Groundcover)"
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1"):
        return "adresse https attendue"
    if _FORBIDDEN_HOST.search(parts.hostname or ""):
        return "serveur Jira / Atlassian refusé : seul Groundcover est autorisé"
    if parts.username or parts.password or any(_SECRET_NAME.search(k) for k, _ in parse_qsl(parts.query)):
        return "adresse porteuse d'un identifiant ou d'un jeton refusée : la connexion se fait par navigateur"
    return ""


def secret_headers(headers: dict) -> list:
    return [name for name in headers or {} if _SECRET_NAME.search(name)]


def parse_connect(text: str) -> dict:
    """
    Saisie de « Connect to MCP » : adresse seule, commande (`claude mcp add … <url> --header "Nom: valeur"`) ou
    configuration JSON (mcpServers). Renvoie {"url", "headers", "refuses", "erreur"} ; les en-têtes secrets
    (Authorization, jeton, clé) sont refusés : jamais de jeton dans le .env.
    """
    text = (text or "").strip()
    url, headers = "", {}
    if text.startswith("{"):
        try:
            document = json.loads(text)
        except ValueError:
            return {"url": "", "headers": {}, "refuses": [], "erreur": "configuration JSON illisible"}
        servers = document.get("mcpServers") or document.get("servers") or {"_": document}
        server = next((s for s in servers.values() if isinstance(s, dict) and s.get("url")), {})
        url, headers = str(server.get("url", "")), {str(k): str(v) for k, v in (server.get("headers") or {}).items()}
    elif re.fullmatch(r"https?://\S+", text):
        url = text
    else:
        try:
            tokens = shlex.split(text.replace("\\\n", " "), posix=True)
        except ValueError:
            tokens = text.split()
        for i, token in enumerate(tokens):
            if not url and re.match(r"https?://", token):
                url = token
            elif token in ("--header", "-H") and i + 1 < len(tokens):
                headers.update(parse_headers(tokens[i + 1]))
    refused = secret_headers(headers)
    headers = {k: v for k, v in headers.items() if k not in refused}
    return {"url": url, "headers": headers, "refuses": refused, "erreur": url_error(url)}


def settings() -> dict:
    headers = parse_headers(os.getenv(HEADERS_ENV, ""))
    return {"url": os.getenv(URL_ENV, "").strip(), "headers": {k: v for k, v in headers.items()
                                                               if k not in secret_headers(headers)},
            "active": os.getenv(ENABLED_ENV, "").strip() == "1"}


def configured() -> bool:
    url = settings()["url"]
    return bool(url) and not url_error(url)


def enabled() -> bool:
    return settings()["active"] and configured()


def config_document() -> dict:
    """Configuration MCP de l'agent : le serveur Groundcover, et lui seul."""
    s = settings()
    server = {"type": "http", "url": s["url"], **({"headers": s["headers"]} if s["headers"] else {})}
    document = {"mcpServers": {SERVER: server}}
    assert tuple(document["mcpServers"]) == ALLOWED_SERVERS
    return document


def config_path(directory: str = "") -> str:
    return os.path.join(directory or MCP_DIR, "mcp-config.json")


def write_config(directory: str = "") -> str:
    """Écrit la configuration MCP (Groundcover seul) et renvoie son chemin ; ValueError si l'adresse est refusée."""
    error = url_error(settings()["url"])
    if error:
        raise ValueError(f"Groundcover : {error}")
    path = config_path(directory)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config_document(), f, indent=2)
    return path


# ---------------------------------------------------------------------------
# Outils autorisés (lecture seule)
# ---------------------------------------------------------------------------

def _words(tool: str) -> list:
    name = tool[len(TOOL_PREFIX):] if tool.startswith(TOOL_PREFIX) else tool
    return [w.lower() for w in re.split(r"[_\-\s.]+|(?<=[a-z])(?=[A-Z])", name) if w]


def read_only(tool: str) -> bool:
    """Outil Groundcover de lecture : premier mot de lecture (get, list, query, search…), aucun mot d'écriture."""
    words = _words(tool)
    return (tool.startswith(TOOL_PREFIX) and bool(words) and words[0] in _READ_VERBS
            and not any(w in _WRITE_WORDS for w in words))


def state_path(directory: str = "") -> str:
    return os.path.join(directory or MCP_DIR, f"{SERVER}-etat.json")


def load_state(directory: str = "") -> dict:
    try:
        with open(state_path(directory), encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(status: str, message: str = "", tools: list = None, directory: str = "") -> dict:
    """État du dernier test ou du dernier accès (sans jeton ; message masqué). Les outils relevés sont conservés."""
    previous = load_state(directory)
    state = {"statut": status, "date": datetime.now().isoformat(timespec="seconds"),
             "message": mask(message)[:500],
             "outils": sorted(t for t in (tools if tools is not None else previous.get("outils") or [])
                              if str(t).startswith(TOOL_PREFIX)),
             "adresse": settings()["url"]}
    os.makedirs(os.path.dirname(state_path(directory)), exist_ok=True)
    with open(state_path(directory), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return state


def _known_tools(directory: str = "") -> list:
    state = load_state(directory)
    # Outils relevés pour une autre adresse : non repris
    return list(state.get("outils") or []) if state.get("adresse", settings()["url"]) == settings()["url"] else []


def allowed_tools(directory: str = "") -> list:
    """Outils de lecture autorisés : GROUNDCOVER_MCP_TOOLS s'il est renseigné, sinon ceux relevés par le test."""
    configured_tools = [t.strip() for t in re.split(r"[,\s]+", os.getenv(TOOLS_ENV, "")) if t.strip()]
    tools = configured_tools or _known_tools(directory)
    return sorted({t for t in tools if read_only(t)})


def denied_tools(directory: str = "") -> list:
    """Outils relevés qui ne sont pas de lecture (ou écartés de GROUNDCOVER_MCP_TOOLS) : refusés explicitement."""
    allowed = set(allowed_tools(directory))
    return sorted(t for t in _known_tools(directory) if t not in allowed)


def unavailable_reason(directory: str = "") -> str:
    """Pourquoi Groundcover n'est pas donné aux diagnostics ('' s'il l'est)."""
    s = settings()
    if not s["url"]:
        return "non configuré"
    if url_error(s["url"]):
        return url_error(s["url"])
    if not s["active"]:
        return "désactivé"
    if not allowed_tools(directory):
        return "aucun outil de lecture connu : bouton « Tester » de l'onglet ⚙️ (section Serveurs MCP)"
    return ""


def claude_args(step: str, directory: str = "") -> dict:
    """
    Options MCP d'un appel `claude` de l'étape `step` : None hors diagnostic ou si Groundcover n'est pas utilisable ;
    sinon {"args": (--mcp-config <fichier>[, --disallowedTools …]), "tools": [outils de lecture]}. --strict-mcp-config
    reste ajouté par l'appelant.
    """
    if step not in DIAGNOSTIC_STEPS or unavailable_reason(directory):
        return None
    tools = allowed_tools(directory)
    denied = denied_tools(directory)
    args = ("--mcp-config", write_config(directory)) + (("--disallowedTools", *denied) if denied else ())
    return {"args": args, "tools": tools}


def agent_env(env: dict = None) -> dict:
    """Environnement Claude Code de l'agent (même CLAUDE_CONFIG_DIR), connecteurs claude.ai désactivés."""
    env = dict(os.environ if env is None else env)
    config_dir = env.pop("CLAUDE_CONFIG_DIR", "").strip()
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config_dir)
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    return env


# ---------------------------------------------------------------------------
# Accès en échec pendant un diagnostic
# ---------------------------------------------------------------------------

def access_failure(output: str) -> str:
    """Raison signalée par l'agent (« GROUNDCOVER_ACCES=echec : … »), '' si l'accès n'a pas échoué."""
    found = re.search(re.escape(ACCESS_MARKER) + r"\s*:?\s*(.*)", output or "")
    return mask(found.group(1).strip()[:300] or "raison non précisée") if found else ""


def record_access_failure(reason: str, directory: str = "") -> dict:
    return save_state("expiré" if _AUTH_ERROR.search(reason or "") else "erreur", reason, directory=directory)


def failure_message(reason: str) -> str:
    return (f"Accès Groundcover en échec ({reason}) : reconnectez-vous (onglet ⚙️, Serveurs MCP, « Se connecter à "
            f"Groundcover »), sinon exportez les logs à la main (fenêtres de l'analyse) et importez-les avec le run.")


# ---------------------------------------------------------------------------
# Bloc de diagnostic : fenêtre de chaque échec, application de test
# ---------------------------------------------------------------------------

def pod_info(console: str) -> dict:
    """Pod du build (« [PodInfo] namespace/pod », « Running on <pod> in … ») et ses conteneurs, lus dans la console."""
    console = console or ""
    found = _POD_INFO.findall(console)
    running = _RUNNING_ON.findall(console)
    namespace, pod = found[-1] if found else ("", "")
    pod = pod or next((r for r in reversed(running) if r.lower() not in ("jenkins", "master", "built-in")), "")
    containers = list(dict.fromkeys(c for c in _CONTAINER.findall(console) if c != "jnlp"))
    return {"namespace": namespace, "pod": pod, "conteneurs": containers}


def _stamp(value: str):
    """Heure UTC (naïve) d'un horodatage ISO ; sans fuseau, lu comme UTC (heures des rapports Cucumber)."""
    try:
        stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(timezone.utc).replace(tzinfo=None) if stamp.tzinfo else stamp


def windows(failures: list, seconds: int = WINDOW_SECONDS) -> list:
    """Fenêtre (UTC) de chaque échec : heure du step en échec −`seconds` / +`seconds`. Sans heure : écarté."""
    out = []
    for f in failures or []:
        stamp = _stamp(f.get("heure") or "")
        if stamp:
            out.append({"id": f.get("id") or f"{f.get('fichier', '')}::{f.get('titre', '')}".strip(":"),
                        "step": str(f.get("step") or "")[:160],
                        "debut": (stamp - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "fin": (stamp + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    return out


def prompt_values(failures: list, namespace: str = "", container: str = "", pod: dict = None,
                  build: str = "", directory: str = "") -> dict:
    """Valeurs du modèle de prompt logs_mcp_access : outils, application de test, fenêtres des échecs."""
    pod = pod or {}
    namespace = namespace or pod.get("namespace", "")
    target = [f"namespace « {namespace} »" if namespace else "namespace inconnu (configuration du job)",
              f"conteneur « {container} »" if container else (
                  "conteneur de l'application à choisir parmi " + ", ".join(f"« {c} »" for c in pod["conteneurs"])
                  if pod.get("conteneurs") else "conteneur inconnu (configuration du job)")]
    if pod.get("pod"):
        target.append(f"pod « {pod['pod']} »")
    lines = [f"- {w['id']} : {w['debut']} → {w['fin']} — step « {w['step']} »" for w in windows(failures)[:20]]
    return {"outils": ", ".join(allowed_tools(directory)), "application": ", ".join(target)
            + (f" (build {build})" if build else ""),
            "fenetres": "\n".join(lines) or "- (heure du step en échec inconnue : fenêtre de la fin du build)",
            "marge": str(WINDOW_SECONDS), "marqueur": ACCESS_MARKER}


# ---------------------------------------------------------------------------
# Connexion et test (onglet ⚙️)
# ---------------------------------------------------------------------------

def claude_exe() -> str:
    return shutil.which("claude") or "claude"


def login_command(directory: str = "") -> list:
    """Claude Code interactif avec la configuration MCP de l'agent (Groundcover seul) : /mcp pour s'authentifier."""
    return [claude_exe(), "--strict-mcp-config", "--mcp-config", write_config(directory)]


LOGIN_STEPS = ("dans la console Claude Code qui s'ouvre : tapez /mcp, choisissez « groundcover », puis "
               "« Authenticate » ; terminez la connexion dans le navigateur avec votre compte Groundcover, puis "
               "/exit et « Tester ».")


def open_login_console(directory: str = "") -> str:
    """
    Ouvre Claude Code dans une nouvelle console (Windows) avec le CLAUDE_CONFIG_DIR de l'agent et sa configuration
    MCP, pour l'authentification initiale. Ailleurs, renvoie la commande à lancer. Renvoie la consigne à afficher.
    """
    cmd = login_command(directory)
    env = agent_env()
    if os.name == "nt":
        subprocess.Popen(["cmd", "/k", *cmd], cwd=AGENT_DIR, env=env,
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return "Console ouverte : " + LOGIN_STEPS
    prefix = f"CLAUDE_CONFIG_DIR={shlex.quote(env['CLAUDE_CONFIG_DIR'])} " if env.get("CLAUDE_CONFIG_DIR") else ""
    return f"Lancez dans un terminal : {prefix}{shlex.join(cmd)} — puis " + LOGIN_STEPS


def _events(stdout: str) -> list:
    out = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def parse_stream(stdout: str) -> dict:
    """
    Sortie stream-json d'un appel de test : statut du serveur Groundcover (message init), outils qu'il expose,
    résultats des appels d'outils (erreur ou non) et texte final.
    """
    events = _events(stdout)
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    server = next((s for s in init.get("mcp_servers") or [] if s.get("name") == SERVER), {})
    results = []
    for event in events:
        for block in ((event.get("message") or {}).get("content") or []) if event.get("type") == "user" else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else " ".join(
                    str(c.get("text", "")) for c in content or [] if isinstance(c, dict))
                results.append({"erreur": bool(block.get("is_error")), "texte": mask(text)[:400]})
    final = next((e for e in reversed(events) if e.get("type") == "result"), {})
    return {"serveur": server.get("status", ""), "outils": [t for t in init.get("tools") or []
                                                            if str(t).startswith(TOOL_PREFIX)],
            "resultats": results, "texte": mask(str(final.get("result") or ""))[:500]}


def _run(cmd: list, prompt: str, timeout: int) -> str:
    result = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            env=agent_env(), cwd=AGENT_DIR, timeout=timeout, shell=(os.name == "nt"))
    return (result.stdout or "") + ("\n" + result.stderr if result.stderr and not result.stdout else "")


TEST_PROMPT = ("Test de connexion. Appelle UNE seule fois l'outil Groundcover de lecture le plus simple : la liste des "
               "services ou workloads, à défaut les logs de la dernière minute (5 lignes au plus). Ne recopie aucune "
               "ligne de log. Réponds ensuite sur une seule ligne : « GROUNDCOVER_TEST=ok : <ce qui a été lu, en "
               "quelques mots> » ou « GROUNDCOVER_TEST=echec : <raison> ».")


def test_connection(directory: str = "", timeout: int = 240, run=None) -> dict:
    """
    Bouton « Tester » : 1) démarrage de Claude Code avec la configuration MCP (statut du serveur, outils exposés) ;
    2) si connecté, appel minimal d'un outil de lecture. Résultat enregistré (état sans jeton). `run` : exécuteur
    (cmd, prompt, timeout) -> stdout, remplaçable par les tests. Renvoie l'état enregistré.
    """
    run = run or _run
    if not configured():
        return {"statut": "non configuré", "message": url_error(settings()["url"]) or "adresse vide", "outils": []}
    model = os.getenv("CLAUDE_MODEL_MCP_TEST", "").strip() or "haiku"
    base = [claude_exe(), "--print", "--output-format", "stream-json", "--verbose", "--strict-mcp-config",
            "--mcp-config", write_config(directory), "--model", model]
    try:
        first = parse_stream(run(base + ["--max-turns", "1"], "Réponds seulement OK.", timeout))
    except (OSError, subprocess.SubprocessError) as e:
        return save_state("erreur", f"Claude Code non lancé : {type(e).__name__}", directory=directory)
    if first["serveur"] in ("needs-auth", "needs_auth"):
        return save_state("expiré", "authentification requise : « Se connecter à Groundcover »", directory=directory)
    if first["serveur"] != "connected":
        return save_state("erreur", f"serveur {first['serveur'] or 'absent du démarrage'} {first['texte']}".strip(),
                          directory=directory)
    tools = first["outils"]
    readers = sorted(t for t in tools if read_only(t))
    if not readers:
        return save_state("erreur", "aucun outil de lecture exposé par le serveur", tools, directory)
    writers = sorted(t for t in tools if t not in readers)
    cmd = base + ["--max-turns", "4", "--allowedTools", *readers] + (["--disallowedTools", *writers] if writers else [])
    try:
        second = parse_stream(run(cmd, TEST_PROMPT, timeout))
    except (OSError, subprocess.SubprocessError) as e:
        return save_state("erreur", f"appel de test interrompu : {type(e).__name__}", tools, directory)
    errors = [r["texte"] for r in second["resultats"] if r["erreur"]]
    ok = second["resultats"] and not errors and "GROUNDCOVER_TEST=ok" in second["texte"]
    if ok:
        return save_state("connecté", second["texte"].split("GROUNDCOVER_TEST=ok", 1)[1].strip(" :"), tools,
                          directory)
    reason = errors[0] if errors else second["texte"] or "aucun outil appelé"
    return save_state("expiré" if _AUTH_ERROR.search(reason) else "erreur", reason, tools, directory)


def status(directory: str = "") -> dict:
    """État affiché : non configuré / désactivé / non testé / connecté / expiré / erreur, avec date et message."""
    s = settings()
    if not s["url"] or url_error(s["url"]):
        return {"statut": "non configuré", "message": url_error(s["url"]) if s["url"] else "", "date": ""}
    state = load_state(directory)
    if not state or state.get("adresse", s["url"]) != s["url"]:
        state = {"statut": "non testé", "message": "", "date": ""}
    if not s["active"]:
        return {**state, "statut": "désactivé", "detail": state.get("statut", "")}
    return state
