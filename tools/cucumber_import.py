"""
Import d'un rapport d'exécution (point 8, onglet Cucumber, main.py --import-run / --import-url), 0 token.

- Entrées : console Jenkins, rapport Cucumber JSON, exports de logs serveur (CSV), déposés ou lus sur un build
  Jenkins (URL). Les secrets sont masqués EN MÉMOIRE, avant tout stockage ou affichage : le fichier brut n'est jamais
  écrit, seule son empreinte (SHA-256) est gardée.
- Doublons : fichier déjà importé (empreinte) ; même run (horodatage de début, numéro de build) : les fichiers sont
  ajoutés à cet import, réanalysé.
- Avant toute analyse : nombre de tests du rapport comparé au nombre attendu (tags lus dans la console, sinon
  nombre attendu de la livraison et scénarios des features) ; un écart = run sur une autre version du code ou
  sélection inopérante, l'import est « non analysé ».
- Analyse : classement de chaque échec (cucumber_verify.failure_kind), regroupement par cause racine (catégorie,
  step, signature du message), couverture des steps en échec par les exports de logs (sinon fenêtre à exporter ;
  export hors de la fenêtre des échecs ou au plafond de lignes de l'outil signalé), comparaison avec l'import
  précédent (corrigés, nouveaux, régressions, inchangés, disparus), durée de chaque scénario (rapport JSON, sinon
  horodatages de la console) et scénarios anormalement longs, lots de correction proposés pour les seuls échecs
  « test » des scénarios générés par l'agent. Initialisation en échec : aucun scénario jugé, aucune correction.

Stockage : .tmp/cucumber/<client>/imports/index.json et imports/<n>/ (fichiers masqués, analyse.json, analyse.md).
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone

from tools import (cucumber_data_rules, cucumber_decisions, cucumber_failures, cucumber_profile,
                   cucumber_server_logs, db_tools, groundcover_tools, jenkins_tools)
from tools.cucumber_diagnostic import TEMP_PREFIX
# Masquage commun à tout l'agent (réexporté : MASK, SECRET_PATTERNS, mask_secrets)
from tools.secret_tools import MASK, SECRET_PATTERNS, mask_secrets  # noqa: F401
from tools.cucumber_tools import _git, _read_json, _read_text_file, _write_json, _write_text, a_revoir_tag, cache_dir
from tools.cucumber_verify import (CAUSE_LABELS, _LOG_STAMP, _duration, _step_outputs, _timestamp, all_verified,
                                   expected_count,
                                   failure_category, failure_kind, feature_texts, log_coverage, log_request,
                                   parse_cucumber_report, report_rel, report_test_count, scenario_id, selection_gap)

_JENKINS_STAMP = re.compile(r"^\[\d{4}-\d\d-\d\dT[\d:.]+Z\]\s?")
_CONSOLE_MARK = re.compile(r"\[INFO\] (Scanning for projects|BUILD|Building )|Launching Cucumber|Tests run:|"
                           r"^\s*(\[[^\]]+\]\s*)?Scenario:", re.M)
_PRETTY_SCENARIO = re.compile(r"^\s*(?:Scenario Outline|Scenario|Scénario|Plan du scénario|Example)\s*:\s*(.+?)\s+#\s+"
                              r"(src/\S+?\.feature):(\d+)\s*$")
_SUREFIRE_FAILURE = re.compile(r"\[ERROR\]\s+(.+?)\s+--\s+Time elapsed:.*<<<\s*(?:FAILURE|ERROR)!")
_STEP_AT = re.compile(r"at ✽\.(.+?)\(file:.*?/(src/test/resources/cucumber/features/.+?\.feature):(\d+)\)")
# « ****s run » : console importée avant le masquage par mot entier d'un identifiant court (« Test »)
_TESTS_RUN = re.compile(r"(?:Tests|\*{4}s) run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)")
# Valeur entre guillemets, sinon jusqu'à un guillemet, une option suivante (« -X ») ou la fin de la ligne
_FILTER_TAGS = re.compile(r"cucumber\.filter\.tags=(?:\"([^\"]+)\"|'([^']+)'|([^\"'\n]+?)(?=[\"']|\s+-|\s*$))", re.M)
_BUILD_NUMBER = re.compile(r"(?:Build|build)\s*#\s*(\d+)|/job/[^\s]+?/(\d+)/")
_LOG_LEVEL = re.compile(r"\b(ERROR|WARN(?:ING)?|SEVERE|FATAL)\b")
EVIDENCE_LINES = 5


# ---------------------------------------------------------------------------
# Fichiers d'un import : nature, faits de la console, identité du run
# ---------------------------------------------------------------------------

def file_kind(name: str, text: str) -> str:
    """cucumber_json (rapport Cucumber), logs (export de logs serveur) ou console (console Jenkins)."""
    if text.lstrip().startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list) and all(isinstance(f, dict) and "elements" in f for f in data):
                return "cucumber_json"
        except ValueError:
            pass
    lines = [line for line in text.splitlines()[:50] if line.strip()]
    stamped = sum(1 for line in lines if _LOG_STAMP.search(line))
    if name.lower().endswith(".csv"):
        return "logs"
    # Console Jenkins horodatée ligne à ligne : reconnue à ses marques Maven / Cucumber, pas à ses horodatages
    if _CONSOLE_MARK.search(text):
        return "console"
    if lines and stamped >= 0.8 * len(lines) and "[Pipeline]" not in text:
        return "logs"
    return "console"


def _unstamped(line: str) -> str:
    """Ligne de console sans l'horodatage du plugin Jenkins « timestamps »."""
    return _JENKINS_STAMP.sub("", line, count=1)


