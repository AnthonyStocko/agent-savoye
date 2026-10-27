"""
Vérification des tests Cucumber générés pour un client (étape generate-cucumber-client), sans token :

- niveau 1, statique : doublons, absence d'assertion métier, champs du JSON vides, réutilisation des steps ;
  identifiant stable et tags @agent_<client>_<lot> de chaque scénario ;
- niveau 2, exécution sur le client : job Jenkins dédié (patch en fichier) ou commande locale, rapport Cucumber JSON ;
- niveau 3, contre-épreuves : override rejoué sur l'environnement Standard, mutation des attendus des Then.

Claude n'est appelé (hors de ce module) que pour corriger, de façon bornée.
"""
import base64
import difflib
import json
import os
import random
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

from tools import jenkins_tools, memory_tools
from tools.cucumber_tools import (_HTTP_CALL, _expression_regex, _first_argument, _git, _http_path, _java_methods,
                                  _read_text_file, _resolve, _step_prefix, _string_constants, _write_text,
                                  WORKSPACE_DIR, a_revoir_tag, blob_texts, disk_class_constants, extract_cucumber,
                                  java_package_segment, ls_tree, report_unresolved_steps, step_expressions,
                                  tags_match)

# ---------------------------------------------------------------------------
# Niveau 1 : contrôle statique (doublons, assertions, réutilisation, champs du JSON)
# ---------------------------------------------------------------------------

DEFAULT_DUP_THRESHOLD = 0.85
# Doublon que la revue peut retirer (RÈGLE 3a) : similarité au moins égale
EXACT_DUPLICATE = 0.95
_DUPLICATE_MOTIF = re.compile(r"^doublon de .*\(similarité ([\d.]+)\)$")
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


_FEATURE_LINE = re.compile(r"^\s*(Feature|Fonctionnalité)\s*:")
_RULE_LINE = re.compile(r"^\s*(Rule|Règle)\s*:")
_EXAMPLES_LINE = re.compile(r"^\s*(Examples|Exemples|Scenarios)\s*:")


def scenario_units(text: str) -> list:
    """
    Scénarios d'une feature tels que Cucumber les sélectionne : [{titre, ligne, propres (tags du scénario),
    feature (tags de la ligne Feature et de la Rule, hérités), tags (tags effectifs), exemples [(tags du bloc
    Examples, lignes)], nombre (tests exécutés : 1, ou une ligne d'Examples par test)}].
    """
    units, pending, feature, rule, current, examples = [], [], [], [], None, None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("@"):
            pending = pending + [t for t in stripped.split() if t.startswith("@")]
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if _FEATURE_LINE.match(line):
            feature, pending, current, examples = pending, [], None, None
        elif _RULE_LINE.match(line):
            rule, pending, current, examples = pending, [], None, None
        elif _BACKGROUND_LINE.match(line):
            pending, current, examples = [], None, None
        elif _SCENARIO_LINE.match(line):
            current = {"titre": _SCENARIO_LINE.match(line).group(2).strip(), "ligne": number, "propres": pending,
                       "feature": feature + rule, "tags": feature + rule + pending, "exemples": []}
            units.append(current)
            pending, examples = [], None
        elif _EXAMPLES_LINE.match(line) and current is not None:
            examples = [pending, -1]
            current["exemples"].append(examples)
            pending = []
        elif stripped.startswith("|") and examples is not None:
            examples[1] += 1
        elif _STEP_LINE.match(line):
            examples = None
    for unit in units:
        unit["exemples"] = [(tags, max(rows, 0)) for tags, rows in unit["exemples"]]
        unit["nombre"] = sum(rows for _, rows in unit["exemples"]) if unit["exemples"] else 1
    return units


def expected_count(texts, expression: str = "", exclude: str = "") -> int:
    """
    Nombre de tests que Cucumber exécuterait sur ces features avec le filtre `expression` ('' : aucun), sans les
    scénarios portant `exclude` : un par scénario, un par ligne d'Examples d'un plan de scénario.
    """
    total = 0
    for text in texts:
        for unit in scenario_units(text):
            for tags, rows in unit["exemples"] or [((), 1)]:
                effective = set(unit["tags"]) | set(tags)
                if tags_match(expression, effective) and not (exclude and exclude in effective):
                    total += rows
    return total


def feature_texts(worktree: str) -> list:
    """Textes des .feature suivis ou nouveaux du worktree."""
    paths = _git(worktree, "ls-files", "-co", "--exclude-standard", "--", "*.feature").stdout.splitlines()
    return [_read_text_file(os.path.join(worktree, *p.split("/"))) for p in paths
            if os.path.exists(os.path.join(worktree, *p.split("/")))]


def report_test_count(report: list) -> int:
    """Tests exécutés d'un rapport Cucumber JSON : éléments hors Background (une ligne d'Examples = un test)."""
    return sum(1 for feature in report if isinstance(feature, dict) for element in feature.get("elements") or []
               if element.get("type") != "background")


