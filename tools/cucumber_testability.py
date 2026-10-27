"""
Testabilité d'une tâche Cucumber (0 token) : une tâche n'est jamais déclarée non testable par manque d'information
non cherchée.

ÉTATS : « testable » ; « testable, informations à confirmer » (il manque une donnée, une clé, une valeur ou un step
à identifier : la tâche est générée, les questions sont listées) ; « non testable aujourd'hui », réservé à un
BLOCAGE PROUVÉ hors du test (BLOCKERS, avec sa preuve) ; « sans objet » : aucun comportement propre à tester
(listener sans filtre, DTO, tâche technique, comportement déjà couvert par le Standard), avec sa preuve.

SOURCES, dans l'ordre (SOURCES) : profil client, features d'init du dépôt client, initializers et configurations du
client (valeurs extraites sans token), fichiers de données et features du dépôt client, Cucumber Standard aligné,
A_REVOIR.md / COUVERTURE.md / connaissances partagées. L'index des valeurs connues (known_values) les réunit :
.tmp/cucumber/<client>/valeurs_connues.json, écrit par la détection, et valeurs_connues.md pour les lots.

CONTRÔLE (assess) d'un résultat de lot « non_couvert » : un « non testable » sans blocage autorisé prouvé, sans
recherche citée dans chacune des 6 sources, ou dont une valeur dite manquante figure dans le projet, est rejeté.
La recherche tourne alors AVANT toute question (requests) : une valeur citée et trouvée dans le projet devient une
RÉPONSE PROPOSÉE avec sa source (à confirmer en un clic) ; une seule question précise porte sur ce qui reste
introuvable (donnée, scénario, candidats proches). Jamais de question générique ni de copie de la raison technique.
« à confirmer » tant qu'une question reste sans réponse, sinon « testable » (les réponses proposées se confirment
ou se corrigent en un clic, sans bloquer la génération).

RÉPONSES : chaque confirmation, correction ou réponse est enregistrée aussitôt dans le profil client (champ
« reponses », clé de la proposition ou de la question) ; elle est injectée dans tous les lots, et la même question
n'est jamais reposée.
"""
import json
import os
import re
import sys
from datetime import datetime

from tools import cucumber_init_data, cucumber_profile
from tools.cucumber_tools import (TECHNICAL_REASON, _read_json, _read_text_file, _write_json, cache_dir,
                                  coverage_path, latest_results, load_state, worktree_dir)

STATES = {"testable": "testable", "a_confirmer": "testable, informations à confirmer",
          "non_testable": "non testable aujourd'hui", "sans_objet": "sans objet (aucun comportement propre)"}
BLOCKERS = {"non_deploye": "code ou personnalisation non déployé",
            "defaut_produit": "défaut du produit prouvé",
            "donnee_impossible": "donnée impossible à créer par l'API et absente de l'environnement",
            "step_impossible": "step indispensable inexistant dans la bibliothèque et impossible à écrire"}
SOURCES = (("profil", "profil client"), ("init", "features d'init du dépôt client"),
           ("initializers", "initializers et configurations du client"),
           ("depot", "fichiers de données et features du dépôt client"), ("standard", "Cucumber Standard aligné"),
           ("documents", "A_REVOIR.md, COUVERTURE.md et connaissances partagées"))
# Valeurs indexées : codes (MAJUSCULES avec _ ou -), constantes, booléens d'une configuration
CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:[_-][A-Z0-9]+)+\b")
_STRING = re.compile(r"\"([^\"\n]{2,80})\"")
_BOOLEAN = re.compile(r"\b(\w+)\s*=\s*(true|false)\b")
_CONFIG_FILE = re.compile(r"(Initiali[sz]er|Config|Constant|Param|Settings)\w*\.java$", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
MAX_PLACES = 3


def index_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "valeurs_connues.json")


def retro_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "testabilite.json")


# ---------------------------------------------------------------------------
# Index des valeurs connues (6 sources)
# ---------------------------------------------------------------------------