def console_results(text: str) -> dict:
    """
    Résultats lus dans la console Jenkins quand le rapport Cucumber JSON manque : scénarios de la sortie lisible de
    Cucumber (« Scenario: <titre> # <feature>:<ligne> »), échecs du résumé Maven Surefire (« [ERROR] <feature>.<titre>
    -- … <<< FAILURE! », exception, « at ✽.<step>(…<feature>:<ligne>) »). {(feature, titre): résultat au format de
    parse_cucumber_report} ; {} si la console ne montre pas les scénarios.
    Chaque en-tête Surefire en échec donne un résultat : nom « <feature>.<titre> », ou « <fin de la feature>.<titre>(
    <début de la feature> » quand le nom de la feature contient une parenthèse ; à défaut de titre reconnu, le
    scénario est celui qui contient la ligne du step en échec (« at ✽.<step>(…<feature>:<ligne>) »).
    """
    lines = [_unstamped(line) for line in text.replace("\r", "").splitlines()]
    scenarios, starts = {}, {}
    for line in lines:
        match = _PRETTY_SCENARIO.match(line)
        if match:
            key = (match.group(2), match.group(1).strip())
            scenarios.setdefault(key, {"statut": "passe", "step": "", "phase": "", "message": ""})
            starts.setdefault(match.group(2), []).append((int(match.group(3)), key))
    for index, line in enumerate(lines):
        failure = _SUREFIRE_FAILURE.search(line)
        if not failure:
            continue
        name = failure.group(1)
        block = []
        # 40 lignes utiles : les lignes vides (console enregistrée en « \r\r\n », doublée à la lecture) ne comptent
        # pas, sinon la ligne « at ✽.<step>(…) » sort de la fenêtre et l'échec est rangé sous le mauvais scénario
        for following in lines[index + 1:]:
            if _SUREFIRE_FAILURE.search(following) or following.startswith("[INFO]") or len(block) >= 40:
                break
            if following.strip():
                block.append(following)
        error = "\n".join(b for b in block if b.strip())
        step_match = next((m for m in map(_STEP_AT.search, block) if m), None)
        path = step_match.group(2) if step_match else ""
        candidates = [key for key in scenarios if name.endswith("." + key[1]) or name == key[1]
                      or f".{key[1]}(" in name]
        key = next((k for k in candidates if k[0] == path), candidates[0] if candidates else None)
        if step_match and (key is None or key[0] != path):
            # Scénario du fichier qui commence au plus près avant la ligne du step en échec
            before = [s for s in starts.get(path, []) if s[0] <= int(step_match.group(3))]
            key = max(before)[1] if before else key
        if key is None:
            # Ni titre ni ligne reconnus : l'échec est gardé sous le nom Surefire, jamais perdu
            key = (path, re.split(r"\(", name.rsplit(".", 1)[-1], maxsplit=1)[0].strip() or name)
        nature, exception = failure_kind(error)
        message = next((b.strip() for b in block if b.strip() and not b.strip().startswith("at ")), "")
        scenarios[key] = {"statut": "echoue", "step": step_match.group(1) if step_match else "", "phase": "",
                          "message": " ".join(message.split())[:300], "nature": nature, "exception": exception,
                          "heure": "", "erreur": error}
    if not any(r["statut"] == "echoue" for r in scenarios.values()):
        scenarios.update(compile_failures(lines))
    return scenarios if any(r["statut"] == "echoue" for r in scenarios.values()) or scenarios else {}


COMPILE_TITLE = "Compilation du dépôt client"
_COMPILE_ERROR = re.compile(r"^\[ERROR\]\s+\S*?(src/test/java/[\w/$.-]+?\.java):\[(\d+),\d+\]\s*(.+)$")
_COMPILE_DETAIL = re.compile(r"^(?:\[ERROR\])?\s*(symbol|location)\s*:\s*(.+)$")


def compile_failures(lines: list) -> dict:
    """
    Échec de « mvn test-compile » (le run n'a exécuté aucun scénario) : un résultat par classe Java en erreur, clé
    (chemin de la classe, COMPILE_TITLE), message « ligne : erreur (symbol, location) » des erreurs distinctes.
    """
    errors = {}
    for index, line in enumerate(lines):
        match = _COMPILE_ERROR.match(line.strip())
        if not match:
            continue
        details = [f"{m.group(1)}: {m.group(2).strip()}" for m in map(_COMPILE_DETAIL.match, lines[index + 1:index + 3])
                   if m]
        text = f"ligne {match.group(2)} : {match.group(3).strip()}" + (f" ({', '.join(details)})" if details else "")
        if text not in errors.setdefault(match.group(1), []):
            errors[match.group(1)].append(text)
    return {(path, COMPILE_TITLE): {"statut": "echoue", "step": "", "phase": "compilation",
                                    "message": " ; ".join(found)[:300], "nature": "compilation",
                                    "exception": "CompilationFailure", "heure": "", "erreur": "\n".join(found)}
            for path, found in errors.items()}


def console_facts(text: str) -> dict:
    """Faits d'une console Jenkins : numéro de build, tags lancés, patch non applicable, dernier « Tests run »."""
    tags = _FILTER_TAGS.findall(text)
    tests = _TESTS_RUN.findall(text)
    build = _BUILD_NUMBER.search(text)
    first = _LOG_STAMP.search(text)
    return {"build": next((g for g in build.groups() if g), "") if build else "",
            "tags": next((g for g in tags[-1] if g), "") if tags else "",
            "patch_non_applicable": "PATCH_NON_APPLICABLE" in text,
            "tests": dict(zip(("executes", "echecs", "erreurs", "ignores"), map(int, tests[-1]))) if tests else {},
            "debut": f"{first.group(1)}T{first.group(2)}" if first else ""}


def full_run(analysis: dict, client_tag: str = "") -> bool:
    """
    Run complet de la suite active : la sélection de tags lancée se réduit au tag client (exclusions « not @… »
    admises) ou est absente, et ne vise aucun lot de l'agent (@agent_…). Inconnu (rapport JSON sans console) : False.
    """
    if "run_complet" in analysis:
        return bool(analysis["run_complet"])
    facts = analysis.get("console") or {}
    if not facts:
        return False
    included = re.findall(r"@[\w.-]+", re.sub(r"\bnot\s+@[\w.-]+", "", facts.get("tags") or ""))
    if any(t.lower().startswith("@agent_") for t in included):
        return False
    return not included or (bool(client_tag) and {t.lower() for t in included} == {client_tag.lower()})


SHARED_MAX = 3
_DATA_CODE = re.compile(r"(?<![\w-])(?:[A-Z][A-Z0-9]*-)+[A-Z0-9]*\d[A-Z0-9]*(?:-[A-Z0-9]+)*(?![\w-])")


def _feature_codes(text: str) -> set:
    """Codes de données d'une feature (OP, UM, fichiers : « TOW-730 », « CIN-TOW-730 »), hors commentaires et tags."""
    return set(_DATA_CODE.findall("\n".join(line for line in text.splitlines()
                                            if not line.strip().startswith(("#", "@")))))


def shared_data(worktree: str, rels, init_features=()) -> dict:
    """
    Règle « données uniques par feature » : {feature: [{code, features}]} des codes de données que chaque feature
    de `rels` partage avec une autre feature du dépôt. Exclus : codes des features d'init (références communes
    voulues) et clés de tickets citées en tag.
    """
    if not worktree or not os.path.isdir(worktree):
        return {}
    codes, tickets = {}, set()
    for rel in _git(worktree, "ls-files", "-co", "--exclude-standard", "--", "*.feature").stdout.splitlines():
        text = _read_text_file(os.path.join(worktree, *rel.split("/")))
        codes[rel] = _feature_codes(text)
        tickets.update(t.lstrip("@") for t in re.findall(r"@[\w.-]+", text))
    common = set().union(*(codes.get(rel, set()) for rel in init_features)) | tickets
    out = {}
    for rel in dict.fromkeys(rels):
        found = [{"code": code, "features": sorted(other.rsplit("/", 1)[-1] for other, theirs in codes.items()
                                                   if other != rel and other not in init_features and code in theirs)}
                 for code in sorted(codes.get(rel, set()) - common)]
        # Code repris par SHARED_MAX autres features ou plus : référence commune (site, donneur d'ordres, poste)
        out[rel] = [f for f in found if 0 < len(f["features"]) < SHARED_MAX]
    return {rel: found for rel, found in out.items() if found}


