"""
Onglet « Cucumber » simplifié : 5 étapes l'une sous l'autre, chacune avec un état, une action principale et un
résultat en une ou deux phrases (0 token ici : ce module ne fait que calculer les états).

États : « a_faire », « en_cours », « ok », « echec », et « bloquee » quand l'étape précédente (obligatoire) n'est
pas OK. L'étape 3 (base de données) est OPTIONNELLE : le clear de l'init efface ce qu'elle recopie et le schéma des
runs Jenkins dépend du slot alloué (cucumber_customer_<slot>) ; elle ne bloque jamais l'étape 4.

Les résultats des étapes sont gardés dans etat.json (« simple » : {depot, base, init, tests}) par
record_step ; aucune valeur propre à un client n'est écrite en dur.
"""
import re
from datetime import datetime

STATES = {"a_faire": "à faire", "en_cours": "en cours", "ok": "OK", "echec": "en échec", "bloquee": "bloquée"}
ICONS = {"a_faire": ":material/radio_button_unchecked:", "en_cours": ":material/hourglass_top:",
         "ok": ":material/check_circle:", "echec": ":material/error:", "bloquee": ":material/lock:"}
STEPS = ((1, "projet", "Projet"), (2, "depot", "Dépôt <client>-cucumber"), (3, "base", "Base de données"),
         (4, "init", "Initialisation"), (5, "tests", "Tests"))
OPTIONAL = {"base"}
INIT_OFF_NOTE = ("Facultative : option « Initialisation » décochée (données du client déjà présentes, ex. recopiées à "
                 "l'étape 3) — l'étape 5 génère et vérifie les tests sans lancer l'init.")


def init_enabled(records: dict) -> bool:
    """Option « Initialisation » de l'onglet (etat.json simple.options.init) : cochée par défaut (projet neuf)."""
    return ((records or {}).get("options") or {}).get("init", True) is not False
OPTIONAL_NOTE = ("Optionnelle : recopie pour les essais locaux. Les runs Jenkins utilisent le schéma de leur slot "
                 "(cucumber_customer_<n>) et l'init commence par un clear qui efface cette configuration : l'étape "
                 "ne bloque pas l'initialisation.")


def record_step(state: dict, step: str, status: str, result: str, detail: dict = None) -> dict:
    """Résultat d'une étape ajouté à l'état (« simple ») : {etat, resultat, detail, date}."""
    entry = {"etat": status, "resultat": result, "detail": detail or {},
             "date": datetime.now().isoformat(timespec="seconds")}
    state.setdefault("simple", {})[step] = entry
    return entry


def version_line(deduction: dict) -> str:
    """« Version client v1.3 → Standard 26.24 » ('' si la version du client n'est pas choisie)."""
    client_version = (deduction or {}).get("version_client") or ""
    standard = ((deduction or {}).get("standard") or {}).get("version") or ""
    if not client_version:
        return ""
    return f"Version client {client_version} → Standard {standard or 'non déterminé'}"


# Prérequis des étapes suivantes, contrôlés dès l'étape 1 : (variables, message)
PREREQUISITES = (
    (("MAVEN_REPOSITORY_URL",), "MAVEN_REPOSITORY_URL (dépôt Nexus des versions publiées de la bibliothèque Cucumber, "
                                "lu à l'étape 2)"),
    (("MAVEN_REPOSITORY_USER", "MAVEN_REPOSITORY_PASSWORD"), "identifiants Nexus MAVEN_REPOSITORY_USER et "
                                                             "MAVEN_REPOSITORY_PASSWORD"),
    (("BITBUCKET_WRITE_TOKEN",), "BITBUCKET_WRITE_TOKEN (jeton réservé aux écritures sur <client>-cucumber ; "
                                 "BITBUCKET_HTTP_TOKEN reste en lecture)"),
)


def missing_prerequisites(env: dict = None) -> list:
    """Prérequis absents de la configuration (messages), lus dans `env` (défaut : variables d'environnement)."""
    import os
    env = os.environ if env is None else env
    return [message for names, message in PREREQUISITES if not all((env.get(n) or "").strip() for n in names)]


