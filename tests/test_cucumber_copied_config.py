"""
Voie A — « configuration copiée, sans purge » (option du profil client, désactivée par défaut), client fictif :
option inactive → comportement inchangé ; option active → init sans clear ni init globale, copie de référence
restaurée avant chaque run, run arrêté s'il utilise un autre schéma, référence invalidée au changement de version.
"""
import importlib
import io
import os
import subprocess
import sys

import pytest

from tools import (cucumber_copied_config as cc, cucumber_profile, cucumber_simple, cucumber_test_data, cucumber_tools,
                   db_restore, jenkins_tools)

CLIENT = "acme"
SCHEMA = "cucumber_customer_1"
VERSION = {"deduction": {"version_client": "2.0", "standard": {"version": "26.24"}, "kis": {"kis": "4.0"}}}
INIT = """@INIT_ACME_WMS @ACME
Feature: Acme - 01 - Initialisation des données

  Scenario: Intégration des données
    Given INIT- Purge de l'environnement par le clear "wms/all"
    And KIS- Création du job
      | name       | type            |
      | JOB_CREE   | a.b.CreatedJob  |
    And KIS- Désactivation du job
      | name       |
      | JOB_CREE   |
      | JOB_CLIENT |
    And KIS- Activation du toggle "TOGGLE_X"
    And KIS- Désactivation de tous les jobs
    And INIT- Initialisation globale par fichiers du Standard "WMS"

  Scenario: Acme - Contrôle de l'environnement (lecture seule)
    Then INIT- Acme contrôle de l'environnement
      | type | entite | code |
      | champ_personnalise | WmsGoodsInFolder | customString1 |
"""


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cucumber_tools, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("CUCUMBER_DB_SCHEMA", SCHEMA)
    monkeypatch.delenv("CUCUMBER_POOL_SCHEMA_PATTERN", raising=False)
    monkeypatch.delenv("CUCUMBER_COPY_REFERENCE_TABLES", raising=False)


def write_document(document: dict) -> None:
    cucumber_tools._write_json(cucumber_profile.profile_path(CLIENT), document)


def copied_profile(tmp_path, version=VERSION, reference_version="client 2.0 / Standard 26.24 / KIS 4.0") -> dict:
    dump = tmp_path / "reference.pgdump"
    dump.write_text("dump")
    write_document({"saisi": {cc.OPTION: True}, "version_client": version,
                    cc.REFERENCE_KEY: {"date": "2026-11-12T10:00:00", "source": "u@h/db / client_schema",
                                       "schema": SCHEMA, "version": reference_version, "dump": str(dump),
                                       "codes": {"sites": {"codes": ["SITE_REEL"], "total": 1}}}})
    return cucumber_profile.effective_profile(cucumber_profile.load_profile(CLIENT))


# ---------------------------------------------------------------------------
# Option inactive : comportement actuel inchangé
# ---------------------------------------------------------------------------

def test_inactive_option_changes_nothing():
    write_document({})
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(CLIENT))
    assert not cc.active(profile) and profile[cc.OPTION] is False
    assert cc.run_guard(CLIENT, profile) is None
    assert cc.prompt_block(profile) == "" and "CONFIGURATION COPIÉE" not in cucumber_test_data.prompt_block(profile)
    steps = cucumber_simple.states({"etat": "ok", "resultat": "ok"}, {"depot": {"etat": "ok"}})
    base = next(s for s in steps if s["cle"] == "base")
    assert base["optionnelle"] and base["resultat"] == cucumber_simple.OPTIONAL_NOTE
    # Copie de base : jamais dans un schéma du pool des runs sans l'option
    source = target = {"type": "postgresql", "name": "db", "user": "u", "host": "h", "port": "5432"}
    assert any("pool" in e for e in db_restore.check_plan("client_schema", SCHEMA, source, {**target, "name": "t"}))


def test_user_profile_edit_keeps_the_option():
    cc.save_option(CLIENT, True, "cucumber_customer_3")
    cucumber_profile.save_user_fields(CLIENT, {"tag_client": "@ACME"})
    saisi = cucumber_profile.load_profile(CLIENT)["saisi"]
    assert saisi[cc.OPTION] is True and saisi[cc.SCHEMA_FIELD] == "cucumber_customer_3"
    assert saisi["tag_client"] == "@ACME"