def feature_codes(worktree: str, rel: str) -> list:
    """Codes de données écrits dans la feature elle-même (ce qu'elle crée ou lit), triés."""
    path = os.path.join(worktree, *rel.split("/")) if worktree and rel else ""
    return sorted(_feature_codes(_read_text_file(path))) if path and os.path.exists(path) else []


def isolated_greens(state: dict) -> dict:
    """Scénarios vérifiés verts par une exécution isolée de l'agent : {id: « lot N »} (le plus récent)."""
    out = {}
    for item in sorted(all_verified(state), key=lambda i: i["lot"]):
        if item.get("id") and item.get("statut") in ("pertinent", "faible"):
            out[item["id"]] = f"lot {item['lot']}"
    return out


def run_contexts(results: dict, state: dict, worktree: str, init_features=(), full: bool = False,
                 client_name: str = "") -> dict:
    """
    Contexte de chaque échec (classify) : codes de données de sa feature, données partagées avec d'autres features
    (règle « données uniques par feature »), contrôles statiques de sa feature (fichier lu absent, création non
    rejouable, code jamais créé : cucumber_data_rules) et, dans un RUN COMPLET, dépendance entre tests si le
    scénario a été vérifié vert seul par l'agent.
    """
    failures = [i for i, r in results.items() if r.get("statut") != "passe"]
    greens = isolated_greens(state) if full else {}
    shared = shared_data(worktree, [i.split("::", 1)[0] for i in failures], init_features)
    checks = cucumber_data_rules.feature_checks(worktree, sorted({i.split("::", 1)[0] for i in failures}),
                                                client_name)
    out = {}
    for identifier in failures:
        own = feature_codes(worktree, identifier.split("::", 1)[0])
        context = {"codes_feature": own} if own else {}
        if checks.get(identifier.split("::", 1)[0]):
            context["controles"] = checks[identifier.split("::", 1)[0]]
        if identifier in greens:
            context["dependance"] = f"vert seul ({greens[identifier]}) mais rouge en run complet"
        if shared.get(identifier.split("::", 1)[0]):
            context["donnees_partagees"] = shared[identifier.split("::", 1)[0]]
        if context:
            out[identifier] = context
    return out


def console_gap(facts: dict, failures: dict) -> str:
    """
    Contrôle automatique : échecs lus scénario par scénario face au total Surefire de la console (Failures +
    Errors). '' si égaux ou total absent ; sinon le message d'écart, affiché en erreur.
    """
    tests = (facts or {}).get("tests") or {}
    if not tests:
        return ""
    expected = tests.get("echecs", 0) + tests.get("erreurs", 0)
    if expected == len(failures):
        return ""
    return (f"écart console / analyse : la console compte {expected} échec(s) (Failures {tests.get('echecs', 0)} + "
            f"Errors {tests.get('erreurs', 0)}), l'analyse en lit {len(failures)} — erreur de l'agent, à signaler")


def run_start(report: list, facts: dict) -> str:
    """Horodatage de début du run (plus petit start_timestamp du rapport, à défaut première heure de la console)."""
    stamps = [_timestamp(e.get("start_timestamp")) for f in report for e in f.get("elements") or []]
    stamps = [s for s in stamps if s]
    return min(stamps).isoformat(timespec="seconds") if stamps else facts.get("debut", "")


# ---------------------------------------------------------------------------
# Stockage des imports
# ---------------------------------------------------------------------------

def imports_dir(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "imports")


def load_index(client_name: str) -> dict:
    return _read_json(os.path.join(imports_dir(client_name), "index.json"), {}) or {"imports": []}


def normalize_analysis(analysis: dict) -> dict:
    """
    Analyse validée à la lecture : chaque scénario d'une proposition devient un dict {tag, id, fichier, titre, step,
    message} (un identifiant « fichier::titre » seul est complété depuis les résultats) ; une entrée inexploitable
    est écartée.
    """
    results = analysis.get("resultats") or {}
    proposals = []
    for proposal in analysis.get("propositions") or []:
        if not isinstance(proposal, dict):
            continue
        scenarios = []
        for item in proposal.get("scenarios") or []:
            if isinstance(item, str):
                item = {"id": item}
            identifier = item.get("id") if isinstance(item, dict) else None
            if not isinstance(identifier, str) or "::" not in identifier:
                continue
            fichier, titre = identifier.split("::", 1)
            result = results.get(identifier) or {}
            scenarios.append({"tag": "", "fichier": fichier, "titre": titre, "step": result.get("step", ""),
                              "message": result.get("message", ""), **item})
        proposals.append({**proposal, "spec": proposal.get("spec") or "", "scenarios": scenarios})
    if "propositions" in analysis:
        analysis = {**analysis, "propositions": proposals}
    return analysis


def load_analysis(client_name: str, numero) -> dict:
    """Analyse (analyse.json) de l'import `numero`, validée à la lecture ({} si absente)."""
    if not numero:
        return {}
    return normalize_analysis(_read_json(os.path.join(imports_dir(client_name), str(numero), "analyse.json"), {})
                              or {})


def _save_index(client_name: str, index: dict) -> None:
    _write_json(os.path.join(imports_dir(client_name), "index.json"), index)


def import_files(client_name: str, files: list, build_url: str = "") -> dict:
    """
    Enregistre un import ({nom: octets} ou [(nom, octets)]) : secrets masqués en mémoire, fichier déjà importé
    écarté (empreinte), même run rattaché à son import. Renvoie {"numero", "messages", "nouveau"} ; numero 0 si rien
    de neuf n'a été importé.
    """
    index = load_index(client_name)
    seen = {f["empreinte"]: entry["numero"] for entry in index["imports"] for f in entry["fichiers"]}
    kept, messages = [], []
    for name, data in (files.items() if isinstance(files, dict) else files):
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            messages.append(f"{name} : fichier déjà importé (import n° {seen[digest]})")
            continue
        masked, counts = mask_secrets(data.decode("utf-8", "replace"))
        kept.append({"nom": os.path.basename(name), "texte": masked, "type": file_kind(name, masked),
                     "empreinte": digest, "masques": counts})
        if counts:
            messages.append(f"{name} : secrets masqués avant stockage ("
                            + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + ")")
    if not kept:
        return {"numero": 0, "messages": messages, "nouveau": False}
    report = [r for f in kept if f["type"] == "cucumber_json" for r in json.loads(f["texte"])]
    facts = next((console_facts(f["texte"]) for f in kept if f["type"] == "console"), {})
    run = run_start(report, facts)
    build = facts.get("build") or (re.search(r"/(\d+)/?$", build_url or "") or [None, ""])[1]
    entry = next((e for e in index["imports"] if run and e["run"] == run), None)
    if entry:
        messages.append(f"même run que l'import n° {entry['numero']} ({run}) : fichiers ajoutés à cet import")
    else:
        entry = {"numero": max((e["numero"] for e in index["imports"]), default=0) + 1, "run": run, "build": build,
                 "url": build_url, "date": datetime.now().isoformat(timespec="seconds"), "fichiers": []}
        index["imports"].append(entry)
    folder = os.path.join(imports_dir(client_name), str(entry["numero"]))
    for f in kept:
        safe = re.sub(r"[^\w.-]", "_", f["nom"])
        stored = f"{len(entry['fichiers']) + 1:02d}-{f['type']}-{safe}"
        _write_text(os.path.join(folder, stored), f["texte"])
        entry["fichiers"].append({k: f[k] for k in ("nom", "type", "empreinte", "masques")} | {"stocke": stored})
    entry["build"] = entry.get("build") or build
    _save_index(client_name, index)
    return {"numero": entry["numero"], "messages": messages, "nouveau": True}


