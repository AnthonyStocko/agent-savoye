"""
Diagnostic de l'environnement (lecture seule) quand le clear de l'init échoue de façon reproductible.

« Batch update returned unexpected row count » (StaleState, verrouillage optimiste) sur la purge de l'init : après
UNE relance en échec (dans le même passage ou au build précédent), ce n'est plus un conflit ponctuel — l'agent ne
relance plus l'init et diagnostique la base du schéma cité :

1. contraintes de clé étrangère de la table citée (pg_constraint : type de suppression, cascade ou non) et
   déclencheurs, comparés à ceux d'un autre schéma du même serveur (autre slot du pool, ou
   CUCUMBER_REFERENCE_SCHEMA, schéma de référence de la version) ;
2. historique des copies de base (étape 3) vers ce schéma, avec date, source et sauvegarde ;
3. logs serveur (Groundcover) de la purge, résumés par l'appelant quand Groundcover est utilisable.

Conclusion explicite : « environnement corrompu (structure ou données du schéma) : l'init ne peut pas réussir »,
avec la preuve et l'action humaine (restaurer la sauvegarde de la copie, ou demander la réinitialisation du
schéma). Rien n'est modifié dans l'init ni dans les tests, et l'init n'est plus relancée tant que l'environnement
n'est pas déclaré réparé (sauvegarde restaurée par l'agent, ou déblocage explicite de l'utilisateur).

Prévention : la copie de base (étape 3) n'écrit jamais dans un schéma du pool des runs Jenkins
(cucumber_customer_<N>, CUCUMBER_POOL_SCHEMA_PATTERN) ; elle va dans un schéma dédié aux essais locaux
(local_schema : cucumber_local_<client>), que l'agent ne lit que si l'utilisateur le choisit.
"""
import json
import os
import re
from datetime import datetime

from tools import cucumber_incidents

FILE_NAME = "environnement.json"
DEFAULT_POOL_PATTERN = r"cucumber_customer_\d+"
DELETE_RULES = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
CONCLUSION = "environnement corrompu (structure ou données du schéma) : l'init ne peut pas réussir"
# Purge de l'init : step « INIT- Purge … », appel DELETE clear/…, ou clear nommé dans le message
_CLEAR = re.compile(r"\bINIT-\s*Purge\b|\bclear/|\bclear\b", re.I)
# « delete from <schéma>.<table> », « update <schéma>.<table> », « insert into <schéma>.<table> »
_STATEMENT = re.compile(r"\b(?:delete\s+from\s+|update\s+(?=\S+\s+set\b)|insert\s+into\s+)(?:([a-z_][a-z0-9_$+]*)\.)?([a-z_][a-z0-9_]*)", re.I)

FK_SQL = ("SELECT c.conname AS nom, src.relname AS table_source, dst.relname AS table_cible, "
          "c.confdeltype AS suppression, pg_get_constraintdef(c.oid) AS definition "
          "FROM pg_constraint c JOIN pg_class src ON src.oid = c.conrelid "
          "JOIN pg_namespace ns ON ns.oid = src.relnamespace JOIN pg_class dst ON dst.oid = c.confrelid "
          "JOIN pg_namespace nd ON nd.oid = dst.relnamespace "
          "WHERE c.contype = 'f' AND ns.nspname = :schema AND nd.nspname = :schema "
          "AND (src.relname = :table OR dst.relname = :table) ORDER BY 2, 3, 1")
TRIGGER_SQL = ("SELECT t.tgname AS nom, pg_get_triggerdef(t.oid) AS definition FROM pg_trigger t "
               "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
               "WHERE NOT t.tgisinternal AND n.nspname = :schema AND c.relname = :table ORDER BY 1")
TABLE_SQL = ("SELECT table_schema AS schema FROM information_schema.tables WHERE table_name = :table "
             "ORDER BY table_schema")


# ---------------------------------------------------------------------------
# Schémas : pool des runs Jenkins et schéma des essais locaux
# ---------------------------------------------------------------------------

def pool_pattern() -> re.Pattern:
    """Schémas du pool des runs Jenkins (un par slot) : CUCUMBER_POOL_SCHEMA_PATTERN, défaut cucumber_customer_<N>."""
    try:
        return re.compile(os.getenv("CUCUMBER_POOL_SCHEMA_PATTERN", "").strip() or DEFAULT_POOL_PATTERN, re.I)
    except re.error:
        return re.compile(DEFAULT_POOL_PATTERN, re.I)


def is_pool_schema(schema: str) -> bool:
    return bool(schema) and bool(pool_pattern().fullmatch(schema.strip()))