# ---------------------------------------------------------------------------
# Option active : copie de référence, étape 3 obligatoire
# ---------------------------------------------------------------------------

def test_copy_targets_only_the_profile_test_schema():
    source = {"type": "postgresql", "name": "db", "user": "u", "host": "h", "port": "5432"}
    target = {**source, "name": "tests"}
    assert db_restore.check_plan("client_schema", SCHEMA, source, target, allowed_pool=SCHEMA) == []
    assert any("pool" in e for e in db_restore.check_plan("client_schema", "cucumber_customer_2", source, target,
                                                          allowed_pool=SCHEMA))


def test_step_3_is_mandatory_and_blocks_step_4_without_reference(tmp_path):
    write_document({"saisi": {cc.OPTION: True}, "version_client": VERSION})
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(CLIENT))
    problem = cc.reference_problem(profile)
    assert "aucune copie de référence" in problem
    records = {"depot": {"etat": "ok"}, "base": {"etat": "ok", "resultat": "ancienne copie"}}
    steps = cucumber_simple.states({"etat": "ok", "resultat": "ok"}, records, {"actif": True, "probleme": problem})
    base, init = (next(s for s in steps if s["cle"] == k) for k in ("base", "init"))
    assert not base["optionnelle"] and base["etat"] == "a_faire" and problem in base["resultat"]
    assert init["etat"] == "bloquee"


def test_valid_reference_unblocks_and_feeds_prompts_with_copied_codes(tmp_path):
    profile = copied_profile(tmp_path)
    assert cc.reference_problem(profile) == ""
    steps = cucumber_simple.states({"etat": "ok", "resultat": "ok"}, {"depot": {"etat": "ok"}, "base": {"etat": "ok"}},
                                   {"actif": True, "probleme": ""})
    assert next(s for s in steps if s["cle"] == "init")["etat"] != "bloquee"
    block = cucumber_profile.profile_block(profile)
    assert "CONFIGURATION COPIÉE" in block and "SITE_REEL" in block and "Données créées par l'init" not in block
    assert "SITE_REEL" in cucumber_test_data.prompt_block(profile)


def test_reference_codes_are_read_from_the_copy():
    def query(sql, schema):
        assert schema == SCHEMA and sql.startswith("SELECT")
        if "ref_carrier" in sql:
            raise RuntimeError("relation absente")
        return [{"code": "B"}, {"code": "A"}] if "ref_site" in sql else []
    codes = cc.read_codes(SCHEMA, query)
    assert codes["sites"] == {"codes": ["B", "A"], "total": 2}
    assert codes["transporteurs"]["erreur"] and codes["secteurs"]["total"] == 0


# ---------------------------------------------------------------------------
# Init sans clear ni initialisation globale
# ---------------------------------------------------------------------------

def test_copied_init_keeps_only_job_deactivations_and_read_only_checks():
    text, removed = cc.copied_feature(INIT, CLIENT)
    active = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert not any("Purge" in l or "Initialisation globale" in l or "Création du job" in l or "toggle" in l
                   for l in active)
    assert "And KIS- Désactivation de tous les jobs" in active
    assert "And KIS- Désactivation du job" in active and "| JOB_CLIENT |" in active
    # Job que l'init créait : sa création est retirée, sa désactivation aussi
    assert "| JOB_CREE   |" not in active
    assert "Then INIT- Acme contrôle de l'environnement" in active
    assert removed == 4 and text.count(cc.MARKER) == 4