def _add(index: dict, value: str, source: str, where: str, entity: str = "") -> None:
    value = (value or "").strip()
    if len(value) < 2:
        return
    places = index.setdefault(value, [])
    if len(places) < MAX_PLACES and not any(p["source"] == source and p["ou"] == where for p in places):
        places.append({"source": source, "ou": where, **({"entite": entity} if entity else {})})


def _scan_text(index: dict, text: str, source: str, rel: str, strings: bool = False) -> None:
    """Codes d'un texte (et littéraux / booléens d'un code Java si `strings`), avec leur ligne."""
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "*", "import ")):
            continue
        for value in CODE.findall(line):
            _add(index, value, source, f"{rel}:{n}")
        if strings:
            for value in _STRING.findall(line):
                _add(index, value, source, f"{rel}:{n}")
            for name, value in _BOOLEAN.findall(line):
                _add(index, f"{name}={value}", source, f"{rel}:{n}")
        elif "|" in stripped:
            for cell in (c.strip() for c in stripped.strip("|").split("|")):
                if cell and not re.search(r"[<$]", cell):
                    _add(index, cell, source, f"{rel}:{n}")


def _walk(root: str, suffixes: tuple) -> list:
    if not root or not os.path.isdir(root):
        return []
    return sorted(os.path.join(d, n) for d, _, names in os.walk(root) for n in names if n.endswith(suffixes))


def known_values(client_name: str, profile: dict, inventory: dict, worktree: str = "", std_features: str = "",
                 documents: list = ()) -> dict:
    """
    Index {valeur: [{source, ou}]} des 6 sources, dans l'ordre : profil, features d'init (et leurs fichiers de
    données), initializers et configurations (inventaire et copies des fichiers des spécifiques), fichiers et
    features du dépôt client (`worktree`, hors init), features du Standard aligné (`std_features`), documents
    (A_REVOIR.md, COUVERTURE.md, connaissances partagées). Une source absente est simplement vide.
    """
    index = {}
    for category, codes in (profile.get("reference_codes") or {}).items():
        for code in codes or []:
            _add(index, code, "profil", f"codes de référence ({category})")
    for item in profile.get("server_initialized_data") or []:
        for key in ("code", "expression"):
            _add(index, item.get(key, ""), "profil", f"données initialisées par le serveur ({item.get('classe', '?')})",
                 item.get("entite", ""))
    for item in profile.get("environment_checks") or []:
        _add(index, item.get("code", ""), "profil", "dépendances d'environnement")
    for answer in profile.get("reponses") or []:
        for value in CODE.findall(answer.get("reponse", "")):
            _add(index, value, "profil", f"réponse : {answer.get('question', '')[:60]}")
    init_paths = set(profile.get("init_features") or [])
    data_paths = set()
    for rel in sorted(init_paths):
        text = _read_text_file(os.path.join(worktree, *rel.split("/"))) if worktree else ""
        _scan_text(index, text, "init", rel)
        present = cucumber_init_data.repo_files(worktree) if worktree and text else set()
        for data in cucumber_init_data.init_data_files(rel, text, present) if text else []:
            data_paths.add(data)
            try:
                rows = cucumber_init_data.read_rows(os.path.join(worktree, *data.split("/")))
            except (OSError, ValueError):
                continue
            for row in rows:
                for cell in row:
                    _add(index, str(cell), "init", data)
    for initializer in inventory.get("initialiseurs") or []:
        for creation in initializer.get("creations") or []:
            for key in ("code", "expression"):
                _add(index, creation.get(key, ""), "initializers",
                     f"{initializer.get('depot', '?')}/{initializer.get('chemin', '?')}", creation.get("entite", ""))
    for spec in inventory.get("specifiques") or []:
        for item in spec.get("fichiers") or []:
            local = item.get("local") or ""
            if _CONFIG_FILE.search(item.get("path") or "") and os.path.isfile(local):
                _scan_text(index, _read_text_file(local), "initializers",
                           f"{item.get('repo', '')}/{item.get('path', '')}", strings=True)
    if worktree:
        root = os.path.join(worktree, *cucumber_init_data.FEATURES_ROOT.strip("/").split("/"))
        files = os.path.join(worktree, *cucumber_init_data.FILES_ROOT.strip("/").split("/"))
        for path in _walk(root, (".feature",)) + _walk(files, (".csv", ".xml", ".json", ".txt")):
            rel = os.path.relpath(path, worktree).replace(os.sep, "/")
            if rel not in init_paths and rel not in data_paths:
                _scan_text(index, _read_text_file(path), "depot", rel)
    for path in _walk(std_features, (".feature",)) if os.path.isdir(std_features or "") else (
            [std_features] if std_features and os.path.isfile(std_features) else []):
        _scan_text(index, _read_text_file(path), "standard", os.path.basename(path))
    for path in documents:
        if path and os.path.isfile(path):
            _scan_text(index, _read_text_file(path), "documents", os.path.basename(path))
    return index