def local_schema(client: str) -> str:
    """Schéma dédié aux essais locaux du client (copie de l'étape 3) : CUCUMBER_LOCAL_SCHEMA, sinon
    cucumber_local_<client> (identifiant PostgreSQL : minuscules, chiffres, « _ »)."""
    configured = os.getenv("CUCUMBER_LOCAL_SCHEMA", "").strip()
    if configured:
        return configured.replace("{client}", re.sub(r"[^a-z0-9_]", "_", (client or "").lower()))[:63]
    return f"cucumber_local_{re.sub(r'[^a-z0-9_]', '_', (client or 'client').lower())}"[:63]


def pool_refusal(schema: str, client: str = "") -> str:
    """Raison du refus d'une copie de base vers `schema` ('' si autorisée)."""
    if not is_pool_schema(schema):
        return ""
    return (f"schéma cible « {schema} » utilisé par les runs Jenkins (pool {pool_pattern().pattern}) : la copie de "
            f"base va dans un schéma dédié aux essais locaux ({local_schema(client) if client else 'cucumber_local_<client>'})")


# ---------------------------------------------------------------------------
# Échec en cause : clear de l'init en conflit de verrouillage
# ---------------------------------------------------------------------------

def is_clear_lock_failure(step: str, message: str) -> bool:
    """Conflit de verrouillage (« Batch update returned unexpected row count »…) pendant la purge de l'init."""
    return cucumber_incidents.is_lock_conflict(message) and bool(_CLEAR.search(f"{step} {message}"))


def statement_target(message: str) -> tuple:
    """(schéma, table) de l'ordre SQL cité par le message du serveur (('', '') s'il n'en cite aucun)."""
    found = _STATEMENT.search(str(message or ""))
    if not found:
        return "", ""
    schema = (found.group(1) or "").lower()
    return (schema if re.fullmatch(r"[a-z_][a-z0-9_$]*", schema) else ""), found.group(2).lower()


# ---------------------------------------------------------------------------
# Lecture de la base (lecture seule)
# ---------------------------------------------------------------------------

def default_query(sql: str, params: dict) -> list:
    """Requête en lecture seule sur la base des tests Cucumber (CUCUMBER_DB_*, à défaut DB_*), transaction annulée."""
    from sqlalchemy import text
    from tools import db_tools
    previous = db_tools.set_profile("cucumber")
    try:
        with db_tools.readonly_connection() as conn:
            return [dict(row._mapping) for row in conn.execute(text(sql), params)]
    finally:
        db_tools.set_profile(previous)


def constraints(schema: str, table: str, query) -> list:
    return [{"nom": r["nom"], "table_source": r["table_source"], "table_cible": r["table_cible"],
             "suppression": DELETE_RULES.get(str(r["suppression"]), str(r["suppression"])),
             "definition": r.get("definition", "")} for r in query(FK_SQL, {"schema": schema, "table": table})]


def triggers(schema: str, table: str, query) -> list:
    return [{"nom": r["nom"], "definition": r.get("definition", "")}
            for r in query(TRIGGER_SQL, {"schema": schema, "table": table})]


def reference_schema(schema: str, table: str, query) -> str:
    """Schéma de comparaison : CUCUMBER_REFERENCE_SCHEMA s'il a la table, sinon un autre slot du pool qui l'a."""
    having = [r["schema"] for r in query(TABLE_SQL, {"table": table})]
    configured = os.getenv("CUCUMBER_REFERENCE_SCHEMA", "").strip()
    if configured and configured in having and configured != schema:
        return configured
    others = [s for s in having if s != schema and is_pool_schema(s)]
    return sorted(others, key=lambda s: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)])[0] \
        if others else ""


def _key(c: dict) -> tuple:
    return c["table_source"], c["table_cible"], c["nom"]


def compare(target: list, reference: list, target_triggers: list = (), reference_triggers: list = ()) -> list:
    """Écarts de structure (phrases) entre le schéma en cause et le schéma de référence."""
    gaps = []
    ref = {_key(c): c for c in reference}
    mine = {_key(c): c for c in target}
    for key, c in mine.items():
        other = ref.get(key)
        if other is None:
            gaps.append(f"contrainte {c['nom']} ({c['table_source']} → {c['table_cible']}, ON DELETE "
                        f"{c['suppression']}) absente du schéma de référence")
        elif other["suppression"] != c["suppression"]:
            gaps.append(f"contrainte {c['nom']} ({c['table_source']} → {c['table_cible']}) : ON DELETE "
                        f"{c['suppression']} ici, {other['suppression']} dans le schéma de référence")
    for key, c in ref.items():
        if key not in mine:
            gaps.append(f"contrainte {c['nom']} ({c['table_source']} → {c['table_cible']}, ON DELETE "
                        f"{c['suppression']}) manquante")
    names = {t["nom"] for t in reference_triggers}
    gaps += [f"déclencheur {t['nom']} absent du schéma de référence" for t in target_triggers
             if t["nom"] not in names]
    gaps += [f"déclencheur {n} manquant" for n in sorted(names - {t['nom'] for t in target_triggers})]
    return gaps


