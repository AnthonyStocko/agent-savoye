"""
Import d'une console Jenkins horodatée sans rapport Cucumber JSON (client fictif) : la console est reconnue comme
telle (pas comme un export de logs serveur), ses scénarios et ses échecs sont lus, et un test de l'agent en échec
reçoit une correction proposée, signalée dans le bandeau « À faire ».
"""
import os

import pytest
from streamlit.testing.v1 import AppTest

from tests.conftest import CLIENT, CLIENT_TAG
from tools import cucumber_detection as cd
from tools import cucumber_import as ci
from tools import cucumber_profile as cp
from tools import cucumber_tools as ct

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEATURE = f"src/test/resources/cucumber/features/{CLIENT}/WMS/Retours.feature"
OK, KO = "Livraison normale : l'UM reste à contrôler", "Retour : l'UM est validée automatiquement"
STAMP = "[2026-10-27T14:15:45.596Z] "
CONSOLE = "\n".join(STAMP + line for line in [
    "Launching Cucumber...",
    "+ mvn --no-transfer-progress test -Dcucumber.filter.tags='" + CLIENT_TAG + "' -Dpassword='****'",
    "[INFO] Scanning for projects...",
    f"Scenario: {OK}   # {FEATURE}:10",
    "  Given INIT- un contexte",
    f"Scenario: {KO}   # {FEATURE}:20",
    "  Then CONTENT- <X> unités de manutention répondant aux caractéristiques",
    "[ERROR] ****s run: 2, Failures: 1, Errors: 0, Skipped: 0 <<< FAILURE! -- in cucumber.Runner",
    f"[ERROR] Retours fictifs.{KO} -- Time elapsed: 3.2 s <<< FAILURE!",
    "java.lang.AssertionError: Il n'existe pas 1 enregistrement(s) pour l'élément {code=UM-FIC-1, status=VALIDATED}",
    "\tat cucumber.features.BaseStep.checkNumberRecords(BaseStep.java:266)",
    "\tat ✽.CONTENT- <X> unités de manutention répondant aux caractéristiques(file:///home/jenkins/workspace/spec/"
    f"{FEATURE}:24)",
    "",
    "[INFO] Results:",
    "[INFO] BUILD FAILURE"])


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path / "ws"))
    os.makedirs(tmp_path / "ws")
    monkeypatch.setattr(cd, "start", lambda *args, **kwargs: False)
    monkeypatch.setattr(cd, "running", lambda *args, **kwargs: False)
    repo = str(tmp_path / "client")
    os.makedirs(repo)
    cd.save_project(CLIENT, {"client_repo": repo, "client_name": CLIENT, "db_schema": "", "application": "",
                             "std_repo": repo})
    ct.save_inventory(CLIENT, {"genere_le": "2026-10-27T10:00:00", "specifiques": [
        {"id": "sp-r", "libelle": "app · retours", "type": "extension", "nature": "objet métier", "depot": "app",
         "dossier": "retours", "nb_fichiers": 1, "mots_cles": [], "couverture": "non_couvert",
         "fichiers": [{"repo": "app", "path": "Retours.java", "blob": "b"}]}]})
    cp.save_user_fields(CLIENT, {"tag_client": CLIENT_TAG, "features_root": FEATURE.rsplit("WMS/", 1)[0]})
    # Le scénario en échec est un test écrit par l'agent (tags du lot, indicateurs)
    ct.save_state(CLIENT, {"client": CLIENT, "base": "base", "lots": [
        {"numero": 1, "ids": ["sp-r"], "statut": "termine", "tags": {f"@agent_{CLIENT}_1_1": f"{FEATURE}::{KO}"},
         "indicateurs": {"scenarios": [{"fichier": FEATURE, "titre": KO, "spec": "sp-r"}]},
         "resultats": [{"id": "sp-r", "statut": "couvert",
                        "scenarios": [{"nom": f"Retours / {KO}", "fichier": FEATURE}]}]}]})


