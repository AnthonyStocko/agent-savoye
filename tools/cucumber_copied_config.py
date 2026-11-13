"""
Mode « configuration copiée, sans purge » (option du profil client, désactivée par défaut) : les tests tournent dans
la configuration réelle du client, copiée de sa base dans le schéma de test des runs Jenkins.

Faits sur le job des runs (Jenkinsfile CUCUMBER_CUSTOMER) qui fondent ce mode :
- aucun paramètre ne choisit le slot : `lock(label: 'CUCUMBER_CUSTOMER', quantity: 1)` prend une ressource libre,
  le slot N est le suffixe de son nom et le schéma de l'application et des tests est <préfixe>_N ;
- la console l'annonce dès le début du build (« Build running on slot N »), avant le démarrage de l'application ;
- le conteneur d'init « dropschema » ne recrée que le schéma des tests du Standard (cucumber_spec_N), jamais le
  schéma client du slot : une copie restaurée avant le run y est encore au démarrage.

Déroulé :
1. option du profil (« config_copiee ») et schéma de test des runs (« schema_copie », vide = CUCUMBER_DB_SCHEMA) ;
2. étape 3 : copie de la base du client dans ce schéma (sauvegarde du schéma remplacé), puis instantané
   « référence » (pg_dump du schéma copié, dans le cache du client) avec date, source, version de l'application et
   codes de référence lus dans la copie ; sans référence valide, l'étape 4 reste bloquée ;
3. init : aucun clear, aucune purge, aucune initialisation globale ; seuls restent actifs les steps sans effet
   destructeur nécessaires aux tests (désactivation de jobs, branchement sur les services de l'environnement de
   test — ENV_CONNECTION_STEPS —, contrôles en lecture seule) — copied_feature ;
4. avant chaque run lancé par l'agent : restauration de la référence (pg_restore, journalisée), Jenkins libre
   (jenkins_tools.run_exclusive) ; après le démarrage, schéma du run lu dans la console : un autre schéma arrête
   le build (« le run n'utilise pas le schéma copié (schéma <N>) ») — RunGuard ;
5. changement de version de l'application : référence invalidée, nouvelle copie demandée avant tout run ;
6. bilan de chaque run : mode, date de la référence, schéma vérifié, restauration faite (bilan_line).

Garde-fous : lecture seule sur la base du client ; écriture seulement dans le schéma de test du profil, jamais
pendant un build ; mots de passe hors des journaux (db_restore). Aucune valeur propre à un client dans ce module.
"""
import os
import re
import time
from datetime import datetime

OPTION = "config_copiee"
SCHEMA_FIELD = "schema_copie"
REFERENCE_KEY = "reference_copie"
LABEL = "Configuration copiée, sans purge"
WARNING = "Les runs ne repartent pas d'un état propre : la copie est restaurée avant chaque run de vérification."
MARKER = "CONFIG-COPIEE:"
MODE = "configuration copiée"
# Codes de référence lus dans la copie : catégorie -> (table, colonne) du schéma de l'application, surchargeable par
# CUCUMBER_COPY_REFERENCE_TABLES (« categorie=table.colonne,… »)
REFERENCE_TABLES = {"sites": ("ref_site", "code"), "donneurs_ordres": ("ref_stock_owner", "code"),
                    "transporteurs": ("ref_carrier", "code"), "secteurs": ("wms_sector", "code"),
                    "emplacements": ("ref_location", "code"), "partenaires": ("exc_message_partner", "code")}
MAX_CODES = 200
PROMPT_CODES = 25
SLOT_WAIT_SECONDS = 240
_SLOT = re.compile(r"Build running on slot\s+(\d+)", re.I)
_SCHEMA_ARG = re.compile(r"bdd_schema\s*[=:]\s*['\"]?([\w$+-]+)", re.I)
_STEP = re.compile(r"^(\s*)(Given|When|Then|And|But|\*)\s+(.*?)\s*$")
_SCENARIO = re.compile(r"^\s*(Scenario|Scenario Outline|Background|Scénario|Contexte)\s*:", re.I)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
# Steps du Standard qui rebranchent l'application sur les services de l'environnement de test (adresses et comptes
# passés par le job Jenkins : -DurlCubing, -DurlTdm, -Durl…) sans toucher à la configuration métier du client. Gardés
# actifs en configuration copiée : sinon la copie garde les adresses de production du client (ex. précubage
# « http://precubing:80 » injoignable depuis le pod → OP en CUBING_ERROR, campagne de lancement refusée).
ENV_CONNECTION_STEPS = (re.compile(r"^INIT- Configuration du colisage$"),
                        re.compile(r'^INIT- Configuration du prestataire transport "[^"]*"$'),
                        re.compile(r'^MESS- Configuration du partenaire "[^"]*"$'))


