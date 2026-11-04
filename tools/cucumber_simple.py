"""
Onglet « Cucumber » simplifié : 5 étapes l'une sous l'autre, chacune avec un état, une action principale et un
résultat en une ou deux phrases (0 token ici : ce module ne fait que calculer les états).

États : « a_faire », « en_cours », « ok », « echec », et « bloquee » quand l'étape précédente (obligatoire) n'est
pas OK. L'étape 3 (base de données) est OPTIONNELLE : le clear de l'init efface ce qu'elle recopie et le schéma des
runs Jenkins dépend du slot alloué (cucumber_customer_<slot>) ; elle ne bloque jamais l'étape 4.

Les résultats des étapes sont gardés dans etat.json (« simple » : {depot, base, init, tests}) par
record_step ; aucune valeur propre à un client n'est écrite en dur.
"""
from datetime import datetime

STATES = {"a_faire": "à faire", "en_cours": "en cours", "ok": "OK", "echec": "en échec", "bloquee": "bloquée"}
ICONS = {"a_faire": ":material/radio_button_unchecked:", "en_cours": ":material/hourglass_top:",
         "ok": ":material/check_circle:", "echec": ":material/error:", "bloquee": ":material/lock:"}
STEPS = ((1, "projet", "Projet"), (2, "depot", "Dépôt <client>-cucumber"), (3, "base", "Base de données"),
         (4, "init", "Initialisation"), (5, "tests", "Tests"))
OPTIONAL = {"base"}
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


def project_step(deduction: dict, ready: bool) -> dict:
    """Étape 1 : OK quand la version du Standard est déterminée pour la version du client choisie."""
    if not ready:
        return {"etat": "a_faire", "resultat": "Renseignez le projet Bitbucket du client."}
    line = version_line(deduction)
    if ((deduction or {}).get("standard") or {}).get("version"):
        return {"etat": "ok", "resultat": line + "."}
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
    out, previous_ok = [], True
    for number, key, title in STEPS:
        record = project if key == "projet" else (records or {}).get(key) or {}
        status = record.get("etat") or "a_faire"
        result = record.get("resultat") or ""
        if not previous_ok:
            status = "bloquee"
            result = "Se débloque quand l'étape précédente est OK."
        if key in OPTIONAL and not result:
            result = OPTIONAL_NOTE
        out.append({"numero": number, "cle": key, "titre": title, "etat": status, "resultat": result,
                    "optionnelle": key in OPTIONAL, "detail": record.get("detail") or {}})
        if key not in OPTIONAL:
            previous_ok = previous_ok and status == "ok"
    return out


def label(step: dict, client: str = "") -> str:
    """Titre affiché : « 2. Dépôt acme-cucumber — OK »."""
    title = step["titre"].replace("<client>", client or "<client>")
    return f"{step['numero']}. {title}" + (" (optionnelle)" if step["optionnelle"] else "") + \
        f" — {STATES[step['etat']]}"


def feature_choices(inventory: dict, profile: dict) -> list:
    """
    Fonctionnalités proposées à l'étape 5 : [{id, libelle « MODULE — titre métier »}], hors modules décochés,
    triées par module puis titre. Aucun compteur.
    """
    from tools import cucumber_coverage, cucumber_modules
    codes = cucumber_modules.codes(profile)
    out = []
    for spec in (inventory or {}).get("specifiques") or []:
        if cucumber_modules.task_excluded(spec, profile):
            continue
        found = sorted(cucumber_modules.task_modules(spec, codes)) if codes else []
        module = found[0] if found else cucumber_coverage.module_of(spec)
        out.append({"id": spec["id"], "module": module,
                    "libelle": f"{module} — {cucumber_coverage.business_title(spec)}"})
    return sorted(out, key=lambda f: (f["module"].lower(), f["libelle"].lower()))
