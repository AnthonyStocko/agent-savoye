"""
Serveurs MCP des appels `claude` : liste fermée. Sans Groundcover activé, appels identiques (--strict-mcp-config sans
--mcp-config) ; activé, fichier MCP limité à Groundcover, outils de lecture ajoutés pour les seules étapes de
diagnostic ; aucun serveur Jira, aucun jeton.
"""
import importlib
import io
import json
import os
import sys

import pytest

from tools import mcp_servers as ms

READ_TOOLS = ["mcp__groundcover__query_logs", "mcp__groundcover__list_workloads", "mcp__groundcover__getTraces"]
WRITE_TOOLS = ["mcp__groundcover__create_monitor", "mcp__groundcover__delete_dashboard",
               "mcp__groundcover__query_and_update"]
URL = "https://mcp.groundcover.example/api/mcp"
CONSOLE = os.path.join(os.path.dirname(__file__), "reference", "11-build-241-run-complet.console.txt")


@pytest.fixture(scope="module")
def main_module():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


@pytest.fixture
def mcp_dir(tmp_path, monkeypatch):
    for name in (ms.URL_ENV, ms.HEADERS_ENV, ms.ENABLED_ENV, ms.TOOLS_ENV):
        monkeypatch.delenv(name, raising=False)
    directory = str(tmp_path / "mcp")
    monkeypatch.setattr(ms, "MCP_DIR", directory)
    return directory


def enable(monkeypatch, tools=READ_TOOLS + WRITE_TOOLS):
    monkeypatch.setenv(ms.URL_ENV, URL)
    monkeypatch.setenv(ms.ENABLED_ENV, "1")
    ms.save_state("connecté", "ok", list(tools))


class FakeProcess:
    calls = []

    def __init__(self, cmd, **kwargs):
        FakeProcess.calls.append({"cmd": cmd, "env": kwargs.get("env")})
        self.returncode = 0

    def communicate(self, input=None):
        return json.dumps({"result": FakeProcess.answer, "usage": {"input_tokens": 1}}), ""


def run(main_module, monkeypatch, step, answer="fini", lean=True):
    FakeProcess.calls, FakeProcess.answer = [], answer
    monkeypatch.setattr(main_module.subprocess, "Popen", FakeProcess)
    out = main_module.run_claude("prompt", "sonnet", ["/std"], ["Read", "Grep"], ("--max-turns", "5"), lean=lean,
                                 mcp_step=step)
    return FakeProcess.calls[0], out


# ---------------------------------------------------------------------------
# Sans Groundcover : appels identiques à aujourd'hui
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("step", ["", "cucumber-import-fix", "cucumber-init-fix", "cucumber-verify-fix"])
def test_without_groundcover_calls_are_unchanged(main_module, monkeypatch, mcp_dir, step):
    call, _ = run(main_module, monkeypatch, step)
    expected = main_module.claude_command("sonnet", ["/std"], ["Read", "Grep"], ("--max-turns", "5"), lean=True)
    assert call["cmd"] == expected
    assert call["cmd"].count("--strict-mcp-config") == 1 and "--mcp-config" not in call["cmd"]
    assert not os.path.exists(ms.config_path())


def test_configured_but_disabled_changes_nothing(main_module, monkeypatch, mcp_dir):
    enable(monkeypatch)
    monkeypatch.setenv(ms.ENABLED_ENV, "0")
    call, _ = run(main_module, monkeypatch, "cucumber-import-fix")
    assert "--mcp-config" not in call["cmd"] and not any(t.startswith("mcp__") for t in call["cmd"])
    assert ms.status()["statut"] == "désactivé"


