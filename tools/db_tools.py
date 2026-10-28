"""
Accès en LECTURE SEULE à la base de données du client, pour l'agent Claude Code et l'IHM.

  python tools/db_tools.py --schema MON_SCHEMA test
  python tools/db_tools.py --schema MON_SCHEMA tables --like "ORD%"
  python tools/db_tools.py --schema MON_SCHEMA describe ORDERS
  python tools/db_tools.py --schema MON_SCHEMA query "SELECT id, status FROM orders WHERE id = 42" --limit 20

La connexion est lue dans le .env (DB_TYPE, DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_SCHEMA) :
le mot de passe n'est jamais affiché, ni dans la sortie, ni dans les messages d'erreur.

Garde-fous cumulés (aucun ne suffit seul ; utilisez aussi un compte base en lecture seule) :
  1. une seule instruction, commençant par SELECT ou WITH ;
  2. mots-clés d'écriture refusés, recherchés hors littéraux et commentaires ;
  3. transaction ouverte en READ ONLY quand le SGBD le permet, rollback systématique ;
  4. noms de schéma et de table validés par expression régulière ;
  5. nombre de lignes plafonné (50 par défaut, 200 maximum) et cellules tronquées.

La dernière ligne de sortie est DB_OK ou DB_ERROR=<message>.
"""
import argparse
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

DB_TYPES = ["postgresql", "oracle", "sqlserver", "mysql"]
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
DEFAULT_CELL_CHARS = 200
MAX_TABLES_LISTED = 300

# (dialecte SQLAlchemy, module Python du pilote, paquet pip) : le premier pilote installé est utilisé
DRIVERS = {
    "postgresql": [("postgresql+psycopg2", "psycopg2", "psycopg2-binary"),
                   ("postgresql+psycopg", "psycopg", "psycopg[binary]")],
    "oracle": [("oracle+oracledb", "oracledb", "oracledb")],
    "sqlserver": [("mssql+pymssql", "pymssql", "pymssql"),
                  ("mssql+pyodbc", "pyodbc", "pyodbc")],
    "mysql": [("mysql+pymysql", "pymysql", "pymysql")],
    # Non proposé dans l'IHM : sert aux tests (DB_NAME = chemin du fichier)
    "sqlite": [("sqlite", "sqlite3", "")],
}

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]{0,127}$")

_WRITE_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "REPLACE INTO", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "RENAME", "GRANT", "REVOKE", "INTO", "EXEC", "EXECUTE", "CALL", "LOCK", "UNLOCK", "COPY", "SET",
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "DECLARE", "PRAGMA", "ATTACH", "DETACH", "VACUUM",
    "REINDEX", "CLUSTER", "LOAD", "HANDLER", "OUTFILE", "DUMPFILE", "SHUTDOWN", "KILL",
)
_FORBIDDEN_RE = [
    re.compile(r"\b(" + "|".join(k.replace(" ", r"\s+") for k in _WRITE_KEYWORDS) + r")\b", re.IGNORECASE),
    # Verrous posés par un SELECT (FOR UPDATE est déjà couvert par UPDATE)
    re.compile(r"\bFOR\s+(NO\s+KEY\s+|KEY\s+)?SHARE\b", re.IGNORECASE),
    # Fonctions à effet de bord appelables depuis un SELECT
    re.compile(r"\b(PG_TERMINATE_BACKEND|PG_CANCEL_BACKEND|PG_RELOAD_CONF|PG_ROTATE_LOGFILE|PG_READ_FILE"
               r"|PG_READ_BINARY_FILE|PG_LS_DIR|PG_ADVISORY_\w*|LO_IMPORT|LO_EXPORT|DBLINK\w*|SETVAL|NEXTVAL"
               r"|OPENROWSET|OPENQUERY|OPENDATASOURCE|DBMS_\w+|UTL_\w+|LOAD_FILE|SLEEP|PG_SLEEP|BENCHMARK)\b",
               re.IGNORECASE),
]


