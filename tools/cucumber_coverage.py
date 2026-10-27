"""
Vue « Couverture » de l'onglet Cucumber (parcours, étapes 2 et 3), 0 token.

Une ligne par spécifique de l'inventaire (tickets Jira en colonne), croisée avec :
- les scénarios écrits par l'agent (résultats des lots) et les features existantes du dépôt client rapprochées à
  l'inventaire ;
- le dernier résultat connu de chaque scénario : imports de rapports d'exécution (le plus récent l'emporte), à
  défaut la vérification par exécution des lots ;
- les scénarios désactivés du worktree (tag à revoir et commentaire de cause).

ÉTATS (STATES) : couvert vert, couvert en échec, couvert désactivé (cause), couvert jamais exécuté, partiel
(vérifie seulement une configuration ou la présence d'une donnée : rapprochement partiel ou scénarios « faibles »),
non couvert, testable avec informations à confirmer (questions), non testable aujourd'hui (blocage prouvé et sa
preuve), sans objet (aucun comportement propre, avec sa preuve). Un spécifique couvert indirectement prend l'état de
celui qui le couvre. Le résultat « non_couvert » d'un lot est jugé par cucumber_testability.assess (6 sources).
TESTABILITÉ (testable, à confirmer, non testable) : facteurs DÉTECTÉS, jamais saisis (code et personnalisation
Studio déployés, données recréables, steps disponibles), chacun oui / non / non déterminé avec sa preuve (dernier
run, init détectée, steps candidats, blocage prouvé) ; un facteur non déterminé ne bloque pas. Chaque tâche a un
titre métier (ticket Jira, sinon nom du processus traduit) et une phrase métier sur ce qu'il faut tester. Le risque
métier est proposé (nature et type du spécifique) et seul modifiable (surcharges enregistrées dans
.tmp/cucumber/<client>/couverture.json).
"""
import os
import re
from datetime import datetime

from tools import cucumber_audit, cucumber_import, cucumber_profile, cucumber_studio, cucumber_testability
from tools.cucumber_tools import (_git, _read_json, _read_text_file, _write_json, a_revoir_tag, cache_dir,
                                  git_repos, latest_results, load_state, studio_dependencies, worktree_dir)
from tools.cucumber_verify import NOT_TEST_STATUSES, all_verified, failure_category, scenario_id
from tools.cucumber_a_revoir import DISABLED_COMMENT
from tools.cucumber_decisions import PENDING_COMMENT

# Clé : (libellé, pastille, couleur Streamlit)
STATES = {
    "vert": ("couvert, vert", "🟢", "green"),
    "echec": ("couvert, en échec", "🔴", "red"),
    "desactive": ("couvert, désactivé", "🟠", "orange"),
    "non_execute": ("couvert, jamais exécuté", "🔵", "blue"),
    "partiel": ("partiel", "🟣", "violet"),
    "non_couvert": ("non couvert", "⚪", "gray"),
    "en_attente": ("couverte par une autre tâche, en attente de sa génération", "🔗", "blue"),
    "a_confirmer": ("testable, informations à confirmer", "🟡", "orange"),
    "non_testable": ("non testable aujourd'hui", "⚫", "gray"),
    "sans_objet": ("sans objet", "▫️", "gray"),
}
WRITTEN_STATES = ("vert", "echec", "desactive", "non_execute")
SELECTABLE_STATES = ("non_couvert", "partiel", "a_confirmer", "non_testable", "sans_objet")
RISKS = ("élevé", "moyen", "faible")
CRITERIA = {"code": "code spécifique déployé", "studio": "personnalisation Studio déployée",
            "donnees": "données recréables par l'init", "steps": "steps disponibles dans la bibliothèque"}
TESTABILITY = ("testable", "à confirmer", "non testable")
# Statuts de vérification des lots : vert, en échec, jamais exécuté
_GREEN = ("pertinent", "faible")
_NOT_RUN = ("non_verifie",)
_TICKET = re.compile(r"^##\s+([A-Z][A-Z0-9]+-\d+)\b", re.M)


def overrides_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "couverture.json")


def load_overrides(client_name: str) -> dict:
    """Surcharges de l'utilisateur : {id: {"risque", "testabilite": {critère: réponse}, "raison"}}."""
    return (_read_json(overrides_path(client_name), {}) or {}).get("surcharges", {})


def save_override(client_name: str, spec_id: str, values: dict) -> None:
    """Enregistre (ou retire, valeurs vides) la surcharge d'un spécifique."""
    data = _read_json(overrides_path(client_name), {}) or {}
    overrides = data.setdefault("surcharges", {})
    clean = {k: v for k, v in values.items() if v not in ("", None, {})}
    if clean:
        overrides[spec_id] = {**clean, "date": datetime.now().isoformat(timespec="seconds")}
    else:
        overrides.pop(spec_id, None)
    _write_json(overrides_path(client_name), data)