# ---------------------------------------------------------------------------
# Groundcover activé : étapes de diagnostic seulement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("step", ms.DIAGNOSTIC_STEPS)
@pytest.mark.parametrize("lean", [True, False])
def test_diagnostic_steps_get_groundcover_only(main_module, monkeypatch, mcp_dir, step, lean):
    enable(monkeypatch)
    call, _ = run(main_module, monkeypatch, step, lean=lean)
    cmd = call["cmd"]
    assert cmd.count("--strict-mcp-config") == 1
    path = cmd[cmd.index("--mcp-config") + 1]
    with open(path, encoding="utf-8") as f:
        document = json.load(f)
    assert list(document) == ["mcpServers"] and list(document["mcpServers"]) == ["groundcover"]
    assert document["mcpServers"]["groundcover"] == {"type": "http", "url": URL}
    allowed = cmd[cmd.index("--allowedTools") + 1:]
    assert allowed[:2] == ["Read", "Grep"] and sorted(allowed[2:]) == sorted(READ_TOOLS)
    denied = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--allowedTools")]
    assert set(WRITE_TOOLS) <= set(denied)
    if lean:
        tools = cmd[cmd.index("--tools") + 1:cmd.index("--strict-mcp-config")]
        assert not any(t.startswith("mcp__") for t in tools)
    # Ni connecteur claude.ai (Atlassian), ni aucun autre serveur ; même compte Claude
    assert call["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert not any("jira" in str(a).lower() or "atlassian" in str(a).lower() for a in cmd)


@pytest.mark.parametrize("step", ["", "cadrage", "refine", "patch", "cucumber-lot", "cucumber-lot-preuve-base",
                                  "cucumber-review", "ask"])
def test_other_steps_never_get_groundcover(main_module, monkeypatch, mcp_dir, step):
    enable(monkeypatch)
    call, _ = run(main_module, monkeypatch, step)
    assert "--mcp-config" not in call["cmd"] and not any(str(t).startswith("mcp__") for t in call["cmd"])
    assert ms.claude_args(step) is None


def test_never_tested_means_no_tools_and_no_server(main_module, monkeypatch, mcp_dir):
    monkeypatch.setenv(ms.URL_ENV, URL)
    monkeypatch.setenv(ms.ENABLED_ENV, "1")
    assert "Tester" in ms.unavailable_reason()
    call, _ = run(main_module, monkeypatch, "cucumber-init-fix")
    assert "--mcp-config" not in call["cmd"]
    assert ms.status()["statut"] == "non testé"


def test_configured_tools_keep_only_read_tools(mcp_dir, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv(ms.TOOLS_ENV, "mcp__groundcover__query_logs, mcp__groundcover__create_monitor, Bash")
    assert ms.allowed_tools() == ["mcp__groundcover__query_logs"]
    assert "mcp__groundcover__list_workloads" in ms.denied_tools()


def test_read_only_classification():
    for tool in READ_TOOLS + ["mcp__groundcover__search_logs", "mcp__groundcover__describe_service"]:
        assert ms.read_only(tool), tool
    for tool in WRITE_TOOLS + ["mcp__groundcover__mute_alert", "mcp__other__query_logs", "query_logs",
                               "mcp__groundcover__logs"]:
        assert not ms.read_only(tool), tool


# ---------------------------------------------------------------------------
# Configuration : jamais de serveur Jira, jamais de jeton
# ---------------------------------------------------------------------------

def test_jira_or_token_addresses_are_refused(mcp_dir, monkeypatch):
    for url in ("https://mcp.atlassian.com/v1/sse", "https://savoye.jira.example/mcp", "ftp://x/mcp",
                "http://mcp.example/api", "https://user:pw@mcp.example/api", "https://mcp.example/api?token=abc"):
        assert ms.url_error(url), url
    monkeypatch.setenv(ms.URL_ENV, "https://mcp.atlassian.com/v1/sse")
    monkeypatch.setenv(ms.ENABLED_ENV, "1")
    assert not ms.enabled() and ms.claude_args("cucumber-import-fix") is None
    with pytest.raises(ValueError):
        ms.write_config()


def test_connect_to_mcp_paste_keeps_address_and_drops_secrets():
    command = (f'claude mcp add --transport http groundcover {URL} --header "X-Timezone: Europe/Paris" '
               f'--header "Authorization: Bearer abc.def"')
    parsed = ms.parse_connect(command)
    assert parsed["url"] == URL and parsed["headers"] == {"X-Timezone": "Europe/Paris"}
    assert parsed["refuses"] == ["Authorization"] and not parsed["erreur"]
    as_json = ms.parse_connect(json.dumps({"mcpServers": {"groundcover": {
        "type": "http", "url": URL, "headers": {"X-Tenant": "t1", "X-Api-Key": "k"}}}}))
    assert as_json["headers"] == {"X-Tenant": "t1"} and as_json["refuses"] == ["X-Api-Key"]
    assert ms.parse_connect(URL)["url"] == URL


def test_secret_headers_from_env_never_reach_the_config_file(mcp_dir, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv(ms.HEADERS_ENV, "X-Timezone: UTC ; Authorization: Bearer abc")
    with open(ms.write_config(), encoding="utf-8") as f:
        text = f.read()
    assert "X-Timezone" in text and "Bearer" not in text and "abc" not in text


# ---------------------------------------------------------------------------
# Accès en échec, masquage, état
# ---------------------------------------------------------------------------

def test_expired_session_is_reported_and_output_masked(main_module, monkeypatch, mcp_dir, capsys):
    enable(monkeypatch)
    answer = ('{"corrections": []}\nGROUNDCOVER_ACCES=echec : 401 Unauthorized, token expired\n'
              '2026-10-30 13:40:01 ERROR login failed password=Secret123 token: abcdef')
    _, (code, output, _, _) = run(main_module, monkeypatch, "cucumber-import-fix", answer)
    assert "Secret123" not in output and "abcdef" not in output and "password=" in output
    assert ms.load_state()["statut"] == "expiré"
    printed = capsys.readouterr().out
    assert "Se connecter à" in printed and "à la main" in printed and "Secret123" not in printed


def test_output_without_groundcover_is_not_rewritten(main_module, monkeypatch, mcp_dir):
    _, (_, output, _, _) = run(main_module, monkeypatch, "", "password=x")
    assert output == "password=x"


def test_test_connection_statuses(mcp_dir, monkeypatch):
    monkeypatch.setenv(ms.URL_ENV, URL)

    def init(status, tools=()):
        return json.dumps({"type": "system", "subtype": "init", "tools": ["Read", *tools],
                           "mcp_servers": [{"name": "groundcover", "status": status}]})

    def result(text, error=False):
        return "\n".join([
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "is_error": error, "content": [{"type": "text", "text": text}]}]}}),
            json.dumps({"type": "result", "result": "GROUNDCOVER_TEST=" + ("echec : x" if error else
                                                                           "ok : 12 workloads")})])

    calls = []

    def fake(answers):
        def runner(cmd, prompt, timeout):
            calls.append(cmd)
            return answers.pop(0)
        return runner

    assert ms.test_connection(run=fake([init("needs-auth")]))["statut"] == "expiré"
    state = ms.test_connection(run=fake([init("connected", READ_TOOLS + WRITE_TOOLS),
                                         init("connected") + "\n" + result("12 workloads")]))
    assert state["statut"] == "connecté" and "12 workloads" in state["message"]
    assert sorted(state["outils"]) == sorted(READ_TOOLS + WRITE_TOOLS)
    second = calls[-1]
    assert "--strict-mcp-config" in second and set(WRITE_TOOLS) <= set(second[second.index("--disallowedTools") + 1:])
    assert not set(WRITE_TOOLS) & set(second[second.index("--allowedTools") + 1:second.index("--disallowedTools")])
    expired = ms.test_connection(run=fake([init("connected", READ_TOOLS),
                                           result("HTTP 401 Unauthorized: token expired", error=True)]))
    assert expired["statut"] == "expiré"
    assert ms.test_connection(run=fake([init("failed")]))["statut"] == "erreur"
    monkeypatch.setenv(ms.URL_ENV, "")
    assert ms.test_connection(run=fake([]))["statut"] == "non configuré"
    assert ms.status()["statut"] == "non configuré"
    # Aucun jeton dans l'état enregistré
    with open(ms.state_path(), encoding="utf-8") as f:
        assert "Bearer" not in f.read()