def project_step(deduction: dict, ready: bool, missing: list = ()) -> dict:
    """
    Étape 1 : OK quand la version du Standard est déterminée pour la version du client choisie et que les prérequis
    des étapes suivantes (`missing`, missing_prerequisites) sont renseignés.
    """
    if not ready:
        return {"etat": "a_faire", "resultat": "Renseignez le projet Bitbucket du client."}
    line = version_line(deduction)
    version = (deduction or {}).get("version_client", "")
    if ((deduction or {}).get("standard") or {}).get("version"):
        if missing:
            return {"etat": "echec", "version": version,
                    "resultat": f"{line}. À renseigner dans l'onglet Configuration avant l'étape 2 : "
                                + " ; ".join(missing) + "."}
        return {"etat": "ok", "resultat": line + ".", "version": version}
    if line:
        return {"etat": "echec", "resultat": f"{line} : la version du Standard n'a pas pu être déduite de cette "
                                             f"version du client."}
    return {"etat": "a_faire", "resultat": "Choisissez la version du client."}


def states(project: dict, records: dict) -> list:
    """
    États des 5 étapes : [{numero, cle, titre, etat, resultat, optionnelle, detail}]. `project` : état de
    l'étape 1 (project_step) ; `records` : etat.json « simple ». Une étape obligatoire non OK bloque les étapes
    obligatoires suivantes ; l'étape optionnelle est bloquée seulement si l'étape 1 ou 2 n'est pas OK.
    """
    from tools.cucumber_repo_setup import branch_version_line, version_line as line_of
    optional = OPTIONAL | ({"init"} if not init_enabled(records) else set())
    out, previous_ok = [], True
    wanted = line_of((project or {}).get("version", ""))
    depot_branch = (((records or {}).get("depot") or {}).get("detail") or {}).get("branche", "")
    for number, key, title in STEPS:
        record = project if key == "projet" else (records or {}).get(key) or {}
        status = record.get("etat") or "a_faire"
        result = record.get("resultat") or ""
        branch = (record.get("detail") or {}).get("branche", "")
        # Résultat obtenu pour une autre version du client (version changée depuis) : à refaire, jamais affiché
        # comme celui de la version choisie
        stale = ""
        if key == "depot" and branch and wanted and branch_version_line(branch) != wanted:
            stale = f"la branche {branch} n'est pas celle de la version {wanted}"
        elif key != "projet" and key != "depot" and branch and depot_branch and branch != depot_branch:
            stale = f"résultat obtenu sur la branche {branch}, la branche de la version est {depot_branch}"
        if stale and record.get("etat"):
            status, result = "a_faire", f"Version changée ({stale}) : relancez l'étape."
            record = {**record, "detail": {k: v for k, v in (record.get("detail") or {}).items() if k != "plan"}}
        if not previous_ok:
            status = "bloquee"
            result = "Se débloque quand l'étape précédente est OK."
        if key in OPTIONAL and not result:
            result = OPTIONAL_NOTE
        if key == "init" and key in optional and status != "bloquee":
            result = INIT_OFF_NOTE + (f" Dernier résultat : {result}" if result else "")
        out.append({"numero": number, "cle": key, "titre": title, "etat": status, "resultat": result,
                    "optionnelle": key in optional, "detail": record.get("detail") or {}})
        if key not in optional:
            previous_ok = previous_ok and status == "ok"
    return out


def label(step: dict, client: str = "") -> str:
    """Titre affiché : « 2. Dépôt acme-cucumber — OK »."""
    title = step["titre"].replace("<client>", client or "<client>")
    return f"{step['numero']}. {title}" + (" (optionnelle)" if step["optionnelle"] else "") + \
        f" — {STATES[step['etat']]}"


# Tickets de montée de version (épopées EPO « Montée de version 26.22 »…) : leurs commits touchent les fichiers de
# toutes les tâches ; ils ne décrivent aucune fonctionnalité et ne servent jamais de libellé
_UPGRADE = re.compile(r"mont[ée]es?\s+(de\s+)?versions?|\bMDV\b|\bupgrade\b|passage\s+en\s+(java|v?\d)", re.I)


def is_upgrade_ticket(ticket: dict) -> bool:
    return bool(_UPGRADE.search(ticket.get("titre") or ""))


def ticket_of(links: list) -> dict:
    """
    Ticket affiché pour une tâche : premier lien du projet SPEC, à défaut le premier lien ; jamais un ticket de
    montée de version.
    """
    links = [l for l in links or [] if l.get("cle") and not is_upgrade_ticket(l)]
    return next((l for l in links if l["cle"].upper().startswith("SPEC-")), links[0] if links else {})


