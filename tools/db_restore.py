"""
Recopie de la configuration client (base client en direct) dans le schéma des tests Cucumber, avant les tests, avec
la méthode du script restore.ps1 de l'équipe (PostgreSQL) :

1. pg_dump (format custom) du schéma client SOURCE ;
2. pg_restore -l : listing ; les DONNÉES des tables d'historique et de journaux (EXCLUDED_DATA, complétée par
   RESTORE_EXCLUDE_DATA) sont mises en commentaire — structure et paramétrage gardés ;
3. sauvegarde (pg_dump) du schéma CIBLE actuel : retour arrière possible (restore_backup) ;
4. DROP SCHEMA cible CASCADE, restauration (--no-owner, listing filtré) sous le nom du schéma source puis
   renommage en schéma cible (pg_restore ne sait pas renommer un schéma) ;
5. ANALYZE de chaque table du schéma ;
6. script SQL facultatif après import (RESTORE_POST_SQL), précédé de « set schema '<cible>'; ».

Source : CLIENT_SOURCE_DB_* (HOST, PORT, NAME, USER, PASSWORD), à défaut DB_* ; schéma donné par l'utilisateur.
Cible : base des tests Cucumber (CUCUMBER_DB_*, PARTAGÉE avec Jenkins), schéma du projet ou CUCUMBER_DB_SCHEMA.
Outils PostgreSQL : PG_BIN_DIR (ex. C:\\Program Files\\PostgreSQL\\16\\bin), sinon le PATH. Mots de passe passés par
PGPASSWORD, jamais en ligne de commande ni dans les journaux.

Garde-fous (base partagée) : jamais pendant un build Jenkins des jobs Cucumber ; schéma cible ≠ schéma source sur la
même base ; schéma « public » et noms invalides refusés ; nom du schéma cible à confirmer par l'appelant.
"""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime

WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(WORKSPACE_DIR, ".tmp", "restauration")
# Données exclues par le script de l'équipe (tables d'historique, de journaux, documents)
EXCLUDED_DATA = [r"TABLE DATA \w* \w*_ ", r"TABLE DATA \w* wms_content_mvt_history ",
                 r"TABLE DATA \w* wms_operation_history ", r"TABLE DATA \w* wms_operation_history_cmpt ",
                 r"TABLE DATA \w* lab_outbox ", r"TABLE DATA \w* lab_task_event_history ",
                 r"TABLE DATA \w* lab_task_event_history_link ", r"TABLE DATA \w* tms_shipping_unit_document ",
                 r"TABLE DATA \w* exc_gs1contact_doc_header ", r"TABLE DATA \w* exc_gs1doc_header_status ",
                 r"TABLE DATA \w* exc_gs1document_header ", r"TABLE DATA \w* exc_gs1message_doc_child ",
                 r"TABLE DATA \w* exc_gs1partner_doc_header ", r"TABLE DATA \w* exc_message_histo ",
                 r"TABLE DATA \w* kis_execution", r"TABLE DATA \w* kis_document ", r"TABLE DATA \w* kis_draft ",
                 r"TABLE DATA \w* kis_failed_orchestrated_exec ", r"TABLE DATA \w* kis_login_log ",
                 r"TABLE DATA \w* ref_activity_report ", r"TABLE DATA \w* ref_activity_report_consumer ",
                 r"TABLE DATA \w* ref_report_doc_instance ", r"TABLE DATA \w* ref_report_doc_instance_exc "]
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class RestoreError(RuntimeError):
    pass


def connection(prefix: str, fallback: str = "") -> dict:
    """Connexion PostgreSQL lue dans le .env (préfixe, à défaut `fallback` champ par champ)."""
    def value(key, default=""):
        return (os.getenv(f"{prefix}{key}", "").strip() or (os.getenv(f"{fallback}{key}", "").strip()
                                                            if fallback else "") or default)
    return {"host": value("HOST", "localhost"), "port": value("PORT", "5432"), "name": value("NAME"),
            "user": value("USER"), "password": os.getenv(f"{prefix}PASSWORD", "") or (
                os.getenv(f"{fallback}PASSWORD", "") if fallback else ""),
            "type": value("TYPE", "postgresql").lower()}


