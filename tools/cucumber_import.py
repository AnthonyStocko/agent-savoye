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
  step, signature du message), couverture des steps en échec par les exports de logs (sinon fenêtre à exporter),
  comparaison avec l'import précédent (corrigés, nouveaux, inchangés, disparus), lots de correction proposés pour
  les seuls échecs « test » des scénarios générés par l'agent.

Stockage : .tmp/cucumber/<client>/imports/index.json et imports/<n>/ (fichiers masqués, analyse.json, analyse.md).
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone

from tools import jenkins_tools
# Masquage commun à tout l'agent (réexporté : MASK, SECRET_PATTERNS, mask_secrets)
from tools.secret_tools import MASK, SECRET_PATTERNS, mask_secrets  # noqa: F401
from tools.cucumber_tools import _read_json, _read_text_file, _write_json, _write_text, a_revoir_tag, cache_dir
from tools.cucumber_verify import (CAUSE_LABELS, _LOG_STAMP, _timestamp, expected_count, failure_category,
                                   feature_texts, log_coverage, log_request, parse_cucumber_report, report_test_count,
                                   scenario_id, selection_gap)

_TESTS_RUN = re.compile(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)")
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
    if name.lower().endswith(".csv") or (lines and stamped >= 0.8 * len(lines) and "[Pipeline]" not in text):
        return "logs"
    return "console"


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
    folder = os.path.join(imports_dir(client_name), str(entry["numero"]))
    return [{**f, "texte": _read_text_file(os.path.join(folder, f["stocke"]))} for f in entry["fichiers"]]


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
    """Corrigés, nouveaux, inchangés (message changé ou non) et disparus entre deux analyses ({id: résultat})."""
    failing = lambda d: {i for i, r in d.items() if r["statut"] != "passe"}  # noqa: E731
    before, now = failing(previous), failing(current)
    unchanged = sorted(before & now)
    return {"corriges": sorted(i for i in before - now if i in current),
            "nouveaux": sorted(now - before),
            "inchanges": unchanged,
            "message_change": sorted(i for i in unchanged if message_signature(previous[i].get("message", ""))
                                     != message_signature(current[i].get("message", ""))),
            "disparus": sorted(i for i in previous if i not in current)}


def agent_scenarios(state: dict) -> dict:
    """Scénarios générés par l'agent : {identifiant: {tag, spec}} (tags des lots, spécifique des indicateurs)."""
    spec_of = {scenario_id(s["fichier"], s["titre"]): s.get("spec", "") for lot in state.get("lots", [])
               for s in (lot.get("indicateurs") or {}).get("scenarios", [])}
    return {identifier: {"tag": tag, "spec": spec_of.get(identifier, "")}
            for lot in state.get("lots", []) for tag, identifier in (lot.get("tags") or {}).items()}


def correction_proposals(failures: dict, state: dict) -> list:
    """Lots de correction proposés : échecs « test » des scénarios de l'agent, un lot par spécifique."""
    ours, by_spec = agent_scenarios(state), {}
    for identifier, result in failures.items():
        if identifier in ours and _category(result) == "test":
            fichier, titre = identifier.split("::", 1)
            by_spec.setdefault(ours[identifier]["spec"], []).append(
                {"tag": ours[identifier]["tag"], "id": identifier, "fichier": fichier, "titre": titre,
                 "step": result.get("step", ""), "message": result.get("message", "")})
    return [{"spec": spec, "scenarios": items} for spec, items in sorted(by_spec.items())]


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
    facts = next((console_facts(f["texte"]) for f in files if f["type"] == "console"), {})
    logs = [f["texte"] for f in files if f["type"] == "logs"]
    out = {"numero": numero, "run": entry["run"], "build": entry.get("build", ""), "console": facts,
           "fichiers": [{k: f[k] for k in ("nom", "type", "masques")} for f in files], "non_analyse": "",
           "tests": report_test_count(report), "groupes": [], "logs": [], "comparaison": {}, "propositions": [],
           "resultats": {}}
    if facts.get("patch_non_applicable"):
        out["non_analyse"] = "patch non applicable : le run n'a pas exécuté les tests de cette version"
    elif not report:
        out["non_analyse"] = "aucun rapport Cucumber JSON dans l'import"
    else:
        out["non_analyse"] = _count_gap(report, facts, worktree, state)
    if not out["non_analyse"]:
        results = {scenario_id(_rel(rel), title): {k: v for k, v in r.items() if k not in ("journal", "sorties")}
                   for (rel, title), r in parse_cucumber_report(report).items()}
        failures = {i: r for i, r in results.items() if r["statut"] != "passe"}
        out["resultats"] = results
        out["groupes"] = group_failures(failures)
        for identifier, result in list(failures.items())[:30]:
            item = {"step": result.get("step", ""), "motif": result.get("message", ""), "heure": result.get("heure")}
            covered = [log_coverage(text, item["heure"] or "") for text in logs]
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
        out["propositions"] = correction_proposals(failures, state)
    folder = os.path.join(imports_dir(client_name), str(numero))
    _write_json(os.path.join(folder, "analyse.json"), out)
    _write_text(os.path.join(folder, "analyse.md"), analysis_markdown(out))
    return out


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
    comparison = analysis.get("comparaison") or {}
    if comparison:
        lines += [f"## Comparaison avec l'import n° {comparison['import']}", "",
                  f"- Corrigés : {len(comparison['corriges'])} ; nouveaux : {len(comparison['nouveaux'])} ; "
                  f"inchangés : {len(comparison['inchanges'])} (message changé : "
                  f"{len(comparison['message_change'])}) ; disparus : {len(comparison['disparus'])}", ""]
    lines += ["## Causes racine", ""]
    for g in analysis["groupes"]:
        lines += [f"### {g['libelle']} ({g['categorie']}) — {len(g['scenarios'])} scénario(s)", "",
                  f"- Step : `{g['step'] or '—'}`", f"- Message : `{g['message']}`"]
        lines += [f"  - {i}" for i in g["scenarios"][:20]] + [""]
    uncovered = [entry for entry in analysis["logs"] if entry["a_exporter"]]
    if uncovered:
        lines += ["## Logs serveur à exporter", ""] + [f"- {e['id']} : {e['a_exporter']}" for e in uncovered] + [""]
    if analysis["propositions"]:
        lines += ["## Lots de correction proposés (échecs « test » des scénarios de l'agent)", ""]
        for p in analysis["propositions"]:
            lines.append(f"- {p['spec'] or '(spécifique inconnu)'} : {len(p['scenarios'])} scénario(s)")
    return "\n".join(lines) + "\n"


def add_correction_lots(state: dict, analysis: dict) -> list:
    """
    Lots « correction » ajoutés à la génération pour les propositions d'une analyse : scénarios existants avec leur
    message réel, sans spécifique à générer (ids vides). Renvoie les nouveaux lots.
    """
    start = max((lot["numero"] for lot in state.get("lots", [])), default=0)
    new = []
    for offset, proposal in enumerate(analysis.get("propositions") or [], 1):
        scenarios = proposal["scenarios"]
        new.append({"numero": start + offset, "ids": [], "type": "correction", "statut": "a_faire",
                    "complement": True, "import": analysis["numero"], "spec_correction": proposal["spec"],
                    "tags": {s["tag"]: s["id"] for s in scenarios}, "types": {}, "corrections": scenarios,
                    "indicateurs": {"scenarios": [{"fichier": s["fichier"], "titre": s["titre"],
                                                   "spec": proposal["spec"], "motifs": []} for s in scenarios]}})
    state["lots"] = state.get("lots", []) + new
    return new