def feature_choices(inventory: dict, profile: dict, links: dict = None) -> list:
    """
    Fonctionnalités proposées à l'étape 5 : [{id, ticket, libelle}], hors modules décochés. Libellé « SPEC-1234 —
    titre du ticket Jira (MODULE) » quand un ticket est lié à la tâche par une preuve (commit ou référence dans ses
    fichiers, cucumber_coverage.verify_ticket_links), à défaut le ticket rapproché par mots-clés à l'inventaire,
    marqué « ticket probable », sinon « MODULE — titre métier (aucun ticket lié) ». Tâches avec
    ticket d'abord, triées par clé ; puis les autres par module et titre.
    """
    from tools import cucumber_coverage, cucumber_modules
    codes = cucumber_modules.codes(profile)
    out = []
    for spec in (inventory or {}).get("specifiques") or []:
        if cucumber_modules.task_excluded(spec, profile):
            continue
        found = sorted(cucumber_modules.task_modules(spec, codes)) if codes else []
        module = found[0] if found else cucumber_coverage.module_of(spec)
        linked = [l for l in (links or {}).get(spec["id"]) or [] if l.get("cle") and not is_upgrade_ticket(l)]
        ticket = ticket_of(linked)
        others = [l["cle"] for l in linked if l["cle"] != ticket.get("cle")]
        probable = {} if ticket else ticket_of([{"cle": k, "titre": t} for k, t in
                                                cucumber_coverage.candidate_tickets(spec).items()])
        if ticket:
            label = f"{ticket['cle']} — {cucumber_coverage.strip_status(ticket.get('titre', ''))} ({module})"
        elif probable:
            # Rapprochement par mots-clés de l'inventaire, sans preuve : signalé comme tel
            ticket = probable
            label = f"{ticket['cle']} — {ticket.get('titre', '')} ({module}, ticket probable)"
        else:
            label = f"{module} — {cucumber_coverage.business_title(spec)} (aucun ticket lié)"
        out.append({"id": spec["id"], "module": module, "ticket": ticket.get("cle", ""), "libelle": label,
                    "titre": ticket.get("titre", ""), "probable": bool(probable), "autres": others})
    # Un ticket = une ligne : ses tâches (plusieurs dossiers ou processus d'une même fonctionnalité) regroupées,
    # générées ensemble (identifiant « a,b »)
    grouped, seen = [], {}
    for item in out:
        if not item["ticket"]:
            grouped.append(item)
            continue
        if item["ticket"] not in seen:
            seen[item["ticket"]] = {**item, "ids": [item["id"]], "modules": [item["module"]],
                                    "autres": list(item["autres"])}
            grouped.append(seen[item["ticket"]])
            continue
        entry = seen[item["ticket"]]
        entry["ids"].append(item["id"])
        entry["autres"] += [k for k in item["autres"] if k not in entry["autres"]]
        if item["module"] not in entry["modules"]:
            entry["modules"].append(item["module"])
        entry["probable"] = entry["probable"] and item["probable"]
    for entry in (e for e in grouped if e.get("ids")):
        entry["id"] = ",".join(entry["ids"])
        title = cucumber_coverage.strip_status(entry["titre"])
        extra = f", {len(entry['ids'])} tâches" if len(entry["ids"]) > 1 else ""
        also = f" ; aussi {', '.join(entry['autres'][:4])}" if entry.get("autres") else ""
        entry["libelle"] = (f"{entry['ticket']} — {title} ({', '.join(entry['modules'])}{extra}"
                            + (", ticket probable" if entry["probable"] else "") + also + ")")
    out = [{k: v for k, v in e.items() if k not in ("ids", "modules", "titre", "probable", "autres")}
           for e in grouped]

    def key(f: dict) -> tuple:
        number = re.search(r"-(\d+)$", f["ticket"])
        return (0, f["ticket"].split("-")[0], int(number.group(1)) if number else 0, "") if f["ticket"] else \
            (1, f["module"].lower(), 0, f["libelle"].lower())
    return sorted(out, key=key)


# ---------------------------------------------------------------------------
# « Détail technique » lisible (jamais un objet replié)
# ---------------------------------------------------------------------------