def source_connection() -> dict:
    return connection("CLIENT_SOURCE_DB_", "DB_")


def target_connection() -> dict:
    return connection("CUCUMBER_DB_", "DB_")


def describe(conn: dict) -> str:
    return f"{conn['user']}@{conn['host']}:{conn['port']}/{conn['name']}"


def tool(name: str) -> str:
    """Chemin d'un outil PostgreSQL (pg_dump, pg_restore, psql) : PG_BIN_DIR, sinon le PATH."""
    folder = os.getenv("PG_BIN_DIR", "").strip()
    for candidate in ([os.path.join(folder, name + (".exe" if os.name == "nt" else ""))] if folder else []):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which(name)
    if not found:
        raise RestoreError(f"{name} introuvable : installe les outils PostgreSQL ou renseigne PG_BIN_DIR "
                           f"(ex. C:\\Program Files\\PostgreSQL\\16\\bin)")
    return found


def filter_listing(listing: str, extra: list = ()) -> tuple:
    """Listing pg_restore -l avec les données exclues mises en commentaire (« ; ») : (listing filtré, nb exclu)."""
    patterns = [re.compile(p) for p in EXCLUDED_DATA + [rf"TABLE DATA \w* {re.escape(t.strip())} "
                                                         for t in extra if t.strip()]]
    out, excluded = [], 0
    for line in listing.splitlines():
        if not line.startswith(";") and any(p.search(line) for p in patterns):
            out.append(";" + line)
            excluded += 1
        else:
            out.append(line)
    return "\n".join(out) + "\n", excluded


def check_plan(source_schema: str, target_schema: str, source: dict, target: dict) -> list:
    """Erreurs bloquantes avant toute action (vide : la restauration peut partir)."""
    errors = []
    for label, schema in (("source", source_schema), ("cible", target_schema)):
        if not _IDENTIFIER.match(schema or ""):
            errors.append(f"schéma {label} invalide ou vide : « {schema} »")
        elif schema.lower() in ("public", "information_schema", "pg_catalog"):
            errors.append(f"schéma {label} « {schema} » interdit")
    for label, conn in (("source", source), ("cible", target)):
        if conn["type"] not in ("postgresql", "postgres"):
            errors.append(f"base {label} de type « {conn['type']} » : seule PostgreSQL est prise en charge")
        if not (conn["name"] and conn["user"]):
            errors.append(f"connexion {label} incomplète (nom de base et utilisateur requis)")
    same_db = (source["host"], source["port"], source["name"]) == (target["host"], target["port"], target["name"])
    if same_db and (source_schema or "").lower() == (target_schema or "").lower():
        errors.append("le schéma source et le schéma cible sont le même schéma de la même base")
    return errors


def _run(args: list, conn: dict, log, data: str = None, timeout: int = 3600) -> subprocess.CompletedProcess:
    env = {**os.environ, "PGPASSWORD": conn.get("password", ""), "PGCLIENTENCODING": "UTF8"}
    shown = " ".join(a for a in args[1:] if not a.startswith("--password"))
    log(f"$ {os.path.basename(args[0])} {shown[:300]}")
    result = subprocess.run(args, input=data, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            env=env, timeout=timeout)
    for line in (result.stderr or "").splitlines()[-15:]:
        if line.strip():
            log(f"  {line.strip()[:300]}")
    return result


def _conn_args(conn: dict) -> list:
    return [f"--host={conn['host']}", f"--port={conn['port']}", f"--username={conn['user']}", "--no-password"]


def _psql(conn: dict, sql: str, log, quiet: bool = True) -> subprocess.CompletedProcess:
    return _run([tool("psql"), *_conn_args(conn), f"--dbname={conn['name']}", "-v", "ON_ERROR_STOP=1", "-t", "-A"]
                + (["-q"] if quiet else []) + ["-c", sql], conn, log)