def selection_gap(report: list, texts, tags: str) -> str:
    """
    Écart entre le nombre de tests d'un rapport et le nombre attendu pour `tags` sur ces features (scénarios
    désactivés exclus ou non : les deux sont admis) ; '' si conforme. Un écart signale un run sur une autre version
    du code ou une sélection de tags inopérante : le rapport n'est jamais analysé comme un résultat de test.
    """
    executed = report_test_count(report)
    texts = list(texts)
    expected, active = expected_count(texts, tags), expected_count(texts, tags, a_revoir_tag())
    if executed in (expected, active):
        return ""
    return (f"{executed} test(s) exécuté(s) pour {active} attendu(s) avec les tags « {tags or 'aucun'} » : run sur "
            f"une autre version du code ou sélection de tags inopérante — résultats non analysés")


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
    steps = [s["expr"] for s in catalogue["steps"]]
    columns = table_columns_by_step(steps, texts.values())
    return {"steps": steps, "scenarios": scenarios, "endpoints_par_step": by_step, "colonnes_par_step": columns,
            "appelants": std_endpoints.get("appelants", {}),
            "titres_structurels": sorted(k for k, n in counts.items() if n >= 10 and re.search(
                r"contexte|description|initialisation|r[ée]initialisation|purge", k))}


def step_tables(text: str) -> list:
    """[(ligne, texte du step, colonnes d'en-tête)] des steps à DataTable d'une feature (Background compris)."""
    out, pending = [], None
    for number, line in enumerate(text.splitlines(), 1):
        step = _STEP_LINE.match(line)
        if step:
            pending = (number, step.group(2))
            continue
        stripped = line.strip()
        if stripped.startswith("|") and pending:
            out.append((pending[0], pending[1], [c.strip() for c in stripped.strip("|").split("|")]))
            pending = None
        elif stripped and not stripped.startswith("#"):
            pending = None
    return out


def table_columns_by_step(expressions: list, texts) -> dict:
    """{expression de step: [colonnes]} : en-têtes des DataTables utilisés pour chaque step dans les features."""
    match, columns = step_matcher(expressions), {}
    for text in texts:
        for _, step, header in step_tables(text):
            expr = match(step)
            if expr:
                columns.setdefault(expr, set()).update(c for c in header if c)
    return {expr: sorted(cols) for expr, cols in columns.items()}


def check_table_columns(worktree: str, base: str, features: list, reference: dict) -> list:
    """
    Colonnes des DataTables des features du lot jamais vues au Standard pour le même step (ex. syntaxe d'enum ou
    chemin de filtre refusés par l'application) : une erreur par colonne, avec la colonne du Standard la plus proche.
    Les tables déjà présentes à `base` et les steps sans table au Standard ne sont pas contrôlés.
    """
    known = (reference or {}).get("colonnes_par_step") or {}
    match, errors = step_matcher(list(known)), []
    for rel in features:
        before = {(step, tuple(header)) for _, step, header in step_tables(_git(worktree, "show", f"{base}:{rel}").stdout)}
        for number, step, header in step_tables(_read_text_file(os.path.join(worktree, *rel.split("/")))):
            expr = match(step)
            if not expr or (step, tuple(header)) in before:
                continue
            for column in (c for c in header if c and "<" not in c and c not in known[expr]):
                closest = difflib.get_close_matches(column, known[expr], n=1, cutoff=0.0)
                errors.append(f"{rel}:{number} colonne « {column} » jamais utilisée au Standard pour le step "
                              f"« {expr} »" + (f" : colonne la plus proche « {closest[0]} »" if closest else "")
                              + " (syntaxe du Standard actuel)")
    return errors


# Step qui crée une donnée (création, ajout, intégration, import, génération) et colonnes qui l'identifient
_CREATION_STEP = re.compile(r"cr[ée]{1,2}|cr[ée]ation|creat|ajout|\badd|int[ée]gr|import|g[ée]n[èée]r|enregistr|"
                            r"^INIT-", re.I)
_KEY_COLUMN = re.compile(r"(^|[._])(code|id|num[ée]?ro|number|r[ée]f[ée]rence|reference|name|nom)$", re.I)
_QUOTED = re.compile(r'"([^"]+)"')


def _identifier(value: str) -> bool:
    """Valeur pouvant identifier une donnée : au moins une lettre et un chiffre ou un séparateur, sans espace."""
    return bool(re.fullmatch(r"[\w.-]{2,}", value)) and bool(re.search(r"[A-Za-z]", value)) \
        and bool(re.search(r"[\d_.-]", value)) and not re.search(r"[<$]", value)