def copies_into(schema: str, history: list) -> list:
    """Copies de base (étape 3) et retours arrière dont la cible est `schema`, de la plus ancienne à la plus récente."""
    return [e for e in history or [] if (e.get("cible") or "").rsplit("/", 1)[-1].strip().lower() == schema.lower()]


# ---------------------------------------------------------------------------
# Diagnostic et conclusion
# ---------------------------------------------------------------------------

def _date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return iso or "?"


def diagnose(client: str, schema: str, table: str, builds: list, history: list = None, query=None,
             logs: str = "") -> dict:
    """
    Diagnostic en lecture seule après un échec reproductible du clear (`builds` : builds en échec, dans l'ordre).
    Renvoie {verdict, schema, table, builds, contraintes, reference, ecarts, copies, copie_en_cause, logs,
    erreur_base, preuves, action, conclusion}. Le verdict est toujours « corrompu » : l'échec est reproductible ;
    les preuves disent ce qui le montre (écarts de structure, copie de base, logs).
    """
    if history is None:
        from tools import db_restore
        history = db_restore.load_history(client)
    query = query or default_query
    out = {"verdict": "corrompu", "schema": schema, "table": table, "builds": [b for b in builds if b],
           "contraintes": [], "declencheurs": [], "reference": {}, "ecarts": [], "copies": [], "copie_en_cause": {},
           "logs": logs, "erreur_base": "", "date": datetime.now().isoformat(timespec="seconds")}
    if schema and table:
        try:
            out["contraintes"] = constraints(schema, table, query)
            out["declencheurs"] = triggers(schema, table, query)
            ref = reference_schema(schema, table, query)
            if ref:
                ref_constraints, ref_triggers = constraints(ref, table, query), triggers(ref, table, query)
                out["reference"] = {"schema": ref, "contraintes": ref_constraints, "declencheurs": ref_triggers}
                out["ecarts"] = compare(out["contraintes"], ref_constraints, out["declencheurs"], ref_triggers)
        except Exception as e:  # noqa: BLE001 - base injoignable : le diagnostic garde les autres preuves
            out["erreur_base"] = f"{type(e).__name__} : {str(e)[:200]}"
    out["copies"] = copies_into(schema, history) if schema else []
    copies = [c for c in out["copies"] if c.get("ok") and not c.get("retour")]
    last_restore = max((c.get("date", "") for c in out["copies"] if c.get("ok") and c.get("retour")), default="")
    if copies and copies[-1].get("date", "") > last_restore:
        out["copie_en_cause"] = copies[-1]
    out["preuves"] = proofs(out)
    out["action"] = action(out)
    out["conclusion"] = f"Init impossible : {CONCLUSION} ({schema or 'schéma non lu'})"
    return out


def proofs(diag: dict) -> list:
    """Preuves du diagnostic, une phrase chacune."""
    lines = []
    builds = diag.get("builds") or []
    if builds:
        lines.append(f"échec reproductible du clear sur {diag.get('table') or 'la table citée'} : "
                     + ", ".join(f"build {_number(b)}" for b in builds) + " (même erreur après relance)")
    if diag.get("ecarts"):
        lines.append(f"structure de {diag['schema']}.{diag['table']} différente de "
                     f"{diag['reference']['schema']} : " + " ; ".join(diag["ecarts"][:6]))
    elif diag.get("reference"):
        lines.append(f"contraintes de clé étrangère et déclencheurs de {diag['table']} identiques à "
                     f"{diag['reference']['schema']} ({len(diag['contraintes'])} contrainte(s)) : les DONNÉES du "
                     f"schéma sont en cause")
    elif diag.get("contraintes") and not diag.get("erreur_base"):
        lines.append(f"aucun autre schéma du serveur n'a la table {diag['table']} : comparaison impossible "
                     f"(CUCUMBER_REFERENCE_SCHEMA)")
    cascades = [c for c in diag.get("contraintes") or [] if c["suppression"] == "CASCADE"]
    if cascades:
        lines.append("suppression en cascade dans la base : " + ", ".join(
            f"{c['table_source']} → {c['table_cible']} ({c['nom']})" for c in cascades[:5]))
    if diag.get("erreur_base"):
        lines.append(f"base non lue ({diag['erreur_base']})")
    copy = diag.get("copie_en_cause") or {}
    if copy:
        lines.append(f"schéma {diag['schema']} recréé par la copie de base du {_date(copy.get('date'))} depuis "
                     f"{copy.get('source', '?')} (étape 3), "
                     + (f"sauvegarde {os.path.basename(copy['sauvegarde'])}" if copy.get("sauvegarde") else
                        "sans sauvegarde (le schéma n'existait pas avant)"))
    if diag.get("logs"):
        lines.append(f"logs serveur de la purge : {diag['logs'][:400]}")
    return lines