def schema_exists(conn: dict, schema: str, log) -> bool:
    result = _psql(conn, f"SELECT count(*) FROM information_schema.schemata WHERE schema_name = '{schema}';", log)
    if result.returncode != 0:
        raise RestoreError(f"base {describe(conn)} injoignable : {(result.stderr or '').strip()[:200]}")
    return (result.stdout or "").strip().splitlines()[-1:] == ["1"]


def dump_schema(conn: dict, schema: str, path: str, log) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    result = _run([tool("pg_dump"), *_conn_args(conn), f"--dbname={conn['name']}", f"--schema={schema}",
                   "--format=c", f"--file={path}"], conn, log)
    if result.returncode != 0 or not os.path.exists(path):
        raise RestoreError(f"pg_dump du schéma {schema} ({describe(conn)}) en échec : "
                           f"{(result.stderr or '').strip()[-300:]}")


def restore_dump(conn: dict, dump: str, dump_schema_name: str, target_schema: str, log, extra_excluded: list = (),
                 post_sql: str = "", exclude: bool = True) -> dict:
    """Étapes 2, 4, 5, 6 sur un dump : schéma `dump_schema_name` du dump restauré en `target_schema`."""
    listing = _run([tool("pg_restore"), "-l", dump], conn, log)
    if listing.returncode != 0:
        raise RestoreError(f"lecture du dump impossible : {(listing.stderr or '').strip()[-300:]}")
    filtered, excluded = filter_listing(listing.stdout, list(extra_excluded)) if exclude else (listing.stdout, 0)
    listing_path = dump + ".listing.txt"
    with open(listing_path, "w", encoding="utf-8") as f:
        f.write(filtered)
    log(f"{excluded} jeu(x) de données d'historique ou de journaux exclu(s) (structure conservée)")
    rename = dump_schema_name.lower() != target_schema.lower()
    if rename and schema_exists(conn, dump_schema_name, log):
        raise RestoreError(f"le schéma « {dump_schema_name} » existe déjà sur la base cible : restauration sous son "
                           f"nom puis renommage impossible sans l'écraser — rien n'a été modifié")
    if schema_exists(conn, target_schema, log):
        log(f"Suppression du schéma cible {target_schema} (DROP SCHEMA … CASCADE)")
        dropped = _psql(conn, f'DROP SCHEMA "{target_schema}" CASCADE;', log)
        if dropped.returncode != 0:
            raise RestoreError(f"suppression du schéma {target_schema} impossible : {dropped.stderr.strip()[:300]}")
    created = _psql(conn, f'CREATE SCHEMA IF NOT EXISTS "{dump_schema_name}" AUTHORIZATION "{conn["user"]}";', log)
    if created.returncode != 0:
        raise RestoreError(f"création du schéma impossible : {created.stderr.strip()[:300]}")
    started = datetime.now()
    restored = _run([tool("pg_restore"), *_conn_args(conn), f"--dbname={conn['name']}", "--no-owner",
                     f"--schema={dump_schema_name}", "--format=c", "-L", listing_path, dump], conn, log)
    if rename:
        renamed = _psql(conn, f'ALTER SCHEMA "{dump_schema_name}" RENAME TO "{target_schema}";', log)
        if renamed.returncode != 0:
            raise RestoreError(f"renommage {dump_schema_name} → {target_schema} impossible : "
                               f"{renamed.stderr.strip()[:300]}")
    warnings = sum(1 for line in (restored.stderr or "").splitlines() if "error" in line.lower())
    log("ANALYZE des tables du schéma " + target_schema)
    _psql(conn, "DO $$ DECLARE r RECORD; BEGIN FOR r IN SELECT table_schema, table_name FROM "
                f"information_schema.tables WHERE table_schema = '{target_schema}' AND table_type = 'BASE TABLE' "
                "LOOP EXECUTE 'ANALYZE ' || quote_ident(r.table_schema) || '.' || quote_ident(r.table_name); "
                "END LOOP; END $$;", log)
    post = ""
    if post_sql and os.path.exists(post_sql):
        with open(post_sql, encoding="utf-8-sig") as f:
            script = f"set schema '{target_schema}';\n" + f.read()
        result = _run([tool("psql"), *_conn_args(conn), f"--dbname={conn['name']}", "-q", "-f", "-"], conn, log,
                      data=script)
        post = "script après import passé" if result.returncode == 0 else "script après import en échec"
        log(post)
    return {"exclus": excluded, "erreurs_restauration": warnings, "apres_import": post,
            "duree_s": int((datetime.now() - started).total_seconds())}