def _step_values(step: dict, keys_only: bool = False) -> set:
    """Valeurs d'un step : arguments entre guillemets et cellules de sa table (colonnes d'identifiant seulement)."""
    values = {v for v in _QUOTED.findall(step["texte"]) if _identifier(v)}
    rows = [[c.strip() for c in row.strip().strip("|").split("|")] for row in step["table"]]
    for row in rows[1:]:
        values |= {v for h, v in zip(rows[0], row) if _identifier(v) and (not keys_only or _KEY_COLUMN.search(h))}
    return values


def _background_values(text: str) -> set:
    """Valeurs citées par le Background d'une feature : disponibles pour tous ses scénarios."""
    lines, values, inside = text.splitlines(), set(), False
    for line in lines:
        if _BACKGROUND_LINE.match(line):
            inside = True
        elif inside and _SCENARIO_LINE.match(line):
            break
        elif inside:
            values |= {v for v in _QUOTED.findall(line) if _identifier(v)}
            values |= {c.strip() for c in line.strip().strip("|").split("|") if line.strip().startswith("|")
                       and _identifier(c.strip())}
    return values


def check_scenario_dependencies(worktree: str, features: list) -> list:
    """
    Scénarios qui dépendent de l'état laissé par un autre (RÈGLE 3, point 3h) : scénario de mise en place séparé
    suivi d'autres scénarios (Background attendu), ou donnée créée par un scénario précédent puis utilisée sans être
    créée ni dans le scénario ni dans le Background. Une erreur par scénario, à corriger.
    """
    errors = []
    for rel in features:
        text = _read_text_file(os.path.join(worktree, *rel.split("/")))
        scenarios = parse_scenarios(text)
        if len(scenarios) < 2:
            continue
        background, created = _background_values(text), {}
        for index, scenario in enumerate(scenarios):
            if is_setup_scenario(scenario["titre"]) and any(not is_setup_scenario(o["titre"])
                                                            for o in scenarios[index + 1:]):
                errors.append(f"{rel}:{scenario['ligne']} scénario de mise en place « {scenario['titre']} » séparé : "
                              f"les scénarios suivants dépendent de son état ; déplace ses steps dans un Background "
                              f"ou rends chaque scénario autonome")
            own = set().union(*(_step_values(s, keys_only=True) for s in scenario["steps"]
                                if _CREATION_STEP.search(s["texte"]))) if scenario["steps"] else set()
            used = set().union(*(_step_values(s) for s in scenario["steps"])) if scenario["steps"] else set()
            borrowed = sorted(v for v in used - own - background if v in created)
            if borrowed and not is_setup_scenario(scenario["titre"]):
                errors.append(f"{rel}:{scenario['ligne']} « {scenario['titre']} » utilise "
                              f"{', '.join(borrowed[:5])} créé(s) par « {created[borrowed[0]]} » : il dépend de l'état "
                              f"laissé par un autre scénario ; crée la donnée dans ce scénario ou dans le Background")
            for value in own:
                created.setdefault(value, scenario["titre"])
    return errors


# Scénario « négatif » : il vérifie qu'une action N'A PAS lieu (exclusion, filtre, refus silencieux, 0 instance)
_NEGATIVE = re.compile(r"(?i)\b(n'est pas|n'a pas|ne \w+ pas|ne \w+ aucun|aucune?|exclue?s?|refus[ée]e?s?|jamais|"
                       r"pas de|pas d'|z[ée]ro|non (?:g[ée]n[ée]r|cr[ée]|envoy|d[ée]clench|rendu|imprim)\w*|not|"
                       r"never|none|excluded)\b|\"0\"|\b0 (?:instance|document|message|ligne|[ée]l[ée]ment)")
_VARIABLE_VALUE = re.compile(r"\"[^\"]*\"|<[^>]+>|\b\d+\b")


_COUNT_COLUMN = re.compile(r"(?i)^(x|n|nb\w*|nombre\w*|count\w*|total\w*)$")


def is_negative(scenario: dict) -> bool:
    """
    Un contrôle (Then) du scénario vérifie l'absence d'une action : formulation négative du step, ou nombre 0 dans
    une colonne de comptage de sa table (« X », « nombre »…). Le titre ne compte que sans aucun Then : un témoin
    intitulé « … document non exclu … » n'est pas négatif.
    """
    checks = [s for s in scenario["steps"] if s["phase"] == "then"]
    if not checks:
        return bool(_NEGATIVE.search(scenario["titre"]))
    for step in checks:
        if _NEGATIVE.search(step["texte"]):
            return True
        rows = [[c.strip() for c in row.strip().strip("|").split("|")] for row in step["table"]]
        if rows and any(cell == "0" and n < len(rows[0]) and _COUNT_COLUMN.match(rows[0][n])
                        for row in rows[1:] for n, cell in enumerate(row)):
            return True
    return False


def action_keys(scenario: dict) -> set:
    """Steps d'action du scénario (When, à défaut Given), valeurs retirées : « mêmes conditions »."""
    steps = [s for s in scenario["steps"] if s["phase"] == "when"] or [
        s for s in scenario["steps"] if s["phase"] == "given"]
    return {" ".join(_VARIABLE_VALUE.sub("_", s["texte"]).lower().split()) for s in steps}