def build_files(build_url: str) -> dict:
    """Console et rapports Cucumber JSON d'un build Jenkins, lus en mémoire : {nom: octets}."""
    info = jenkins_tools.build_info(build_url)
    files = {"console.txt": jenkins_tools.build_bytes(build_url, "consoleText")}
    for artifact in (info or {}).get("artifacts") or []:
        if artifact.lower().endswith(".json") and "cucumber" in artifact.lower():
            files[artifact.rsplit("/", 1)[-1]] = jenkins_tools.build_bytes(build_url, f"artifact/{artifact}")
    return {name: data for name, data in files.items() if data}


# ---------------------------------------------------------------------------
# Analyse d'un import
# ---------------------------------------------------------------------------

def _stored(client_name: str, entry: dict) -> list:
    """Fichiers stockés d'un import, nature recalculée (un import antérieur a pu prendre une console pour des logs)."""
    folder = os.path.join(imports_dir(client_name), str(entry["numero"]))
    files = [{**f, "texte": _read_text_file(os.path.join(folder, f["stocke"]))} for f in entry["fichiers"]]
    return [{**f, "type": file_kind(f["nom"], f["texte"]) if f["type"] in ("logs", "console") else f["type"]}
            for f in files]


def merge_reports(reports: list) -> list:
    """
    Rapports Cucumber d'un même run fusionnés : un scénario présent dans plusieurs fichiers (rapport réimporté ou
    découpé) ne compte qu'une fois (feature, scénario, ligne, début).
    """
    merged, seen = {}, set()
    for report in reports:
        for feature in report:
            target = merged.setdefault(feature.get("uri", ""), {**feature, "elements": []})
            for element in feature.get("elements") or []:
                key = (feature.get("uri", ""), element.get("type"), element.get("id") or element.get("name"),
                       element.get("line"), element.get("start_timestamp"))
                if element.get("type") == "background" or key not in seen:
                    seen.add(key)
                    target["elements"].append(element)
    return list(merged.values())


def _rel(path: str) -> str:
    """Chemin d'une feature du rapport rapporté au dépôt (classpath:cucumber/… -> src/test/resources/cucumber/…)."""
    return "src/test/resources/" + path if path.startswith("cucumber/") else path


def message_signature(message: str) -> str:
    """Message d'échec sans ses valeurs variables (dates, identifiants, valeurs citées, codes, nombres)."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?", "<date>", message or "")
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{20,}\b", "<id>", text, flags=re.I)
    text = re.sub(r"\"[^\"]*\"|'[^']*'|«[^»]*»|<[^<>]*>", "<v>", text)
    text = re.sub(r"\b(?=[\w-]*\d)[\w-]+\b", "<n>", text)
    text = re.sub(r"\b[A-Z][A-Z0-9_-]{2,}\b", "<code>", text)
    return " ".join(text.split())[:160]


def _category(result: dict) -> str:
    if result["statut"] == "cascade":
        return "cascade"
    if result["statut"] == "indefini":
        return "test"
    return failure_category(result.get("nature", ""))


def group_failures(failures: dict) -> list:
    """Échecs ({id: résultat}) regroupés par cause racine : catégorie, nature, step (valeurs masquées), signature."""
    groups = {}
    for identifier, result in failures.items():
        step = re.sub(r"\"[^\"]*\"", '"<v>"', result.get("step", ""))
        key = (_category(result), result.get("cause") or result.get("nature", "") or result["statut"],
               step if result["statut"] != "cascade" else "", message_signature(result.get("message", "")))
        group = groups.setdefault(key, {"categorie": key[0], "nature": key[1],
                                        "libelle": CAUSE_LABELS.get(key[1], key[1]), "step": result.get("step", ""),
                                        "message": result.get("message", ""), "scenarios": []})
        group["scenarios"].append(identifier)
    return sorted(groups.values(), key=lambda g: -len(g["scenarios"]))


def _log_evidence(texts: list, when: str) -> list:
    """Lignes ERROR / WARN des exports à une minute au plus de `when` (UTC ou heure locale)."""
    stamp = _timestamp(when)
    if not stamp:
        return []
    out = []
    for text in texts:
        for line in text.splitlines():
            found = _LOG_STAMP.search(line)
            if not found or not _LOG_LEVEL.search(line):
                continue
            naive = datetime.fromisoformat(f"{found.group(1)}T{found.group(2)}")
            if any(abs((t - stamp).total_seconds()) <= 60
                   for t in (naive.replace(tzinfo=timezone.utc), naive.astimezone())):
                out.append(" ".join(line.split())[:300])
            if len(out) >= EVIDENCE_LINES:
                return out
    return out


def compare(previous: dict, current: dict) -> dict:
    """
    Corrigés, nouveaux (absents du run précédent), régressions (verts au run précédent), inchangés (message changé
    ou non) et disparus entre deux analyses ({id: résultat}).
    """
    failing = lambda d: {i for i, r in d.items() if r["statut"] != "passe"}  # noqa: E731
    before, now = failing(previous), failing(current)
    unchanged = sorted(before & now)
    return {"corriges": sorted(i for i in before - now if i in current),
            "nouveaux": sorted(i for i in now - before if i not in previous),
            "regressions": sorted(i for i in now - before if i in previous),
            "inchanges": unchanged,
            "message_change": sorted(i for i in unchanged if message_signature(previous[i].get("message", ""))
                                     != message_signature(current[i].get("message", ""))),
            "disparus": sorted(i for i in previous if i not in current)}


# ---------------------------------------------------------------------------
# Durées, initialisation en échec, exports de logs
# ---------------------------------------------------------------------------

DEFAULT_SLOW_SECONDS = 300
SLOW_FACTOR = 3
_TIMEOUT_MESSAGE = re.compile(r"(?i)time ?out|timed out|délai|expir|awaitility|condition .* within")
_CONSOLE_STAMP = re.compile(r"^\[?(?:(\d{4}-\d{2}-\d{2})[T ])?(\d{2}:\d{2}:\d{2})")
_CONSOLE_SCENARIO = re.compile(r"\b(?:Scenario Outline|Scenario Template|Scenario|Example|Plan du [Ss]cénario|Scénario|"
                               r"Exemple)\s*:\s*(.+?)\s*(?:#.*)?$")
_INIT_TAG = re.compile(r"^@INIT_", re.I)


def _elements(report: list):
    """(identifiant, feature, élément) des scénarios du rapport, dans l'ordre d'exécution (Background exclu)."""
    for feature in report if isinstance(report, list) else []:
        rel = _rel(report_rel(feature.get("uri", "")))
        for element in feature.get("elements") or []:
            if element.get("type") != "background":
                yield scenario_id(rel, str(element.get("name", ""))), feature, element


def report_durations(report: list) -> dict:
    """Durée de chaque scénario d'après le rapport JSON (steps et Background, en secondes) : {id: secondes}."""
    durations = {}
    for feature in report if isinstance(report, list) else []:
        rel, background = _rel(report_rel(feature.get("uri", ""))), 0.0
        for element in feature.get("elements") or []:
            seconds = sum(_duration(step) for step in element.get("steps") or [])
            if element.get("type") == "background":
                background = seconds
                continue
            identifier = scenario_id(rel, str(element.get("name", "")))
            # Exemples d'un Scenario Outline : durées additionnées
            durations[identifier] = durations.get(identifier, 0.0) + seconds + background
            background = 0.0
    return durations