def document_paths(client_name: str) -> list:
    """A_REVOIR.md du worktree, COUVERTURE (récapitulatif) et documents partagés du client."""
    paths = [os.path.join(worktree_dir(client_name), "A_REVOIR.md"), coverage_path(client_name)]
    try:
        from tools import knowledge_tools
        if knowledge_tools.enabled():
            folder = os.path.join(knowledge_tools.knowledge_dir(), knowledge_tools.safe_name(client_name))
            paths += _walk(folder, (".md",))
    except (ImportError, OSError):
        pass
    return paths


def save_index(client_name: str, index: dict) -> None:
    _write_json(index_path(client_name), {"date": datetime.now().isoformat(timespec="seconds"), "valeurs": index})


def load_index(client_name: str) -> dict:
    return (_read_json(index_path(client_name), {}) or {}).get("valeurs") or {}


def index_markdown(index: dict) -> str:
    """valeurs_connues.md des lots : une ligne par valeur, source et emplacement (Grep)."""
    labels = dict(SOURCES)
    lines = ["# Valeurs connues du projet (profil, init, initializers, dépôt client, Standard, documents)", ""]
    for value in sorted(index):
        lines.append(f"- {value} : " + " ; ".join(f"{labels.get(p['source'], p['source'])} ({p['ou']})"
                                                for p in index[value]))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Réponses de l'utilisateur
# ---------------------------------------------------------------------------

def question_key(question: str) -> str:
    """Clé normalisée d'une question : minuscules, mots seulement (même question → même clé)."""
    return " ".join(re.findall(r"\w+", (question or "").lower()))


def answers(profile: dict) -> dict:
    """{clé: réponse} des réponses enregistrées dans le profil (clé fournie, sinon clé de la question)."""
    return {a.get("cle") or question_key(a.get("question", "")): a for a in profile.get("reponses") or []
            if a.get("reponse")}


def save_answer(client_name: str, question: str, answer: str, task: str = "", key: str = "",
                status: str = "saisie") -> dict:
    """
    Réponse enregistrée dans le profil client (saisie), sans doublon de clé : `key` (réponse proposée) sinon la clé
    de la question. `status` : saisie, confirmee (réponse proposée confirmée), corrigee. Renvoie la réponse.
    """
    document = cucumber_profile.load_profile(client_name)
    saved = (document.setdefault("saisi", {}).get("reponses") or [])
    key = key or question_key(question)
    entry = {"cle": key, "question": question.strip(), "reponse": answer.strip(), "statut": status, "tache": task,
             "date": datetime.now().isoformat(timespec="seconds")}
    saved = [a for a in saved if (a.get("cle") or question_key(a.get("question", ""))) != key] + [entry]
    document["saisi"]["reponses"] = saved
    _write_json(cucumber_profile.profile_path(client_name), document)
    return entry


def answers_block(profile: dict) -> str:
    """Bloc du prompt de lot : réponses de l'utilisateur à appliquer, questions à ne jamais reposer."""
    saved = list(answers(profile).values())
    if not saved:
        return ""
    return ("=== RÉPONSES DE L'UTILISATEUR (à appliquer ; ne repose jamais ces questions) ===\n"
            + "\n".join(f"- {a['question']} → {a['reponse']}" for a in saved))


# ---------------------------------------------------------------------------
# Réponses proposées et questions précises (recherche AVANT toute question)
# ---------------------------------------------------------------------------