def test_copied_init_keeps_the_test_environment_connections():
    """Colisage, prestataire transport, partenaire de messages : adresses du job Jenkins, jamais celles du client."""
    init = INIT.replace("    And KIS- Désactivation de tous les jobs\n",
                        "    And KIS- Désactivation de tous les jobs\n"
                        '    Given MESS- Configuration du partenaire "WMS_TMS_LOCAL"\n'
                        "    Given INIT- Configuration du colisage\n"
                        '    Given INIT- Configuration du prestataire transport "TDM"\n'
                        '    Given MESS- Configuration du partenaire  "WMS_TMS_LOCAL"\n'
                        "      | url  |\n"
                        "      | http://ailleurs |\n")
    text, removed = cc.copied_feature(init, CLIENT)
    active = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert 'Given MESS- Configuration du partenaire "WMS_TMS_LOCAL"' in active
    assert "Given INIT- Configuration du colisage" in active
    assert 'Given INIT- Configuration du prestataire transport "TDM"' in active
    # Variante à table : modification libre du partenaire, pas un branchement → commentée
    assert 'Given MESS- Configuration du partenaire  "WMS_TMS_LOCAL"' not in active
    assert "| http://ailleurs |" not in active
    assert removed == 5


def test_already_reduced_init_gets_its_connections_back():
    """Init réduite et poussée par une version précédente : l'étape 4 réactive les branchements, rien d'autre."""
    pushed = ("  Scenario: Intégration des données\n"
              "    And KIS- Désactivation de tous les jobs\n"
              f"    # {cc.MARKER} configuration copiée du client (aucune purge ni initialisation)\n"
              '    # Given MESS- Configuration du partenaire "WMS_TMS_LOCAL"\n'
              f"    # {cc.MARKER} configuration copiée du client (aucune purge ni initialisation)\n"
              "    # Given INIT- Configuration du colisage\n"
              f"    # {cc.MARKER} configuration copiée du client (aucune purge ni initialisation)\n"
              "    # And INIT- Configuration du prestataire transport \"TDM\"\n"
              f"    # {cc.MARKER} configuration copiée du client (aucune purge ni initialisation)\n"
              "    # And KIS- Création du job\n"
              "    # | name | type |\n"
              "    # | J1   | a.B  |\n")
    text, removed = cc.copied_feature(pushed, CLIENT)
    active = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert active == ["Scenario: Intégration des données", "And KIS- Désactivation de tous les jobs",
                      'Given MESS- Configuration du partenaire "WMS_TMS_LOCAL"',
                      "Given INIT- Configuration du colisage", 'And INIT- Configuration du prestataire transport "TDM"']
    assert removed == 0 and cc.restored_connections(pushed, text) == 3
    assert "    # And KIS- Création du job\n    # | name | type |\n" in text
    assert cc.copied_feature(text, CLIENT) == (text, 0)


def test_connection_without_its_data_in_the_copy_stays_commented():
    """Partenaire WMS_TMS_LOCAL et prestataire TDM créés par l'init globale du Standard : absents de la copie."""
    queries = []

    def query(sql, schema):
        queries.append((sql, schema))
        if "table_absente" in sql or "tms_provider_gen_exc" in sql:
            raise RuntimeError('(psycopg2.errors.UndefinedTable) relation "x" does not exist')
        return [{"code": "CUBING"}] if "CUBING" in sql else []

    available = cc.connection_checker(SCHEMA, query)
    pushed = ("  Scenario: Intégration des données\n"
              '    Given MESS- Configuration du partenaire "WMS_TMS_LOCAL"\n'
              "    Given INIT- Configuration du colisage\n"
              f"    # {cc.MARKER} configuration copiée du client (aucune purge ni initialisation)\n"
              '    # Given INIT- Configuration du prestataire transport "TDM"\n')
    text, removed = cc.copied_feature(pushed, CLIENT, available)
    active = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert active == ["Scenario: Intégration des données", "Given INIT- Configuration du colisage"]
    assert removed == 1 and cc.copied_feature(text, CLIENT, available) == (text, 0)
    assert all(schema == SCHEMA for _, schema in queries)
    assert "WHERE code = 'WMS_TMS_LOCAL'" in " ".join(sql for sql, _ in queries)
    # Base injoignable : le step est gardé (l'échec de l'init le montrera)
    down = cc.connection_checker(SCHEMA, lambda sql, schema: (_ for _ in ()).throw(RuntimeError("timeout")))
    assert down("INIT- Configuration du colisage")


def import_main():
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        return importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved


