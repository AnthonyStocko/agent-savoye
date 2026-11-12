"""
Clear de l'init en échec reproductible (« Batch update returned unexpected row count ») : plus de relance après
UNE relance en échec, diagnostic de la base en lecture seule, conclusion « environnement corrompu » avec preuve et
action humaine, init bloquée ; prévention : aucune copie de base vers un schéma du pool des runs Jenkins.
"""
import importlib
import io
import sys

import pytest

from tools import cucumber_env_diagnosis as env, cucumber_incidents, cucumber_tools, db_restore

CLIENT = "acme"
MESSAGE = ("java.lang.RuntimeException: 500 - Batch update returned unexpected row count from update 0 (expected row "
           "count 1 but was 0) [delete from cucumber_customer_1.wms_device_identifier where "
           "wms_device_identifier_id=? and version=?]")
STEP = "INIT- Purge de l'environnement par le clear \"wms/all\""
BUILD = "https://jenkins.example/job/CUCUMBER-CUSTOMER/{}/"


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cucumber_tools, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.delenv("CUCUMBER_REFERENCE_SCHEMA", raising=False)
    monkeypatch.delenv("CUCUMBER_POOL_SCHEMA_PATTERN", raising=False)
    monkeypatch.delenv("CUCUMBER_LOCAL_SCHEMA", raising=False)


def failed_run(number: int) -> dict:
    return {"ok": False, "build": BUILD.format(number), "message": "init en échec",
            "echecs": [{"step": STEP, "message": MESSAGE}]}


# ---------------------------------------------------------------------------
# Lecture de l'erreur
# ---------------------------------------------------------------------------

def test_table_and_schema_come_from_the_sql_statement_not_the_schema_name():
    assert cucumber_incidents.lock_table(MESSAGE) == "wms_device_identifier"
    assert cucumber_incidents.schema_in_error(MESSAGE) == "cucumber_customer_1"
    assert env.statement_target(MESSAGE) == ("cucumber_customer_1", "wms_device_identifier")
    # « Batch update returned » n'est pas un ordre SQL
    assert env.statement_target("Batch update returned unexpected row count") == ("", "")
    assert env.is_clear_lock_failure(STEP, MESSAGE)
    assert not env.is_clear_lock_failure("Quand je valide la commande", "expected:<A> but was:<B>")


# ---------------------------------------------------------------------------
# Une relance, pas deux
# ---------------------------------------------------------------------------

def test_failure_already_retried_at_previous_build_is_not_relaunched():
    runs = iter([failed_run(369), failed_run(370)])
    launched, diagnosed = [], []

    def run_fn():
        launched.append(1)
        return next(runs)
    out = cucumber_incidents.run_with_lock_retry(
        run_fn, lambda run: {"message": "aucun autre build"}, log=lambda *_: None,
        already_retried=lambda run: True, diagnosis_fn=lambda run, builds: diagnosed.append(builds) or {"x": 1})
    assert len(launched) == 1 and out["reproductible"] and out["relance"] == 0
    assert diagnosed == [[BUILD.format(369)]] and out["environnement"] == {"x": 1}


def test_first_failure_is_retried_once_then_diagnosed():
    runs = iter([failed_run(366), failed_run(369)])
    diagnosed = []
    out = cucumber_incidents.run_with_lock_retry(
        lambda: next(runs), lambda run: {"message": "-"}, log=lambda *_: None, already_retried=lambda run: False,
        diagnosis_fn=lambda run, builds: diagnosed.append(builds) or {})
    assert out["relance"] == 1 and diagnosed == [[BUILD.format(366), BUILD.format(369)]]
    assert "environnement" not in out    # diagnostic vide : rien à ajouter


def test_consecutive_clear_failures_are_counted_per_schema():
    assert env.record_clear_failure(CLIENT, BUILD.format(363), "cucumber_customer_1", "wms_device_identifier") == 1
    assert env.record_clear_failure(CLIENT, BUILD.format(363), "cucumber_customer_1", "wms_device_identifier") == 1
    assert env.record_clear_failure(CLIENT, BUILD.format(369), "cucumber_customer_1", "wms_device_identifier") == 2
    env.reset_failures(CLIENT)
    assert env.clear_failures(CLIENT) == []


# ---------------------------------------------------------------------------
# Diagnostic de la base (lecture seule)
# ---------------------------------------------------------------------------