# Sources propres au projet : une valeur trouvée seulement dans le Standard n'est qu'un exemple d'usage
PROJECT_SOURCES = ("profil", "init", "initializers", "depot", "documents")
SOURCE_LABELS = {"profil": "profil client", "init": "init du projet", "initializers": "initializer",
                 "depot": "dépôt client", "standard": "Cucumber Standard", "documents": "documents du projet"}
_GENERIC_QUESTION = re.compile(r"(?i)\b(quelle?s?|quoi)\b.{0,40}\b(donn[ée]es?|cl[ée]s?|valeurs?|informations?)\b"
                               r".{0,40}\b(faut|manque|n[ée]cessaire|tester cette t[âa]che)")
_WORD = re.compile(r"\w{4,}")
# Référence à un ticket ou à un scénario (CLE-123, TOW-1037-4) : pas une donnée
_REFERENCE = re.compile(r"^[A-Z][A-Z0-9]*-\d+(?:-\d+)*$")
MAX_CANDIDATES = 5


def source_line(places: list) -> str:
    """« init du projet (fichier:67), initializer (dépôt/Classe.java) »."""
    return ", ".join(dict.fromkeys(f"{SOURCE_LABELS.get(p['source'], p['source'])} ({p['ou']})" for p in places))


def candidates(value: str, index: dict, limit: int = MAX_CANDIDATES) -> list:
    """Valeurs connues du projet proches de `value` : mêmes deux premiers segments, à défaut le même premier."""
    parts = re.split(r"[_-]", value)
    for size in (2, 1):
        prefix = "_".join(parts[:size])
        if len(parts) <= size or len(prefix) < 3:
            continue
        found = [v for v, places in index.items() if v != value and re.split(r"[_-]", v)[:size] == parts[:size]
                 and any(p["source"] in PROJECT_SOURCES for p in places)]
        if found:
            return sorted(found)[:limit]
    return []


def copies_reason(question: str, reason: str) -> bool:
    """Question générique, ou qui recopie la raison technique (70 % de ses mots s'y trouvent)."""
    if _GENERIC_QUESTION.search(question or ""):
        return True
    words = {w.lower() for w in _WORD.findall(question or "")}
    reason_words = {w.lower() for w in _WORD.findall(reason or "")}
    return len(words) >= 6 and len(words & reason_words) / len(words) >= 0.7


def requests(result: dict, index: dict, profile: dict = None, title: str = "") -> tuple:
    """
    Recherche dans les 6 sources AVANT toute question. (réponses proposées, questions) :
    - chaque valeur citée par la tâche et trouvée dans une source du projet devient une RÉPONSE PROPOSÉE avec sa
      source, regroupée par entité : {"cle", "libelle", "valeurs", "source", "reponse" (réponse enregistrée)} ;
    - une seule question précise pour les valeurs introuvables partout (donnée, scénario, candidats proches),
      plus les questions de l'agent qui ne sont ni génériques ni une copie de la raison :
      {"cle", "texte", "candidats", "reponse"}.
    Une valeur trouvée seulement dans le Standard est un exemple d'usage : ni proposée ni demandée.
    """
    known = answers(profile or {})
    reason = result.get("raison") or ""
    blocker = result.get("blocage") if isinstance(result.get("blocage"), dict) else {}
    asked = [q for q in result.get("questions") or [] if isinstance(q, str) and q.strip()]
    cited = [v for v in dict.fromkeys(CODE.findall(" ".join([reason, blocker.get("preuve") or "", *asked])))
             if not _REFERENCE.match(v)]
    groups = {}
    for value in cited:
        places = [p for p in index.get(value) or [] if p["source"] in PROJECT_SOURCES]
        if places:
            entity = next((p["entite"] for p in places if p.get("entite")), "")
            group = groups.setdefault(entity or places[0]["source"], {"entite": entity, "valeurs": [], "places": []})
            group["valeurs"].append(value)
            group["places"] += places
    proposals = []
    for key, group in groups.items():
        label = (f"{group['entite']} présent(s) sur l'environnement" if group["entite"] else
                 f"Valeur(s) connue(s) du projet ({SOURCE_LABELS.get(key, key)})")
        cle = question_key("proposition " + " ".join(sorted(group["valeurs"])))
        proposals.append({"cle": cle, "libelle": label, "valeurs": group["valeurs"],
                          "source": source_line(group["places"]), "reponse": known.get(cle)})
    questions = [{"cle": question_key(q), "texte": q, "candidats": [], "reponse": known.get(question_key(q))}
                 for q in asked if not copies_reason(q, reason)]
    # Valeur introuvable déjà visée par une question précise de l'agent : pas de seconde question
    unknown = [v for v in cited if v not in index and not any(v in q["texte"] for q in questions)]
    if unknown:
        shown = ", ".join(unknown[:5]) + (f" (+{len(unknown) - 5})" if len(unknown) > 5 else "")
        text = (f"Pour tester « {title or 'cette tâche'} » : {shown} "
                + ("est cité mais introuvable" if len(unknown) == 1 else "sont cités mais introuvables")
                + " dans le profil, l'init, les initializers, le dépôt client, le Standard et les documents du "
                  "projet. Quelle valeur utiliser sur l'environnement, ou faut-il créer la donnée dans le test ?")
        cle = question_key("introuvable " + " ".join(sorted(unknown)))
        questions.append({"cle": cle, "texte": text, "reponse": known.get(cle),
                          "candidats": list(dict.fromkeys(c for v in unknown for c in candidates(v, index)))
                          [:MAX_CANDIDATES]})
    return proposals, questions