def spec_tickets(spec: dict) -> list:
    """Clés des tickets Jira rattachés au spécifique (titres « ## CLÉ — … » de son extrait Jira)."""
    path = spec.get("jira") or ""
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return list(dict.fromkeys(_TICKET.findall(f.read())))


def studio_files(spec: dict) -> list:
    """customized.xml du spécifique (personnalisations Studio)."""
    return [f for f in spec.get("fichiers") or [] if f["path"].rsplit("/", 1)[-1] == "customized.xml"]


def studio_pending(spec: dict) -> bool:
    """Comportements Studio pas encore calculés (inventaire antérieur à leur calcul) : relancer l'inventaire."""
    return any("nb_comportements_studio" not in f for f in studio_files(spec))


def module_of(spec: dict) -> str:
    """Module affiché : dépôt et premier dossier (processus : nom sans préfixe technique)."""
    folder = spec.get("dossier", "")
    if spec.get("nature") == "processus":
        return re.sub(r"^(spec|ref)-", "", folder.rsplit("/", 1)[-1])
    return folder.split("/")[0] or spec.get("depot", "")


def entry_point(spec: dict) -> str:
    """Point d'entrée : écrans Studio, endpoints, sinon nature du spécifique."""
    counts = cucumber_studio.behaviour_counts(spec)
    if counts.get("écran"):
        return f"processus interactif ({counts['écran']} écran(s))"
    endpoints = (spec.get("endpoints_client") or []) + (spec.get("endpoints_std") or [])
    if endpoints:
        first = endpoints[0]
        return f"{first.get('methode', '')} {first.get('chemin', '')}".strip() + (
            f" (+{len(endpoints) - 1})" if len(endpoints) > 1 else "")
    if counts:
        return "processus Studio"
    return spec.get("nature", "")


def proposed_risk(spec: dict) -> str:
    """Risque métier proposé : libellés et paramétrage faibles, override et processus élevés, sinon moyen."""
    if spec.get("nature") in ("libellés", "scripts SQL") or spec.get("type") == "parametrage":
        return "faible"
    if spec.get("type") == "override" or spec.get("nature") == "processus":
        return "élevé"
    return "moyen"