def check_positive_witness(worktree: str, features: list) -> list:
    """
    Règle du témoin positif : un scénario négatif (l'action N'A PAS lieu) est accompagné, dans la même feature, d'un
    scénario non négatif qui partage un de ses steps d'action (valeurs retirées) et où l'action A lieu. Sans témoin,
    le test passerait aussi si le mécanisme ne fonctionnait jamais. Une erreur par scénario négatif sans témoin ;
    un plan de scénario dont le comptage vient de ses exemples (« <X> ») n'est pas jugé négatif.
    """
    errors = []
    for rel in features:
        scenarios = [s for s in parse_scenarios(_read_text_file(os.path.join(worktree, *rel.split("/"))))
                     if not is_setup_scenario(s["titre"], s["tags"])]
        positives = [s for s in scenarios if not is_negative(s)]
        for scenario in (s for s in scenarios if is_negative(s)):
            keys = action_keys(scenario)
            if not any(keys & action_keys(p) for p in positives):
                errors.append(f"{rel}:{scenario['ligne']} scénario négatif « {scenario['titre']} » sans témoin "
                              f"positif : ajoute dans cette feature un scénario dans les mêmes conditions (même step "
                              f"d'action) où l'action A lieu, sinon le test passerait aussi si le mécanisme ne "
                              f"fonctionnait jamais")
    return errors


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


def exact_duplicate(motif: str) -> bool:
    """Motif de doublon quasi exact (similarité >= EXACT_DUPLICATE) : seul retrait permis à la revue."""
    match = _DUPLICATE_MOTIF.match(motif)
    return bool(match) and float(match.group(1)) >= EXACT_DUPLICATE


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


# Donnée de référence absente de l'environnement (« Site non trouvé(e) avec code XXX »)
_REFERENCE_FAILURE = re.compile(r"non\s+trouv[ée]+(?:\(e\))?s?\s+avec\s+(?:le\s+)?code", re.I)
# Écart de version entre la bibliothèque de steps et le serveur : route absente, chemin ou champ inconnu du POJO,
# paramètre exigé par le serveur et absent de la bibliothèque (un chemin de champ inventé n'est alors plus corrigé)
_VERSION_FAILURE = re.compile(r"No route found|Invalid path|has no field|UnrecognizedPropertyException|"
                              r"Unrecognized field|parameter\b[^\n]{0,80}?\bis required", re.I)
# Environnement : ressource introuvable, champ exigé par le serveur
_ENVIRONMENT_FAILURE = re.compile(r"\b404\s*-\s*\w|must not be (?:null|blank|empty)|ne doit pas être (?:nul|vide)|"
                                  r"is required|est obligatoire|required (?:field|property)|champ obligatoire", re.I)
# Erreur du test lui-même : colonne refusée, fichier de données absent
_TEST_FAILURE = re.compile(r"No such path|FileNotFoundException|NoSuchFileException", re.I)
# Erreur serveur : bug applicatif probable
_APPLICATION_FAILURE = re.compile(r"Internal Server Error|\bHTTP[/ ]?\d?\.?\d?\s*5\d\d\b|status(?: code)?\D{0,12}5\d\d\b",
                                  re.I)
# Champ contrôlé non renvoyé par l'endpoint ou non mappé par le POJO de réponse : cause « test », à corriger
_NULL_FIELD_FAILURE = re.compile(r"expected:?\s*<[^>]*>\s*but was:?\s*<null>", re.I)
NULL_FIELD_HINT = ("champ contrôlé renvoyé null : l'endpoint ne le renvoie pas ou le POJO de réponse du step ne le "
                   "mappe pas — corrige la colonne ou le step (cause « test », ni retrait ni désactivation)")
ENV_NOT_VERIFIED = "non vérifié — environnement"
VERSION_GAP = "écart de version bibliothèque / serveur"
REFERENCE_NOT_VERIFIED = "non vérifié — donnée de référence absente"
APPLICATION_BUG = "bug applicatif probable"
# Natures d'échec qui ne viennent pas du test : le scénario n'est ni corrigé pour cela ni retiré
NOT_TEST_NATURES = ("environnement", "version", "donnee_reference", "bug_applicatif")
# Niveau d'exécution de chaque nature hors test : un écart de version relève de l'environnement
_NOT_TEST_LEVELS = {"environnement": "env", "version": "env", "donnee_reference": "ref", "bug_applicatif": "bug"}