def fake_query(schemas: dict, triggers: dict = None):
    """`schemas` : {schéma: [(nom, source, cible, confdeltype)]} ; seules des requêtes SELECT sont reçues."""
    def query(sql, params):
        assert sql.lstrip().upper().startswith("SELECT")
        if "information_schema.tables" in sql:
            return [{"schema": s} for s in sorted(schemas)]
        if "pg_trigger" in sql:
            return [{"nom": n, "definition": ""} for n in (triggers or {}).get(params["schema"], [])]
        return [{"nom": n, "table_source": src, "table_cible": dst, "suppression": rule, "definition": ""}
                for n, src, dst, rule in schemas.get(params["schema"], [])
                if params["table"] in (src, dst)]
    return query


FK = ("fk_device_identifier_device", "wms_device_identifier", "wms_device", "a")
FK_CASCADE = ("fk_device_identifier_device", "wms_device_identifier", "wms_device", "c")


def test_structural_gap_with_another_slot_and_copy_without_backup():
    history = [{"date": "2026-11-10T13:23:04", "source": "u@h:5432/db / source_client",
                "cible": "u@h:5432/db_cucumber / cucumber_customer_1", "ok": True, "sauvegarde": ""}]
    diag = env.diagnose(CLIENT, "cucumber_customer_1", "wms_device_identifier", ["363", "369"], history,
                        fake_query({"cucumber_customer_1": [FK_CASCADE], "cucumber_customer_2": [FK]},
                                   {"cucumber_customer_1": ["trg_purge_device"]}))
    assert diag["verdict"] == "corrompu" and diag["reference"]["schema"] == "cucumber_customer_2"
    assert any("ON DELETE CASCADE ici, NO ACTION" in g for g in diag["ecarts"])
    assert any("déclencheur trg_purge_device" in g for g in diag["ecarts"])
    assert diag["copie_en_cause"]["date"] == "2026-11-10T13:23:04"
    text = " ".join(diag["preuves"])
    assert "build 363, build 369" in text and "sans sauvegarde" in text and "suppression en cascade" in text
    assert diag["action"].startswith("Demander la réinitialisation du schéma cucumber_customer_1")
    assert diag["conclusion"] == ("Init impossible : environnement corrompu (structure ou données du schéma) : "
                                  "l'init ne peut pas réussir (cucumber_customer_1)")


def test_same_structure_points_at_data_and_copy_backup_is_the_action():
    history = [{"date": "2026-11-10T13:23:04", "source": "x", "cible": "h / cucumber_customer_1", "ok": True,
                "sauvegarde": "C:/sauvegardes/sauvegarde-cucumber_customer_1-20261110.pgdump"}]
    diag = env.diagnose(CLIENT, "cucumber_customer_1", "wms_device_identifier", ["369"], history,
                        fake_query({"cucumber_customer_1": [FK], "cucumber_customer_3": [FK]}))
    assert diag["ecarts"] == [] and any("DONNÉES du schéma sont en cause" in p for p in diag["preuves"])
    assert diag["action"].startswith("Restaurer la sauvegarde sauvegarde-cucumber_customer_1-20261110.pgdump")


def test_unreachable_database_still_concludes_with_the_reproducible_failure():
    def broken(sql, params):
        raise ConnectionError("base injoignable")
    diag = env.diagnose(CLIENT, "cucumber_customer_1", "wms_device_identifier", ["363", "369"], [], broken)
    assert diag["verdict"] == "corrompu" and "ConnectionError" in diag["erreur_base"]
    assert any("échec reproductible" in p for p in diag["preuves"])
    assert "réinitialisation du schéma cucumber_customer_1" in diag["action"]


def test_reference_schema_from_configuration(monkeypatch):
    monkeypatch.setenv("CUCUMBER_REFERENCE_SCHEMA", "reference_26_24")
    query = fake_query({"cucumber_customer_1": [FK], "cucumber_customer_2": [FK], "reference_26_24": [FK_CASCADE]})
    assert env.reference_schema("cucumber_customer_1", "wms_device_identifier", query) == "reference_26_24"


# ---------------------------------------------------------------------------
# Blocage de l'init
# ---------------------------------------------------------------------------