def effort(spec: dict) -> int:
    """Effort estimé (plus petit = plus simple) : fichiers, sans test Standard ni step candidat, comportements."""
    behaviours = sum(cucumber_studio.behaviour_counts(spec).values())
    return (min(spec.get("nb_fichiers", 0), 20) + (0 if spec.get("tests_std") else 5)
            + (0 if spec.get("steps_candidats") else 3) + min(behaviours // 10, 5))


def disabled_scenarios(worktree: str) -> dict:
    """Scénarios désactivés des features du worktree : {id: cause} (commentaire « # À revoir : » au-dessus)."""
    tag, found = a_revoir_tag(), {}
    if not worktree or not os.path.isdir(worktree):
        return found
    for root, dirs, files in os.walk(worktree):
        dirs[:] = [d for d in dirs if d not in (".git", "target", "node_modules")]
        for name in files:
            if not name.endswith(".feature"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            if tag in text:
                rel = os.path.relpath(path, worktree).replace("\\", "/")
                found.update(_disabled_in(text, rel, tag))
    return found


def _disabled_in(text: str, rel: str, tag: str) -> dict:
    """Scénarios d'une feature précédés du tag à revoir : {id: cause}."""
    found, cause, tagged = {}, "", False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(DISABLED_COMMENT):
            cause = stripped[len(DISABLED_COMMENT):].strip()
        elif stripped.startswith(PENDING_COMMENT):
            cause = "décision en attente : " + stripped[len(PENDING_COMMENT):].strip()
        elif stripped.startswith("@"):
            tagged = tagged or tag in stripped.split()
        elif re.match(r"^(Scenario|Scénario|Scenario Outline|Plan du [Ss]cénario|Example|Exemple)\s*:", stripped):
            if tagged:
                found[scenario_id(rel, stripped.split(":", 1)[1])] = cause or "cause non précisée"
            cause, tagged = "", False
        elif stripped and not stripped.startswith("#"):
            cause, tagged = "", False
    return found


def import_results(client_name: str) -> dict:
    """Dernier résultat de chaque scénario dans les imports analysés : {id: {statut, nature, motif, import, date}}."""
    results = {}
    index = cucumber_import.load_index(client_name)
    for entry in sorted(index.get("imports", []), key=lambda e: e["numero"]):
        analysis = _read_json(os.path.join(cucumber_import.imports_dir(client_name), str(entry["numero"]),
                                           "analyse.json"), {}) or {}
        # Run non analysé ou initialisation en échec : aucun scénario n'y est jugé
        if analysis.get("non_analyse") or analysis.get("init_en_echec"):
            continue
        for identifier, r in (analysis.get("resultats") or {}).items():
            results[identifier] = {"statut": r.get("statut", ""), "nature": r.get("nature", ""),
                                   "motif": r.get("message", ""), "import": entry["numero"],
                                   "date": entry.get("date", "")}
    return results


def verified_results(state: dict) -> dict:
    """Statut de vérification des scénarios des lots : {id: {statut, motif, lot}}."""
    return {item["id"]: {"statut": item.get("statut", ""), "motif": item.get("motif", ""), "lot": item["lot"]}
            for item in all_verified(state) if item.get("id")}


def scenario_status(identifier: str, sources: dict) -> dict:
    """
    État d'un scénario : {id, etat (vert | echec | desactive | non_execute), faible, cause, nature, derniere,
    audit, audit_motifs}. Ordre : désactivé dans le worktree, dernier import, vérification du lot ; verdict du
    dernier audit joint.
    """
    audited = (sources.get("audit") or {}).get(identifier) or {}
    out = {"id": identifier, "etat": "non_execute", "faible": False, "cause": "", "nature": "", "derniere": "",
           "audit": audited.get("verdict", ""), "audit_motifs": audited.get("motifs") or []}
    if identifier in sources["desactives"]:
        return {**out, "etat": "desactive", "cause": sources["desactives"][identifier]}
    imported = sources["imports"].get(identifier)
    if imported:
        passed = imported["statut"] == "passe"
        return {**out, "etat": "vert" if passed else "echec", "cause": "" if passed else imported["motif"],
                "nature": imported["nature"], "derniere": f"import n° {imported['import']} du "
                                                          f"{imported['date'][:10]}"}
    verified = sources["verifies"].get(identifier)
    if verified and verified["statut"] not in _NOT_RUN:
        green = verified["statut"] in _GREEN
        return {**out, "etat": "vert" if green else "echec", "faible": verified["statut"] == "faible",
                "cause": "" if green else verified["motif"], "nature": verified["statut"],
                "derniere": f"vérification du lot {verified['lot']}"}
    return out


def linked_scenarios(spec: dict, result: dict, sources: dict) -> list:
    """Scénarios liés au spécifique : écrits par l'agent, puis scénarios importés des features existantes liées."""
    ids = [scenario_id(s.get("fichier", ""), str(s.get("nom", "")).rsplit(" / ", 1)[-1])
           for s in result.get("scenarios") or [] if isinstance(s, dict) and s.get("fichier")]
    features = [entry.rsplit(" (", 1)[0] for entry in spec.get("scenarios_existants") or []]
    for identifier in list(sources["imports"]) + list(sources["desactives"]):
        if identifier.split("::", 1)[0] in features and identifier not in ids:
            ids.append(identifier)
    return [scenario_status(i, sources) for i in ids]


UNDETERMINED = "non déterminé"
ANSWERS = ("oui", "non", UNDETERMINED)
# Vocabulaire générique Odatio des noms de processus Studio et de classes (aucun nom propre à un client)
GLOSSARY = {"render": "rendu", "rendering": "rendu", "request": "demande", "reportdoc": "document", "report": "document",
            "doc": "document", "document": "document", "label": "étiquette", "print": "impression", "printing":
            "impression", "goodsin": "réception", "folder": "dossier", "controlandvalidate": "contrôle et validation",
            "control": "contrôle", "check": "contrôle", "validate": "validation", "validation": "validation",
            "identify": "identification", "identification": "identification", "repack": "reconditionnement",
            "repackhandlingunit": "reconditionnement d'UM", "repackcontent": "reconditionnement du contenu",
            "repackidentifiedcontent": "reconditionnement du contenu identifié", "handlingunit": "UM",
            "selectslot": "choix d'alvéole", "slot": "alvéole", "product": "produit", "create": "création",
            "createorupdate": "création ou mise à jour", "update": "mise à jour", "feedback": "retour",
            "logisticunitsite": "UL du site", "qty": "quantité", "preparation": "préparation", "dispatch": "expédition",
            "shipping": "expédition", "order": "ordre", "location": "emplacement", "stock": "stock",
            "replenishment": "réapprovisionnement", "inventory": "inventaire", "stocktaking": "inventaire",
            "packaging": "emballage", "cubing": "colisage", "message": "message", "partner": "partenaire",
            "production": "production", "cartography": "cartographie", "unit": "unité", "content": "contenu",
            "operationalorder": "ordre logistique", "destination": "destination", "merge": "fusion",
            "advice": "avis", "activity": "activité", "line": "ligne", "reintegration": "réintégration",
            "estimation": "estimation", "assembly": "assemblage", "transport": "transport", "rejection": "rejet",
            "invoice": "facture", "route": "routage", "routing": "routage", "rooting": "routage", "status": "statut",
            "type": "type", "custom": "personnalisé", "clearance": "dédouanement", "need": "besoin", "full": "complet",
            "pick": "prélèvement", "movement": "mouvement", "pack": "colis", "list": "liste", "public": "public",
            "api": "API", "identifier": "identifiant", "tray": "bac", "number": "numéro", "miniload": "miniload"}
# Segments techniques sans sens métier
_TECHNICAL_WORDS = {"ref", "spec", "wms", "tms", "oms", "exc", "listener", "event", "process", "interactive",
                    "studio", "by", "with", "to", "on", "of", "the", "impl", "service", "action", "actions", "core",
                    "java", "manager", "helper", "util", "utils", "from", "for", "and", "any"}
# Actions (début du titre) parmi les mots traduits
_ACTIONS = ("rendu", "création", "création ou mise à jour", "mise à jour", "contrôle", "validation",
            "contrôle et validation", "identification", "reconditionnement", "reconditionnement d'UM",
            "reconditionnement du contenu", "reconditionnement du contenu identifié", "impression", "choix d'alvéole",
            "réapprovisionnement", "inventaire", "fusion", "expédition", "préparation", "réception", "retour")


def split_words(token: str) -> list:
    """Mots accolés d'un nom (« dispatchadvice » → dispatch, advice) d'après le vocabulaire ; [token] sinon."""
    vocabulary = sorted(set(GLOSSARY) | _TECHNICAL_WORDS, key=len, reverse=True)
    if token in GLOSSARY:
        return [token]
    words, rest = [], token
    while rest:
        word = next((w for w in vocabulary if len(w) > 2 and rest.startswith(w)), "")
        if not word:
            return [token]
        words.append(word)
        rest = rest[len(word):]
    return words


_TICKET_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_STATUS_SUFFIX = re.compile(r"\s*\[[^\]]*\]\s*$")


def ticket_links_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "tickets_verifies.json")


def strip_status(summary: str) -> str:
    """Titre d'un ticket sans son statut Jira (« … [Analysée] » → « … »)."""
    return _STATUS_SUFFIX.sub("", " ".join((summary or "").replace("\xa0", " ").split()))


def candidate_tickets(spec: dict) -> dict:
    """Tickets de l'extrait Jira du spécifique (rapprochés par mots-clés, non vérifiés) : {clé: titre}."""
    path = spec.get("jira") or ""
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8", errors="replace") as f:
        return {m.group(1): strip_status(m.group(2))
                for m in re.finditer(r"^##\s+([A-Z][A-Z0-9]+-\d+)\s*[—–-]\s*(.+)$", f.read(), re.M)}


def verify_ticket_links(client_name: str, inventory: dict, client_root: str, tickets: list) -> dict:
    """
    Liens ticket ↔ tâche VÉRIFIÉS (0 token, lecture seule) : clé d'un ticket Jira connu dans le message d'un commit
    qui modifie les fichiers de la tâche, ou référence explicite dans l'un de ses fichiers ; le plus souvent lié
    d'abord, puis le plus ancien. Le rapprochement par mots-clés de l'inventaire n'en est pas un. {id: [{"cle", "titre", "preuve"}]}, écrit dans tickets_verifies.json.
    """
    known = {t["key"]: strip_status(t.get("summary", "")) for t in tickets or [] if t.get("key")}
    repos = git_repos(client_root)
    out = {}
    for spec in inventory.get("specifiques") or []:
        titles = {**candidate_tickets(spec), **known}
        found = {}
        by_repo = {}
        for item in spec.get("fichiers") or []:
            by_repo.setdefault(item.get("repo", ""), []).append(item.get("path", ""))
        count = {}
        for repo, paths in by_repo.items():
            if repo not in repos:
                continue
            log = _git(repos[repo], "log", "--reverse", "--format=%h%x1f%s%n%b%x1e", "--", *paths).stdout
            for entry in log.split("\x1e"):
                sha, _, message = entry.strip().partition("\x1f")
                for key in set(_TICKET_KEY.findall(message)):
                    found.setdefault(key, f"commit {sha} de {repo} qui modifie les fichiers de la tâche")
                    count[key] = count.get(key, 0) + 1
        for item in spec.get("fichiers") or []:
            local = item.get("local") or ""
            if local and os.path.isfile(local):
                for key in _TICKET_KEY.findall(_read_text_file(local)):
                    found.setdefault(key, f"référence dans {item.get('repo', '')}/{item.get('path', '')}")
                    count[key] = count.get(key, 0) + 1
        # Ticket le plus souvent lié aux fichiers de la tâche, puis le plus ancien (celui qui l'a créée)
        order = list(found)
        links = [{"cle": k, "titre": titles[k], "preuve": found[k]}
                 for k in sorted(order, key=lambda k: (-count.get(k, 0), order.index(k))) if titles.get(k)]
        if links:
            out[spec["id"]] = links
    _write_json(ticket_links_path(client_name), out)
    return out


def load_ticket_links(client_name: str) -> dict:
    return _read_json(ticket_links_path(client_name), {}) or {}


def business_title(spec: dict, links: list = None) -> str:
    """
    Titre métier d'une tâche : titre (sans statut Jira) d'un ticket dont le lien est VÉRIFIÉ (verify_ticket_links),
    sinon déduit du nom du processus ou du dossier (vocabulaire générique : « render-reportdoc-request » →
    « Rendu : document, demande »).
    """
    name = (spec.get("libelle") or spec.get("id") or "").split("·")[-1].strip()
    # Nature d'un élément Studio dans le titre : un événement, son déclencheur et leur processus se distinguent
    kind = next((label for token, label in (("listener", " (déclencheur)"), ("event", " (événement)"))
                 if re.search(rf"(^|[-_]){token}([-_]|$)", name.lower())), "")
    if links:
        return links[0]["titre"] + kind
    words = []
    for token in (w for t in re.split(r"[-_./\s]+", name.lower()) if t for w in split_words(t)):
        if token and token not in _TECHNICAL_WORDS:
            word = GLOSSARY.get(token, token)
            if word not in words:
                words.append(word)
    if not words:
        return name + kind
    action = next((w for w in words if w in _ACTIONS), "")
    others = [w for w in words if w != action]
    text = f"{action} : {', '.join(others)}" if action and others else action or ", ".join(others)
    return text[:1].upper() + text[1:] + kind


def business_sentence(spec: dict, state_key: str, evaluation: dict, cause: str = "", title: str = "") -> str:
    """
    Une phrase métier sur ce qu'il faut tester (résumé du lot, sinon déduite du type et des comportements Studio),
    ou sur ce qui bloque (non testable, sans objet). Jamais la raison technique recopiée.
    """
    if state_key == "non_testable":
        return (f"{cucumber_testability.BLOCKERS.get(evaluation.get('blocage'), 'Blocage prouvé')}. "
                f"Preuve : {evaluation.get('preuve') or cause or '—'}")
    if state_key == "sans_objet":
        return f"Aucun comportement propre au client à tester. Preuve : {evaluation.get('preuve') or cause or '—'}"
    if evaluation.get("resume"):
        return evaluation["resume"]
    title = (title or business_title(spec)).lower()
    counts = cucumber_studio.behaviour_counts(spec)
    if counts:
        return (f"Vérifier le comportement propre au client ({cucumber_studio.counts_line(counts)}) de la "
                f"personnalisation « {title} ».")
    if spec.get("type") == "override":
        return f"Vérifier ce que le client fait autrement que le Standard pour « {title} »."
    return f"Vérifier l'effet propre au client de « {title} » (statut, donnée enregistrée, message ou document)."


def testability(spec: dict, result: dict, scenarios: list, index: dict = None, profile: dict = None,
                init_state: str = "", title: str = "") -> dict:
    """
    Facteurs de testabilité DÉTECTÉS (jamais saisis), chacun (réponse, preuve) parmi oui / non / non déterminé :
    code et personnalisation Studio déployés (dernier run, blocage prouvé), données recréables (init détectée,
    dernier run), steps disponibles (steps candidats, scénario vert, blocage prouvé). Un facteur non déterminé ne
    bloque pas. Synthèse « testabilite » (testable, à confirmer, non testable), « raisons » et « evaluation »
    (cucumber_testability.assess, réponses proposées et questions précises).
    """
    evaluation = cucumber_testability.assess(result, index or {}, profile or {}, title or business_title(spec))
    green = any(s["etat"] == "vert" for s in scenarios)
    natures = {s["nature"] for s in scenarios}
    blocked = evaluation["etat"] == "non_testable"
    criteria = {}
    for key, applies in (("code", any(f["path"].endswith(".java") for f in spec.get("fichiers") or [])),
                         ("studio", bool(studio_files(spec)))):
        if applies:
            criteria[key] = (("non", evaluation["preuve"]) if blocked and evaluation["blocage"] == "non_deploye" else
                             ("non", "échec « déploiement » d'un scénario lié au dernier run")
                             if "deploiement" in natures else ("oui", "scénario lié vert au dernier run") if green else
                             (UNDETERMINED, "aucun scénario lié exécuté"))
    criteria["donnees"] = (("non", evaluation["preuve"]) if blocked and evaluation["blocage"] == "donnee_impossible"
                           else ("oui", "scénario lié vert au dernier run") if green else
                           (UNDETERMINED, "donnée de référence absente au dernier run : à créer dans le Background")
                           if "donnee_reference" in natures else
                           ("oui", "init du projet complète (détection)") if init_state == "complete" else
                           (UNDETERMINED, {"absente": "init du projet absente : lot d'initialisation proposé",
                                           "incomplete": "init du projet incomplète : compléments proposés"}.get(
                               init_state, "init du projet non encore détectée")))
    candidates = len(spec.get("steps_candidats") or []) + len(spec.get("tests_std") or [])
    criteria["steps"] = (("non", evaluation["preuve"]) if blocked and evaluation["blocage"] == "step_impossible" else
                         ("oui", "scénario lié vert au dernier run") if green else
                         ("oui", f"{candidates} step(s) ou test(s) Standard candidat(s)") if candidates else
                         (UNDETERMINED, "aucun step Standard candidat trouvé : steps à écrire dans le projet"))
    answers = [a for a, _ in criteria.values()]
    summary = ("non testable" if blocked or "non" in answers else
               "à confirmer" if evaluation["etat"] == "a_confirmer" else "testable")
    reasons = [f"{CRITERIA[k]} : {a}" + (f" ({r})" if r else "") for k, (a, r) in criteria.items() if a != "oui"]
    return {"criteres": criteria, "testabilite": summary, "raisons": reasons, "evaluation": evaluation}


def spec_state(spec: dict, result: dict, scenarios: list, testable: dict) -> tuple:
    """(état, cause) d'un spécifique à partir de ses scénarios, du résultat de son lot et de sa testabilité."""
    active = [s for s in scenarios if s["etat"] != "desactive"]
    if scenarios and not active:
        return "desactive", " ; ".join(dict.fromkeys(s["cause"] for s in scenarios))
    if active:
        failing = [s for s in active if s["etat"] == "echec"]
        if failing:
            return "echec", failing[0]["cause"]
        green = [s for s in active if s["etat"] == "vert"]
        if green and all(s["faible"] for s in green):
            return "partiel", "scénarios « faibles » : ils passent aussi sans le spécifique"
        # Audit (9i) : scénarios qui ne vérifient qu'une configuration ou la présence d'une donnée
        if all(s["faible"] or s.get("audit") == "partiel" for s in active):
            motifs = [m for s in active for m in s.get("audit_motifs") or []]
            return "partiel", "audit : " + (motifs[0] if motifs else "scénarios partiels")
        return ("vert", "") if green else ("non_execute", "")
    evaluation = testable.get("evaluation") or {}
    if result.get("statut") == "non_couvert":
        if evaluation.get("etat") == "non_testable":
            return "non_testable", cucumber_testability.blocker_line(evaluation)
        if evaluation.get("etat") == "sans_objet":
            return "sans_objet", evaluation["preuve"]
        if evaluation.get("etat") == "a_confirmer":
            return "a_confirmer", evaluation["resume"]
    if testable["testabilite"] == "non testable":
        return "non_testable", " ; ".join(r for r in testable["raisons"] if ": non" in r)
    if spec.get("couverture") == "partiel":
        return "partiel", "features existantes rapprochées en partie seulement"
    if spec.get("couverture") == "deja_couvert":
        return "non_execute", "features existantes du dépôt client, aucun résultat d'exécution connu"
    return "non_couvert", ""


def last_execution(scenarios: list) -> str:
    """« import n° 3 du 2026-10-26 : 4 vert(s), 1 échec(s) » ; '' si aucun scénario exécuté."""
    run = [s for s in scenarios if s["derniere"]]
    if not run:
        return ""
    latest = max(s["derniere"] for s in run)
    green = sum(1 for s in run if s["etat"] == "vert")
    return f"{latest} : {green} vert(s), {len(run) - green} échec(s)"


def coverage_sources(client_name: str, state: dict) -> dict:
    """Résultats croisés une fois pour toutes les lignes : imports, vérifications, scénarios désactivés."""
    return {"imports": import_results(client_name), "verifies": verified_results(state),
            "desactives": disabled_scenarios(worktree_dir(client_name)),
            "audit": cucumber_audit.load_audit(client_name).get("scenarios") or {}}


def init_state_of(client_name: str, profile: dict) -> str:
    """État détecté de l'init du projet (absente, incomplete, complete) ; '' sans détection."""
    from tools import cucumber_detection, cucumber_journey
    facts = cucumber_detection.load_detection(client_name).get("faits") or {}
    return cucumber_detection.init_status(facts, profile, client_name, cucumber_journey.last_analysis(client_name),
                                          cucumber_import.imports_dir(client_name))["etat"] if facts else ""


def coverage_rows(client_name: str, inventory: dict, state: dict = None, sources: dict = None) -> list:
    """Lignes de la vue Couverture, triées dans l'ordre de sélection proposé (selection_order)."""
    state = load_state(client_name) if state is None else state
    sources = coverage_sources(client_name, state) if sources is None else sources
    overrides, results = load_overrides(client_name), latest_results(state)
    index = cucumber_testability.load_index(client_name) if client_name else {}
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client_name)) if client_name else {}
    init_state = init_state_of(client_name, profile) if client_name else ""
    links = load_ticket_links(client_name) if client_name else {}
    specifics = {s["id"]: s for s in inventory.get("specifiques") or []}
    lots_of = {r.get("id"): lot["numero"] for lot in state.get("lots") or [] for r in lot.get("resultats") or []}
    rows = {}
    for spec in specifics.values():
        result, override = results.get(spec["id"], {}), overrides.get(spec["id"], {})
        scenarios = linked_scenarios(spec, result, sources)
        title = business_title(spec, links.get(spec["id"]))
        testable = testability(spec, result, scenarios, index, profile, init_state, title)
        state_key, cause = spec_state(spec, result, scenarios, testable)
        rows[spec["id"]] = {
            "id": spec["id"], "tache": spec["libelle"], "module": module_of(spec), "point_entree": entry_point(spec),
            "tickets": spec_tickets(spec), "risque": override.get("risque") or proposed_risk(spec),
            "risque_propose": proposed_risk(spec), "features": sorted({s["id"].split("::", 1)[0] for s in scenarios}),
            "etat": state_key, "cause": cause, "testabilite": testable["testabilite"],
            "criteres": testable["criteres"], "raisons": testable["raisons"], "derniere": last_execution(scenarios),
            "evaluation": testable["evaluation"], "titre": title, "tickets_verifies": links.get(spec["id"]) or [],
            "phrase": business_sentence(spec, state_key, testable["evaluation"], cause, title),
            "relance_inutile": useless_relaunch(spec, result, lots_of.get(spec["id"]), profile)
            if state_key in ("non_couvert", "partiel", "a_confirmer") else {},
            "scenarios": scenarios, "effort": effort(spec), "constats": result.get("constats") or [],
            "studio": cucumber_studio.spec_behaviours(spec), "studio_total": cucumber_studio.behaviour_counts(spec),
            "studio_a_calculer": studio_pending(spec),
            "couvert_par": "", "chaine": [], "chaine_titre": ""}
    propagate(rows, coverage_links(specifics, results))
    return selection_order(list(rows.values()))