def failure_kind(error_message: str) -> tuple:
    """
    (nature, nom de l'exception) d'un message d'échec Cucumber : échecs connus des campagnes (KNOWN_FAILURES :
    champ personnalisé non déclaré, longueur, brouillon incomplet, liste de choix vide, ordre non lançable, délai du
    canal « form »), champ_absent (« expected:<X> but was:<null> » :
    champ contrôlé non renvoyé ou non mappé, cause « test »), donnee_reference, version (route, chemin ou champ
    inconnu, paramètre exigé : écart bibliothèque / serveur), environnement (champ exigé), bug_applicatif (erreur
    serveur), sinon échec du test : technique (colonne, fichier, code du step) ou assertion.
    """
    message = str(error_message or "")
    exception = _EXCEPTION_NAME.search(message)
    name = exception.group(1).rsplit(".", 1)[-1] if exception else ""
    # Enseignements des campagnes (point 9f) : nature fixée par le motif, piste donnée par failure_hint
    known = next((nature for pattern, nature, _ in KNOWN_FAILURES if pattern.search(message)), "")
    if known:
        return known, name
    if _NULL_FIELD_FAILURE.search(message):
        return "champ_absent", name
    if _REFERENCE_FAILURE.search(message):
        return "donnee_reference", name
    if _VERSION_FAILURE.search(message):
        return "version", name
    if _ENVIRONMENT_FAILURE.search(message):
        return "environnement", name
    if _TEST_FAILURE.search(message):
        return "technique", name
    if _APPLICATION_FAILURE.search(message):
        return "bug_applicatif", name
    return ("assertion" if _ASSERTION_FAILURE.search(message) else "technique"), name


# Échecs connus (point 9f) : (motif, nature, piste de correction)
KNOWN_FAILURES = (
    (re.compile(r"Unable to find the custom field", re.I), "environnement",
     "champ personnalisé non déclaré sur l'entité dans cet environnement : sa déclaration fait partie des données que "
     "l'init du projet recrée (contrôle d'environnement) ; écris un champ personnalisé par le mécanisme prévu à cet "
     "effet (à identifier dans le Standard, jamais deviné) et relis-le après écriture"),
    (re.compile(r"taille de la valeur.{0,200}trop grande|value too long|exceeds? (?:the )?max(?:imum)? length", re.I),
     "technique", "longueur maximale d'un code dépassée : raccourcis le code créé à la longueur du modèle de l'entité"),
    (re.compile(r"La propri[ée]t[ée]\b.{0,120}\best requise", re.I), "technique",
     "mise à jour par brouillon incomplète : repars du brouillon complet lu, ne modifie que les champs visés et renvoie "
     "tous les attributs requis"),
    (re.compile(r"liste de choix vide|aucun choix (?:n'est )?disponible|no (?:available )?(?:choices|options) "
                r"(?:available|for)|empty (?:choice|option) list", re.I), "environnement",
     "liste de choix vide sur un champ obligatoire : le paramétrage qui l'alimente manque dans l'environnement — à "
     "créer dans le Background (ou à recréer par l'init du projet)"),
    (re.compile(r"ne peut pas être lanc[ée]+|non lan[cç]able|cannot be (?:launched|started|released)", re.I),
     "technique", "ordre non lançable après le contrôle du client : les données reprises du Standard ne respectent pas "
     "une règle active du client ; crée dans le Background des données conformes (fichiers propres à la feature), "
     "sans modifier l'init globale ni affaiblir la règle"),
    (re.compile(r"(?:websocket|ws)\b.{0,80}\bform\b|\bform\b.{0,80}(?:websocket|ws)\b|canal\s*«?\s*form", re.I),
     "technique", "écran attendu différent de l'écran reçu (délai sur le canal « form ») : ajoute un diagnostic "
     "temporaire d'écran avant le step (titre, éléments, erreur, dernier bouton soumis), exécuté au run suivant"),
)


def failure_hint(error_message: str) -> str:
    """Piste de correction d'un échec connu (KNOWN_FAILURES) ; '' sinon."""
    return next((hint for pattern, _, hint in KNOWN_FAILURES if pattern.search(str(error_message or ""))), "")


def failure_category(nature: str) -> str:
    """
    Catégorie d'un échec : environnement (version comprise), donnee_reference, bug_applicatif, cascade ou test
    (assertion, technique).
    """
    if nature == "version":
        return "environnement"
    return nature if nature in NOT_TEST_NATURES or nature == "cascade" else "test"