class DbError(Exception):
    """Erreur d'accès à la base à remonter à l'agent ou à l'utilisateur (sans mot de passe)."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Profil de connexion : "" = base du socle (DB_*) ; "cucumber" = base de l'environnement où sont joués les tests
# Cucumber (CUCUMBER_DB_*), à défaut DB_*
PROFILES = ("", "cucumber")
_profile = ""


def set_profile(profile: str) -> str:
    """Choisit le profil de connexion ; renvoie le précédent."""
    global _profile
    previous, _profile = _profile, (profile or "").strip().lower()
    if _profile not in PROFILES:
        raise DbError(f"Profil de base inconnu : '{profile}' (attendu : cucumber ou vide).")
    return previous


def config_prefix(profile: str = None) -> str:
    """Préfixe des variables du profil : CUCUMBER_DB_ si sa connexion est renseignée, sinon DB_."""
    profile = _profile if profile is None else profile
    if profile == "cucumber" and (os.getenv("CUCUMBER_DB_HOST", "").strip() or os.getenv("CUCUMBER_DB_NAME", "").strip()):
        return "CUCUMBER_DB_"
    return "DB_"


def get_db_config() -> dict:
    """Paramètres de connexion du profil courant, lus dans l'environnement (.env)."""
    prefix = config_prefix()
    return {
        "type": os.getenv(f"{prefix}TYPE", "").strip().lower(),
        "host": os.getenv(f"{prefix}HOST", "").strip(),
        "port": os.getenv(f"{prefix}PORT", "").strip(),
        "name": os.getenv(f"{prefix}NAME", "").strip(),
        "user": os.getenv(f"{prefix}USER", "").strip(),
        "password": os.getenv(f"{prefix}PASSWORD", ""),
        "schema": os.getenv(f"{prefix}SCHEMA", "").strip(),
    }


def is_db_configured(profile: str = None) -> bool:
    if profile is not None:
        previous = set_profile(profile)
        try:
            return is_db_configured()
        finally:
            set_profile(previous)
    cfg = get_db_config()
    if cfg["type"] == "sqlite":
        return bool(cfg["name"])
    return cfg["type"] in DB_TYPES and all(cfg[k] for k in ("host", "name", "user"))


def describe_db_target() -> str:
    """Cible de connexion lisible, sans mot de passe (pour les logs)."""
    cfg = get_db_config()
    if cfg["type"] == "sqlite":
        return f"sqlite {cfg['name']}"
    port = f":{cfg['port']}" if cfg["port"] else ""
    return f"{cfg['type']} {cfg['user']}@{cfg['host']}{port}/{cfg['name']}"


def is_valid_identifier(name: str) -> bool:
    return bool(name) and bool(IDENTIFIER_RE.match(name))


def _check_identifier(name: str, label: str) -> str:
    if not is_valid_identifier(name or ""):
        raise DbError(f"Nom de {label} invalide : '{name}' (lettres, chiffres, _, $ ou # uniquement).")
    return name


def _server_message(error) -> str:
    """
    Sous Windows en français, PostgreSQL renvoie ses erreurs de connexion en Windows-1252 :
    psycopg2 lève alors UnicodeDecodeError, qui masque le vrai message. On le décode ici.
    """
    seen = error
    while seen is not None:
        if isinstance(seen, UnicodeDecodeError) and isinstance(seen.object, (bytes, bytearray)):
            return seen.object.decode("cp1252", errors="replace")
        seen = getattr(seen, "orig", None) or seen.__cause__ or seen.__context__
    return str(error)


def _scrub(message) -> str:
    """Retire le mot de passe d'un message d'erreur de pilote."""
    password = get_db_config()["password"]
    text = _server_message(message) if isinstance(message, BaseException) else str(message)
    if password:
        text = text.replace(password, "***")
    return text.strip().splitlines()[0][:500] if text.strip() else "erreur inconnue"


# ---------------------------------------------------------------------------
# Filtre SQL (lecture seule)
# ---------------------------------------------------------------------------

def _strip_literals_and_comments(sql: str, db_type: str = "") -> str:
    """
    Remplace littéraux, identifiants délimités et commentaires par un espace, pour ne chercher
    les mots-clés interdits que dans le code SQL effectif. Lève DbError si un bloc n'est pas fermé.
    """
    out, i, n = [], 0, len(sql)
    while i < n:
        c, two = sql[i], sql[i:i + 2]
        if two == "--":
            end = sql.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
        elif two == "/*":
            end = sql.find("*/", i + 2)
            if end < 0:
                raise DbError("Commentaire /* non fermé.")
            i = end + 2
            out.append(" ")
        elif c in ("'", '"', "`", "["):
            close = {"'": "'", '"': '"', "`": "`", "[": "]"}[c]
            j = i + 1
            while True:
                if j >= n:
                    raise DbError(f"Littéral ou identifiant {c}…{close} non fermé.")
                if c == "'" and db_type == "mysql" and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == close:
                    if j + 1 < n and sql[j + 1] == close and close != "]":
                        j += 2  # guillemet doublé = échappement
                        continue
                    break
                j += 1
            i = j + 1
            out.append(" ")
        elif c == "$" and db_type == "postgresql" and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if not m:
                out.append(c)
                i += 1
                continue
            end = sql.find(m.group(0), i + len(m.group(0)))
            if end < 0:
                raise DbError("Chaîne $$ non fermée.")
            i = end + len(m.group(0))
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def validate_select(sql: str, db_type: str = "") -> str:
    """
    Vérifie qu'il s'agit d'une requête de lecture unique. Renvoie la requête à exécuter
    (sans point-virgule final) ou lève DbError avec la raison du refus.
    """
    if not sql or not sql.strip():
        raise DbError("Requête vide.")
    code = _strip_literals_and_comments(sql, db_type)
    body = re.sub(r"[;\s]+$", "", code)
    if ";" in body:
        raise DbError("Une seule instruction SQL est autorisée.")
    if not re.match(r"^\s*(SELECT|WITH)\b", body, re.IGNORECASE):
        raise DbError("Seules les requêtes commençant par SELECT ou WITH sont autorisées.")
    for pattern in _FORBIDDEN_RE:
        m = pattern.search(body)
        if m:
            raise DbError(f"Mot-clé interdit en lecture seule : {m.group(0).upper()}.")
    executed = sql.strip()
    while executed.endswith(";"):
        executed = executed[:-1].rstrip()
    return executed


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------