def console_durations(text: str) -> dict:
    """
    Durée de chaque scénario d'après les horodatages de la console (plugin d'horodatage de Jenkins) : écart entre
    sa ligne « Scenario: » et la ligne de scénario suivante (à défaut, la dernière ligne horodatée). {titre: secondes}.
    """
    marks, last = [], None
    for line in (text or "").splitlines():
        stamp = _CONSOLE_STAMP.match(line.strip())
        if not stamp:
            continue
        moment = datetime.fromisoformat(f"{stamp.group(1) or '2000-01-01'}T{stamp.group(2)}")
        last = moment
        found = _CONSOLE_SCENARIO.search(line)
        if found:
            marks.append((found.group(1).strip(), moment))
    durations = {}
    for (title, start), following in zip(marks, marks[1:] + [(None, last)]):
        seconds = (following[1] - start).total_seconds()
        if seconds >= 0:
            durations[title] = durations.get(title, 0.0) + seconds
    return durations


def scenario_durations(report: list, console: str = "") -> dict:
    """Durées {id: secondes} : rapport JSON, sinon horodatages de la console (scénario reconnu par son titre)."""
    durations = {i: d for i, d in report_durations(report).items() if d > 0}
    by_title = console_durations(console) if console else {}
    for identifier, _, element in _elements(report):
        title = str(element.get("name", "")).strip()
        if identifier not in durations and title in by_title:
            durations[identifier] = by_title[title]
    return durations


def slow_threshold() -> float:
    """Seuil absolu d'un scénario anormalement long (CUCUMBER_SLOW_SCENARIO_SECONDS, 300 s par défaut)."""
    try:
        return float(os.getenv("CUCUMBER_SLOW_SCENARIO_SECONDS", "") or DEFAULT_SLOW_SECONDS)
    except ValueError:
        return float(DEFAULT_SLOW_SECONDS)


def slow_scenarios(durations: dict, results: dict) -> dict:
    """
    Scénarios anormalement longs : au-delà du seuil absolu ou de SLOW_FACTOR fois la durée médiane, et échecs sur
    une attente expirée. {"mediane", "total", "lents": [{id, secondes, motif}]} (plus longs d'abord).
    """
    values = sorted(durations.values())
    median = values[len(values) // 2] if values else 0.0
    limit = min(slow_threshold(), SLOW_FACTOR * median) if median else slow_threshold()
    slow = []
    for identifier, seconds in durations.items():
        result = results.get(identifier) or {}
        if result.get("statut") == "echoue" and _TIMEOUT_MESSAGE.search(result.get("message", "")):
            slow.append({"id": identifier, "secondes": round(seconds),
                         "motif": "attente expirée : " + result["message"][:120]})
        elif seconds >= max(limit, 1):
            slow.append({"id": identifier, "secondes": round(seconds),
                         "motif": f"{round(seconds)} s pour une médiane de {round(median)} s"})
    return {"mediane": round(median), "total": round(sum(values)),
            "lents": sorted(slow, key=lambda e: -e["secondes"])}


def init_markers(client_name: str) -> tuple:
    """(features d'init, tags d'init) du profil client."""
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client_name))
    return set(profile["init_features"]), set(re.findall(r"@[\w-]+", profile["init_tags"] or ""))


def console_init_failure(results: dict, features=(), tags=()) -> dict:
    """Init en échec d'après la console (sans rapport JSON) : premier scénario d'une feature d'init du profil."""
    for identifier, result in results.items():
        if identifier.split("::", 1)[0] in features and result.get("statut") not in (None, "passe"):
            return {"id": identifier, "step": result.get("step", ""), "message": result.get("message", ""),
                    "nature": result.get("nature", "") or result.get("statut", "")}
    return {}


def init_failure(report: list, results: dict, features=(), tags=()) -> dict:
    """
    Premier scénario d'initialisation en échec (feature d'init du profil, tag d'init du profil ou @INIT_*) :
    {id, step, message, nature} ; {} si l'initialisation passe ou n'est pas dans le run.
    """
    for identifier, feature, element in _elements(report):
        names = [t.get("name", "") for t in (feature.get("tags") or []) + (element.get("tags") or [])
                 if isinstance(t, dict)]
        is_init = identifier.split("::", 1)[0] in features or any(n in tags or _INIT_TAG.match(n) for n in names)
        result = results.get(identifier) or {}
        if is_init and result.get("statut") not in (None, "passe"):
            return {"id": identifier, "step": result.get("step", ""), "message": result.get("message", ""),
                    "nature": result.get("nature", "") or result.get("statut", "")}
    return {}


def export_line_cap() -> int:
    """Plafond de lignes de l'outil d'export des logs serveur (CUCUMBER_LOG_EXPORT_MAX_LINES, 0 : inconnu)."""
    try:
        return int(os.getenv("CUCUMBER_LOG_EXPORT_MAX_LINES", "") or 0)
    except ValueError:
        return 0


def log_warnings(logs: list, failures: dict) -> list:
    """
    Avertissements sur les exports de logs ([(nom, texte)]) : export qui ne couvre l'heure d'aucun échec (fenêtre
    à exporter en UTC et en heure locale, texte à filtrer), export qui atteint le plafond de lignes de l'outil.
    """
    timed = [(i, r) for i, r in failures.items() if r.get("heure")]
    cap, warnings = export_line_cap(), []
    for name, text in logs:
        lines = len((text or "").splitlines())
        if cap and lines >= cap:
            warnings.append(f"export « {name} » : {lines} lignes, le plafond de l'outil d'export ({cap}) — les "
                            f"lignes les plus anciennes ont pu être perdues : resserrez la fenêtre ou le filtre")
        if timed and not any(log_coverage(text, r["heure"])[0] for _, r in timed):
            result = timed[0][1]
            warnings.append(f"export « {name} » hors de la fenêtre des échecs ({log_coverage(text, result['heure'])[1]})"
                            f" : " + log_request({"heure": result["heure"], "step": result.get("step", ""),
                                                  "motif": result.get("message", "")}))
    return warnings


def agent_scenarios(state: dict) -> dict:
    """Scénarios générés par l'agent : {identifiant: {tag, spec}} (tags des lots, spécifique des indicateurs)."""
    spec_of = {scenario_id(s["fichier"], s["titre"]): s.get("spec", "") for lot in state.get("lots", [])
               for s in (lot.get("indicateurs") or {}).get("scenarios", [])}
    return {identifier: {"tag": tag, "spec": spec_of.get(identifier, "")}
            for lot in state.get("lots", []) for tag, identifier in (lot.get("tags") or {}).items()}