def report_rel(uri) -> str:
    """Chemin d'une feature du rapport Cucumber JSON (uri absolue, file: ou classpath:), relatif au dépôt si possible."""
    uri = str(uri or "").replace("\\", "/")
    if "src/test/resources/" in uri:
        return "src/test/resources/" + uri.split("src/test/resources/", 1)[1]
    return re.sub(r"^(file|classpath):/*", "", uri)


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
        rel = report_rel(feature.get("uri", ""))
        background, setup_failed = [], None
        for element in feature.get("elements") or []:
            if element.get("type") == "background":
                background = element.get("steps") or []
                continue
            steps, phase, outcome = background + (element.get("steps") or []), "given", None
            # Heure du step en échec : début du cas de test (Background compris), plus la durée des steps précédents
            started, elapsed = _timestamp(element.get("start_timestamp")), 0.0
            background = []
            statuses = []
            outputs = [o for st in steps for o in _step_outputs(st)]
            for step in steps:
                phase = _phase_of(step.get("keyword", ""), phase)
                status = (step.get("result") or {}).get("status", "")
                statuses.append(status)
                if status == "failed" and not outcome:
                    error = str((step.get("result") or {}).get("error_message", ""))
                    nature, exception = failure_kind(error)
                    outcome = {"statut": "echoue", "step": f"{step.get('keyword', '').strip()} {step.get('name', '')}",
                               "phase": phase, "message": " ".join(error.split())[:300], "nature": nature,
                               "exception": exception,
                               "heure": (started + timedelta(seconds=elapsed)).isoformat(timespec="seconds")
                               if started else "", "erreur": error}
                elapsed += _duration(step)
            if not outcome:
                if any(s in ("undefined", "pending", "ambiguous") for s in statuses) or not statuses:
                    outcome = {"statut": "indefini", "step": "", "phase": "", "message": "step non défini ou ignoré"}
                elif all(s == "passed" for s in statuses):
                    outcome = {"statut": "passe", "step": "", "phase": "", "message": ""}
                else:
                    outcome = {"statut": "indefini", "step": "", "phase": "", "message": "steps ignorés"}
            # Journal du scénario (message d'échec complet, sorties des steps) : preuve des constats de l'agent
            outcome["sorties"] = outputs[:MAX_OUTPUTS]
            outcome["journal"] = " | ".join([outcome.pop("erreur", "")] + outputs)[:MAX_JOURNAL_CHARS]
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


MAX_OUTPUTS = 20
MAX_JOURNAL_CHARS = 4000


def _step_outputs(step: dict) -> list:
    """Sorties d'un step du rapport : scenario.log (output) et pièces jointes texte (embeddings)."""
    outputs = [str(o) for o in step.get("output") or [] if str(o).strip()]
    for embedding in step.get("embeddings") or []:
        if str(embedding.get("mime_type", "")).startswith("text/"):
            try:
                outputs.append(base64.b64decode(embedding.get("data", "")).decode("utf-8", "replace"))
            except (ValueError, TypeError):
                continue
    return outputs


def _timestamp(value) -> datetime:
    """Horodatage ISO d'un rapport Cucumber (start_timestamp), en UTC ; None s'il est absent ou illisible."""
    try:
        stamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _duration(step: dict) -> float:
    """Durée d'un step du rapport, en secondes (Cucumber l'écrit en nanosecondes)."""
    try:
        return float((step.get("result") or {}).get("duration") or 0) / 1e9
    except (TypeError, ValueError):
        return 0.0


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
    désignant le dépôt et les commits de base. Rapport dont le nombre de tests diffère du nombre attendu pour
    `tags` (selection_gap) : non analysé (ok faux, ecart_selection). Renvoie {ok, message, build, resultats}.
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
        gap = selection_gap(report, feature_texts(worktree), tags)
        if gap:
            return {"ok": False, "message": gap, "build": "", "resultats": {}, "ecart_selection": True}
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
    gap = selection_gap(report, feature_texts(worktree), tags)
    if gap:
        return {"ok": False, "message": gap, "build": build["url"], "resultats": {}, "ecart_selection": True}
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
        if status == "echoue" and result.get("nature") in NOT_TEST_NATURES:
            detail = f"{result['step']} — {result['message']}".strip(" —")
            return _NOT_TEST_LEVELS[result["nature"]], (f"{VERSION_GAP} : {detail}" if result["nature"] == "version"
                                                        else detail)
        hint = f" — {NULL_FIELD_HINT}" if result.get("nature") == "champ_absent" else ""
        return ("ok", "passe") if status == "passe" else (
            "ko", f"{status} : {result['step']} — {result['message']}".strip(" —:") + hint)
    if status == "echoue":
        if result.get("nature", "assertion") != "assertion":
            return "nv", f"non concluant : {result.get('exception') or 'exception technique'} ({result['step']})"
        if then_only and result["phase"] != "then":
            return "nv", f"non concluant : échec avant les Then ({result['step']})"
        return "ok", f"échoue comme attendu ({result['step']})"
    if status == "passe":
        return "ko", PASSES_STILL
    return "nv", result["message"] or status