def test_simple_init_plan_reduces_every_init_feature(tmp_path):
    main = import_main()
    rel = "src/test/resources/cucumber/features/acme/01-Contexte/WMS/01-Initialisation.feature"
    plan = main.copied_init_plan(CLIENT, str(tmp_path), {"present": {}, "complements": {}, "changes": {rel: INIT},
                                                          "binaries": {"x.xlsx": "src"}, "standard": {"WMS": {}}})
    assert plan["binaries"] == {} and plan["standard"] == {}
    assert cc.MARKER in plan["changes"][rel] and "configuration copiée" in " ".join(plan["remarks"])


# ---------------------------------------------------------------------------
# Avant chaque run : restauration, schéma du run vérifié
# ---------------------------------------------------------------------------

def guard_for(profile, consoles, events):
    def restore(client, dump, schema, log):
        events.append(("restauration", schema))
        return {"ok": True, "date": "2026-11-12T11:00:00", "duree_s": 3}
    return cc.RunGuard(CLIENT, profile, lambda m: None, restore=restore, console=lambda url: consoles.pop(0),
                       stop=lambda url: events.append(("arret", url)), wait_s=0, poll_s=0)


def run_exclusive(monkeypatch, guard, events):
    monkeypatch.setattr(jenkins_tools, "_acquire_local_lock", lambda t: True)
    monkeypatch.setattr(jenkins_tools, "wait_until_idle", lambda jobs, t: events.append(("libre", None)) or True)
    monkeypatch.setattr(jenkins_tools, "wait_build_started", lambda q, t, job_name="": "https://j/job/X/7/")
    monkeypatch.setattr(jenkins_tools, "wait_build_result", lambda q, t, job_name="": {
        "url": "https://j/job/X/7/", "result": "SUCCESS", "artifacts": []})
    return jenkins_tools.run_exclusive("X", lambda: events.append(("lancement", None)) or "https://j/queue/item/1",
                                       60, guard)


def test_reference_restored_before_each_run_and_schema_checked(tmp_path, monkeypatch):
    events = []
    guard = guard_for(copied_profile(tmp_path), ["Lock acquired\nBuild running on slot 1\n"], events)
    build = run_exclusive(monkeypatch, guard, events)
    assert build["result"] == "SUCCESS"
    assert [e[0] for e in events][:3] == ["libre", "restauration", "lancement"] and ("restauration", SCHEMA) in events
    assert guard.info["schema_verifie"] == SCHEMA and guard.info["restauration"]
    line = cc.bilan_line(guard.info)
    assert "configuration copiée" in line and "2026-11-12 10:00" in line and SCHEMA in line
    assert "restauration faite" in line


def test_run_on_another_schema_is_stopped(tmp_path, monkeypatch):
    events = []
    guard = guard_for(copied_profile(tmp_path), ["Build running on slot 2\n"], events)
    build = run_exclusive(monkeypatch, guard, events)
    assert build["schema_errone"] and build["result"] == "ABORTED"
    assert build["message"] == "le run n'utilise pas le schéma copié (schéma cucumber_customer_2)"
    assert ("arret", "https://j/job/X/7/") in events


def test_schema_read_from_test_arguments_when_slot_line_is_missing():
    assert cc.run_schema("-Dbdd_schema='cucumber_customer_+3' -DuserName", SCHEMA)[0] == "cucumber_customer_3"
    assert cc.run_schema("rien", SCHEMA) == ("", "")


# ---------------------------------------------------------------------------
# Changement de version : référence invalidée, nouvelle copie demandée
# ---------------------------------------------------------------------------

def test_version_change_invalidates_the_reference(tmp_path, monkeypatch):
    profile = copied_profile(tmp_path, reference_version="client 1.9 / Standard 26.22 / KIS 4.0")
    assert "version de l'application changée" in cc.reference_problem(profile)
    events = []
    guard = guard_for(profile, [], events)
    build = run_exclusive(monkeypatch, guard, events)
    assert build["result"] == "INDISPONIBLE" and "nouvelle copie" in build["message"]
    assert ("lancement", None) not in events and not any(e[0] == "restauration" for e in events)
    stored = cucumber_profile.load_profile(CLIENT)[cc.REFERENCE_KEY]
    assert "version de l'application changée" in stored["invalide"]
    reloaded = cucumber_profile.effective_profile(cucumber_profile.load_profile(CLIENT))
    assert "invalidée" in cc.reference_problem(reloaded)