def test_login_command_uses_agent_config(mcp_dir, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/.claude-pro")
    cmd = ms.login_command()
    assert cmd[1:3] == ["--strict-mcp-config", "--mcp-config"] and cmd[3] == ms.config_path()
    env = ms.agent_env()
    assert env["CLAUDE_CONFIG_DIR"] == os.path.expanduser("~/.claude-pro")
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"


# ---------------------------------------------------------------------------
# Fenêtre des échecs, namespace / conteneur / pod
# ---------------------------------------------------------------------------

def test_pod_read_from_jenkins_console():
    with open(CONSOLE, encoding="utf-8") as f:
        pod = ms.pod_info(f.read())
    assert pod["namespace"] == "jenkins" and pod["pod"] == "cucumber-customer-241-4kb2c-wpvnh-895n7"
    assert "local-appli" in pod["conteneurs"] and "jnlp" not in pod["conteneurs"]


def test_failure_window_is_step_time_plus_minus_30_seconds(mcp_dir, monkeypatch):
    enable(monkeypatch)
    failures = [{"id": "a.feature::A", "step": "Then ok", "heure": "2026-10-30T13:40:00"},
                {"fichier": "b.feature", "titre": "B", "step": "When x", "heure": "2026-10-30T13:45:10.500Z"},
                {"id": "c.feature::C", "step": "Given y", "heure": ""}]
    assert ms.windows(failures) == [
        {"id": "a.feature::A", "step": "Then ok", "debut": "2026-10-30T13:39:30Z", "fin": "2026-10-30T13:40:30Z"},
        {"id": "b.feature::B", "step": "When x", "debut": "2026-10-30T13:44:40Z", "fin": "2026-10-30T13:45:40Z"}]
    values = ms.prompt_values(failures, "", "local-appli", {"namespace": "jenkins", "pod": "p-1",
                                                            "conteneurs": ["local-appli"]}, "https://j/b/241/")
    assert "namespace « jenkins »" in values["application"] and "conteneur « local-appli »" in values["application"]
    assert "pod « p-1 »" in values["application"] and "13:39:30Z" in values["fenetres"]
    assert values["outils"] == ", ".join(sorted(READ_TOOLS))


def test_diagnostic_prompt_block(main_module, monkeypatch, mcp_dir):
    assert main_module.build_mcp_logs_block("cucumber-import-fix", "", [{"id": "x", "heure": "2026-10-30T13:40:00"}]) == ""
    enable(monkeypatch)
    block = main_module.build_mcp_logs_block("cucumber-init-fix", "", [{"id": "x", "step": "Given z",
                                                                        "heure": "2026-10-30T13:40:00"}],
                                             pod={"namespace": "jenkins", "pod": "p-1", "conteneurs": ["app"]})
    assert "13:39:30Z → 2026-10-30T13:40:30Z" in block and "GROUNDCOVER_ACCES=echec" in block
    assert "namespace « jenkins »" in block and "« app »" in block
    assert main_module.build_mcp_logs_block("cucumber-lot", "", [{"id": "x", "heure": "2026-10-30T13:40:00"}]) == ""