def test_timestamped_console_is_a_console_not_server_logs():
    assert ci.file_kind("log (78).txt", CONSOLE) == "console"
    server_log = "\n".join(f"2026-10-27 14:15:{n:02d}.000 INFO [main] message {n}" for n in range(30))
    assert ci.file_kind("export.txt", server_log) == "logs"


def test_console_results_without_json_report():
    results = ci.console_results(CONSOLE)
    assert results[(FEATURE, OK)]["statut"] == "passe"
    failed = results[(FEATURE, KO)]
    assert failed["statut"] == "echoue" and failed["nature"] == "assertion"
    assert failed["step"] == "CONTENT- <X> unités de manutention répondant aux caractéristiques"
    assert failed["message"].startswith("java.lang.AssertionError: Il n'existe pas 1 enregistrement(s)")


def test_failed_agent_test_gets_a_correction_from_the_console(project):
    imported = ci.import_files(CLIENT, {"log (78).txt": CONSOLE.encode("utf-8")})
    assert ci.load_index(CLIENT)["imports"][0]["fichiers"][0]["type"] == "console"
    analysis = ci.analyse(CLIENT, imported["numero"], ct.load_state(CLIENT))
    assert analysis["non_analyse"] == "" and analysis["source"] == "console" and analysis["tests"] == 2
    assert [(p["spec"], [s["titre"] for s in p["scenarios"]]) for p in analysis["propositions"]] == [("sp-r", [KO])]
    # Bandeau « À faire » : la correction est proposée hors du mode expert
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=120)
    at.session_state[f"cucumber_section_detail_{CLIENT}"] = True  # détail du projet ouvert
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("1 test en échec au dernier résultat importé peut être corrigé automatiquement" in m.value
               for m in at.markdown)
    assert [b for b in at.button if b.label == "Corriger"]


def test_console_saved_with_doubled_line_breaks_keeps_every_failure_under_its_feature(tmp_path):
    # Deux features de même nom (#1, #2) aux scénarios de même titre ; console enregistrée en « \r\r\n » : chaque ligne
    # de la trace est suivie d'une ligne vide, la ligne « at ✽ » qui situe l'échec est au-delà de 40 lignes brutes
    tms, wms = (f"src/test/resources/cucumber/features/{CLIENT}/{d}/Init.feature" for d in ("TMS", "WMS"))
    trace = "".join(f"\tat org.example.Frame{i}.call(Frame.java:{i})\n" for i in range(25))
    out = [f"Scenario: Intégration des données # {tms}:4", f"Scenario: Intégration des données # {wms}:4",
           f"Scenario: Règles # {wms}:80"]
    for number, path, title, kind, error in ((1, tms, "Intégration des données", "FAILURE", "java.lang.AssertionError: x"),
                                              (2, wms, "Intégration des données", "FAILURE", "java.lang.AssertionError: y"),
                                              (2, wms, "Règles", "ERROR", "java.lang.NullPointerException")):
        out += [f"[ERROR] Init #{number}.{title} -- Time elapsed: 1 s <<< {kind}!", error, trace.rstrip("\n"),
                f"\tat ✽.KIS- Step(file:///w/spec/{path}:{6 if title != 'Règles' else 87})", ""]
    out += ["[ERROR] Tests run: 3, Failures: 2, Errors: 1, Skipped: 0", "[INFO]"]
    text = "\r\r\n".join(STAMP + line for line in "\n".join(out).split("\n"))
    with open(tmp_path / "console.txt", "w", encoding="utf-8", newline="") as f:
        f.write(text)
    with open(tmp_path / "console.txt", encoding="utf-8") as f:
        results = ci.console_results(f.read())
    failed = {k: r["exception"] for k, r in results.items() if r["statut"] == "echoue"}
    assert failed == {(tms, "Intégration des données"): "AssertionError", (wms, "Intégration des données"): "AssertionError",
                      (wms, "Règles"): "NullPointerException"}