DETAIL_LABELS = (
    ("projet", "Projet Bitbucket"), ("depot", "Dépôt"), ("branche", "Branche"),
    ("branche_agent", "Branche de l'agent (seule branche où il écrit)"), ("patch", "Patch à appliquer"),
    ("commit", "Commit"),
    ("bibliotheque", "Bibliothèque Cucumber"), ("choix_bibliotheque", "Choix de la bibliothèque"),
    ("compilation", "Compilation"), ("clear", "Clear retenu"), ("preuves_clear", "Preuves du clear"),
    ("init", "Init"), ("tags", "Tags lancés"), ("build", "Build Jenkins"), ("builds", "Builds Jenkins"),
    ("standard_indisponible", "Init du Standard non lancée par le job (copie adaptée)"), ("scenarios", "Scénarios"),
    ("ecarts_standard", "Écarts avec l'init du Standard"), ("remarques", "Remarques"),
    ("sauvegarde", "Sauvegarde"), ("message", "Message"), ("erreur", "Erreur"), ("erreurs", "Erreurs"),
)
HIDDEN_DETAIL = {"plan", "fonctionnalites", "correction"}


def _text(value) -> str:
    if isinstance(value, dict):
        return " ; ".join(f"{k} : {_text(v)}" for k, v in value.items() if v not in ("", None, [], {}))
    if isinstance(value, (list, tuple)):
        return " ; ".join(_text(v) for v in value if v not in ("", None))
    return str(value)


def detail_lines(detail: dict) -> list:
    """
    Détail technique d'une étape en lignes lisibles : [(libellé, texte, liste)] ; `liste` : éléments affichés un
    par ligne (erreurs, remarques, preuves), sinon None. Ordre fixe des clés connues, puis les autres.
    """
    detail = {k: v for k, v in (detail or {}).items() if k not in HIDDEN_DETAIL and v not in ("", None, [], {})}
    labels = dict(DETAIL_LABELS)
    keys = [k for k, _ in DETAIL_LABELS if k in detail] + [k for k in detail if k not in labels]
    out = []
    for key in keys:
        value = detail[key]
        label = labels.get(key) or key.replace("_", " ").capitalize()
        if isinstance(value, (list, tuple)):
            out.append((label, f"{len(value)} élément(s)" if len(value) > 1 else _text(value),
                        [_text(v) for v in value] if len(value) > 1 else None))
        else:
            out.append((label, _text(value), None))
    return out


# ---------------------------------------------------------------------------
# Étape 5 : résultat d'une fonctionnalité générée puis vérifiée
# ---------------------------------------------------------------------------

def feature_result(state: dict, spec_id: str, label: str = "") -> dict:
    """
    Résultat de la dernière génération de `spec_id` (lots de etat.json) : {id, libelle, vert, resultat}. Vert :
    couvert, et chaque scénario écrit vérifié pertinent par exécution (jamais par la seule compilation).
    """
    lot = next((l for l in reversed((state or {}).get("lots") or []) if spec_id in (l.get("ids") or [])), None)
    out = {"id": spec_id, "libelle": label or spec_id, "vert": False}
    if not lot:
        return {**out, "resultat": "Aucune génération trouvée pour cette fonctionnalité."}
    result = next((r for r in lot.get("resultats") or [] if r.get("id") == spec_id), {})
    status = result.get("statut") or lot.get("statut") or ""
    if lot.get("statut") == "erreur_compilation":
        error = (lot.get("erreur") or "").strip()
        return {**out, "resultat": ("Tests écrits, mais la compilation échoue après toutes les corrections : rien n'a "
                                    "été vérifié." + (f" Erreur : {error}" if error else ""))[:400]}
    if status != "couvert":
        why = result.get("resume") or result.get("raison") or "aucun scénario écrit"
        return {**out, "resultat": f"Non couverte : {why}"[:400]}
    files = set(result.get("fichiers") or [])
    runs = [s for s in ((lot.get("execution") or {}).get("scenarios") or {}).values()
            if (s.get("id") or "").split("::", 1)[0] in files]
    if not runs:
        message = (lot.get("execution") or {}).get("message") or "aucune exécution"
        return {**out, "resultat": f"Scénarios écrits, non vérifiés : {message}"[:400]}
    passed = [s for s in runs if s.get("statut") == "pertinent"]
    if len(passed) == len(runs):
        build = next((s.get("build") for s in runs if s.get("build")), "")
        return {**out, "vert": True, "resultat": f"{len(runs)} scénario(s) vérifié(s) pertinent(s) par exécution"
                                                 + (f" ({build})" if build else "") + "."}
    first = next(s for s in runs if s.get("statut") != "pertinent")
    return {**out, "resultat": f"{len(passed)}/{len(runs)} scénario(s) vérifié(s) : {first.get('statut')} — "
                               f"{first.get('motif') or ''}"[:400]}


