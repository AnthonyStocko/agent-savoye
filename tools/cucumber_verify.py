"""
Vérification des tests Cucumber générés pour un client (étape generate-cucumber-client), sans token :

- niveau 1, statique : doublons, absence d'assertion métier, champs du JSON vides, réutilisation des steps ;
  identifiant stable et tags @agent_<client>_<lot> de chaque scénario ;
- niveau 2, exécution sur le client : job Jenkins dédié (patch en fichier) ou commande locale, rapport Cucumber JSON ;
- niveau 3, contre-épreuves : override rejoué sur l'environnement Standard, mutation des attendus des Then.

Claude n'est appelé (hors de ce module) que pour corriger, de façon bornée.
"""
import difflib
import json
import os
import random
import re
import subprocess
import time

from tools import jenkins_tools, memory_tools
from tools.cucumber_tools import (_HTTP_CALL, _expression_regex, _first_argument, _git, _http_path, _java_methods,
                                  _read_text_file, _resolve, _step_prefix, _string_constants, _write_text,
                                  WORKSPACE_DIR, blob_texts, disk_class_constants, extract_cucumber,
                                  java_package_segment, ls_tree, report_unresolved_steps, step_expressions)

# ---------------------------------------------------------------------------
# Niveau 1 : contrôle statique (doublons, assertions, réutilisation, champs du JSON)
# ---------------------------------------------------------------------------

DEFAULT_DUP_THRESHOLD = 0.85
_PHASES = {"given": "given", "soit": "given", "sachant que": "given", "etant donné": "given", "étant donné": "given",
           "etant donnée": "given", "étant donnée": "given", "etant donnés": "given", "étant donnés": "given",
           "when": "when", "quand": "when", "lorsque": "when", "then": "then", "alors": "then"}
_STEP_LINE = re.compile(r"^\s*(Given|When|Then|And|But|\*|Soit|Sachant que|[EÉ]tant donn[ée]e?s?|Quand|Lorsque|Alors|"
                        r"Et|Mais)\s+(.+?)\s*$")
_SCENARIO_LINE = re.compile(r"^\s*(Scenario Outline|Scenario Template|Scenario|Example|Plan du [Ss]cénario|Scénario|"
                            r"Exemple)\s*:\s*(.*)$")
_BACKGROUND_LINE = re.compile(r"^\s*(Background|Contexte)\s*:", re.I)
_SETUP_TITLE = re.compile(r"^(contexte|initialisation|r[ée]initialisation|description|purge|cr[ée]ation du contexte|"
                          r"mise en place)\b")
# Mise en place exécutée avec les scénarios du lot qui la suivent (tag_scenarios)
_SETUP_TAG = re.compile(r"^@agent_\w+_\d+_0$")
# Then sans valeur métier : statut HTTP, « succès », « sans erreur »
_GENERIC_THEN = re.compile(r"(?i)\b(code|statut|status)\b[^\"]*\b[1-5]\d\d\b|^\W*[1-5]\d\d\b|\b(réponse|response|requête|"
                           r"appel)\b.*\b(ok|succès|success|réussie?|valide)\s*$|\bsans erreur\b|\bs'exécute\b")


def parse_scenarios(text: str) -> list:
    """
    Scénarios d'une feature : [{titre, ligne, tags, outline, steps: [{phase, texte, table}]}] (Background exclu ;
    tables d'Examples ignorées).
    """
    scenarios, current, phase, tags, examples = [], None, "given", [], False
    for number, line in enumerate(text.splitlines(), 1):
        scenario = _SCENARIO_LINE.match(line)
        if scenario:
            current = {"titre": scenario.group(2).strip(), "ligne": number, "tags": tags, "steps": [],
                       "outline": "lan" in scenario.group(1) or "Outline" in scenario.group(1)
                       or "Template" in scenario.group(1)}
            scenarios.append(current)
            phase, tags, examples = "given", [], False
            continue
        if line.strip().startswith("@"):
            tags = tags + [t for t in line.split() if t.startswith("@")]
            continue
        if re.match(r"^\s*(Feature|Fonctionnalité)\s*:", line):
            tags = []
            continue
        if _BACKGROUND_LINE.match(line):
            current, tags = None, []
            continue
        if re.match(r"^\s*(Examples|Exemples|Scenarios)\s*:", line):
            examples = True
            continue
        step = _STEP_LINE.match(line)
        if examples:
            continue
        if step and current is not None:
            keyword = step.group(1).lower()
            phase = _PHASES.get(keyword, phase)
            current["steps"].append({"phase": phase, "texte": step.group(2), "table": []})
        elif line.strip().startswith("|") and current is not None and current["steps"]:
            current["steps"][-1]["table"].append(" ".join(line.split()))
    return scenarios


def step_matcher(expressions) -> callable:
    """Fonction texte de step -> expression qui le définit (None si aucune), par préfixe puis regex."""
    by_prefix = {}
    for expr in dict.fromkeys(expressions):
        regex = _expression_regex(expr)
        if regex:
            by_prefix.setdefault(_step_prefix(expr), []).append((regex, expr))

    def match(text):
        for regex, expr in by_prefix.get(_step_prefix(text), []) + by_prefix.get("(sans préfixe)", []):
            if regex.match(text):
                return expr
        return None
    return match


def _disk_steps(root: str, rel_paths=None) -> dict:
    """{expression: chemin relatif} des Step Definitions des .java sur disque (tous ceux de src/test/java ou `rel_paths`)."""
    paths = rel_paths
    if paths is None:
        base = os.path.join(root, "src", "test", "java")
        paths = [os.path.relpath(os.path.join(d, f), root).replace("\\", "/") for d, _, names in os.walk(base)
                 for f in names if f.endswith(".java")] if os.path.isdir(base) else []
    found, class_constants = {}, disk_class_constants(os.path.join(root, "src", "test", "java"))
    for rel in paths:
        text = _read_text_file(os.path.join(root, *rel.split("/")))
        for expr in step_expressions(text, class_constants)[0]:
            found.setdefault(expr.replace('\\"', '"'), rel)
    return found


def std_reference(cucumber_std_dir: str, std_endpoints: dict) -> dict:
    """
    Référence Standard des contrôles (calculée une fois par exécution) : expressions des steps, scénarios
    (titre, steps et tables) pour les doublons, endpoints par expression de step et par méthode Java.
    """
    catalogue = extract_cucumber(cucumber_std_dir, "HEAD")
    report_unresolved_steps("Cucumber Standard", catalogue)
    tree = ls_tree(cucumber_std_dir, "HEAD")
    texts = blob_texts(cucumber_std_dir, [tree[f["path"]] for f in catalogue["features"] if f["path"] in tree])
    scenarios =[{"path": f["path"], "titre": s["titre"], "steps": [_step_key(st) for st in s["steps"]]}
                 for f in catalogue["features"] for s in parse_scenarios(texts.get(tree.get(f["path"]), ""))]
    by_step = {}
    for e in std_endpoints.get("endpoints", []):
        for expr in e["steps"]:
            by_step.setdefault(expr, []).append(f"{e['methode']} {e['chemin']}")
    # Scénarios de structure, présents dans au moins 10 features (« Description de la fiche de test »,
    # « Réinitialisation du contexte »...) : reproduits volontairement, exclus des contrôles
    counts = {}
    for scenario in scenarios:
        key = _title_key(scenario["titre"])
        counts[key] = counts.get(key, 0) + 1
    return {"steps": [s["expr"] for s in catalogue["steps"]], "scenarios": scenarios, "endpoints_par_step": by_step,
            "appelants": std_endpoints.get("appelants", {}),
            "titres_structurels": sorted(k for k, n in counts.items() if n >= 10 and re.search(
                r"contexte|description|initialisation|r[ée]initialisation|purge", k))}