def useless_relaunch(spec: dict, result: dict, lot: int, profile: dict) -> dict:
    """
    Dernier lot de la tâche sans rien écrit, entrées inchangées depuis (code, profil client, réponses : empreinte
    task_inputs) : {"lot", "raison"} — relancer donnerait le même résultat ; {} sinon (ou empreinte inconnue).
    """
    if not result or result.get("scenarios") or result.get("fichiers") or not result.get("entrees"):
        return {}
    if result["entrees"] != cucumber_testability.task_inputs(spec, profile):
        return {}
    return {"lot": lot, "raison": result.get("resume") or cucumber_testability.summary(result.get("raison", ""))}


def coverage_links(specifics: dict, results: dict) -> dict:
    """
    Couverture indirecte {id: (id de la tâche qui la couvre, origine)} : éléments Studio sans logique propre (XML,
    studio_dependencies), puis tâches déclarées « couvert_indirectement » par leur lot (statut déclaré gardé même
    si la tâche qui couvre n'est pas encore couverte).
    """
    links = {i: (d["couvert_par"], d["raison"]) for i, d in studio_dependencies(specifics).items() if d["couvert_par"]}
    for i, result in results.items():
        declared = result.get("statut_declare") or result.get("statut")
        if declared == "couvert_indirectement" and result.get("couvert_par") and i not in links:
            links[i] = (result["couvert_par"], "déclaré par le lot " + (result.get("raison") or "")[:120])
    return links