def test_restore_reference_refuses_missing_dump(tmp_path, monkeypatch):
    monkeypatch.setattr(db_restore, "ROOT", str(tmp_path / "restauration"))
    entry = db_restore.restore_reference(CLIENT, str(tmp_path / "absent.pgdump"), SCHEMA, lambda m: None)
    assert not entry["ok"] and "introuvable" in entry["message"]
    assert db_restore.load_history(CLIENT)[-1]["reference_restauree"]
    assert not os.path.exists(tmp_path / "absent.pgdump")


REF_LISTING = """;
; Archive created at 2026-11-13
13157; 1259 561293378 TABLE cucumber_customer_1 ref_site pgadm
13158; 1259 561293383 TABLE cucumber_customer_1 wms_preparation_order pgadm
13159; 1259 561293388 TABLE cucumber_customer_1 kis_property pgadm
;13160; 1259 561293390 TABLE cucumber_customer_1 commentee pgadm
20001; 0 561293378 TABLE DATA cucumber_customer_1 ref_site pgadm
"""


class FakeRestore:
    """pg_restore / psql simulés : `tables` successifs comptés après chaque pg_restore."""

    def __init__(self, tables):
        self.tables, self.restores = list(tables), 0

    def run(self, args, conn, log, data=None, timeout=3600):
        out = REF_LISTING if "-l" in args else ""
        if "-l" not in args and os.path.basename(args[0]).startswith("pg_restore"):
            self.restores += 1
            return subprocess.CompletedProcess(args, 1, "", "pg_restore: error: connection lost")
        return subprocess.CompletedProcess(args, 0, out, "")

    def psql(self, conn, sql, log, quiet=True):
        if "relkind" in sql:
            return subprocess.CompletedProcess([], 0, f"{self.tables[min(self.restores, len(self.tables)) - 1]}\n", "")
        if "information_schema.schemata" in sql:
            return subprocess.CompletedProcess([], 0, "1\n", "")
        return subprocess.CompletedProcess([], 0, "", "")


def fake_restore(tmp_path, monkeypatch, tables):
    fake = FakeRestore(tables)
    monkeypatch.setattr(db_restore, "ROOT", str(tmp_path / "restauration"))
    monkeypatch.setattr(db_restore, "_run", fake.run)
    monkeypatch.setattr(db_restore, "_psql", fake.psql)
    dump = tmp_path / "reference.pgdump"
    dump.write_text("dump")
    return fake, str(dump)


def test_listed_tables_counts_structure_only():
    assert db_restore.listed_tables(REF_LISTING, SCHEMA) == 3
    assert db_restore.listed_tables(REF_LISTING, "autre") == 0


def test_interrupted_reference_restore_is_retried_once(tmp_path, monkeypatch):
    fake, dump = fake_restore(tmp_path, monkeypatch, [2, 3])
    logs = []
    entry = db_restore.restore_reference(CLIENT, dump, SCHEMA, logs.append)
    assert entry["ok"] and fake.restores == 2
    assert "copie incomplète (2/3 tables" in entry["premiere_tentative"]
    assert any("nouvelle tentative" in line for line in logs)


def test_reference_restore_incomplete_twice_fails_and_blocks_the_run(tmp_path, monkeypatch):
    fake, dump = fake_restore(tmp_path, monkeypatch, [1, 1])
    entry = db_restore.restore_reference(CLIENT, dump, SCHEMA, lambda m: None)
    assert not entry["ok"] and fake.restores == 2
    assert entry["message"].startswith("copie incomplète (1/3 tables dans cucumber_customer_1")
    assert db_restore.load_history(CLIENT)[-1]["ok"] is False
    # Garde du run : restauration en échec → rien n'est lancé
    events = []
    guard = cc.RunGuard(CLIENT, copied_profile(tmp_path), lambda m: None,
                        restore=lambda client, d, schema, log: db_restore.restore_reference(client, dump, schema, log))
    assert "copie incomplète (1/3 tables" in guard.prepare() and not guard.info["restauration"]