def _import_sqlalchemy():
    try:
        import sqlalchemy
    except ImportError:
        raise DbError("Paquet Python manquant : installez-le avec  pip install sqlalchemy")
    return sqlalchemy


def _select_driver(db_type: str) -> str:
    if db_type not in DRIVERS:
        raise DbError(f"DB_TYPE '{db_type}' non supporté (valeurs : {', '.join(DB_TYPES)}).")
    import importlib
    for dialect, module, _ in DRIVERS[db_type]:
        try:
            importlib.import_module(module)
            return dialect
        except ImportError:
            continue
    packages = " ou ".join(p for _, _, p in DRIVERS[db_type])
    raise DbError(f"Pilote {db_type} manquant : installez-le avec  pip install {packages}")


def _create_engine(cfg: dict):
    sqlalchemy = _import_sqlalchemy()
    from sqlalchemy.engine import URL
    from sqlalchemy.pool import NullPool

    if not is_db_configured():
        raise DbError("Connexion base non configurée (DB_TYPE, DB_HOST, DB_NAME, DB_USER dans le .env).")
    db_type = cfg["type"]
    dialect = _select_driver(db_type)
    try:
        port = int(cfg["port"]) if cfg["port"] else None
    except ValueError:
        raise DbError(f"DB_PORT invalide : '{cfg['port']}'.")

    connect_args = {}
    if db_type == "sqlite":
        url = URL.create("sqlite", database=cfg["name"])
    elif db_type == "oracle":
        # DB_NAME = service name Oracle
        url = URL.create(dialect, username=cfg["user"], password=cfg["password"], host=cfg["host"],
                         port=port, query={"service_name": cfg["name"]})
        connect_args = {"tcp_connect_timeout": 10}
    else:
        query = {}
        if dialect == "mssql+pyodbc":
            query = {"driver": os.getenv("DB_ODBC_DRIVER", "ODBC Driver 18 for SQL Server"),
                     "TrustServerCertificate": "yes"}
        url = URL.create(dialect, username=cfg["user"], password=cfg["password"], host=cfg["host"],
                         port=port, database=cfg["name"], query=query)
        connect_args = {"login_timeout": 10} if dialect == "mssql+pymssql" else (
            {} if dialect == "mssql+pyodbc" else {"connect_timeout": 10})
    # NullPool : chaque commande ouvre puis ferme sa session (aucun état de session réutilisé)
    return sqlalchemy.create_engine(url, poolclass=NullPool, connect_args=connect_args)


def _apply_readonly(conn, db_type: str, schema: str = "") -> None:
    """Transaction en lecture seule quand le SGBD le permet, puis schéma par défaut (nom déjà validé)."""
    if db_type == "postgresql":
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        conn.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
        if schema:
            conn.exec_driver_sql(f"SET LOCAL search_path TO {schema}")
    elif db_type == "oracle":
        if schema:
            conn.exec_driver_sql(f"ALTER SESSION SET CURRENT_SCHEMA = {schema}")
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
    elif db_type == "mysql":
        conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        if schema:
            conn.exec_driver_sql(f"USE `{schema}`")
    elif db_type == "sqlserver":
        # Pas de transaction READ ONLY sous SQL Server : filtre SQL + rollback + compte en lecture seule
        conn.exec_driver_sql("SET LOCK_TIMEOUT 10000")
    elif db_type == "sqlite":
        conn.exec_driver_sql("PRAGMA query_only = ON")