def final_status(levels: dict) -> tuple:
    """
    (statut, motif) : pertinent, faible, invalide, non_verifie, environnement, donnee_reference, bug_applicatif ou
    cascade à partir des niveaux client / standard / mutation. Faible seulement si le scénario passe sur le Standard
    ou passe avec mutation ; tout niveau nv : non_verifie. Seuls invalide et faible (échecs du test) peuvent être
    corrigés puis retirés ; environnement, donnée de référence absente, bug applicatif probable et cascade (mise en
    place en échec) ne le sont pas.
    """
    client, standard, mutation = levels.get("client", ("nv", "")), levels.get("standard"), levels.get("mutation",
                                                                                                         ("nv", ""))
    if client[0] == "nv":
        return "non_verifie", client[1]
    if client[0] == "env":
        return "environnement", f"{ENV_NOT_VERIFIED} : {client[1]}"
    if client[0] == "ref":
        return "donnee_reference", f"{REFERENCE_NOT_VERIFIED} : {client[1]}"
    if client[0] == "bug":
        return "bug_applicatif", f"{APPLICATION_BUG} : {client[1]}"
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
                    if status == "cascade" else "",
                    # Step en échec et son heure (UTC) : fenêtre d'export des logs serveur
                    **{k: (client["resultats"].get(key_of.get(tag, ("", ""))) or {}).get(k, "")
                       for k in ("step", "heure", "journal")},
                    "sorties": (client["resultats"].get(key_of.get(tag, ("", ""))) or {}).get("sorties") or []}
    return {"scenarios": out, "builds": builds, "classes_executees": executed}


# ---------------------------------------------------------------------------
# Exports de logs serveur : fenêtre étroite autour du step en échec, couverture vérifiée à l'import
# ---------------------------------------------------------------------------

LOG_WINDOW_MINUTES = 1
_LOG_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")


def log_filter(item: dict) -> str:
    """Texte de filtre d'un export de logs : valeur citée du step en échec, sinon nom de l'exception, sinon step."""
    step = str(item.get("step", ""))
    quoted = re.findall(r'"([^"]{3,})"', step)
    if quoted:
        return max(quoted, key=len)
    exception = _EXCEPTION_NAME.search(str(item.get("motif", "")))
    return exception.group(1).rsplit(".", 1)[-1] if exception else re.sub(r"^\w+\s+", "", step)[:60]


def log_window(when: str, minutes: int = LOG_WINDOW_MINUTES) -> dict:
    """
    Fenêtre d'export des logs serveur autour de `when` (heure UTC du step en échec) : deux minutes, en UTC et en
    heure locale ; {} sans heure. L'outil d'export peut plafonner le nombre de lignes en gardant les plus récentes :
    une fenêtre étroite évite de perdre le step.
    """
    stamp = _timestamp(when)
    if not stamp:
        return {}
    start, end = stamp - timedelta(minutes=minutes), stamp + timedelta(minutes=minutes)
    fmt = "%Y-%m-%d %H:%M:%S"
    return {"heure": stamp.strftime(fmt), "debut_utc": start.strftime(fmt), "fin_utc": end.strftime(fmt),
            "debut_local": start.astimezone().strftime(fmt), "fin_local": end.astimezone().strftime(fmt)}


def log_request(item: dict) -> str:
    """Consigne d'export des logs serveur d'un scénario en échec : fenêtre UTC et locale, texte de filtre."""
    window = log_window(item.get("heure", ""))
    if not window:
        return "exporter les logs serveur du step en échec (heure inconnue : rapport sans start_timestamp)"
    cap = os.getenv("CUCUMBER_LOG_EXPORT_MAX_LINES", "").strip()
    return (f"exporter les logs serveur du {window['debut_utc']} au {window['fin_utc']} UTC (heure locale : "
            f"{window['debut_local']} → {window['fin_local']}), filtre « {log_filter(item)} », puis vérifier à "
            f"l'import que l'export couvre {window['heure']} UTC"
            + (f" (l'outil d'export garde au plus {cap} lignes, les plus récentes : un export qui les atteint a "
               f"perdu le début de la fenêtre)" if cap.isdigit() and int(cap) > 0 else ""))


def log_coverage(text: str, when: str) -> tuple:
    """
    (couvre, message) : l'export de logs `text` couvre-t-il l'heure `when` (UTC) du step ? Horodatages lus comme
    UTC ou comme heure locale (l'un des deux suffit). Un export qui commence après le step a été tronqué par l'outil
    (il garde les lignes les plus récentes) ; un export qui finit avant ne contient pas le step.
    """
    stamp = _timestamp(when)
    if not stamp:
        return False, "heure du step inconnue : couverture non vérifiable"
    found = [datetime.fromisoformat(f"{d}T{t}") for d, t in _LOG_STAMP.findall(text or "")]
    if not found:
        return False, "aucun horodatage lisible dans l'export"
    first, last = min(found), max(found)
    readings = {"UTC": (first.replace(tzinfo=timezone.utc), last.replace(tzinfo=timezone.utc)),
                "heure locale": (first.astimezone(), last.astimezone())}
    for zone, (start, end) in readings.items():
        if start <= stamp <= end:
            return True, f"l'export ({zone}) couvre le step ({stamp:%Y-%m-%d %H:%M:%S} UTC)"
    if all(start > stamp for start, _ in readings.values()):
        return False, (f"l'export commence le {first:%Y-%m-%d %H:%M:%S}, après le step ({stamp:%Y-%m-%d %H:%M:%S} "
                       f"UTC) : l'outil a gardé les lignes les plus récentes — resserre la fenêtre ou le filtre")
    return False, (f"l'export finit le {last:%Y-%m-%d %H:%M:%S}, avant le step ({stamp:%Y-%m-%d %H:%M:%S} UTC) : "
                   f"il ne contient pas le step")