def test_complete_restore_with_pg_restore_errors_stays_ok(tmp_path, monkeypatch):
    """Une copie complète a aussi des erreurs pg_restore (commentaires, extensions) : seules les tables comptent."""
    fake, dump = fake_restore(tmp_path, monkeypatch, [3])
    entry = db_restore.restore_reference(CLIENT, dump, SCHEMA, lambda m: None)
    assert entry["ok"] and fake.restores == 1 and entry["erreurs_restauration"] == 1
    assert "premiere_tentative" not in entry


def test_reference_copy_lifts_a_corrupted_environment_block():
    from tools import cucumber_env_diagnosis as env
    env.block(CLIENT, {"schema": SCHEMA, "table": "t", "conclusion": "corrompu", "preuves": [], "action": "a",
                       "date": "2026-11-12T15:31:32"})
    assert env.blocked(CLIENT, [])
    copy = {"date": "2026-11-12T18:00:00", "cible": f"u@h:5432/db / {SCHEMA}", "ok": True,
            "reference": "ref.pgdump"}
    assert env.blocked(CLIENT, [{**copy, "date": "2026-11-12T10:00:00"}])
    assert env.blocked(CLIENT, [copy]) == {}


def test_business_data_of_the_copy_is_refused_in_the_lot_files(tmp_path):
    """Config copiée : un OP / une UP / un produit de la copie repris par le test est une erreur de fichier."""
    rel = "src/test/resources/cucumber/features/clientx/Palettiser.feature"
    data = "src/test/resources/cucumber/files/clientx/WOI_test.xml"
    feature = ('Feature: Palettiser\n  Scenario: Dernière UP\n'
               '    Given PREP- Initialisation de la première UP de l\'ordre de préparation "0082387332" répondant\n'
               '      | code               | site |\n      | 314895000000005829 | 01   |\n'
               '    # | 999999999999 | commenté |\n'
               '    Then KIS- Le message contient "Edition du bon de livraison"\n')
    for path, text in ((rel, feature), (data, "<order><product>A1710227</product><qty>120000</qty></order>")):
        full = tmp_path / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
    copy = {"wms_preparation_order": {"0082387332"}, "wms_preparation_unit": {"314895000000005829"},
            "ref_product": {"A1710227"}}
    queries = []

    def query(sql, schema):
        queries.append(sql)
        table = sql.split(" FROM ")[1].split()[0]
        if table not in copy and table != "wms_handling_unit":
            raise RuntimeError("relation does not exist")
        return [{"code": v} for v in copy.get(table, ()) if f"'{v}'" in sql]
    profile = {cc.OPTION: True, cc.SCHEMA_FIELD: "cucumber_customer_1",
               cc.REFERENCE_KEY: {"date": "2026-11-13T09:56:45"}}
    errors = cc.check_business_data(str(tmp_path), [rel, data], profile, query)
    assert len(errors) == 3
    assert any("« 0082387332 » est un(e) ordre de préparation" in e for e in errors)
    assert any("« 314895000000005829 » est un(e) UP" in e for e in errors)
    assert any(e.startswith(data) and "« A1710227 » est un(e) produit" in e for e in errors)
    # Lignes créées par un run (après la copie) ignorées ; valeurs courtes, phrases et lignes commentées non lues
    assert "created_date < TIMESTAMP '2026-11-13 09:56:45' - INTERVAL '3 hours'" in queries[0]
    assert not any("'01'" in q or "Edition du bon" in q or "999999999999" in q for q in queries)
    # Option inactive : aucun contrôle
    assert cc.check_business_data(str(tmp_path), [rel], {}, query) == []
    assert "Données MÉTIER" in cc.prompt_block(profile)


def test_copied_prompt_sends_test_services_to_the_job_arguments():
    block = cc.prompt_block({cc.OPTION: True, cc.SCHEMA_FIELD: "cucumber_customer_1"})
    assert "ADDITIONAL_ARGLINE" in block and "System.getProperty" in block and "JAMAIS" in block