@contextmanager
def readonly_connection(schema: str = ""):
    """Connexion en lecture seule ; la transaction est toujours annulée (rollback) à la sortie."""
    cfg = get_db_config()
    if schema:
        _check_identifier(schema, "schéma")
    engine = _create_engine(cfg)
    try:
        conn = engine.connect()
    except Exception as e:
        engine.dispose()
        raise DbError(f"Connexion impossible ({describe_db_target()}) : {_scrub(e)}")
    try:
        _apply_readonly(conn, cfg["type"], schema)
        yield conn
    except DbError:
        raise
    except Exception as e:
        raise DbError(_scrub(e))
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------

def _cell(value, max_chars: int) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binaire {len(bytes(value))} octets>"
    if isinstance(value, (datetime, date)):
        text = value.isoformat()
    else:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ⏎ ").replace("|", "¦")
    return text if len(text) <= max_chars else text[:max_chars] + "…"


def run_query(sql: str, schema: str = "", limit: int = DEFAULT_LIMIT) -> str:
    """Exécute une requête de lecture validée et renvoie un tableau texte compact."""
    cfg = get_db_config()
    executed = validate_select(sql, cfg["type"])
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    try:
        max_chars = max(20, int(os.getenv("DB_MAX_CELL_CHARS", DEFAULT_CELL_CHARS)))
    except ValueError:
        max_chars = DEFAULT_CELL_CHARS
    with readonly_connection(schema) as conn:
        result = conn.exec_driver_sql(executed)
        columns = list(result.keys())
        rows = result.fetchmany(limit + 1)
    truncated = len(rows) > limit
    rows = rows[:limit]
    lines = [" | ".join(columns)]
    lines += [" | ".join(_cell(v, max_chars) for v in row) for row in rows]
    lines.append(f"({len(rows)} ligne(s)" + (f", limite de {limit} atteinte : résultat tronqué)" if truncated else ")"))
    return "\n".join(lines)


def _like_to_regex(pattern: str):
    regex = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
    return re.compile(f"^{regex}$", re.IGNORECASE)


def list_tables(schema: str = "", like: str = "") -> str:
    """Tables et vues du schéma, filtrées par un motif LIKE (% et _)."""
    _import_sqlalchemy()
    from sqlalchemy import inspect
    with readonly_connection(schema) as conn:
        insp = inspect(conn)
        names = [(t, "table") for t in insp.get_table_names(schema=schema or None)]
        names += [(v, "vue") for v in insp.get_view_names(schema=schema or None)]
    if like:
        regex = _like_to_regex(like)
        names = [n for n in names if regex.match(n[0])]
    names.sort(key=lambda n: n[0].lower())
    lines = [f"{name} ({kind})" for name, kind in names[:MAX_TABLES_LISTED]]
    suffix = f", {MAX_TABLES_LISTED} affichés : affinez avec --like" if len(names) > MAX_TABLES_LISTED else ""
    lines.append(f"({len(names)} objet(s){suffix})")
    return "\n".join(lines)


def describe_table(table: str, schema: str = "") -> str:
    """Colonnes, clé primaire, clés étrangères et index d'une table."""
    _check_identifier(table, "table")
    _import_sqlalchemy()
    from sqlalchemy import inspect
    from sqlalchemy.exc import NoSuchTableError
    target = f"{schema}.{table}" if schema else table
    with readonly_connection(schema) as conn:
        insp = inspect(conn)
        try:
            columns = insp.get_columns(table, schema=schema or None)
        except NoSuchTableError:
            raise DbError(f"Table introuvable : {target}")
        if not columns:
            raise DbError(f"Table introuvable : {target}")
        pk = insp.get_pk_constraint(table, schema=schema or None) or {}
        fks = insp.get_foreign_keys(table, schema=schema or None) or []
        indexes = insp.get_indexes(table, schema=schema or None) or []
    lines = [f"Table {target}", "colonne | type | nullable | défaut"]
    for col in columns:
        default = col.get("default")
        lines.append(f"{col['name']} | {col['type']} | {'oui' if col.get('nullable', True) else 'non'} | "
                     f"{_cell(default, 60) if default is not None else ''}")
    if pk.get("constrained_columns"):
        lines.append(f"Clé primaire : {', '.join(pk['constrained_columns'])}")
    for fk in fks:
        ref = f"{fk.get('referred_schema') + '.' if fk.get('referred_schema') else ''}{fk.get('referred_table')}"
        lines.append(f"Clé étrangère : ({', '.join(fk.get('constrained_columns', []))}) -> "
                     f"{ref}({', '.join(fk.get('referred_columns', []))})")
    for idx in indexes:
        cols = ", ".join(c for c in idx.get("column_names", []) if c)
        lines.append(f"Index{' unique' if idx.get('unique') else ''} {idx.get('name')} : ({cols})")
    return "\n".join(lines)