# ---------------------------------------------------------------------------
# Option du profil
# ---------------------------------------------------------------------------

def active(profile: dict) -> bool:
    value = (profile or {}).get(OPTION)
    return value is True or str(value).strip().lower() in ("1", "true", "oui", "vrai")


def test_schema(profile: dict) -> str:
    """Schéma de test des runs Jenkins visé par la copie : champ du profil, à défaut CUCUMBER_DB_SCHEMA."""
    return ((profile or {}).get(SCHEMA_FIELD) or os.getenv("CUCUMBER_DB_SCHEMA", "")).strip()


def app_version(profile: dict) -> str:
    """Version de l'application testée (version du client, Standard, KIS), déduite de la version choisie."""
    deduction = ((profile or {}).get("version_client") or {}).get("deduction") or {}
    parts = [("client", deduction.get("version_client")),
             ("Standard", (deduction.get("standard") or {}).get("version")),
             ("KIS", (deduction.get("kis") or {}).get("kis") or (deduction.get("jenkins") or {}).get("kis_version"))]
    return " / ".join(f"{label} {value}" for label, value in parts if value)


def reference(profile: dict) -> dict:
    return dict((profile or {}).get(REFERENCE_KEY) or {})


def reference_problem(profile: dict) -> str:
    """'' si la copie de référence est utilisable par les runs ; sinon pourquoi (à refaire à l'étape 3)."""
    ref = reference(profile)
    schema = test_schema(profile)
    if not schema:
        return "schéma de test des runs non renseigné (profil client ou CUCUMBER_DB_SCHEMA)"
    if not ref:
        return "aucune copie de référence : faites la copie de l'étape 3"
    if ref.get("invalide"):
        return f"copie de référence invalidée ({ref['invalide']}) : nouvelle copie à faire à l'étape 3"
    if (ref.get("schema") or "").lower() != schema.lower():
        return (f"copie de référence faite dans {ref.get('schema') or '?'}, le schéma de test est {schema} : nouvelle "
                f"copie à faire à l'étape 3")
    current = app_version(profile)
    if current and ref.get("version") and ref["version"] != current:
        return (f"version de l'application changée ({ref['version']} → {current}) : nouvelle copie à faire à "
                f"l'étape 3")
    if not ref.get("dump") or not os.path.exists(ref["dump"]):
        return "instantané de référence introuvable dans le cache : nouvelle copie à faire à l'étape 3"
    return ""


def reference_dir(client_name: str) -> str:
    from tools.cucumber_tools import cache_dir
    return os.path.join(cache_dir(client_name), "reference_copie")


def save_option(client_name: str, enabled: bool, schema: str = "") -> dict:
    """Option et schéma enregistrés dans la saisie du profil client (les autres champs saisis sont gardés)."""
    from tools import cucumber_profile
    from tools.cucumber_tools import _write_json
    document = cucumber_profile.load_profile(client_name)
    saisi = dict(document.get("saisi") or {})
    saisi[OPTION] = bool(enabled)
    if schema.strip():
        saisi[SCHEMA_FIELD] = schema.strip()
    else:
        saisi.pop(SCHEMA_FIELD, None)
    document["saisi"] = saisi
    _write_json(cucumber_profile.profile_path(client_name), document)
    return document


def record_reference(client_name: str, entry: dict) -> dict:
    """Référence enregistrée au profil (profil.json, clé reference_copie) : {date, source, schema, version, dump,
    codes, sauvegarde}."""
    from tools import cucumber_profile
    from tools.cucumber_tools import _write_json
    document = cucumber_profile.load_profile(client_name)
    document[REFERENCE_KEY] = entry
    _write_json(cucumber_profile.profile_path(client_name), document)
    return entry


def invalidate(client_name: str, reason: str) -> bool:
    """Référence invalidée (changement de version…) : nouvelle copie demandée avant tout run. True si elle l'était."""
    from tools import cucumber_profile
    from tools.cucumber_tools import _write_json
    document = cucumber_profile.load_profile(client_name)
    ref = document.get(REFERENCE_KEY)
    if not ref or ref.get("invalide"):
        return False
    ref["invalide"] = reason
    _write_json(cucumber_profile.profile_path(client_name), document)
    return True


def invalidate_on_version_change(client_name: str, profile: dict) -> str:
    """Version de l'application différente de celle de la référence : référence invalidée ; renvoie la raison."""
    ref = reference(profile)
    current = app_version(profile)
    if not (active(profile) and ref and current and ref.get("version") and ref["version"] != current):
        return ""
    reason = f"version de l'application changée ({ref['version']} → {current})"
    return reason if invalidate(client_name, reason) else ""