def resolve_chain(start: str, links: dict) -> tuple:
    """(chaîne [start, …, fin], cycle) : suit les liens de couverture indirecte jusqu'à une tâche sans lien."""
    chain = [start]
    while chain[-1] in links:
        target = links[chain[-1]][0]
        if target in chain:
            return chain + [target], True
        chain.append(target)
    return chain, False


def propagate(rows: dict, links: dict) -> None:
    """
    État des tâches couvertes indirectement, d'après la FIN de leur chaîne (qui doit avoir un vrai test) : test vert,
    en échec, désactivé, jamais exécuté ou partiel → même état (« couvert indirectement par … ») ; pas encore de test
    → « en_attente » (attend sa génération) ; non testable → même blocage ; cycle, ou chaîne sans aucun vrai test →
    « non_couvert », la tâche se teste elle-même. Une tâche qui a ses propres scénarios garde son état.
    """
    for spec_id, row in rows.items():
        if spec_id not in links or row["scenarios"]:
            continue
        chain, cycle = resolve_chain(spec_id, links)
        row["chaine"] = chain
        end = rows.get(chain[-1])
        names = " → ".join(rows[i]["titre"] if i in rows else i for i in chain)
        if cycle:
            row["etat"], row["cause"] = "non_couvert", f"cycle de couverture indirecte ({names}) : aucune tâche de " \
                                                       f"la chaîne n'a de vrai test, celle-ci se teste elle-même"
        elif end is None or end["etat"] == "sans_objet":
            row["etat"], row["cause"] = "non_couvert", f"la chaîne de couverture ({names}) n'aboutit à aucun vrai test"
        elif end["scenarios"]:
            row["etat"], row["cause"] = end["etat"], f"couvert indirectement par « {end['titre']} »"
            row["couvert_par"] = end["id"]
        elif end["etat"] == "non_testable":
            row["etat"], row["cause"] = "non_testable", f"dépend de « {end['titre']} » : {end['cause']}"
            row["couvert_par"] = end["id"]
        else:
            row["etat"], row["cause"] = "en_attente", f"couverte par « {end['titre']} », pas encore testée"
            row["couvert_par"] = end["id"]
        if row["etat"] != "non_couvert":
            row["chaine_titre"] = end["titre"]
            row["phrase"] = (f"Couverte par « {end['titre']} » : son test couvre aussi cette tâche."
                             + ("" if end["scenarios"] else " Générez d'abord celui-ci."))
            row["relance_inutile"] = {}