def history_path(client_name: str) -> str:
    return os.path.join(ROOT, client_name, "historique.json")


def load_history(client_name: str) -> list:
    try:
        with open(history_path(client_name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _record(client_name: str, entry: dict) -> None:
    history = load_history(client_name) + [entry]
    os.makedirs(os.path.dirname(history_path(client_name)), exist_ok=True)
    with open(history_path(client_name), "w", encoding="utf-8") as f:
        json.dump(history[-50:], f, ensure_ascii=False, indent=1)


def restore_client_config(client_name: str, source_schema: str, target_schema: str, log=print,
                          jenkins_busy=None) -> dict:
    """
    Recopie complète (étapes 1 à 6), avec sauvegarde préalable du schéma cible. `jenkins_busy()` : builds Jenkins en
    cours (liste) — non vide, rien n'est fait. Renvoie l'entrée d'historique ({ok, message, sauvegarde, ...}).
    """
    source, target = source_connection(), target_connection()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    entry = {"date": datetime.now().isoformat(timespec="seconds"), "source": f"{describe(source)} / {source_schema}",
             "cible": f"{describe(target)} / {target_schema}", "ok": False, "sauvegarde": ""}
    try:
        errors = check_plan(source_schema, target_schema, source, target)
        if errors:
            raise RestoreError(" ; ".join(errors))
        busy = list(jenkins_busy() if jenkins_busy else [])
        if busy:
            raise RestoreError("un build Jenkins utilise la base des tests : " + ", ".join(busy[:3])
                               + " — relance une fois terminé")
        folder = os.path.join(ROOT, client_name)
        if schema_exists(target, target_schema, log):
            backup = os.path.join(folder, f"sauvegarde-{target_schema}-{stamp}.pgdump")
            log(f"Sauvegarde du schéma cible {target_schema} avant remplacement")
            dump_schema(target, target_schema, backup, log)
            entry["sauvegarde"] = backup
        dump = os.path.join(folder, f"config-{source_schema}-{stamp}.pgdump")
        log(f"Lecture de la config client : schéma {source_schema} ({describe(source)})")
        dump_schema(source, source_schema, dump, log)
        result = restore_dump(target, dump, source_schema, target_schema, log,
                              [t for t in os.getenv("RESTORE_EXCLUDE_DATA", "").split(",") if t.strip()],
                              os.getenv("RESTORE_POST_SQL", "").strip())
        entry.update(result, ok=True, message=f"config client recopiée dans {target_schema} : "
                                              f"{result['exclus']} jeu(x) de données d'historique exclu(s), "
                                              f"{result['duree_s']} s")
    except (RestoreError, subprocess.TimeoutExpired, OSError) as e:
        entry["message"] = str(e)
    _record(client_name, entry)
    return entry


def restore_backup(client_name: str, backup: str, target_schema: str, log=print, jenkins_busy=None) -> dict:
    """Retour arrière : la sauvegarde `backup` (schéma cible d'avant) remise en place."""
    target = target_connection()
    entry = {"date": datetime.now().isoformat(timespec="seconds"), "source": f"sauvegarde {os.path.basename(backup)}",
             "cible": f"{describe(target)} / {target_schema}", "ok": False, "sauvegarde": "", "retour": True}
    try:
        busy = list(jenkins_busy() if jenkins_busy else [])
        if busy:
            raise RestoreError("un build Jenkins utilise la base des tests : relance une fois terminé")
        if not os.path.exists(backup):
            raise RestoreError(f"sauvegarde introuvable : {backup}")
        result = restore_dump(target, backup, target_schema, target_schema, log, exclude=False)
        entry.update(result, ok=True, message=f"schéma {target_schema} remis tel qu'avant la recopie")
    except (RestoreError, subprocess.TimeoutExpired, OSError) as e:
        entry["message"] = str(e)
    _record(client_name, entry)
    return entry