# ---------------------------------------------------------------------------
# Codes de référence lus dans la copie
# ---------------------------------------------------------------------------

def reference_tables() -> dict:
    configured = os.getenv("CUCUMBER_COPY_REFERENCE_TABLES", "").strip()
    if not configured:
        return dict(REFERENCE_TABLES)
    out = {}
    for item in configured.split(","):
        category, _, target = item.partition("=")
        table, _, column = target.strip().partition(".")
        if category.strip() and _IDENTIFIER.match(table) and _IDENTIFIER.match(column or "code"):
            out[category.strip()] = (table, column or "code")
    return out


def read_codes(schema: str, query) -> dict:
    """
    {catégorie: {"codes": [...], "total": n}} lus dans la copie (lecture seule) ; `query(sql, schema)` renvoie des
    lignes [{colonne: valeur}]. Une table absente est notée ({"erreur"}), jamais devinée.
    """
    out = {}
    for category, (table, column) in reference_tables().items():
        try:
            rows = query(f"SELECT {column} AS code FROM {table} WHERE {column} IS NOT NULL ORDER BY {column}", schema)
        except Exception as e:  # noqa: BLE001 - table absente de cette version : catégorie non lue
            out[category] = {"codes": [], "total": 0, "erreur": str(e)[:150]}
            continue
        codes = [str(r.get("code")).strip() for r in rows if str(r.get("code") or "").strip()]
        out[category] = {"codes": codes[:MAX_CODES], "total": len(codes)}
    return out


def prompt_block(profile: dict) -> str:
    """Bloc des prompts (lots, init, corrections) en mode configuration copiée ; '' si l'option est inactive."""
    if not active(profile):
        return ""
    ref = reference(profile)
    lines = ["=== CONFIGURATION COPIÉE DU CLIENT (sans purge) ===",
             f"Les tests tournent dans la configuration réelle du client, copiée de sa base dans le schéma "
             f"{test_schema(profile) or '?'} (référence du {(ref.get('date') or '?')[:16].replace('T', ' ')}, "
             f"version {ref.get('version') or '?'}) et restaurée avant chaque run.",
             "- Aucun clear, aucune purge, aucune initialisation globale par fichiers : la configuration vient de la "
             "copie. N'en écris jamais.",
             "- Codes de référence (sites, donneurs d'ordres, transporteurs, emplacements, échanges…) : UNIQUEMENT ceux "
             "présents dans la copie ci-dessous ; jamais les codes de l'init du Standard s'ils n'y sont pas.",
             "- Avant de créer une donnée de configuration (échange, document, règle, paramètre, imprimante, "
             "planification…) : cherche celle du client dans la copie (requête en lecture seule) et RÉUTILISE-la "
             "(ex. échange déjà lié au document). Ne crée que ce qui n'existe vraiment pas, et justifie-le dans un "
             "commentaire de la feature (table lue, résultat vide).",
             "- Création d'une donnée de configuration : step idempotent qui relit avec le même critère que la création "
             "(code ET site), attend l'enregistrement s'il est asynchrone et, en cas d'échec, remonte la réponse du "
             "serveur (code HTTP, message) — jamais d'échec silencieux.",
             "- Données propres à chaque test : créées par le test, avec des codes uniques au test, en création "
             "idempotente (rejouable sur une copie qui contient déjà la donnée). Fichiers GS1 (WOI…) : destinataire "
             "(receiver) avec son code (additionalPartyIdentification) unique au test, sinon intégration en erreur "
             "« La propriété code est requise »."]
    codes = ref.get("codes") or {}
    for category, found in codes.items():
        values = found.get("codes") or []
        if found.get("erreur"):
            lines.append(f"  - {category} : non lus ({found['erreur'][:80]})")
        elif values:
            more = f", … ({found.get('total', len(values)) - PROMPT_CODES} autres)" \
                if found.get("total", len(values)) > PROMPT_CODES else ""
            lines.append(f"  - {category} : {', '.join(values[:PROMPT_CODES])}{more}")
        else:
            lines.append(f"  - {category} : aucun dans la copie")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Init en mode configuration copiée
# ---------------------------------------------------------------------------

def _connects_test_environment(step: str) -> bool:
    """Step qui branche l'application sur les services de l'environnement de test (ENV_CONNECTION_STEPS)."""
    return any(pattern.match(step.strip()) for pattern in ENV_CONNECTION_STEPS)