def substitute_dependents(rows: list, ids: list) -> tuple:
    """
    Tâches cochées qui dépendent d'une autre (en attente de sa génération) : (identifiants à générer, la tâche dont
    elles dépendent à leur place ; substitutions [(dépendante, tâche couvrante)]).
    """
    by_id = {r["id"]: r for r in rows}
    out, substitutions = [], []
    for i in ids:
        row = by_id.get(i) or {}
        target = row.get("couvert_par") if row.get("etat") == "en_attente" else ""
        if target:
            substitutions.append((i, target))
            i = target
        if i not in out:
            out.append(i)
    return out, substitutions


def selection_order(rows: list) -> list:
    """Ordre proposé : risque métier, puis testabilité, puis effort."""
    return sorted(rows, key=lambda r: (RISKS.index(r["risque"]) if r["risque"] in RISKS else 1,
                                       TESTABILITY.index(r["testabilite"]), r["effort"], r["tache"]))


def rates(rows: list) -> dict:
    """Couverture RÉELLE (actifs et verts) et ÉCRITE (actifs et désactivés), en % des tâches, et partielles."""
    total = len(rows)
    count = {key: sum(1 for r in rows if r["etat"] == key) for key in STATES}

    def pct(n):
        return round(100 * n / total) if total else 0
    return {"total": total, "par_etat": count, "reelle": pct(count["vert"]),
            "ecrite": pct(sum(count[k] for k in WRITTEN_STATES)), "partielles": count["partiel"],
            "couvertes": count["vert"]}