DEFAULT_MAX_ATTEMPTS = 3


def max_attempts() -> int:
    """Corrections infructueuses tolérées sur une même cause (CUCUMBER_MAX_CAUSE_ATTEMPTS, 3 par défaut)."""
    try:
        return max(1, int(os.getenv("CUCUMBER_MAX_CAUSE_ATTEMPTS", "") or DEFAULT_MAX_ATTEMPTS))
    except ValueError:
        return DEFAULT_MAX_ATTEMPTS


def attempts(identifier: str, message: str, state: dict) -> list:
    """
    Corrections déjà tentées sur ce scénario pour la même cause (même signature de message) : lots « correction »,
    du plus ancien au plus récent, avec ce que la correction a déclaré faire.
    """
    signature = message_signature(message)
    out = []
    for lot in sorted(state.get("lots") or [], key=lambda x: x["numero"]):
        item = next((s for s in lot.get("corrections") or [] if s.get("id") == identifier
                     and message_signature(s.get("message", "")) == signature), None)
        if item:
            done = next((c.get("correction", "") for c in (lot.get("correction") or {}).get("corrections", [])
                         if c.get("tag") == item.get("tag")), "")
            out.append({"lot": lot["numero"], "import": lot.get("import"), "correction": done or "non décrite"})
    return out


def proof_methods(result: dict, db_configured: bool) -> list:
    """Méthodes de preuve proposées quand l'agent arrête de deviner (3 tentatives infructueuses)."""
    methods = []
    if db_configured:
        methods.append("lecture en base en lecture seule (outil base de l'agent) des données lues ou écrites par le "
                       "step en échec, pour comparer l'état réel à l'attendu")
    methods.append("requête de référence capturée depuis l'interface web (outils de développement du navigateur, "
                   "onglet Réseau) en rejouant le scénario à la main, à comparer à la requête envoyée par le step")
    methods.append("trace serveur : " + log_request({"heure": result.get("heure", ""), "step": result.get("step", ""),
                                                     "motif": result.get("message", "")}))
    return methods


def correction_proposals(failures: dict, state: dict, extras: dict = None, db_configured: bool = False,
                         candidates=None) -> tuple:
    """
    (lots de correction proposés, causes à prouver) pour les échecs « test » des scénarios de l'agent, un lot par
    spécifique. `candidates` : identifiants à proposer (échecs à écrire de cucumber_failures, tagués par l'agent ou
    non : tous les scénarios du dépôt sont les siens) ; à défaut, les échecs « test » des scénarios tagués. `extras` : {id: {diagnostic, preuves, refus}} joints au scénario (sortie d'un diagnostic temporaire,
    lignes de logs serveur, commentaires de décisions refusées). Après max_attempts() corrections infructueuses sur
    la même cause, le scénario n'est plus proposé : il rejoint les causes à prouver, avec ce qui a été essayé et les
    méthodes de preuve.
    """
    ours, by_spec, to_prove = agent_scenarios(state), {}, []
    for identifier, result in failures.items():
        if candidates is not None:
            if identifier not in candidates:
                continue
        elif identifier not in ours or _category(result) != "test":
            continue
        tried = attempts(identifier, result.get("message", ""), state)
        if len(tried) >= max_attempts():
            to_prove.append({"id": identifier, "message": result.get("message", ""), "step": result.get("step", ""),
                             "tentatives": tried, "methodes": proof_methods(result, db_configured)})
            continue
        fichier, titre = identifier.split("::", 1)
        mine = ours.get(identifier) or {"tag": "", "spec": ""}
        by_spec.setdefault(mine["spec"], []).append(
            {"tag": mine["tag"], "id": identifier, "fichier": fichier, "titre": titre,
             "step": result.get("step", ""), "message": result.get("message", ""), "tentatives": len(tried),
             **((extras or {}).get(identifier) or {})})
    return [{"spec": spec, "scenarios": items} for spec, items in sorted(by_spec.items())], to_prove


def diagnostic_outputs(report: list) -> dict:
    """Sorties des scénarios qui ont exécuté un step TEMPORAIRE (diagnostic temporaire) : {id: texte}."""
    out = {}
    for identifier, _, element in _elements(report):
        steps = element.get("steps") or []
        if any(str(step.get("name", "")).startswith(TEMP_PREFIX) for step in steps):
            text = " | ".join(o for step in steps for o in _step_outputs(step))
            out[identifier] = " ".join(text.split())[:1500] or "(aucune sortie)"
    return out


def _count_gap(report: list, facts: dict, worktree: str, state: dict) -> str:
    """Écart entre les tests du rapport et le nombre attendu (tags de la console, sinon livraison et features)."""
    texts = feature_texts(worktree) if worktree and os.path.isdir(worktree) else []
    executed = report_test_count(report)
    if facts.get("tags") and texts:
        return selection_gap(report, texts, facts["tags"])
    expected = {(state.get("selection") or {}).get("nombre_attendu") or 0}
    if texts:
        expected |= {expected_count(texts, "", a_revoir_tag()), expected_count(texts)}
    expected.discard(0)
    if expected and executed not in expected:
        return (f"{executed} test(s) exécuté(s) pour {' ou '.join(map(str, sorted(expected)))} attendu(s) : run sur "
                f"une autre version du code ou sélection de tags inopérante — import non analysé")
    return ""