def test_block_until_backup_restored_or_explicit_unblock():
    diag = env.diagnose(CLIENT, "cucumber_customer_1", "wms_device_identifier", ["369"], [], fake_query({}))
    env.block(CLIENT, diag)
    assert env.blocked(CLIENT, [])["schema"] == "cucumber_customer_1"
    restored = [{"date": "2999-01-01T00:00:00", "cible": "h / cucumber_customer_1", "ok": True, "retour": True}]
    assert env.blocked(CLIENT, restored) == {}           # sauvegarde restaurée après le diagnostic : débloqué
    env.block(CLIENT, diag)
    env.unblock(CLIENT, "schéma réinitialisé")
    assert env.blocked(CLIENT, []) == {} and env.load(CLIENT)["deblocages"][-1]["raison"] == "schéma réinitialisé"


@pytest.fixture
def main_module(monkeypatch):
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        main = importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved
    return main


def test_blocked_environment_launches_no_init(main_module, monkeypatch):
    env.block(CLIENT, env.diagnose(CLIENT, "cucumber_customer_1", "wms_device_identifier", ["363", "369"], [],
                                   fake_query({})))
    monkeypatch.setattr(main_module.cucumber_verify, "run_init",
                        lambda *a, **k: pytest.fail("aucune init ne doit être lancée"))
    out = main_module.run_init_and_import(CLIENT, {"verify_branch": "agent"}, "", "repo", "branche")
    assert out["bloque"] and not out["ok"] and out["environnement"]["schema"] == "cucumber_customer_1"


def test_reproducible_clear_failure_is_diagnosed_without_second_relaunch(main_module, monkeypatch):
    env.record_clear_failure(CLIENT, BUILD.format(363), "cucumber_customer_1", "wms_device_identifier")
    launched = []
    monkeypatch.setattr(main_module.cucumber_tools, "rev_parse", lambda *a: "abc12345")
    monkeypatch.setattr(main_module.cucumber_verify, "branch_sources", lambda *a: {})
    monkeypatch.setattr(main_module.cucumber_verify, "run_init",
                        lambda *a, **k: launched.append(1) or failed_run(369))
    monkeypatch.setattr(main_module.cucumber_incidents, "check_concurrency",
                        lambda *a, **k: {"message": "aucun autre build", "schema": "cucumber_customer_1"})
    monkeypatch.setattr(main_module.cucumber_import, "build_files", lambda build: {})
    monkeypatch.setattr(main_module.cucumber_import, "import_files", lambda *a, **k: {"numero": 0})
    monkeypatch.setattr(main_module.cucumber_incidents, "observe",
                        lambda *a, **k: {"incident": {}, "enregistres": []})
    monkeypatch.setattr(main_module, "purge_server_logs", lambda *a, **k: "non lus (test)")
    monkeypatch.setattr(main_module.cucumber_env_diagnosis, "default_query", fake_query({}))
    monkeypatch.setattr(main_module.db_restore, "load_history", lambda client: [])
    out = main_module.run_init_and_import(CLIENT, {"verify_branch": "agent"}, "", "repo", "branche")
    assert launched == [1]                                  # le build 369 seul : pas de nouvelle relance
    diag = out["environnement"]
    assert diag["builds"] == [BUILD.format(363), BUILD.format(369)] and env.blocked(CLIENT, [])
    line = main_module.simple_init_environment_line("init du client", diag)
    assert line.startswith("Init impossible : environnement (init du client) — environnement corrompu")
    assert "Action : Demander la réinitialisation du schéma cucumber_customer_1" in line


# ---------------------------------------------------------------------------
# Prévention : la copie de base n'écrit jamais dans un schéma des runs Jenkins
# ---------------------------------------------------------------------------

def test_copy_refuses_pool_schema_and_uses_the_local_schema():
    conn = {"host": "h", "port": "5432", "name": "db", "user": "u", "type": "postgresql"}
    errors = db_restore.check_plan("source_client", "cucumber_customer_1", conn, {**conn, "name": "tests"})
    assert any("utilisé par les runs Jenkins" in e for e in errors)
    assert env.local_schema("Canson-Test") == "cucumber_local_canson_test"
    assert db_restore.check_plan("source_client", env.local_schema(CLIENT), conn, {**conn, "name": "tests"}) == []
    assert env.is_pool_schema("cucumber_customer_12") and not env.is_pool_schema("cucumber_local_acme")