def _step_key(step: dict) -> str:
    return " ".join([step["texte"].lower()] + [row.lower() for row in step["table"]])


def _similar(a, b, threshold: float) -> float:
    """Similarité de deux titres ou listes de steps, calculée seulement si les bornes rapides atteignent le seuil."""
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
        return 0.0
    return matcher.ratio()


def _title_key(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", title.lower()).split())


def _lot_scenarios(worktree: str, features: list, before_ref: str = "HEAD") -> tuple:
    """(scénarios nouveaux des .feature du lot, scénarios client déjà là) ; un scénario existant au HEAD n'est pas nouveau."""
    new, existing = [], []
    for rel in features:
        before = {_title_key(s["titre"]) for s in parse_scenarios(_git(worktree, "show", f"{before_ref}:{rel}").stdout)}
        for scenario in parse_scenarios(_read_text_file(os.path.join(worktree, *rel.split("/")))):
            scenario["fichier"] = rel
            (existing if _title_key(scenario["titre"]) in before else new).append(scenario)
    base = os.path.join(worktree, "src", "test", "resources")
    for directory, _, names in os.walk(base) if os.path.isdir(base) else ():
        for name in names:
            rel = os.path.relpath(os.path.join(directory, name), worktree).replace("\\", "/")
            if name.endswith(".feature") and rel not in features:
                for scenario in parse_scenarios(_read_text_file(os.path.join(directory, name))):
                    scenario["fichier"] = rel
                    existing.append(scenario)
    return new, existing


def _result_scenarios(results: list) -> list:
    """Scénarios déclarés dans le JSON final : [(id du spécifique, objet scénario)] (ancien format texte accepté)."""
    out = []
    for r in results or []:
        for s in r.get("scenarios") or []:
            out.append((r.get("id", ""), s if isinstance(s, dict) else {"nom": str(s)}))
    return out


def _declared(scenario: dict, results: list):
    """(id du spécifique, objet JSON) qui déclare un scénario analysé : même fichier et même titre."""
    title = _title_key(scenario["titre"])
    for spec_id, declared in _result_scenarios(results):
        name = _title_key(declared.get("nom", "").rsplit(" / ", 1)[-1])
        same_file = not declared.get("fichier") or declared["fichier"].replace("\\", "/").endswith(scenario["fichier"])
        if same_file and title and (name == title or (len(title) > 15 and title in _title_key(declared.get("nom", "")))):
            return spec_id, declared
    for r in results or []:
        if any(scenario["fichier"].endswith(f.replace("\\", "/")) for f in r.get("fichiers") or [] if f):
            return r.get("id", ""), None
    return "", None


def _client_step_endpoints(worktree: str, java_files: list, reference: dict) -> dict:
    """{expression de step client: [endpoints]} : helpers du Standard appelés, ou appels HTTP écrits dans la classe."""
    found, class_constants = {}, disk_class_constants(os.path.join(worktree, "src", "test", "java"))
    for rel in java_files:
        text = _read_text_file(os.path.join(worktree, *rel.split("/")))
        constants = _string_constants(text)
        methods = _java_methods(text, class_constants)
        local = {m["name"]: m for m in methods}
        for method in methods:
            if not method["steps"]:
                continue
            bodies = [method] + [local[n] for n in re.findall(r"(?<![.\w])(\w+)\s*\(", method["masked"])
                                 if n in local and n != method["name"]]
            endpoints = []
            for body in bodies:
                for cls, name in re.findall(r"\b([A-Z]\w+)\s*\.\s*(\w+)\s*\(", body["masked"]):
                    endpoints += reference["appelants"].get(f"{cls}#{name}", [])
                for call in _HTTP_CALL.finditer(body["masked"]):
                    verb = (call.group(1) or call.group(2)).upper()
                    path = _resolve(_first_argument(body["masked"], body["body"], call.end() - 1), constants, {})
                    if path:
                        endpoints.append(f"{'GET' if verb == 'COUNT' else verb} {_http_path(path)} (client)")
            for expr in method["steps"]:
                found[expr.replace('\\"', '"')] = list(dict.fromkeys(endpoints))
    return found


def analyse_lot(worktree: str, files: list, results: list, reference: dict, threshold: float = None,
                before_ref: str = "HEAD") -> dict:
    """
    Contrôles Python des scénarios nouveaux d'un lot : doublons (titre ou steps, similarité >= threshold) d'un
    scénario Standard ou client, absence d'assertion métier, champs difference_standard / source vides ; steps
    Standard réutilisés / client / non définis et endpoints utilisés, par scénario et pour le lot. `before_ref` :
    version des fichiers avant le lot (ses scénarios ne sont pas nouveaux).
    """
    if threshold is None:
        try:
            threshold = float(os.getenv("CUCUMBER_DUP_THRESHOLD", "").strip() or DEFAULT_DUP_THRESHOLD)
        except ValueError:
            threshold = DEFAULT_DUP_THRESHOLD
    features = [f for f in files if f.endswith(".feature")]
    java_files = [f for f in files if f.endswith(".java")]
    new, existing = _lot_scenarios(worktree, features, before_ref)
    structural = set(reference.get("titres_structurels", []))

    def is_setup(scenario):
        # Mise en place reprise volontairement du Standard (« Contexte… », « Initialisation… ») : pas un test
        title = _title_key(scenario["titre"])
        return title in structural or bool(_SETUP_TITLE.match(title))
    new = [s for s in new if not is_setup(s)]
    existing = [s for s in existing if not is_setup(s)]
    std_match = step_matcher(reference["steps"])
    client_steps = _disk_steps(worktree)
    client_match = step_matcher(client_steps)
    created = _disk_steps(worktree, java_files)
    client_endpoints = _client_step_endpoints(worktree, sorted({client_steps[e] for e in client_steps}), reference)
    candidates = [(s["path"], s["titre"], s["steps"]) for s in reference["scenarios"]
                  if _title_key(s["titre"]) not in structural and not _SETUP_TITLE.match(_title_key(s["titre"]))] + [
        (s["fichier"], s["titre"], [_step_key(st) for st in s["steps"]]) for s in existing]
    analysed = []
    for scenario in new:
        keys = [_step_key(st) for st in scenario["steps"]]
        counts, endpoints, motifs = {"std": 0, "client": 0, "non_definis": 0}, [], []
        for step in scenario["steps"]:
            expr = std_match(step["texte"])
            if expr:
                counts["std"] += 1
                endpoints += reference["endpoints_par_step"].get(expr, [])
                continue
            expr = client_match(step["texte"])
            counts["client" if expr else "non_definis"] += 1
            endpoints += client_endpoints.get(expr, []) if expr else []
        title = _title_key(scenario["titre"])
        best = (0.0, "", "")
        for path, other_title, other_keys in candidates:
            title_sim = _similar(title, _title_key(other_title), threshold) if title else 0.0
            steps_sim = _similar(keys, other_keys, threshold) if len(keys) >= 3 and len(other_keys) >= 3 else 0.0
            if max(title_sim, steps_sim) > best[0]:
                best = (max(title_sim, steps_sim), path, other_title)
        if best[0] >= threshold:
            motifs.append(f"doublon de {best[1]} / {best[2]} (similarité {best[0]:.2f})")
        thens = [st["texte"] for st in scenario["steps"] if st["phase"] == "then"]
        if not thens:
            motifs.append("aucune assertion (pas de Then)")
        elif all(_GENERIC_THEN.search(t) for t in thens):
            motifs.append("assertions génériques uniquement (statut HTTP, succès)")
        spec_id, declared = _declared(scenario, results)
        if declared is not None:
            for field in ("difference_standard", "source"):
                if not str(declared.get(field) or "").strip():
                    motifs.append(f"« {field} » vide dans le JSON")
        analysed.append({"fichier": scenario["fichier"], "titre": scenario["titre"], "ligne": scenario["ligne"],
                         "spec": spec_id, "steps_std": counts["std"], "steps_client": counts["client"],
                         "steps_non_definis": counts["non_definis"], "endpoints": list(dict.fromkeys(endpoints))[:8],
                         "motifs": motifs})
    total = sum(s["steps_std"] + s["steps_client"] + s["steps_non_definis"] for s in analysed)
    std = sum(s["steps_std"] for s in analysed)
    return {"scenarios": analysed, "nb_scenarios": len(analysed), "steps_std": std,
            "steps_client": sum(s["steps_client"] for s in analysed),
            "steps_non_definis": sum(s["steps_non_definis"] for s in analysed), "steps_crees": len(created),
            "taux_reutilisation": round(100 * std / total) if total else None,
            "signales": sum(1 for s in analysed if s["motifs"])}


def apply_review(results: list, before: dict, after: dict, decisions: list) -> list:
    """
    Après la revue : scénarios signalés absents des fichiers = supprimés ; retirés du JSON du lot (spécifique sans
    scénario restant : non couvert) ; difference_standard / source complétés par la revue. Renvoie les supprimés.
    """
    remaining = {(s["fichier"], _title_key(s["titre"])) for s in after["scenarios"]}
    decisions = [d for d in decisions or [] if isinstance(d, dict)]
    deleted = []
    for s in before["scenarios"]:
        if s["motifs"] and (s["fichier"], _title_key(s["titre"])) not in remaining:
            decision = next((d for d in decisions if _title_key(str(d.get("scenario", ""))).endswith(
                _title_key(s["titre"]))), {})
            deleted.append({"fichier": s["fichier"], "titre": s["titre"], "spec": s["spec"], "motifs": s["motifs"],
                            "justification": decision.get("justification", "")})
    for r in results or []:
        kept = []
        for declared in r.get("scenarios") or []:
            obj = declared if isinstance(declared, dict) else {"nom": str(declared)}
            name = _title_key(obj.get("nom", "").rsplit(" / ", 1)[-1])
            if any(name == _title_key(d["titre"]) and d["spec"] in ("", r.get("id")) for d in deleted):
                continue
            decision = next((d for d in decisions if name and _title_key(str(d.get("scenario", ""))).endswith(name)), {})
            for field in ("difference_standard", "source"):
                if isinstance(declared, dict) and not str(obj.get(field) or "").strip() and decision.get(field):
                    obj[field] = decision[field]
            kept.append(declared)
        if r.get("statut") == "couvert" and r.get("scenarios") and not kept:
            r["statut"] = "non_couvert"
            reasons = {m.split(" (")[0] for d in deleted if d["spec"] in ("", r.get("id")) for m in d["motifs"]}
            r["raison"] = "Scénario(s) supprimé(s) à la revue : " + " ; ".join(sorted(reasons))
        r["scenarios"] = kept
    return deleted


def review_block(lot_specs: list, analysis: dict, results: list) -> str:
    """Partie variable du prompt de revue : seulement les scénarios signalés, leur motif et le test Standard lié."""
    specs = {s["id"]: s for s in lot_specs}
    lines = []
    for s in analysis["scenarios"]:
        if not s["motifs"]:
            continue
        spec = specs.get(s["spec"], {})
        _, declared = _declared(s, results)
        tests = spec.get("tests_std") or []
        lines += [f"### '{s['fichier']}' (ligne {s['ligne']}) — Scénario : {s['titre']}",
                  f"- Spécifique : {spec.get('libelle', s['spec'] or 'inconnu')} ({spec.get('type', '?')})",
                  f"- Motif(s) : {' ; '.join(s['motifs'])}",
                  f"- Test Standard lié : '{tests[0]['extrait']}' — {tests[0]['feature']}" if tests and tests[0].get(
                      "extrait") else "- Test Standard lié : aucun"]
        if declared:
            lines.append(f"- JSON du lot : difference_standard = « {declared.get('difference_standard', '')} » ; "
                         f"source = « {declared.get('source', '')} »")
        lines.append("")
    return "\n".join(lines).rstrip()


def parse_review(text: str) -> list:
    """Décisions de la revue (liste JSON), [] si absentes."""
    for candidate in re.findall(r"```(?:json)?\s*(.*?)```", text or "", flags=re.S)[::-1] + [text or ""]:
        start, end = candidate.find("["), candidate.rfind("]")
        if 0 <= start < end:
            try:
                data = json.loads(candidate[start:end + 1])
            except ValueError:
                continue
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
    return []




# ---------------------------------------------------------------------------
# Identifiants et tags des scénarios d'un lot
# ---------------------------------------------------------------------------

def scenario_id(feature: str, title: str) -> str:
    """Identifiant stable d'un scénario : <feature>::<scenario>."""
    return f"{feature}::{title.strip()}"


def lot_tag(client_name: str, lot_number: int) -> str:
    return f"@agent_{java_package_segment(client_name)}_{lot_number}"


def _add_tags(lines: list, index: int, tags: tuple) -> None:
    """Ajoute `tags` à la ligne de tags du scénario de la ligne `index` (0-based), créée au besoin."""
    indent = re.match(r"^\s*", lines[index]).group(0)
    if index > 0 and lines[index - 1].strip().startswith("@"):
        lines[index - 1] = lines[index - 1].rstrip() + " " + " ".join(
            t for t in tags if t not in lines[index - 1].split())
    else:
        lines.insert(index, f"{indent}{' '.join(tags)}")


def is_setup_scenario(title: str, tags=()) -> bool:
    """Scénario de mise en place (« Contexte… », « Initialisation… ») ou tagué comme tel (@agent_<client>_<lot>_0)."""
    return bool(_SETUP_TITLE.match(_title_key(title))) or any(_SETUP_TAG.match(t) for t in tags)


def tag_scenarios(worktree: str, scenarios: list, client_name: str, lot_number: int) -> dict:
    """
    Ajoute à chaque scénario nouveau du lot (analyse du niveau 1) le tag du lot et son tag propre
    (@agent_<client>_<lot>_<n>), pour l'exécuter seul. Les scénarios de mise en place qui les précèdent dans la
    même feature reçoivent @agent_<client>_<lot>_0 (s'ils n'ont pas déjà un tel tag) : ils sont exécutés avec eux.
    Renvoie {tag du scénario: identifiant} (sans les mises en place).
    """
    base, tagged, by_file = lot_tag(client_name, lot_number), {}, {}
    for scenario in scenarios:
        by_file.setdefault(scenario["fichier"], []).append(scenario)
    used = {t for s in scenarios for t in s.get("tags", []) if t.startswith(base + "_")}
    counter = 1
    for rel, items in by_file.items():
        path = os.path.join(worktree, *rel.split("/"))
        lines = _read_text_file(path).splitlines()
        parsed = parse_scenarios("\n".join(lines))
        current = {s["titre"]: s for s in parsed}
        targets = [current[s["titre"]] for s in items if s["titre"] in current]
        last = max((s["ligne"] for s in targets), default=0)
        setups = [s for s in parsed if s["ligne"] < last and is_setup_scenario(s["titre"])
                  and not any(_SETUP_TAG.match(t) for t in s["tags"]) and s not in targets]
        for found in sorted(targets + setups, key=lambda s: -s["ligne"]):
            if found in setups:
                _add_tags(lines, found["ligne"] - 1, (f"{base}_0",))
                continue
            own = next((t for t in found["tags"] if t.startswith(base + "_")), "")
            if not own:
                while f"{base}_{counter}" in used:
                    counter += 1
                own = f"{base}_{counter}"
                used.add(own)
                _add_tags(lines, found["ligne"] - 1, (base, own))
            tagged[own] = scenario_id(rel, found["titre"])
        _write_text(path, "\n".join(lines) + "\n")
    return tagged


def setup_tags(worktree: str, files: list, tags) -> set:
    """Tags @agent_…_0 des mises en place des features qui contiennent les scénarios `tags`, à exécuter avec eux."""
    out = set()
    for rel, _ in tagged_scenarios(worktree, files, tags).values():
        for scenario in parse_scenarios(_read_text_file(os.path.join(worktree, *rel.split("/")))):
            out |= {t for t in scenario["tags"] if _SETUP_TAG.match(t)}
    return out


def tagged_scenarios(worktree: str, files: list, tags) -> dict:
    """{tag du scénario: (fichier, scénario)} des scénarios des .feature du lot portant l'un des tags."""
    wanted, found = set(tags), {}
    for rel in files:
        if rel.endswith(".feature"):
            for scenario in parse_scenarios(_read_text_file(os.path.join(worktree, *rel.split("/")))):
                for tag in set(scenario["tags"]) & wanted:
                    found[tag] = (rel, scenario)
    return found


# ---------------------------------------------------------------------------
# Niveau 2 : exécution (job Jenkins dédié ou commande locale) et rapport Cucumber JSON
# ---------------------------------------------------------------------------

REQUIRED_JOB_PARAMETERS = ("PATCH", "TAGS", "ENV", "CUCUMBER_SPEC_REPOSITORY")
# Commits de base : sans eux, le job part du dernier commit des branches et le patch peut ne plus s'appliquer
PINNING_JOB_PARAMETERS = ("CUCUMBER_SPEC_COMMIT", "CUCUMBER_STANDARD_COMMIT")
NOT_VERIFIED = "pertinence non vérifiée par exécution"
PATCH_NOT_APPLICABLE = "non vérifié : patch non applicable"
# Marqueur archivé par le Jenkinsfile quand `git apply` échoue à l'étape Sources
PATCH_STATUS_ARTIFACT = "verification-status.txt"


def job_sources(cucumber_repo: str, base_branch: str, base_commit: str, standard_commit: str) -> dict:
    """Paramètres du job désignant les sources de l'exécution locale : dépôt client, branche et commits de base."""
    return {"CUCUMBER_SPEC_REPOSITORY": cucumber_repo, "CUCUMBER_SPEC_BRANCH": base_branch,
            "CUCUMBER_SPEC_COMMIT": base_commit, "CUCUMBER_STANDARD_COMMIT": standard_commit}


def execution_mode() -> tuple:
    """
    ("jenkins" | "local" | "", message) : job CUCUMBER_VERIFY_JOB s'il accepte PATCH (fichier), TAGS, ENV et
    CUCUMBER_SPEC_REPOSITORY, sinon commande CUCUMBER_VERIFY_LOCAL_CMD, sinon aucun moyen d'exécution (niveau 1
    seulement). Job sans CUCUMBER_SPEC_COMMIT / CUCUMBER_STANDARD_COMMIT : utilisable, avec un avertissement.
    """
    job = os.getenv("CUCUMBER_VERIFY_JOB", "").strip()
    reasons = []
    if job and os.getenv("JENKINS_URL", "").strip():
        parameters = jenkins_tools.job_parameters(job)
        missing = [p for p in REQUIRED_JOB_PARAMETERS if p not in parameters]
        if parameters and not missing and "File" in parameters["PATCH"]:
            unpinned = [p for p in PINNING_JOB_PARAMETERS if p not in parameters]
            return "jenkins", f"job Jenkins {job}" + (
                f" (⚠️ paramètres {', '.join(unpinned)} absents du job : l'application du patch peut échouer si la "
                f"branche a avancé depuis la base)" if unpinned else "")
        reasons.append(f"job {job} inutilisable (" + (f"paramètres absents : {', '.join(missing)}" if parameters
                                                      and missing else "PATCH n'est pas un paramètre fichier"
                                                      if parameters else "introuvable") + ")")
    if os.getenv("CUCUMBER_VERIFY_LOCAL_CMD", "").strip():
        return "local", "commande locale CUCUMBER_VERIFY_LOCAL_CMD" + (f" ({reasons[0]})" if reasons else "")
    return "", f"{NOT_VERIFIED} : " + (reasons[0] if reasons else
                                     "ni CUCUMBER_VERIFY_JOB ni CUCUMBER_VERIFY_LOCAL_CMD configuré")


def _phase_of(keyword: str, previous: str) -> str:
    return _PHASES.get(keyword.strip().lower(), previous)


_ASSERTION_FAILURE = re.compile(r"AssertionError|ComparisonFailure|AssertionFailedError|\bexpected\b|attendu|"
                                r"but was|Expecting", re.I)
_EXCEPTION_NAME = re.compile(r"\b((?:[a-z_]\w*\.)*[A-Z]\w*(?:Exception|Error|Failure))\b")


# Environnement de test incomplet : donnée de référence absente (« Site non trouvé(e) avec code VDA »), route absente
_ENVIRONMENT_FAILURE = re.compile(r"non\s+trouv[ée]+(?:\(e\))?s?\s+avec\s+(?:le\s+)?code|No route found|"
                                  r"\b404\s*-\s*\w", re.I)
ENV_NOT_VERIFIED = "non vérifié — environnement"


def failure_kind(error_message: str) -> tuple:
    """(environnement | assertion | technique, nom de l'exception) d'un message d'échec Cucumber."""
    message = str(error_message or "")
    exception = _EXCEPTION_NAME.search(message)
    name = exception.group(1).rsplit(".", 1)[-1] if exception else ""
    if _ENVIRONMENT_FAILURE.search(message):
        return "environnement", name
    return ("assertion" if _ASSERTION_FAILURE.search(message) else "technique"), name


def parse_cucumber_report(report: list) -> dict:
    """
    Rapport Cucumber JSON -> {(fichier relatif, titre): {statut: passe|echoue|indefini|cascade, step, phase, message,
    nature (environnement | assertion | technique, échec seulement), exception}} ; Background rattaché au scénario
    qui le suit, exemples d'un Scenario Outline agrégés (le pire l'emporte). Après l'échec d'un scénario de mise en
    place, les scénarios suivants de la feature qui ne passent pas sont « cascade » (cause : nature de cet échec).
    """
    rank = {"passe": 0, "indefini": 1, "cascade": 2, "echoue": 3}
    results = {}
    for feature in report if isinstance(report, list) else []:
        uri = str(feature.get("uri", "")).replace("\\", "/")
        rel = "src/test/resources/" + uri.split("src/test/resources/", 1)[1] if "src/test/resources/" in uri else \
            re.sub(r"^(file|classpath):/*", "", uri)
        background, setup_failed = [], None
        for element in feature.get("elements") or []:
            if element.get("type") == "background":
                background = element.get("steps") or []
                continue
            steps, phase, outcome = background + (element.get("steps") or []), "given", None
            background = []
            statuses = []
            for step in steps:
                phase = _phase_of(step.get("keyword", ""), phase)
                status = (step.get("result") or {}).get("status", "")
                statuses.append(status)
                if status == "failed" and not outcome:
                    error = str((step.get("result") or {}).get("error_message", ""))
                    nature, exception = failure_kind(error)
                    outcome = {"statut": "echoue", "step": f"{step.get('keyword', '').strip()} {step.get('name', '')}",
                               "phase": phase, "message": " ".join(error.split())[:300], "nature": nature,
                               "exception": exception}
            if not outcome:
                if any(s in ("undefined", "pending", "ambiguous") for s in statuses) or not statuses:
                    outcome = {"statut": "indefini", "step": "", "phase": "", "message": "step non défini ou ignoré"}
                elif all(s == "passed" for s in statuses):
                    outcome = {"statut": "passe", "step": "", "phase": "", "message": ""}
                else:
                    outcome = {"statut": "indefini", "step": "", "phase": "", "message": "steps ignorés"}
            title = str(element.get("name", "")).strip()
            if setup_failed and outcome["statut"] != "passe":
                outcome = {**outcome, "statut": "cascade", "cause": setup_failed[2],
                           "message": f"mise en place « {setup_failed[0]} » en échec : {setup_failed[1]}"[:300]}
            elif outcome["statut"] == "echoue" and is_setup_scenario(
                    title, [t.get("name", "") for t in element.get("tags") or [] if isinstance(t, dict)]):
                setup_failed = (title, f"{outcome['step']} — {outcome['message']}"[:200], outcome["nature"])
            key = (rel, title)
            if key not in results or rank[outcome["statut"]] > rank[results[key]["statut"]]:
                results[key] = outcome
    return results


def _report_files(build: str) -> list:
    target = os.path.join(build, "target")
    found = []
    for directory, _, names in os.walk(target) if os.path.isdir(target) else ():
        found += [os.path.join(directory, n) for n in names if n.lower().endswith(".json") and "cucumber" in
                  (directory + n).lower()]
    return found


def _read_report(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def run_tags(mode: str, worktree: str, base: str, build_fn, tags: str, env: str, overrides: dict, label: str,
             sources: dict = None) -> dict:
    """
    Exécute les scénarios filtrés par `tags` sur l'environnement `env` ; `overrides` {chemin relatif: texte}
    remplace des .feature le temps de l'exécution (mutation) ; `sources` (job_sources) : paramètres du job Jenkins
    désignant le dépôt et les commits de base. Renvoie {ok, message, build, resultats}.
    """
    started = time.time()
    if mode == "local":
        command = os.getenv("CUCUMBER_VERIFY_LOCAL_CMD", "").strip()
        if env and "{env}" not in command:
            return {"ok": False, "message": "CUCUMBER_VERIFY_LOCAL_CMD sans {env} : environnement non sélectionnable",
                    "build": "", "resultats": {}}
        build = build_fn()
        for rel, text in overrides.items():
            _write_text(os.path.join(build, *rel.split("/")), text)
        for old in _report_files(build):
            os.remove(old)
        command = command.replace("{tags}", tags).replace("{env}", env)
        try:
            run = subprocess.run(command, cwd=build, shell=True, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=_timeout())
            output = f"{run.stdout}\n{run.stderr}"
        except subprocess.TimeoutExpired:
            return {"ok": False, "message": f"délai CUCUMBER_VERIFY_TIMEOUT dépassé ({_timeout()} s)", "build": "",
                    "resultats": {}}
        report = [r for path in _report_files(build) for r in _read_report(path)]
        if not report:
            tail = " | ".join(line.strip() for line in output.strip().splitlines()[-3:])
            return {"ok": False, "message": f"aucun rapport Cucumber JSON produit ({tail[:300]})", "build": "",
                    "resultats": {}}
        return {"ok": True, "message": f"exécution locale en {time.time() - started:.0f} s", "build": "",
                "resultats": parse_cucumber_report(report)}
    # Jenkins : patch du worktree depuis la base (fichiers mutés écrits le temps de produire le patch)
    originals = {rel: _read_text_file(os.path.join(worktree, *rel.split("/"))) for rel in overrides}
    try:
        for rel, text in overrides.items():
            _write_text(os.path.join(worktree, *rel.split("/")), text)
        _git(worktree, "add", "-N", ".")
        patch = _git(worktree, "diff", base, "--", ".", ":(exclude)target", ":(exclude)**/target/**").stdout
    finally:
        for rel, text in originals.items():
            _write_text(os.path.join(worktree, *rel.split("/")), text)
    patch_file = os.path.join(os.path.dirname(worktree), "verification", f"{label}.patch")
    _write_text(patch_file, patch)
    parameters = {"TAGS": tags, "ENV": env, **{k: v for k, v in (sources or {}).items() if v}}
    queue = jenkins_tools.trigger_cucumber_job_with_file(
        os.getenv("CUCUMBER_VERIFY_JOB", "").strip(), parameters, "PATCH", patch_file)
    if not queue:
        return {"ok": False, "message": "lancement du job Jenkins impossible", "build": "", "resultats": {}}
    build = jenkins_tools.wait_build_result(queue, _timeout())
    if build["result"] not in ("SUCCESS", "UNSTABLE") and _patch_not_applicable(build, patch_file, label):
        return {"ok": False, "message": PATCH_NOT_APPLICABLE, "build": build["url"], "resultats": {}}
    reports = [a for a in build["artifacts"] if a.lower().endswith(".json") and "cucumber" in a.lower()]
    report = []
    for artifact in reports:
        target = os.path.join(os.path.dirname(patch_file), f"{label}-{os.path.basename(artifact)}")
        if jenkins_tools.fetch_artifact(build["url"], artifact, target):
            report += _read_report(target)
    if not report:
        return {"ok": False, "message": f"build {build['result']} sans rapport Cucumber JSON", "build": build["url"],
                "resultats": {}}
    return {"ok": True, "message": f"build {build['result']}", "build": build["url"],
            "resultats": parse_cucumber_report(report), "artefacts": build["artifacts"]}


def _patch_not_applicable(build: dict, patch_file: str, label: str) -> bool:
    """Build en échec à l'étape Sources (patch non applicable) : marqueur archivé, à défaut étape en échec du pipeline."""
    marker = next((a for a in build["artifacts"] if a.rsplit("/", 1)[-1] == PATCH_STATUS_ARTIFACT), "")
    if marker:
        target = os.path.join(os.path.dirname(patch_file), f"{label}-{PATCH_STATUS_ARTIFACT}")
        if jenkins_tools.fetch_artifact(build["url"], marker, target):
            return "PATCH_NON_APPLICABLE" in _read_text_file(target)
    return bool(build["url"]) and jenkins_tools.failed_stage(build["url"]) == "Sources"


def _timeout() -> int:
    try:
        return max(60, int(os.getenv("CUCUMBER_VERIFY_TIMEOUT", "").strip() or 3600))
    except ValueError:
        return 3600


# ---------------------------------------------------------------------------
# Niveau 3 : mutation des attendus des Then
# ---------------------------------------------------------------------------

_BOOLEANS = {"true": "false", "false": "true", "vrai": "faux", "faux": "vrai", "TRUE": "FALSE", "FALSE": "TRUE",
             "True": "False", "False": "True", "oui": "non", "non": "oui"}


def _mutate_cell(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return value
    if stripped in _BOOLEANS:
        return value.replace(stripped, _BOOLEANS[stripped])
    number = re.fullmatch(r"(-?)(\d+)([.,]\d+)?", stripped)
    if number:
        return value.replace(stripped, f"{number.group(1)}{int(number.group(2)) + 1}{number.group(3) or ''}")
    return value.replace(stripped, stripped + "_MUT")


_QUOTED = re.compile(r'"([^"]*)"')
# En-tête de 1re colonne désignant le champ vérifié (pas une valeur attendue)
_KEY_HEADER = re.compile(r"(?i)^(champs?|cl[ée]s?|keys?|fields?|names?|noms?|codes?|propri[ée]t[ée]s?|attributs?)$")


def mutate_feature(text: str, tags: set) -> tuple:
    """
    Copie d'une feature où les attendus des Then des scénarios portant l'un des `tags` sont faussés : seul le
    dernier argument entre guillemets d'un Then (la valeur attendue, pas le nom du champ) est suffixé _MUT ; une
    ligne de données sur deux des tables des Then est modifiée dans ses colonnes de valeurs (nombres +1, booléens
    inversés, texte suffixé _MUT), sans la 1re colonne si son en-tête désigne un champ ou une clé.
    Renvoie (texte muté, {tag: nombre de valeurs faussées}).
    """
    lines, counts = text.splitlines(), {}
    current, phase, row, pending_tags, skip_first = None, "given", 0, [], False
    for i, line in enumerate(lines):
        if line.strip().startswith("@"):
            pending_tags += line.split()
            continue
        if _SCENARIO_LINE.match(line):
            current = next((t for t in pending_tags if t in tags), None)
            pending_tags, phase = [], "given"
            continue
        if re.match(r"^\s*(Examples|Exemples|Scenarios|Background|Contexte)\s*:", line):
            current, pending_tags = None, []
            continue
        step = _STEP_LINE.match(line)
        if step:
            phase, row = _PHASES.get(step.group(1).lower(), phase), 0
            if current and phase == "then":
                quoted = list(_QUOTED.finditer(line))
                if quoted:
                    last = quoted[-1]
                    lines[i] = f'{line[:last.start()]}"{last.group(1)}_MUT"{line[last.end():]}'
                    counts[current] = counts.get(current, 0) + 1
            continue
        if current and phase == "then" and line.strip().startswith("|"):
            row += 1
            cells = line.split("|")
            if row == 1:
                skip_first = len(cells) > 3 and bool(_KEY_HEADER.match(cells[1].strip()))
            # Ligne 1 : en-tête ; lignes de données paires (2e, 4e...) faussées : une sur deux
            if row >= 2 and row % 2 == 0:
                first = 2 if skip_first else 1
                mutated = "|".join(cells[:first] + [_mutate_cell(c) for c in cells[first:-1]] + [cells[-1]])
                if mutated != line:
                    counts[current] = counts.get(current, 0) + 1
                    lines[i] = mutated
    return "\n".join(lines) + "\n", counts


# ---------------------------------------------------------------------------
# Vérification d'un lot : 3 exécutions au plus (client, Standard pour les overrides, mutation)
# ---------------------------------------------------------------------------

PASSES_STILL = "passe encore"
NO_VALUE_TO_MUTATE = "assertion sans valeur à vérifier en revue"


def _level(result: dict, expect_failure: bool, then_only: bool = False) -> tuple:
    """
    (ok | ko | nv, détail) d'un niveau d'exécution. Échec attendu (contre-épreuve, mutation) : seul un échec
    d'assertion compte ; une exception technique, ou un échec avant les Then (`then_only`), n'est pas concluant (nv).
    """
    if not result:
        return "nv", "non exécuté"
    status = result["statut"]
    if status == "cascade":
        # Mise en place en échec : le scénario n'a pas été jugé sur ses propres steps
        return ("nv" if expect_failure else "cascade"), result["message"]
    if not expect_failure:
        if status == "echoue" and result.get("nature") == "environnement":
            return "env", f"{result['step']} — {result['message']}".strip(" —")
        return ("ok", "passe") if status == "passe" else (
            "ko", f"{status} : {result['step']} — {result['message']}".strip(" —:"))
    if status == "echoue":
        if result.get("nature", "assertion") in ("technique", "environnement"):
            return "nv", f"non concluant : {result.get('exception') or 'exception technique'} ({result['step']})"
        if then_only and result["phase"] != "then":
            return "nv", f"non concluant : échec avant les Then ({result['step']})"
        return "ok", f"échoue comme attendu ({result['step']})"
    if status == "passe":
        return "ko", PASSES_STILL
    return "nv", result["message"] or status


def final_status(levels: dict) -> tuple:
    """
    (statut, motif) : pertinent, faible, invalide, non_verifie, environnement ou cascade à partir des niveaux client /
    standard / mutation. Faible seulement si le scénario passe sur le Standard ou passe avec mutation ; tout niveau
    nv : non_verifie. Environnement (donnée de référence, route absente) et cascade (mise en place en échec) : le
    scénario n'est ni corrigé pour cela ni retiré.
    """
    client, standard, mutation = levels.get("client", ("nv", "")), levels.get("standard"), levels.get("mutation",
                                                                                                         ("nv", ""))
    if client[0] == "nv":
        return "non_verifie", client[1]
    if client[0] == "env":
        return "environnement", f"{ENV_NOT_VERIFIED} : {client[1]}"
    if client[0] == "cascade":
        return "cascade", client[1]
    if client[0] == "ko":
        return "invalide", f"échoue sur le client : {client[1]}"
    if standard and standard[0] == "ko" and standard[1] == PASSES_STILL:
        return "faible", "passe sur le Standard : le scénario ne prouve pas l'override"
    if mutation[0] == "ko" and mutation[1] == PASSES_STILL:
        return "faible", "mutation non détectée : assertions à renforcer"
    if standard and standard[0] != "ok":
        return "non_verifie", f"contre-épreuve Standard : {standard[1]}"
    if mutation[0] != "ok":
        return "non_verifie", mutation[1] if mutation[1] == NO_VALUE_TO_MUTATE else f"mutation : {mutation[1]}"
    return "pertinent", ""


def verify_lot(mode: str, worktree: str, base: str, build_fn, files: list, scenarios: dict, kinds: dict,
               label: str, sources: dict = None) -> dict:
    """
    Niveaux 2 et 3 pour les scénarios `scenarios` {tag: identifiant} d'un lot, `kinds` {tag: type du spécifique},
    `sources` : paramètres du job Jenkins désignant le dépôt et les commits de base (job_sources).
    Au plus 3 exécutions : client (CUCUMBER_VERIFY_ENV), Standard pour les overrides (CUCUMBER_VERIFY_ENV_STD),
    mutation des attendus sur le client. Les mises en place (@agent_…_0) des features concernées sont exécutées
    avec les scénarios. Renvoie {tag: {id, statut, motif, niveaux, build, cause}} et les builds ; cause : nature
    de l'échec de la mise en place pour un scénario « cascade ».
    """
    found = tagged_scenarios(worktree, files, scenarios)
    key_of = {tag: (rel, s["titre"]) for tag, (rel, s) in found.items()}

    def expression(selected):
        return " or ".join(sorted(set(selected) | setup_tags(worktree, files, selected)))
    builds, out = [], {}
    client = run_tags(mode, worktree, base, build_fn, expression(scenarios),
                      os.getenv("CUCUMBER_VERIFY_ENV", "").strip(), {}, f"{label}-client", sources)
    builds.append({"execution": "client", "build": client["build"], "message": client["message"]})
    levels = {tag: {"client": _level(client["resultats"].get(key_of.get(tag, ("", ""))), False)
                    if client["ok"] else ("nv", client["message"])} for tag in scenarios}
    passing = [t for t in scenarios if levels[t]["client"][0] == "ok"]
    overrides = [t for t in passing if kinds.get(t) == "override"]
    std_env = os.getenv("CUCUMBER_VERIFY_ENV_STD", "").strip()
    if overrides and std_env:
        standard = run_tags(mode, worktree, base, build_fn, expression(overrides), std_env, {}, f"{label}-standard",
                            sources)
        builds.append({"execution": "standard", "build": standard["build"], "message": standard["message"]})
        for tag in overrides:
            levels[tag]["standard"] = _level(standard["resultats"].get(key_of.get(tag, ("", ""))), True, True) \
                if standard["ok"] else ("nv", standard["message"])
    elif overrides:
        for tag in overrides:
            levels[tag]["standard"] = ("nv", "CUCUMBER_VERIFY_ENV_STD non configuré")
    if passing:
        mutated, counts = {}, {}
        for rel in {key_of[t][0] for t in passing if t in key_of}:
            text, mutations = mutate_feature(_read_text_file(os.path.join(worktree, *rel.split("/"))), set(passing))
            mutated[rel] = text
            counts.update(mutations)
        to_run = [t for t in passing if counts.get(t)]
        for tag in passing:
            if not counts.get(tag):
                levels[tag]["mutation"] = ("nv", NO_VALUE_TO_MUTATE)
        if to_run:
            mutation = run_tags(mode, worktree, base, build_fn, expression(to_run),
                                os.getenv("CUCUMBER_VERIFY_ENV", "").strip(), mutated, f"{label}-mutation", sources)
            builds.append({"execution": "mutation", "build": mutation["build"], "message": mutation["message"]})
            for tag in to_run:
                levels[tag]["mutation"] = _level(mutation["resultats"].get(key_of.get(tag, ("", ""))), True) \
                    if mutation["ok"] else ("nv", mutation["message"])
    executed = None
    if os.getenv("CUCUMBER_JACOCO", "").strip() == "1" and client["ok"]:
        # Couverture du code spécifique : jacoco.exec de l'environnement client (artefact Jenkins ou dump local)
        dump = os.getenv("CUCUMBER_JACOCO_EXEC", "").strip()
        jacoco = [a for a in client.get("artefacts", []) if "jacoco" in a.lower() and a.endswith(".exec")]
        if jacoco:
            dump = os.path.join(os.path.dirname(worktree), "verification", f"{label}-jacoco.exec")
            dump = dump if jenkins_tools.fetch_artifact(client["build"], jacoco[0], dump) else ""
        executed = sorted(jacoco_executed_classes(dump)) if dump and os.path.exists(dump) else None
    for tag, identifier in scenarios.items():
        status, reason = final_status(levels[tag])
        out[tag] = {"id": identifier, "statut": status, "motif": reason, "type": kinds.get(tag, ""),
                    "niveaux": {k: list(v) for k, v in levels[tag].items()},
                    "build": next((b["build"] for b in builds if b["execution"] == "client"), ""),
                    "cause": (client["resultats"].get(key_of.get(tag, ("", ""))) or {}).get("cause", "")
                    if status == "cascade" else ""}
    return {"scenarios": out, "builds": builds, "classes_executees": executed}


def init_tags() -> str:
    """CUCUMBER_INIT_TAGS : expression de tags de l'initialisation du client (ex. @INIT_APPAREL_WMS), '' si aucune."""
    return os.getenv("CUCUMBER_INIT_TAGS", "").strip()


def run_init(mode: str, worktree: str, base: str, build_fn, tags: str, sources: dict = None) -> dict:
    """
    Exécute une fois, avant de juger les lots, l'initialisation `tags` sur CUCUMBER_VERIFY_ENV. Renvoie {ok, message,
    build} : ok faux si l'exécution échoue ou si un scénario de l'initialisation ne passe pas.
    """
    run = run_tags(mode, worktree, base, build_fn, tags, os.getenv("CUCUMBER_VERIFY_ENV", "").strip(), {}, "init",
                   sources)
    failed = [f"{title} : {r['step']} — {r['message']}".strip(" —") for (_, title), r in run["resultats"].items()
              if r["statut"] != "passe"]
    if not run["ok"] or not run["resultats"] or failed:
        reason = (failed[0] if failed else run["message"] if not run["ok"] else f"aucun scénario pour {tags}")[:300]
        return {"ok": False, "message": f"environnement non initialisé ({tags}) : {reason}", "build": run["build"]}
    return {"ok": True, "message": f"initialisation {tags} : {len(run['resultats'])} scénario(s) OK",
            "build": run["build"]}


def not_verified(scenarios: dict, kinds: dict, message: str, status: str = "non_verifie") -> dict:
    """
    Résultat d'un lot non exécuté : tout est `status` (non_verifie sans moyen d'exécution, environnement si
    l'initialisation CUCUMBER_INIT_TAGS a échoué), jamais compté pertinent.
    """
    return {"scenarios": {tag: {"id": identifier, "statut": status, "motif": message,
                                "type": kinds.get(tag, ""), "niveaux": {"client": ["nv", message]}, "build": ""}
                          for tag, identifier in scenarios.items()}, "builds": []}


# ---------------------------------------------------------------------------
# Retrait des scénarios invalides ou faibles (patch « à revoir »)
# ---------------------------------------------------------------------------

def a_revoir_path(client_name: str) -> str:
    return os.path.join(WORKSPACE_DIR, f"{client_name}-cucumber-a-revoir.patch")


def remove_scenarios(worktree: str, files: list, items: dict, client_name: str) -> list:
    """
    Retire des .feature les scénarios {tag: motif} (de leurs tags jusqu'au scénario suivant) et les ajoute, avec leur
    motif, à <client>-cucumber-a-revoir.patch (diff qui les réintroduit). Renvoie [{fichier, titre, motif}].
    """
    found = tagged_scenarios(worktree, files, items)
    removed, chunks = [], []
    for rel in sorted({rel for rel, _ in found.values()}):
        path = os.path.join(worktree, *rel.split("/"))
        original = _read_text_file(path).splitlines()
        starts = [s["ligne"] - 1 for s in parse_scenarios("\n".join(original))]
        drop = set()
        for tag, (file_rel, scenario) in found.items():
            if file_rel != rel:
                continue
            start = scenario["ligne"] - 1
            while start > 0 and original[start - 1].strip().startswith(("@", "#")):
                start -= 1
            following = [s for s in starts if s > scenario["ligne"] - 1]
            end = following[0] if following else len(original)
            while end > start and following and original[end - 1].strip().startswith(("@", "#")):
                end -= 1
            drop |= set(range(start, end))
            removed.append({"fichier": rel, "titre": scenario["titre"], "tag": tag, "motif": items[tag]})
        cleaned = [line for i, line in enumerate(original) if i not in drop]
        _write_text(path, "\n".join(cleaned) + "\n")
        motifs = "; ".join(f"{r['titre']} : {r['motif']}" for r in removed if r["fichier"] == rel)
        chunks.append(f"# À revoir — {rel} — {motifs}\n" + "".join(difflib.unified_diff(
            [line + "\n" for line in cleaned], [line + "\n" for line in original], f"a/{rel}", f"b/{rel}")))
    if chunks:
        with open(a_revoir_path(client_name), "a", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(chunks) + "\n")
    return removed


def drop_results(results: list, removed: list, reason_prefix: str) -> None:
    """Retire du JSON du lot les scénarios retirés ; un spécifique sans scénario restant devient non couvert."""
    for r in results or []:
        kept = []
        for declared in r.get("scenarios") or []:
            obj = declared if isinstance(declared, dict) else {"nom": str(declared)}
            name = _title_key(obj.get("nom", "").rsplit(" / ", 1)[-1])
            if any(name == _title_key(d["titre"]) for d in removed):
                continue
            kept.append(declared)
        if r.get("statut") == "couvert" and r.get("scenarios") and not kept:
            r["statut"] = "non_couvert"
            r["raison"] = f"{reason_prefix} : " + " ; ".join(sorted({d["motif"] for d in removed}))
        r["scenarios"] = kept


# ---------------------------------------------------------------------------
# Revue humaine : échantillon, verdicts et consignes projet
# ---------------------------------------------------------------------------

def all_verified(state: dict) -> list:
    """Scénarios vérifiés de tous les lots : [{tag, lot, id, statut, motif, niveaux, build, type}]."""
    return [{"tag": tag, "lot": lot["numero"], **item} for lot in state.get("lots", [])
            for tag, item in ((lot.get("execution") or {}).get("scenarios") or {}).items()]


LEVEL_ICONS = {"ok": "✅", "ko": "❌", "nv": "⚪", "env": "🌐", "cascade": "⛓️"}
# Statuts hors jugement du test : l'exécution n'a pas pu trancher (moyen d'exécution, environnement, mise en place)
UNJUDGED = ("non_verifie", "environnement", "cascade")


def scenario_rows(state: dict) -> list:
    """Lignes du tableau de revue humaine : statut, 3 niveaux, différence avec le Standard, source, inspiration, build."""
    rows = []
    for item in all_verified(state):
        lot = next((x for x in state.get("lots", []) if x["numero"] == item["lot"]), {})
        title = item["id"].split("::", 1)[-1]
        declared, inspired = {}, []
        for r in lot.get("resultats") or []:
            for s in r.get("scenarios") or []:
                if isinstance(s, dict) and _title_key(s.get("nom", "").rsplit(" / ", 1)[-1]) == _title_key(title):
                    declared, inspired = s, r.get("inspire_de") or []
        levels = item.get("niveaux") or {}
        rows.append({
            "tag": item["tag"], "Lot": item["lot"], "Scénario": title, "Statut": item["statut"],
            "Statique": "❌" if (lot.get("indicateurs") or {}) and any(
                _title_key(s["titre"]) == _title_key(title) and s["motifs"]
                for s in lot["indicateurs"].get("scenarios", [])) else "✅",
            "Client": LEVEL_ICONS.get((levels.get("client") or ["nv"])[0], "⚪"),
            "Standard": LEVEL_ICONS.get(levels["standard"][0], "⚪") if levels.get("standard") else "—",
            "Mutation": LEVEL_ICONS.get((levels.get("mutation") or ["nv"])[0], "⚪"),
            "Motif": item.get("motif", ""), "Différence avec le Standard": declared.get("difference_standard", ""),
            "Source": declared.get("source", ""), "Inspiré du Standard": " ; ".join(map(str, inspired)),
            "Build": item.get("build") or None,
            "Verdict": ((state.get("verdicts") or {}).get(item["tag"]) or {}).get("verdict", "")})
    return rows


def review_sample(state: dict, size: int = None) -> list:
    """
    Échantillon de CUCUMBER_REVIEW_SAMPLE scénarios à juger (tirage stable pour une génération donnée) : d'abord les
    « non_verifie » (jamais retirés, l'exécution n'a pas tranché), puis des « pertinent ».
    """
    if size is None:
        try:
            size = max(0, int(os.getenv("CUCUMBER_REVIEW_SAMPLE", "").strip() or 5))
        except ValueError:
            size = 5
    pick = random.Random(state.get("debut", ""))
    sample = []
    for status in ("non_verifie", "pertinent"):
        pool = sorted((s for s in all_verified(state) if s["statut"] == status), key=lambda s: s["tag"])
        sample += pick.sample(pool, min(size - len(sample), len(pool)))
    return sample


def record_verdict(client_name: str, state: dict, item: dict, verdict: str, reason: str) -> None:
    """
    Verdict humain sur un scénario (etat.json). « Non pertinent » et son motif deviennent une consigne projet
    (memory_tools, même mécanisme que les retours de révision), injectée dans les lots suivants.
    """
    state.setdefault("verdicts", {})[item["tag"]] = {"verdict": verdict, "motif": reason.strip(), "id": item["id"]}
    if verdict == "non_pertinent":
        title = item["id"].split("::", 1)[-1]
        memory_tools.save_refine_feedback(f"CUCUMBER-{client_name.upper()}",
                                          f"Scénario Cucumber jugé non pertinent (« {title} ») : {reason.strip()}")


def project_guidelines(client_name: str, max_items: int = 8) -> str:
    """Consignes issues des verdicts « Non pertinent » de la revue humaine, pour le prompt des lots."""
    memory = memory_tools.load_ticket_memory(f"CUCUMBER-{client_name.upper()}")
    items = [f["feedback"] for f in memory.get("feedbacks", []) if f.get("source", "consigne") == "consigne"
             and f.get("feedback")][-max_items:]
    if not items:
        return ""
    return ("=== CONSIGNES DU PROJET (revue humaine des scénarios précédents, à respecter) ===\n"
            + "\n".join(f"- {i}" for i in items) + "\n")


# ---------------------------------------------------------------------------
# Couverture du code spécifique (JaCoCo, facultatif)
# ---------------------------------------------------------------------------

def _varint(data: bytes, pos: int) -> tuple:
    value, shift = 0, 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def jacoco_executed_classes(path: str) -> set:
    """
    Classes exécutées d'un jacoco.exec (format binaire JaCoCo : blocs en-tête 0x01, session 0x10, données 0x11) :
    noms VM (com/asis/...) dont au moins une sonde est passée, classes internes ramenées à leur classe englobante.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return set()
    executed, pos = set(), 0

    def utf(position):
        length = int.from_bytes(data[position:position + 2], "big")
        return data[position + 2:position + 2 + length].decode("utf-8", errors="replace"), position + 2 + length
    try:
        while pos < len(data):
            block = data[pos]
            pos += 1
            if block == 0x01:
                pos += 4
            elif block == 0x10:
                _, pos = utf(pos)
                pos += 16
            elif block == 0x11:
                pos += 8
                name, pos = utf(pos)
                count, pos = _varint(data, pos)
                size = (count + 7) // 8
                if any(data[pos:pos + size]):
                    executed.add(name.split("$", 1)[0])
                pos += size
            else:
                break
    except IndexError:
        pass
    return executed


def specific_coverage(specifics: list, executed: set) -> dict:
    """Part des classes Java de chaque spécifique exécutées : {id: {classes, executees, taux}} (classes seulement)."""
    coverage = {}
    for spec in specifics:
        classes = {f["path"].split("src/main/java/", 1)[1][:-5] for f in spec["fichiers"]
                   if f["path"].endswith(".java") and "src/main/java/" in f["path"]}
        if classes:
            hit = len(classes & executed)
            coverage[spec["id"]] = {"classes": len(classes), "executees": hit, "taux": round(100 * hit / len(classes))}
    return coverage


def verification_indicators(state: dict) -> dict:
    """Indicateurs d'exécution de la génération (récapitulatif et IHM)."""
    items = all_verified(state)
    verified = [s for s in items if s["statut"] not in UNJUDGED]
    mutations = [s for s in items if (s["niveaux"].get("mutation") or ["nv"])[0] in ("ok", "ko")]
    standards = [s for s in items if (s["niveaux"].get("standard") or ["nv"])[0] in ("ok", "ko")]
    removed = [r for lot in state.get("lots", []) for r in (lot.get("execution") or {}).get("retires", [])]
    verdicts = list((state.get("verdicts") or {}).values())
    reasons = {}
    for r in removed:
        key = re.sub(r" : .*$", "", r["motif"])
        reasons[key] = reasons.get(key, 0) + 1

    def rate(part, whole):
        return round(100 * part / whole) if whole else None
    return {"scenarios": len(items), "verifies": len(verified), "non_verifies": len(items) - len(verified),
            "environnement": sum(1 for s in items if s["statut"] == "environnement"),
            "cascade": sum(1 for s in items if s["statut"] == "cascade"),
            "pertinents": sum(1 for s in verified if s["statut"] == "pertinent"),
            "taux_pertinent": rate(sum(1 for s in verified if s["statut"] == "pertinent"), len(verified)),
            "taux_mutation": rate(sum(1 for s in mutations if s["niveaux"]["mutation"][0] == "ok"), len(mutations)),
            "taux_echec_standard": rate(sum(1 for s in standards if s["niveaux"]["standard"][0] == "ok"),
                                        len(standards)),
            "retires": len(removed), "motifs_retrait": reasons,
            "taux_validation_humaine": rate(sum(1 for v in verdicts if v["verdict"] == "pertinent"), len(verdicts)),
            "verdicts": len(verdicts)}