def pending(evaluation: dict) -> int:
    """
    Réponses encore attendues : questions sans réponse. Une réponse proposée vaut réponse (sa source est connue) :
    elle se confirme ou se corrige, sans bloquer la génération.
    """
    return sum(1 for item in evaluation.get("questions") or [] if not item.get("reponse"))


# ---------------------------------------------------------------------------
# Contrôle d'un résultat de lot
# ---------------------------------------------------------------------------

def summary(text: str, sentences: int = 2, limit: int = 300) -> str:
    """Les premières phrases d'un texte, bornées."""
    parts = _SENTENCE.split(" ".join((text or "").split()))
    out = " ".join(parts[:sentences])
    return out if len(out) <= limit else out[:limit - 1] + "…"


def cited_sources(searches) -> set:
    """Sources citées par les recherches du lot ([{source, cherche, trouve}])."""
    keys = {k for k, _ in SOURCES}
    return {s.get("source") for s in searches or [] if isinstance(s, dict) and s.get("source") in keys
            and (s.get("cherche") or "").strip()}


def found_values(text: str, index: dict) -> dict:
    """Valeurs de type code citées dans `text` et présentes dans l'index : {valeur: [emplacements]}."""
    return {v: index[v] for v in dict.fromkeys(CODE.findall(text or "")) if v in index}