def analyse(client_name: str, numero: int, state: dict, worktree: str = "") -> dict:
    """Analyse de l'import `numero` (voir docstring du module). Écrit analyse.json et analyse.md, renvoie l'analyse."""
    index = load_index(client_name)
    entry = next(e for e in index["imports"] if e["numero"] == numero)
    files = _stored(client_name, entry)
    report = merge_reports([json.loads(f["texte"]) for f in files if f["type"] == "cucumber_json"])
    console = "\n".join(f["texte"] for f in files if f["type"] == "console")
    facts = console_facts(console) if console else {}
    logs = [f["texte"] for f in files if f["type"] == "logs"]
    out = {"numero": numero, "run": entry["run"], "build": entry.get("build", ""), "console": facts,
           "fichiers": [{k: f[k] for k in ("nom", "type", "masques")} for f in files], "non_analyse": "",
           "tests": report_test_count(report), "groupes": [], "logs": [], "comparaison": {}, "propositions": [],
           "resultats": {}, "init_en_echec": {}, "durees": {}, "avertissements": [], "diagnostics": {},
           "a_prouver": []}
    from_console = console_results(console) if console and not report else {}
    if facts.get("patch_non_applicable"):
        out["non_analyse"] = "patch non applicable : le run n'a pas exécuté les tests de cette version"
    elif not report and not from_console:
        out["non_analyse"] = ("ni rapport Cucumber JSON, ni détail des scénarios dans la console : importez le "
                              "rapport JSON du build, ou l'adresse du build Jenkins")
    elif report:
        out["non_analyse"] = _count_gap(report, facts, worktree, state)
    if not out["non_analyse"]:
        parsed = parse_cucumber_report(report) if report else from_console
        results = {scenario_id(_rel(rel), title): {k: v for k, v in r.items() if k not in ("journal", "sorties")}
                   for (rel, title), r in parsed.items()}
        if not report:
            out["tests"], out["source"] = len(results), "console"
        failures = {i: r for i, r in results.items() if r["statut"] != "passe"}
        # Logs applicatifs déposés dans CUCUMBER_SERVER_LOG_DIR : fenêtre de chaque échec, sans export manuel
        hours = [r.get("heure") for r in failures.values() if r.get("heure")]
        server = cucumber_server_logs.extract(cucumber_server_logs.log_dir(), hours)
        # Logs de l'environnement de test dans groundcover (GROUNDCOVER_API_KEY, CUCUMBER_GROUNDCOVER_FILTER)
        remote = groundcover_tools.window_extract(hours)
        server = {"texte": "\n".join(t for t in (server["texte"], remote["texte"]) if t),
                  "lignes": server["lignes"] + remote["lignes"], "fichiers": server["fichiers"] + remote["fichiers"],
                  "tronque": server["tronque"] or remote["tronque"], "plages": server["plages"] + remote["plages"],
                  "erreur": remote["erreur"]}
        if server["texte"]:
            logs.append(server["texte"])
            _write_text(os.path.join(imports_dir(client_name), str(numero), cucumber_server_logs.EXTRACT_NAME),
                        server["texte"] + "\n")
        out["logs_serveur"] = {k: server[k] for k in ("lignes", "fichiers", "tronque", "plages", "erreur")}
        out["resultats"] = results
        out["groupes"] = group_failures(failures)
        durations = scenario_durations(report, console)
        out["durees"] = {**slow_scenarios(durations, results),
                         "par_scenario": {i: round(d, 1) for i, d in durations.items()}}
        out["init_en_echec"] = init_failure(report, results, *init_markers(client_name)) if report else \
            console_init_failure(results, *init_markers(client_name))
        out["avertissements"] = log_warnings([(f["nom"], f["texte"]) for f in files if f["type"] == "logs"],
                                             failures)
        # Contrôle automatique : chaque échec compté par la console a son résultat dans l'analyse
        out["ecart_console"] = "" if report else console_gap(facts, failures)
        if out["ecart_console"]:
            out["avertissements"].insert(0, out["ecart_console"])
        client_tag = cucumber_profile.effective_profile(cucumber_profile.load_profile(client_name)).get("tag_client")
        out["run_complet"] = full_run({"console": facts}, client_tag or "")
        # Créations refusées en doublon (« existe déjà ») relevées dans la console : contrôle d'idempotence
        learned = cucumber_data_rules.learn_from_console(client_name, console, worktree) if console else {}
        if any(learned.values()):
            out["avertissements"].append("créations qui refusent un doublon relevées dans la console : "
                                         + ", ".join(learned["evenements"] + learned["steps"]))
        contexts = run_contexts(results, state, worktree, init_markers(client_name)[0], out["run_complet"],
                                client_name)
        if server["erreur"]:
            out["avertissements"].append(f"logs groundcover non lus : {server['erreur']}")
        for identifier, result in list(failures.items())[:30]:
            item = {"step": result.get("step", ""), "motif": result.get("message", ""), "heure": result.get("heure")}
            covered = [log_coverage(text, item["heure"] or "") for text in logs]
            if cucumber_server_logs.covers(server["plages"], item["heure"] or ""):
                covered.insert(0, (True, "couvert par les logs du répertoire CUCUMBER_SERVER_LOG_DIR"))
            out["logs"].append({"id": identifier, "heure": item["heure"] or "",
                                "couvert": any(c[0] for c in covered),
                                "message": next((c[1] for c in covered if c[0]), covered[0][1] if covered else ""),
                                "a_exporter": "" if any(c[0] for c in covered) else log_request(item),
                                "preuves": _log_evidence(logs, item["heure"] or "")})
        previous = max((e for e in index["imports"] if e["numero"] < numero), key=lambda e: e["numero"],
                       default=None)
        previous_analysis = _read_json(os.path.join(imports_dir(client_name), str(previous["numero"]),
                                                    "analyse.json"), {}) if previous else {}
        if previous_analysis.get("resultats"):
            out["comparaison"] = {"import": previous["numero"],
                                  **compare(previous_analysis["resultats"], results)}
        # Initialisation en échec : aucun scénario n'est jugé, aucune correction proposée
        out["diagnostics"] = diagnostic_outputs(report)
        evidence = {e["id"]: e["preuves"] for e in out["logs"] if e.get("preuves")}
        extras = {i: {k: v for k, v in (("diagnostic", out["diagnostics"].get(i, "")),
                                        ("preuves", evidence.get(i, [])),
                                        ("refus", cucumber_decisions.refused_comments(client_name, i)),
                                        ("dependance", (contexts.get(i) or {}).get("dependance", "")),
                                        ("donnees_partagees", (contexts.get(i) or {}).get("donnees_partagees")),
                                        ("controles", (contexts.get(i) or {}).get("controles")))
                      if v}
                  for i in failures}
        # Un résultat explicite par échec (cucumber_failures) ; ceux déjà traités par une correction sont gardés
        out["echecs"] = failure_results(client_name, numero, results, out["init_en_echec"], state, worktree,
                                        contexts)
        if not out["init_en_echec"]:
            out["propositions"], out["a_prouver"] = correction_proposals(
                failures, state, extras, db_tools.is_db_configured("cucumber"),
                {e["id"] for e in cucumber_failures.pending(out)})
    folder = os.path.join(imports_dir(client_name), str(numero))
    _write_json(os.path.join(folder, "analyse.json"), out)
    _write_text(os.path.join(folder, "analyse.md"), analysis_markdown(out))
    return out


def failure_results(client_name: str, numero: int, results: dict, init: dict, state: dict, worktree: str,
                    contexts: dict = None) -> list:
    """
    Résultat explicite de chaque échec du run (cucumber_failures.failure_results) : propriétaire jugé sur le
    worktree et la branche de base, désactivation proposée (patch) pour un défaut du produit, résultats déjà
    traités d'une analyse précédente du même import conservés.
    """
    base = state.get("base", "")
    is_owned = (lambda rel: cucumber_failures.owned(worktree, base, rel)) if worktree and os.path.isdir(worktree) \
        else None
    items = cucumber_failures.failure_results(results, init, is_owned, contexts=contexts)
    previous = {e["id"]: e for e in (_read_json(cucumber_failures.analysis_path(imports_dir(client_name), numero),
                                                {}) or {}).get("echecs") or [] if e.get("statut") == "traite"}
    folder = os.path.join(imports_dir(client_name), str(numero))
    for index, item in enumerate(items, 1):
        if item["id"] in previous:
            item.update(previous[item["id"]])
        elif item["categorie"] == "produit" and worktree:
            patch = cucumber_failures.disable_patch(worktree, item["fichier"], item["titre"], a_revoir_tag())
            if patch:
                item["patch"] = os.path.join(folder, f"desactivation-{index}.patch")
                _write_text(item["patch"], patch)
    return items