def test_connection(schema: str = "", profile: str = None) -> tuple:
    """Renvoie (succès, message) ; utilisé par le bouton « Tester la connexion » de l'IHM."""
    if profile is not None:
        previous = set_profile(profile)
        try:
            return test_connection(schema)
        finally:
            set_profile(previous)
    cfg = get_db_config()
    try:
        _import_sqlalchemy()
        from sqlalchemy import inspect
        with readonly_connection(schema) as conn:
            conn.exec_driver_sql("SELECT 1 FROM DUAL" if cfg["type"] == "oracle" else "SELECT 1").fetchall()
            detail = ""
            if schema:
                tables = inspect(conn).get_table_names(schema=schema)
                detail = f" — schéma {schema} : {len(tables)} table(s)"
        return True, f"Connexion OK ({describe_db_target()}, lecture seule){detail}"
    except DbError as e:
        return False, str(e)
    except Exception as e:
        return False, _scrub(e)


# ---------------------------------------------------------------------------
# Journal des commandes de l'agent (AGENT_DB_LOG), pour rendre vérifiable l'usage de la base
# ---------------------------------------------------------------------------
_FROM_JOIN = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_$#]*(?:\.[A-Za-z_][A-Za-z0-9_$#]*)?)", re.I)


def query_tables(sql: str, db_type: str = "") -> list:
    """Tables lues par une requête (après FROM et JOIN, littéraux et commentaires écartés), sans doublon."""
    try:
        code = _strip_literals_and_comments(sql or "", db_type)
    except DbError:
        return []
    return list(dict.fromkeys(m.group(1) for m in _FROM_JOIN.finditer(code)))


def log_command(args, schema: str, error: str) -> None:
    """
    Ajoute la commande au journal AGENT_DB_LOG (une ligne JSON) s'il est défini : nature, schéma, tables lues,
    erreur. Ni le texte de la requête ni ses valeurs littérales ne sont journalisés (données éventuellement
    nominatives). Un journal illisible n'empêche jamais la commande.
    """
    path = os.getenv("AGENT_DB_LOG", "").strip()
    if not path:
        return
    if args.command == "query":
        tables = query_tables(args.sql, get_db_config().get("type", ""))
    elif args.command == "describe":
        tables = [args.table]
    else:
        tables = []
    entry = {"date": datetime.now().isoformat(timespec="seconds"), "commande": args.command, "schema": schema,
             "tables": tables, "motif": getattr(args, "like", "") or "", "ok": not error, "erreur": error[:300]}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--schema", default=argparse.SUPPRESS, help="Schéma cible (défaut : DB_SCHEMA du .env)")
    common.add_argument("--profile", default=argparse.SUPPRESS,
                        help="cucumber = base de l'environnement de test Cucumber (CUCUMBER_DB_*, à défaut DB_*)")

    parser = argparse.ArgumentParser(description="Accès en lecture seule à la base de données client",
                                     parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("test", parents=[common], help="Teste la connexion")
    tables = sub.add_parser("tables", parents=[common], help="Liste les tables et vues du schéma")
    tables.add_argument("--like", default="", help="Motif LIKE (ex: ORD%%)")
    describe = sub.add_parser("describe", parents=[common], help="Décrit une table")
    describe.add_argument("table")
    query = sub.add_parser("query", parents=[common], help="Exécute une requête SELECT / WITH")
    query.add_argument("sql")
    query.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Lignes max (défaut {DEFAULT_LIMIT}, max {MAX_LIMIT})")

    args = parser.parse_args()
    try:
        set_profile(getattr(args, "profile", ""))
        schema = (getattr(args, "schema", None) or get_db_config()["schema"]).strip()
        if args.command == "test":
            ok, message = test_connection(schema)
            if not ok:
                raise DbError(message)
            print(message)
        elif args.command == "tables":
            print(list_tables(schema, args.like))
        elif args.command == "describe":
            print(describe_table(args.table, schema))
        else:
            print(run_query(args.sql, schema, args.limit))
        print("DB_OK")
        log_command(args, schema, "")
        return 0
    except DbError as e:
        print(f"DB_ERROR={e}")
        log_command(args, schema, str(e))
        return 1
    except Exception as e:
        print(f"DB_ERROR={_scrub(e)}")
        log_command(args, schema, _scrub(e))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