def assess(result: dict, index: dict, profile: dict = None, title: str = "") -> dict:
    """
    Testabilité d'un résultat de lot : {"etat", "resume", "blocage", "preuve", "proposees", "questions",
    "attendues", "trouvees", "recherches", "rejets", "raison"}. Un « non testable » n'est retenu qu'avec un blocage
    autorisé prouvé, les 6 sources citées et aucune valeur dite manquante présente dans l'index. Sinon la recherche
    dans les 6 sources donne des réponses proposées et des questions précises (requests) : « à confirmer » tant
    qu'une question reste sans réponse (pending), « testable » sinon. Jamais de question générique.
    """
    reason = result.get("raison") or ""
    out = {"etat": "testable", "resume": result.get("resume") or "", "blocage": "", "preuve": "", "proposees": [],
           "questions": [], "attendues": 0, "trouvees": {}, "recherches": result.get("recherches") or [],
           "rejets": [], "raison": reason}
    if result.get("statut") != "non_couvert":
        return out
    if result.get("tri") or reason == TECHNICAL_REASON:
        return {**out, "etat": "sans_objet", "preuve": reason}
    sans_objet = result.get("sans_objet") or {}
    if isinstance(sans_objet, dict) and (sans_objet.get("preuve") or "").strip():
        return {**out, "etat": "sans_objet", "preuve": sans_objet["preuve"].strip()}
    blocker = result.get("blocage") or {}
    kind = blocker.get("type", "") if isinstance(blocker, dict) else ""
    proof = (blocker.get("preuve") or "").strip() if isinstance(blocker, dict) else ""
    asked = [q for q in result.get("questions") or [] if isinstance(q, str)]
    found = found_values(" ".join([reason, proof, *asked]), index)
    rejections = []
    if kind not in BLOCKERS or not proof:
        rejections.append("aucun blocage prouvé (non déployé, défaut produit, donnée impossible à créer et absente, "
                          "step impossible) : une absence d'information n'est pas un blocage")
    missing = [label for key, label in SOURCES if key not in cited_sources(out["recherches"])]
    if missing:
        rejections.append(f"sources non citées : {', '.join(missing)}")
    project = {v: p for v, p in found.items() if any(x["source"] in PROJECT_SOURCES for x in p)}
    if project:
        rejections.append(f"{len(project)} valeur(s) dite(s) manquante(s) trouvée(s) dans le projet : "
                          f"{', '.join(list(project)[:6])}")
    if not rejections:
        return {**out, "etat": "non_testable", "blocage": kind, "preuve": proof}
    proposals, questions = requests(result, index, profile, title)
    evaluation = {**out, "proposees": proposals, "questions": questions, "trouvees": found, "rejets": rejections}
    evaluation["attendues"] = pending(evaluation)
    return {**evaluation, "etat": "a_confirmer" if evaluation["attendues"] else "testable"}


def blocker_line(assessment: dict) -> str:
    """« <blocage> : <preuve> » pour l'avertissement de sélection."""
    return f"{BLOCKERS.get(assessment.get('blocage'), 'blocage')} — preuve : {assessment.get('preuve') or '—'}"


# ---------------------------------------------------------------------------
# Rétro-analyse
# ---------------------------------------------------------------------------

def retro(client_name: str, state: dict = None, index: dict = None, profile: dict = None) -> dict:
    """
    Reclasse les résultats « non_couvert » de la dernière génération au crible des 6 sources :
    {"date", "taches": {id: état}, "comptes": {état: n}, "reclassees": n} écrit dans testabilite.json.
    """
    state = load_state(client_name) if state is None else state
    index = load_index(client_name) if index is None else index
    profile = profile if profile is not None else cucumber_profile.effective_profile(
        cucumber_profile.load_profile(client_name))
    tasks = {}
    for spec_id, result in latest_results(state).items():
        if result.get("statut") == "non_couvert":
            tasks[spec_id] = assess(result, index, profile)["etat"]
    counts = {k: sum(1 for v in tasks.values() if v == k) for k in STATES}
    document = {"date": datetime.now().isoformat(timespec="seconds"), "taches": tasks, "comptes": counts,
                "reclassees": sum(1 for v in tasks.values() if v != "non_testable")}
    _write_json(retro_path(client_name), document)
    return document


def retro_line(document: dict) -> str:
    """« 12 tâche(s) reclassée(s) : 3 testable(s), 8 à confirmer, 1 sans objet ; 2 restent non testables »."""
    counts = document.get("comptes") or {}
    return (f"{document.get('reclassees', 0)} tâche(s) reclassée(s) : {counts.get('testable', 0)} testable(s), "
            f"{counts.get('a_confirmer', 0)} à confirmer, {counts.get('sans_objet', 0)} sans objet ; "
            f"{counts.get('non_testable', 0)} reste(nt) non testable(s) (blocage prouvé)")


def retro_all(cache_root: str) -> dict:
    """Rétro-analyse de chaque projet connu : {client: document}."""
    from tools import cucumber_detection
    return {client: retro(client) for client in cucumber_detection.known_projects(cache_root)
            if load_state(client).get("lots")}


if __name__ == "__main__":
    from tools.cucumber_tools import CACHE_ROOT
    for name, doc in retro_all(CACHE_ROOT).items():
        print(f"[CUCUMBER TESTABILITÉ] {name} : {retro_line(doc)}")
    sys.exit(0)