def analysis_markdown(analysis: dict) -> str:
    """Rapport lisible d'une analyse d'import."""
    lines = [f"# Import n° {analysis['numero']} — run {analysis['run'] or '?'}"
             + (f", build #{analysis['build']}" if analysis.get("build") else ""), ""]
    lines += [f"- {f['nom']} ({f['type']})" + (" — secrets masqués : " + ", ".join(
        f"{n} {k}" for k, n in sorted(f["masques"].items())) if f["masques"] else "") for f in analysis["fichiers"]]
    if analysis["non_analyse"]:
        return "\n".join(lines + ["", f"**Non analysé** : {analysis['non_analyse']}", ""])
    failures = sum(len(g["scenarios"]) for g in analysis["groupes"])
    lines += ["", f"{analysis['tests']} test(s), {failures} échec(s), {len(analysis['groupes'])} cause(s) racine.", ""]
    init = analysis.get("init_en_echec") or {}
    if init:
        lines += ["## ⛔ Initialisation en échec : aucun scénario n'est jugé", "", f"- Scénario : {init['id']}",
                  f"- Premier step en échec : `{init['step'] or '—'}`", f"- Cause : {init['message']}", ""]
    lines += [f"- ⚠️ {w}" for w in analysis.get("avertissements") or []]
    server = analysis.get("logs_serveur") or {}
    if cucumber_server_logs.log_dir() or groundcover_tools.configured() or server.get("lignes"):
        source = " + ".join(x for x in (cucumber_server_logs.log_dir(), "groundcover" if groundcover_tools.configured()
                                        else "") if x) or "répertoire"
        lines += [f"- Logs applicatifs du serveur ({source}) : "
                  + (f"{server['lignes']} ligne(s) dans la fenêtre des échecs ({', '.join(server['fichiers'][:5])})"
                     + (" — plafond atteint, extrait tronqué" if server.get("tronque") else "")
                     if server.get("lignes") else "aucune ligne dans la fenêtre des échecs (fichiers déposés pour ce "
                                                  "run ? même fuseau horaire ?)"), ""]
    comparison = analysis.get("comparaison") or {}
    if comparison:
        lines += [f"## Comparaison avec l'import n° {comparison['import']}", "",
                  f"- Corrigés : {len(comparison['corriges'])} ; nouveaux : {len(comparison['nouveaux'])} ; "
                  f"régressions : {len(comparison.get('regressions', []))} ; "
                  f"inchangés : {len(comparison['inchanges'])} (message changé : "
                  f"{len(comparison['message_change'])}) ; disparus : {len(comparison['disparus'])}", ""]
        lines += [f"  - régression : {i}" for i in comparison.get("regressions", [])[:20]]
    slow = (analysis.get("durees") or {}).get("lents") or []
    if slow:
        lines += ["", f"## Scénarios anormalement longs : {len(slow)} (médiane {analysis['durees']['mediane']} s)", ""]
        lines += [f"- {e['id']} : {e['secondes']} s — {e['motif']}" for e in slow[:20]]
        lines.append("")
    lines += ["## Causes racine", ""]
    for g in analysis["groupes"]:
        lines += [f"### {g['libelle']} ({g['categorie']}) — {len(g['scenarios'])} scénario(s)", "",
                  f"- Step : `{g['step'] or '—'}`", f"- Message : `{g['message']}`"]
        lines += [f"  - {i}" for i in g["scenarios"][:20]] + [""]
    uncovered = [entry for entry in analysis["logs"] if entry["a_exporter"]]
    if uncovered:
        lines += ["## Logs serveur à exporter", ""] + [f"- {e['id']} : {e['a_exporter']}" for e in uncovered] + [""]
    if analysis.get("diagnostics"):
        lines += ["## Sorties des diagnostics temporaires", ""]
        lines += [f"- {i} : {text[:300]}" for i, text in analysis["diagnostics"].items()] + [""]
    if analysis.get("a_prouver"):
        lines += [f"## Causes à prouver après {max_attempts()} corrections infructueuses : "
                  f"{len(analysis['a_prouver'])}", ""]
        for item in analysis["a_prouver"]:
            lines += [f"- {item['id']} — `{item['message'][:200]}`",
                      "  - Essayé : " + " ; ".join(f"lot {t['lot']} : {t['correction']}" for t in item["tentatives"])]
            lines += [f"  - Méthode de preuve : {m}" for m in item["methodes"]]
        lines.append("")
    if analysis["propositions"]:
        lines += ["## Lots de correction proposés (échecs « test » des scénarios de l'agent)", ""]
        for p in analysis["propositions"]:
            lines.append(f"- {p['spec'] or '(spécifique inconnu)'} : {len(p['scenarios'])} scénario(s)")
    return "\n".join(lines) + "\n"


def already_fixed(state: dict, numero) -> set:
    """Scénarios d'un run importé déjà corrigés par l'agent ET vérifiés verts sur Jenkins (lots de correction)."""
    return {item.get("id") for lot in state.get("lots") or []
            if lot.get("type") == "correction" and str(lot.get("import")) == str(numero)
            for item in ((lot.get("execution") or {}).get("scenarios") or {}).values()
            if item.get("statut") == "pertinent"}


def remaining_proposals(state: dict, analysis: dict) -> list:
    """
    Propositions d'une analyse sans les scénarios déjà corrigés (relance d'une correction). Run complet : un vert
    isolé de l'agent ne vaut pas correction (seul le prochain run complet tranche) ; seuls les échecs déjà traités
    par un lot de correction de ce run sont écartés.
    """
    if full_run(analysis):
        done = {e["id"] for e in analysis.get("echecs") or [] if e.get("statut") == "traite"}
    else:
        done = already_fixed(state, analysis.get("numero"))
    out = []
    for proposal in analysis.get("propositions") or []:
        scenarios = [s for s in proposal["scenarios"] if s.get("id") not in done]
        if scenarios:
            out.append({**proposal, "scenarios": scenarios})
    return out


def add_correction_lots(state: dict, analysis: dict) -> list:
    """
    Lots « correction » ajoutés à la génération pour les propositions d'une analyse : scénarios existants avec leur
    message réel, sans spécifique à générer (ids vides). Une relance sur le même run écarte les scénarios déjà
    corrigés et vérifiés verts. Renvoie les nouveaux lots.
    """
    start = max((lot["numero"] for lot in state.get("lots", [])), default=0)
    new = []
    for offset, proposal in enumerate(remaining_proposals(state, analysis), 1):
        scenarios = proposal["scenarios"]
        new.append({"numero": start + offset, "ids": [], "type": "correction", "statut": "a_faire",
                    "complement": True, "import": analysis["numero"], "spec_correction": proposal["spec"],
                    "tags": {s["tag"]: s["id"] for s in scenarios}, "types": {}, "corrections": scenarios,
                    "indicateurs": {"scenarios": [{"fichier": s["fichier"], "titre": s["titre"],
                                                   "spec": proposal["spec"], "motifs": []} for s in scenarios]}})
    state["lots"] = state.get("lots", []) + new
    return new