def _number(build: str) -> str:
    found = re.search(r"/(\d+)/?$", build or "")
    return found.group(1) if found else (build or "?")


def action(diag: dict) -> str:
    """Action humaine, en une phrase."""
    schema = diag.get("schema") or "du slot"
    copy = diag.get("copie_en_cause") or {}
    if copy.get("sauvegarde"):
        return (f"Restaurer la sauvegarde {os.path.basename(copy['sauvegarde'])} du schéma {schema} (bouton "
                f"« Restaurer la sauvegarde » de l'étape 3), puis relancer l'init.")
    if copy:
        return (f"Demander la réinitialisation du schéma {schema} (recréé par la copie de base du "
                f"{_date(copy.get('date'))}, sans sauvegarde) à l'équipe qui gère l'environnement Jenkins, puis "
                f"débloquer l'init.")
    return (f"Demander la réinitialisation du schéma {schema} à l'équipe qui gère l'environnement Jenkins, puis "
            f"débloquer l'init.")


def line(diag: dict) -> str:
    """Bilan en une ligne : conclusion — preuves — action."""
    return (f"{diag.get('conclusion') or CONCLUSION} — preuve : " + " ; ".join(diag.get("preuves") or ["?"])
            + f" — action : {diag.get('action', '')}")


# ---------------------------------------------------------------------------
# Blocage de l'init (environnement.json du client)
# ---------------------------------------------------------------------------

def _path(client: str) -> str:
    from tools import cucumber_tools
    return os.path.join(cucumber_tools.cache_dir(client), FILE_NAME)


def load(client: str) -> dict:
    try:
        with open(_path(client), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(client: str, data: dict) -> None:
    os.makedirs(os.path.dirname(_path(client)), exist_ok=True)
    with open(_path(client), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def record_clear_failure(client: str, build: str, schema: str, table: str) -> int:
    """Échec du clear en conflit de verrouillage noté ; renvoie le nombre d'échecs consécutifs (même schéma)."""
    data = load(client)
    failures = [f for f in data.get("echecs_clear") or [] if f.get("schema") == schema]
    if build and build not in {f.get("build") for f in failures}:
        failures.append({"build": build, "schema": schema, "table": table,
                         "date": datetime.now().isoformat(timespec="seconds")})
    data["echecs_clear"] = failures[-10:]
    save(client, data)
    return len(failures)


def clear_failures(client: str) -> list:
    return list(load(client).get("echecs_clear") or [])


def block(client: str, diag: dict) -> dict:
    """Init bloquée : environnement déclaré corrompu (le diagnostic est gardé)."""
    data = load(client)
    data["blocage"] = diag
    save(client, data)
    return diag


def blocked(client: str, history: list = None) -> dict:
    """
    Diagnostic qui bloque l'init ({} si aucun). Une sauvegarde restaurée par l'agent sur ce schéma après le
    diagnostic lève le blocage d'elle-même.
    """
    data = load(client)
    diag = data.get("blocage") or {}
    if not diag:
        return {}
    if history is None:
        from tools import db_restore
        history = db_restore.load_history(client)
    restored = [c for c in copies_into(diag.get("schema", ""), history) if c.get("ok") and c.get("retour")
                and c.get("date", "") > diag.get("date", "")]
    if restored:
        unblock(client, f"sauvegarde restaurée le {_date(restored[-1]['date'])}")
        return {}
    return diag


def unblock(client: str, reason: str) -> None:
    """Environnement déclaré réparé (sauvegarde restaurée, schéma réinitialisé) : l'init peut être relancée."""
    data = load(client)
    if data.get("blocage"):
        data.setdefault("deblocages", []).append({"date": datetime.now().isoformat(timespec="seconds"),
                                                  "raison": reason, "schema": data["blocage"].get("schema", "")})
        data["deblocages"] = data["deblocages"][-10:]
    data.pop("blocage", None)
    data["echecs_clear"] = []
    save(client, data)


def reset_failures(client: str) -> None:
    """Init verte : plus d'échec du clear en cours."""
    data = load(client)
    if data.get("echecs_clear"):
        data["echecs_clear"] = []
        save(client, data)