def init_tags() -> str:
    """CUCUMBER_INIT_TAGS : expression de tags de l'initialisation du client (ex. @INIT_CLIENT_WMS), '' si aucune."""
    return os.getenv("CUCUMBER_INIT_TAGS", "").strip()


CAUSE_LABELS = {"environnement": "environnement", "version": VERSION_GAP, "donnee_reference": "donnée de référence "
                                                                                       "absente",
                "bug_applicatif": APPLICATION_BUG, "champ_absent": "champ contrôlé non renvoyé (test)",
                "assertion": "assertion (test)", "technique": "erreur du test (step, colonne, fichier)"}


def run_init(mode: str, worktree: str, base: str, build_fn, tags: str, sources: dict = None) -> dict:
    """
    Exécute une fois, avant de juger les lots, l'initialisation `tags` sur CUCUMBER_VERIFY_ENV. Renvoie {ok, message,
    build} : ok faux si l'exécution échoue ou si un scénario de l'initialisation ne passe pas ; le message donne
    alors le premier step en échec et sa cause.
    """
    run = run_tags(mode, worktree, base, build_fn, tags, os.getenv("CUCUMBER_VERIFY_ENV", "").strip(), {}, "init",
                   sources)
    failed = [f"{title} : {r['step']} — {r['message']}".strip(" —")
              + (f" (cause : {CAUSE_LABELS.get(r.get('cause') or r.get('nature'), r.get('nature'))})"
                 if r.get("nature") or r.get("cause") else "")
              for (_, title), r in run["resultats"].items() if r["statut"] != "passe"]
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
# Revue humaine : échantillon, verdicts et consignes projet
# ---------------------------------------------------------------------------

def all_verified(state: dict) -> list:
    """Scénarios vérifiés de tous les lots : [{tag, lot, id, statut, motif, niveaux, build, type}]."""
    return [{"tag": tag, "lot": lot["numero"], **item} for lot in state.get("lots", [])
            for tag, item in ((lot.get("execution") or {}).get("scenarios") or {}).items()]


LEVEL_ICONS = {"ok": "✅", "ko": "❌", "nv": "⚪", "env": "🌐", "ref": "📭", "bug": "🐞", "cascade": "⛓️"}
# Statuts hors jugement du test : l'exécution n'a pas pu trancher (moyen d'exécution, environnement, mise en place)
UNJUDGED = ("non_verifie", "environnement", "donnee_reference", "bug_applicatif", "cascade", "deploiement")
# Statuts affichés à part dans l'IHM et le récapitulatif : l'échec ne vient pas du test
NOT_TEST_STATUSES = ("environnement", "donnee_reference", "bug_applicatif", "cascade", "deploiement")


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
    disabled = [e for lot in state.get("lots", []) for e in lot.get("a_revoir") or []]
    verdicts = list((state.get("verdicts") or {}).values())
    causes = {}
    for e in disabled:
        causes[e["cause"]] = causes.get(e["cause"], 0) + 1

    def rate(part, whole):
        return round(100 * part / whole) if whole else None
    return {"scenarios": len(items), "verifies": len(verified), "non_verifies": len(items) - len(verified),
            "environnement": sum(1 for s in items if s["statut"] == "environnement"),
            "donnee_reference": sum(1 for s in items if s["statut"] == "donnee_reference"),
            "bug_applicatif": sum(1 for s in items if s["statut"] == "bug_applicatif"),
            "cascade": sum(1 for s in items if s["statut"] == "cascade"),
            "deploiement": sum(1 for s in items if s["statut"] == "deploiement"),
            "hors_test": sum(1 for s in items if s["statut"] in NOT_TEST_STATUSES),
            "pertinents": sum(1 for s in verified if s["statut"] == "pertinent"),
            "taux_pertinent": rate(sum(1 for s in verified if s["statut"] == "pertinent"), len(verified)),
            "taux_mutation": rate(sum(1 for s in mutations if s["niveaux"]["mutation"][0] == "ok"), len(mutations)),
            "taux_echec_standard": rate(sum(1 for s in standards if s["niveaux"]["standard"][0] == "ok"),
                                        len(standards)),
            "desactives": len(disabled), "causes_desactivation": causes,
            "faibles": sum(len(lot.get("faibles") or []) for lot in state.get("lots", [])),
            "taux_validation_humaine": rate(sum(1 for v in verdicts if v["verdict"] == "pertinent"), len(verdicts)),
            "verdicts": len(verdicts)}