def lots_signature(state: dict, ids: str) -> str:
    """Empreinte des lots qui concernent `ids` (début de la génération, statuts, résultats) : inchangée = rien de neuf."""
    import json
    wanted = {i for i in (ids or "").split(",") if i}
    lots = [{k: lot.get(k) for k in ("numero", "statut", "resultats", "execution")}
            for lot in (state or {}).get("lots") or [] if wanted & set(lot.get("ids") or [])]
    return json.dumps({"debut": (state or {}).get("debut"), "lots": lots}, sort_keys=True, default=str)


def group_result(state: dict, ids: str, label: str = "") -> dict:
    """Résultat d'une ligne (ticket à plusieurs tâches « a,b ») : verte si toutes ses tâches le sont."""
    parts = [feature_result(state, i, label) for i in ids.split(",") if i]
    if len(parts) == 1:
        return {**parts[0], "id": ids}
    green = [p for p in parts if p["vert"]]
    first = next((p for p in parts if not p["vert"]), None)
    return {"id": ids, "libelle": label or ids, "vert": not first,
            "resultat": f"{len(green)}/{len(parts)} tâche(s) verte(s)" + (f" — {first['resultat']}" if first else ".")}


def merge_features(previous: list, current: list) -> list:
    """Lignes de l'étape 5 : les nouvelles remplacent les anciennes de même id, ordre d'arrivée conservé."""
    ids = {line["id"] for line in current}
    return [line for line in previous or [] if line.get("id") not in ids] + list(current)


# Environnement des runs Jenkins remonté à chaque run : schéma supprimé puis recréé (conteneur « dropschema »),
# slot alloué au build (« Build running on slot N ») ; lu dans les consoles des builds du client (0 token)
_REBUILT_MARKERS = (re.compile(r"\bdrop_?schema\b", re.I), re.compile(r"\bBuild running on slot \d+", re.I))


def rebuilt_environment(consoles: list) -> str:
    """Preuve qu'un run Jenkins remonte son environnement (premier indice trouvé dans les consoles) ; '' sinon."""
    for text in consoles or []:
        for marker in _REBUILT_MARKERS:
            found = marker.search(text or "")
            if found:
                return found.group(0)
    return ""


def no_init_warning(proof: str, db_target: str = "", db_schema: str = "") -> str:
    """
    Avertissement « sans init » sur un environnement remonté à chaque run : ce qui est lu dans la base de test n'est
    pas garanti dans le schéma du slot alloué au run ; propose de cocher l'initialisation. '' sans preuve.
    """
    if not proof:
        return ""
    where = f" ({db_target}{', schéma ' + db_schema if db_schema else ''})" if db_target or db_schema else ""
    return (f"Sans initialisation, sur un environnement remonté à chaque run (console Jenkins : « {proof} ») : les "
            f"données lues dans la base de test{where} ne sont PAS garanties dans l'environnement du run (schéma "
            f"cucumber_customer_<slot> recréé pour le build). Les tests qui s'appuient sur elles échoueront ou ne "
            f"prouveront rien : cochez « Initialisation de l'environnement » pour que l'init les crée à chaque run.")


def client_no_init_warning(client_name: str) -> str:
    """
    no_init_warning pour un client : consoles Jenkins de ses derniers builds (cache de vérification) et base de test
    lue par l'agent (profil « cucumber » de la connexion : cible et schéma, sans mot de passe).
    """
    import os
    from tools import db_tools
    from tools.cucumber_tools import cache_dir
    folder = os.path.join(cache_dir(client_name), "verification")
    names = [n for n in os.listdir(folder) if n.endswith("console.txt")] if os.path.isdir(folder) else []
    consoles = []
    for name in sorted(names, key=lambda n: -os.path.getmtime(os.path.join(folder, n)))[:5]:
        with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
            consoles.append(f.read(200_000))
    proof = rebuilt_environment(consoles)
    if not proof:
        return ""
    previous = db_tools.set_profile("cucumber")
    try:
        config = db_tools.get_db_config()
        configured = bool(config.get("host") or config.get("name"))
        target, schema = (db_tools.describe_db_target(), config.get("schema", "")) if configured else ("", "")
    finally:
        db_tools.set_profile(previous)
    return no_init_warning(proof, target, schema)