def state_label(row: dict) -> str:
    """Libellé affiché de l'état, avec sa pastille et sa cause (désactivé, non testable)."""
    label, badge, _ = STATES[row["etat"]]
    cause = row["cause"] if row["etat"] in ("desactive", "non_testable") and row["cause"] else ""
    return f"{badge} {label}" + (f" ({cause[:80]})" if cause else "")


def legend() -> str:
    """Légende des états (pastilles)."""
    return " · ".join(f"{badge} {label}" for label, badge, _ in STATES.values())


def partial_ids(rows: list) -> set:
    """Tâches « partielles » : leur lot doit tester l'EFFET, pas la configuration."""
    return {r["id"] for r in rows if r["etat"] == "partiel"}


def selection_warnings(rows: list, selected: list) -> list:
    """Avertissements d'une sélection : tâches non testables aujourd'hui, avec le blocage prouvé et sa preuve."""
    by_id = {r["id"]: r for r in rows}
    return [f"{by_id[i]['tache']} : non testable aujourd'hui — {by_id[i]['cause'] or 'blocage non précisé'}"
            for i in selected if i in by_id and by_id[i]["etat"] == "non_testable"]


def summary_line(rows: list) -> str:
    """« 12 tâches couvertes sur 67 (réelle 18 %, écrite 25 %), 4 partielle(s) »."""
    r = rates(rows)
    if not r["total"]:
        return "aucune tâche inventoriée"
    return (f"{r['couvertes']} tâche(s) couverte(s) et vertes sur {r['total']} (réelle {r['reelle']} %, écrite "
            f"{r['ecrite']} %), {r['partielles']} partielle(s)")


def failing_test_causes(rows: list) -> int:
    """Nombre de tâches en échec pour une cause « test » (à corriger par l'agent)."""
    def is_test(scenario):
        nature = scenario["nature"]
        return nature not in NOT_TEST_STATUSES and failure_category(nature) == "test"
    return sum(1 for r in rows if r["etat"] == "echec"
               and any(s["etat"] == "echec" and is_test(s) for s in r["scenarios"]))