def _is_kept(step: str, scenario: str, client_check: str = "") -> bool:
    from tools import cucumber_jobs
    if cucumber_jobs.step_key(step) in (cucumber_jobs.ONE_OFF, cucumber_jobs.ALL_OFF):
        return True
    if _connects_test_environment(step):
        return True
    if client_check and step.startswith(client_check):
        return True
    return "lecture seule" in scenario.lower() and "contr" in scenario.lower()


def copied_feature(text: str, client_name: str = "") -> tuple:
    """
    Feature d'init en mode configuration copiée : seuls restent actifs les steps sans effet destructeur
    nécessaires aux tests — désactivation de jobs (sauf ceux que l'init créait, la création étant retirée),
    branchement sur les services de l'environnement de test (ENV_CONNECTION_STEPS) et contrôles en lecture seule
    de l'environnement ; tout le reste (purge, clear, initialisation globale, fichiers, créations, paramétrages,
    recréations) est commenté sous « # CONFIG-COPIEE: », tables comprises. Renvoie
    (texte, nombre de steps commentés).
    """
    from tools import cucumber_environment, cucumber_jobs
    check = cucumber_environment.check_expression(client_name).split("{", 1)[0].strip() if client_name else ""
    created = {u["nom"] for u in cucumber_jobs.usages(text or "") if u["step"] == "KIS- Création du job" and u["nom"]}
    lines = (text or "").splitlines(keepends=True)
    out, scenario, commented, i = [], "", 0, 0
    while i < len(lines):
        line = lines[i]
        if _SCENARIO.match(line):
            scenario = line
        match = _STEP.match(line.rstrip("\r\n"))
        if not match or line.lstrip().startswith("#"):
            out.append(line)
            i += 1
            continue
        step = match.group(3)
        block = [line]
        i += 1
        while i < len(lines) and lines[i].strip().startswith(("|", "#", '"""')):
            block.append(lines[i])
            i += 1
        keep = _is_kept(step, scenario, check)
        if keep and created and cucumber_jobs.step_key(step) == cucumber_jobs.ONE_OFF:
            # Désactivation d'un job que l'init créait : la création est retirée, la ligne aussi
            block, header = _drop_rows(block, created)
            keep = header
        if keep:
            out += block
            continue
        commented += 1
        indent = match.group(1)
        eol = "\r\n" if line.endswith("\r\n") else "\n"
        out.append(f"{indent}# {MARKER} configuration copiée du client (aucune purge ni initialisation)" + eol)
        out += [f"{indent}# {b.lstrip()}" if b.strip() and not b.lstrip().startswith("#") else b for b in block]
    return "".join(out), commented


def _drop_rows(block: list, created: set) -> tuple:
    """Lignes de table des jobs créés par l'init commentées ; (bloc, reste-t-il au moins une ligne de job)."""
    out, header, kept = [block[0]], None, False
    for line in block[1:]:
        if not line.strip().startswith("|"):
            out.append(line)
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
            out.append(line)
            continue
        row = dict(zip(header, cells))
        if row.get("name") in created:
            out.append(line[:len(line) - len(line.lstrip())] + f"# {line.lstrip()}")
        else:
            out.append(line)
            kept = True
    return out, kept


# ---------------------------------------------------------------------------
# Schéma réellement utilisé par un run
# ---------------------------------------------------------------------------

def schema_for_slot(slot: str, schema: str) -> str:
    """Schéma du slot `slot`, sur le modèle du schéma de test (suffixe numérique remplacé)."""
    return re.sub(r"\d+$", slot, schema) if re.search(r"\d+$", schema or "") else ""


def run_schema(console: str, schema: str) -> tuple:
    """(schéma du run, preuve) lus dans la console : slot alloué, à défaut argument bdd_schema ; ('', '') sinon."""
    slot = _SLOT.search(console or "")
    if slot and schema_for_slot(slot.group(1), schema):
        return schema_for_slot(slot.group(1), schema), slot.group(0)
    argument = _SCHEMA_ARG.search(console or "")
    if argument:
        return argument.group(1).replace("+", ""), argument.group(0)
    return "", ""


class RunGuard:
    """
    Garde d'un run lancé par l'agent en mode configuration copiée (jenkins_tools.run_exclusive) : `prepare()` restaure
    la copie de référence (Jenkins déjà libre), `check(build_url)` vérifie le schéma du run dès son démarrage et
    arrête le build s'il en utilise un autre. `info` alimente le bilan du run.
    """

    def __init__(self, client_name: str, profile: dict, log=print, restore=None, console=None, stop=None,
                 wait_s: int = SLOT_WAIT_SECONDS, poll_s: int = 5):
        self.client, self.profile, self.log = client_name, profile, log
        self.schema = test_schema(profile)
        self.ref = reference(profile)
        self._restore, self._console, self._stop = restore, console, stop
        self.wait_s, self.poll_s = wait_s, poll_s
        self.info = {"mode": MODE, "reference": self.ref.get("date", ""), "schema": self.schema,
                     "restauration": False, "schema_verifie": ""}

    def prepare(self) -> str:
        problem = reference_problem(self.profile)
        if problem:
            # Version de l'application changée : référence invalidée (nouvelle copie demandée avant tout run)
            invalidate_on_version_change(self.client, self.profile)
            self.log(f"[CONFIG COPIÉE] ⛔ Run non lancé : {problem}.")
            return f"mode configuration copiée : {problem}"
        restore = self._restore or _restore_reference
        entry = restore(self.client, self.ref["dump"], self.schema, lambda m: self.log(f"[CONFIG COPIÉE] {m}"))
        if not entry.get("ok"):
            return f"restauration de la copie de référence dans {self.schema} impossible : {entry.get('message', '')}"
        self.info.update(restauration=True, restauration_date=entry.get("date", ""),
                         restauration_duree_s=entry.get("duree_s"))
        self.log(f"[CONFIG COPIÉE] Copie de référence du {self.ref.get('date', '?')[:16]} restaurée dans "
                 f"{self.schema} ({entry.get('duree_s', '?')} s).")
        return ""

    def check(self, build_url: str) -> str:
        """'' si le run utilise le schéma copié ; sinon le build est arrêté et le message est renvoyé."""
        from tools import jenkins_tools
        read = self._console or (lambda url: jenkins_tools.build_bytes(url, "consoleText").decode("utf-8", "replace"))
        deadline = time.time() + self.wait_s
        found, proof = "", ""
        while True:
            try:
                found, proof = run_schema(read(build_url), self.schema)
            except Exception as e:  # noqa: BLE001 - console momentanément illisible : nouvel essai
                self.log(f"[CONFIG COPIÉE] Console du build illisible ({type(e).__name__}).")
            if found or time.time() >= deadline:
                break
            time.sleep(self.poll_s)
        if found and found.lower() == self.schema.lower():
            self.info["schema_verifie"] = found
            self.log(f"[CONFIG COPIÉE] Schéma du run vérifié : {found} (console : « {proof} »).")
            return ""
        message = (f"le run n'utilise pas le schéma copié (schéma {found})" if found else
                   f"le run n'utilise pas le schéma copié (schéma non lu dans la console après {self.wait_s} s)")
        (self._stop or jenkins_tools.stop_build)(build_url)
        self.info["schema_verifie"] = found or "non lu"
        self.info["arret"] = message
        self.log(f"[CONFIG COPIÉE] ⛔ {message} : build arrêté, lot non jugé.")
        return message


def _restore_reference(client_name: str, dump: str, schema: str, log) -> dict:
    from tools import db_restore
    return db_restore.restore_reference(client_name, dump, schema, log)


def run_guard(client_name: str, profile: dict = None, log=print):
    """Garde des runs du client en mode configuration copiée ; None si l'option est inactive."""
    if not client_name:
        return None
    if profile is None:
        from tools import cucumber_profile
        profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client_name))
    return RunGuard(client_name, profile, log) if active(profile) else None


def client_of(sources: dict = None, worktree: str = "") -> str:
    """Client d'un run : dépôt <client>-cucumber des paramètres, à défaut le dossier du cache du worktree."""
    depot = ((sources or {}).get("depot") or "").strip().lower()
    if depot:
        return re.sub(r"-cucumber$", "", depot.rsplit("/", 1)[-1])
    parts = os.path.normpath(worktree or "").split(os.sep)
    return parts[-2] if len(parts) >= 2 and "cucumber" in parts[:-2] else ""


def bilan_line(info: dict) -> str:
    """« Mode configuration copiée — référence du …, schéma vérifié …, restauration faite » ('' sans info)."""
    if not info:
        return ""
    when = (info.get("reference") or "?")[:16].replace("T", " ")
    verified = info.get("schema_verifie") or "non vérifié"
    restored = "restauration faite" if info.get("restauration") else "restauration non faite"
    return (f"Mode {info.get('mode', MODE)} — référence du {when}, schéma vérifié : {verified}, {restored}"
            + (f" — {info['arret']}" if info.get("arret") else ""))


def new_reference_entry(source: str, schema: str, version: str, dump: str, codes: dict, backup: str = "") -> dict:
    return {"date": datetime.now().isoformat(timespec="seconds"), "source": source, "schema": schema,
            "version": version, "dump": dump, "codes": codes, "sauvegarde": backup}
