"""
Écarts de version du Cucumber Standard (0 token).

Le client exécute ses features avec SA bibliothèque de steps (test-jar du Standard en version `library_version`),
alors que la génération s'appuie sur le Standard aligné sur le serveur. Pour les steps réellement utilisés par les
features du client, ce module compare les deux versions du dépôt Standard (référence v<library_version> et
référence de génération) : step absent d'un côté, signature de la méthode, endpoints appelés, champs des POJO
référencés par la méthode du step. Il inventorie aussi les marqueurs « TODO-VERSION: » laissés dans le dépôt client.

Champs contrôlés (check_response_fields) : chaque colonne d'un step de vérification généré doit être un champ mappé
par le POJO de réponse du step (chemin pointé résolu sur les classes du projet client et de la bibliothèque) ; sinon
le contrôle lirait null (« expected:<X> but was:<null> »).

Instantané d'une référence en cache : .tmp/cucumber/_standard/versions_<commit>.json.
"""
import os
import re

import difflib

from tools.cucumber_tools import (STD_CACHE_DIR, _GHERKIN_STEP, _expression_regex, _git, _java_methods, _mask_java,
                                  _read_json, _read_text_file, _step_prefix, _string_constants, _test_java_files,
                                  _write_json, _write_text, extract_std_endpoints, java_classes, rev_parse)
from tools.cucumber_verify import _KEY_HEADER, _PHASES, _SCENARIO_LINE, _STEP_LINE, step_matcher

SNAPSHOT_FORMAT = 2
TODO_MARKER = "TODO-VERSION:"
KINDS = {"absent_bibliotheque": "step absent de la bibliothèque du client",
         "absent_standard": "step absent du Standard aligné",
         "signature": "signature du step modifiée",
         "endpoint": "endpoints appelés modifiés",
         "pojo": "champs POJO modifiés"}
_FIELD = re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:private|protected|public|final|transient|volatile)\s+)+"
                    r"(?!static\b)[\w.<>\[\]?,\s]+?\s+(\w+)\s*(?:=[^;]*)?;", re.M)
_JSON_PROPERTY = re.compile(r'@JsonProperty\s*\(\s*(?:value\s*=\s*)?"([^"]+)"')
_TYPED_FIELD = re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:private|protected|public|final|transient|volatile)\s+)+"
                          r"(?!static\b)([\w.<>\[\]?,\s]+?)\s+(\w+)\s*(?:=[^;]*)?;", re.M)
# Types conteneurs : le type utile est leur paramètre (List<Foo> -> Foo)
_CONTAINERS = re.compile(r"^(?:java\.util\.)?(?:List|Set|Collection|Optional|ArrayList|LinkedList|HashSet|Iterable)"
                         r"\s*<\s*(.+)>$")


def library_ref(cucumber_std_dir: str, version: str) -> str:
    """Référence du dépôt Standard pour la version de bibliothèque (branches puis tags v<version>), '' si aucune."""
    clean = (version or "").replace("-SNAPSHOT", "").strip()
    if not clean or not cucumber_std_dir:
        return ""
    for ref in (f"origin/v{clean}", f"refs/tags/v{clean}", f"origin/release/{clean}", f"origin/{clean}",
                f"refs/tags/{clean}"):
        if _git(cucumber_std_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").stdout.strip():
            return ref
    return ""


def pojo_fields(text: str) -> list:
    """Champs d'instance d'une classe POJO (nom JSON de @JsonProperty s'il est donné), dans l'ordre."""
    masked = _mask_java(text)
    fields = []
    for match in _FIELD.finditer(masked):
        # Le masque vide les chaînes : le nom JSON se lit dans le texte d'origine, mêmes positions
        renamed = _JSON_PROPERTY.search(text[match.start():match.end()])
        name = renamed.group(1) if renamed else match.group(1)
        if name not in fields:
            fields.append(name)
    return fields


def pojo_field_types(text: str) -> dict:
    """{champ (nom JSON de @JsonProperty s'il est donné): type} des champs d'instance d'une classe POJO."""
    masked, fields = _mask_java(text), {}
    for match in _TYPED_FIELD.finditer(masked):
        renamed = _JSON_PROPERTY.search(text[match.start():match.end()])
        fields.setdefault(renamed.group(1) if renamed else match.group(2), " ".join(match.group(1).split()))
    return fields


def element_type(java_type: str) -> str:
    """Classe portée par un type de champ : List<Foo>, Foo[], Optional<Foo> -> Foo ; Map ou générique -> ''."""
    java_type = java_type.strip()
    while True:
        container = _CONTAINERS.match(java_type)
        if not container:
            break
        java_type = container.group(1).strip()
    java_type = java_type.rstrip("[] ")
    return "" if "<" in java_type or "," in java_type else java_type.rsplit(".", 1)[-1]


def java_model(files: dict) -> dict:
    """
    Steps et POJO de fichiers Java {chemin: texte} : {"steps": {expression: {classe, methode, params, pojos}},
    "pojos": {classe: [champs]}, "types": {classe: {champ: type}}, "parents": {classe: classe mère}} ; pojos d'un
    step : POJO cités dans le corps ou les paramètres de sa méthode.
    """
    classes = java_classes(files)
    pojo_classes = [c for c in classes if c["category"] == "pojo"]
    pojos = {c["name"]: pojo_fields(files[c["path"]]) for c in pojo_classes}
    constants = {p: _string_constants(t) for p, t in files.items()}
    by_class = {p.rsplit("/", 1)[-1][:-5]: p for p in files}
    steps = {}
    for c in classes:
        if c["category"] != "steps":
            continue
        params = {m["name"]: m["params"] for m in c["members"] if not m["constructor"]}
        for method in _java_methods(files[c["path"]], lambda cls: constants.get(by_class.get(cls, ""), {})):
            names = set(re.findall(r"\b([A-Z]\w+)\b", method["masked"] + " " + params.get(method["name"], "")))
            for expr in method["steps"]:
                steps.setdefault(expr, {"classe": c["name"], "methode": method["name"],
                                        "params": params.get(method["name"], ""), "pojos": sorted(names & set(pojos))})
    return {"steps": steps, "pojos": pojos, "types": {c["name"]: pojo_field_types(files[c["path"]])
                                                      for c in pojo_classes},
            "parents": {c["name"]: c["extends"] for c in pojo_classes if c["extends"]}}


def std_snapshot(cucumber_std_dir: str, ref: str) -> dict:
    """
    Instantané des steps du Standard à `ref` : {"commit", "steps": {expression: {classe, methode, params, endpoints,
    pojos}}, "pojos": {classe: [champs]}}, en cache par commit.
    """
    commit = rev_parse(cucumber_std_dir, ref)
    cache = os.path.join(STD_CACHE_DIR, f"versions_{commit[:12] or 'inconnu'}.json")
    cached = _read_json(cache, {}) or {}
    if commit and cached.get("commit") == commit and cached.get("format") == SNAPSHOT_FORMAT:
        return cached
    files = _test_java_files(cucumber_std_dir, ref)
    model = java_model(files)
    endpoints, _ = extract_std_endpoints(files)
    called = {}
    for e in endpoints:
        for expr in e["steps"]:
            called.setdefault(expr, set()).add(f"{e['methode']} {e['chemin']}")
    for expr, step in model["steps"].items():
        step["endpoints"] = sorted(called.get(expr, ()))
    snapshot = {"commit": commit, "format": SNAPSHOT_FORMAT, "ref": ref, **model}
    if commit:
        _write_json(cache, snapshot)
    return snapshot


def _step_index(snapshot: dict) -> dict:
    """{préfixe: [(expression, regex)]} des steps d'un instantané, pour retrouver la définition d'une ligne."""
    index = {}
    for expr in snapshot["steps"]:
        regex = _expression_regex(expr)
        if regex:
            index.setdefault(_step_prefix(expr), []).append((expr, regex))
    return index


def _definition(text: str, index: dict) -> str:
    for expr, regex in index.get(_step_prefix(text), []) + index.get("(sans préfixe)", []):
        if regex.match(text):
            return expr
    return ""


def feature_steps(texts) -> list:
    """Lignes de step distinctes des features (texte après le mot-clé Gherkin), dans l'ordre."""
    return list(dict.fromkeys(m.group(1) for text in texts for line in text.splitlines()
                              for m in [_GHERKIN_STEP.match(line)] if m))


def _compare(old_expr: str, new_expr: str, library: dict, aligned: dict) -> list:
    """(nature, détail) des écarts d'un step entre la bibliothèque (`old_expr`) et le Standard aligné (`new_expr`)."""
    if not old_expr:
        step = aligned["steps"][new_expr]
        return [("absent_bibliotheque", f"défini dans le Standard aligné ({step['classe']}#{step['methode']}) : step "
                                        f"non défini à l'exécution")]
    if not new_expr:
        step = library["steps"][old_expr]
        return [("absent_standard", f"défini dans la bibliothèque ({step['classe']}#{step['methode']}), plus dans le "
                                    f"Standard aligné")]
    old, new, found = library["steps"][old_expr], aligned["steps"][new_expr], []
    if old["params"] != new["params"]:
        found.append(("signature", f"{old['methode']}({old['params']}) → {new['methode']}({new['params']})"))
    if old["endpoints"] != new["endpoints"]:
        gone = sorted(set(old["endpoints"]) - set(new["endpoints"]))
        added = sorted(set(new["endpoints"]) - set(old["endpoints"]))
        found.append(("endpoint", "; ".join(filter(None, [
            f"bibliothèque seule : {', '.join(gone)}" if gone else "",
            f"Standard aligné seul : {', '.join(added)}" if added else ""]))))
    for pojo in sorted(set(old["pojos"]) & set(new["pojos"])):
        before, after = library["pojos"].get(pojo, []), aligned["pojos"].get(pojo, [])
        gone, added = [f for f in before if f not in after], [f for f in after if f not in before]
        if gone or added:
            found.append(("pojo", f"{pojo} : " + "; ".join(filter(None, [
                f"champs absents du Standard aligné : {', '.join(gone)}" if gone else "",
                f"champs absents de la bibliothèque : {', '.join(added)}" if added else ""]))))
    return found


def step_differences(library: dict, aligned: dict, steps: list) -> list:
    """
    Écarts entre la bibliothèque du client (`library`) et le Standard aligné (`aligned`) pour les lignes de step
    `steps` : [{"step", "expression", "nature" (clé de KINDS), "detail"}], une entrée par expression et nature. Une
    ligne qui n'est définie dans aucune des deux versions (step du client) est ignorée.
    """
    old_index, new_index = _step_index(library), _step_index(aligned)
    differences, seen = [], set()
    for text in steps:
        old_expr, new_expr = _definition(text, old_index), _definition(text, new_index)
        if not old_expr and not new_expr:
            continue
        expression = new_expr or old_expr
        for nature, detail in _compare(old_expr, new_expr, library, aligned):
            if (expression, nature, detail) not in seen:
                seen.add((expression, nature, detail))
                differences.append({"step": text, "expression": expression, "nature": nature, "detail": detail})
    return differences


def catalogue_differences(library: dict, aligned: dict) -> list:
    """
    Écarts de tout le catalogue du Standard aligné avec la bibliothèque, par expression : steps absents de la
    bibliothèque, signature, endpoints ou champs POJO modifiés ({"step", "expression", "nature", "detail"}).
    """
    differences = []
    for expression in sorted(aligned["steps"]):
        old = expression if expression in library["steps"] else ""
        differences += [{"step": expression, "expression": expression, "nature": nature, "detail": detail}
                        for nature, detail in _compare(old, expression, library, aligned)]
    return differences


def library_gap_file(cucumber_std_dir: str, library_version: str, aligned_ref: str = "HEAD") -> str:
    """
    Fichier des écarts du catalogue aligné avec la bibliothèque du client, à interroger par Grep pendant un lot
    (versions_<bibliothèque>_<commit>.md) ; '' si la bibliothèque est introuvable ou identique.
    """
    ref = library_ref(cucumber_std_dir, library_version)
    if not ref:
        return ""
    library, aligned = std_snapshot(cucumber_std_dir, ref), std_snapshot(cucumber_std_dir, aligned_ref)
    if library["commit"] == aligned["commit"]:
        return ""
    path = os.path.join(STD_CACHE_DIR, f"ecarts_{library_version}_{aligned['commit'][:10]}.md")
    if not os.path.exists(path):
        differences = catalogue_differences(library, aligned)
        lines = [f"# Steps du Standard aligné différents de la bibliothèque {library_version} ({ref})",
                 "Une ligne par écart : expression — nature : détail. À interroger par Grep (expression, classe), "
                 "jamais en entier.", ""] + [f"- {d['expression']} — {KINDS[d['nature']]} : {d['detail']}"
                                             for d in differences]
        _write_text(path, "\n".join(lines) + "\n")
    return path


def version_report(cucumber_std_dir: str, library_version: str, feature_texts, aligned_ref: str = "HEAD") -> dict:
    """
    Écarts des steps utilisés par `feature_texts` entre la bibliothèque `library_version` et le Standard aligné
    (`aligned_ref` du dépôt Standard) : {"ref_bibliotheque", "ecarts", "avertissement"}. Sans référence de la
    bibliothèque dans le dépôt Standard, aucun écart n'est calculé (avertissement).
    """
    ref = library_ref(cucumber_std_dir, library_version)
    if not ref:
        return {"ref_bibliotheque": "", "ecarts": [],
                "avertissement": (f"bibliothèque {library_version or '?'} introuvable dans le dépôt Standard "
                                  f"(ni branche ni tag v{library_version or '?'}) : écarts des steps non calculés")}
    library, aligned = std_snapshot(cucumber_std_dir, ref), std_snapshot(cucumber_std_dir, aligned_ref)
    if library["commit"] == aligned["commit"]:
        return {"ref_bibliotheque": ref, "ecarts": [], "avertissement": ""}
    return {"ref_bibliotheque": ref, "ecarts": step_differences(library, aligned, feature_steps(feature_texts)),
            "avertissement": ""}


def difference_line(d: dict) -> str:
    return f"{KINDS[d['nature']]} — « {d['step']} » : {d['detail']}"


# ---------------------------------------------------------------------------
# Champs contrôlés réellement mappés par le POJO de réponse du step (0 token)
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"[_\-\s]", "", name).lower()


def _fields_of(model: dict, pojo: str) -> dict:
    """{nom normalisé: (nom, type)} des champs d'un POJO, classes mères comprises ; None si une mère est inconnue."""
    fields, seen = {}, set()
    while pojo and pojo not in seen:
        seen.add(pojo)
        if pojo not in model["types"]:
            # POJO ou classe mère hors des classes lues : non contrôlable
            return None
        for name, java_type in model["types"][pojo].items():
            fields.setdefault(_norm(name), (name, java_type))
        pojo = model["parents"].get(pojo, "")
    return fields


def resolve_path(model: dict, pojo: str, path: str) -> tuple:
    """
    (résolu, champs disponibles au niveau en échec) d'un chemin pointé (« a.b[0].c ») depuis `pojo`. Un niveau dont
    le type n'est pas un POJO connu (Map, Object, classe hors projet) ne se contrôle plus : résolu.
    """
    current = pojo
    for segment in (re.sub(r"\[[^\]]*\]", "", s) for s in path.split(".")):
        fields = _fields_of(model, current)
        if fields is None or not fields or any(element_type(t) in ("Map", "Object", "JsonNode")
                                               for _, t in fields.values()):
            return True, []
        if _norm(segment) not in fields:
            return False, sorted(name for name, _ in fields.values())
        current = element_type(fields[_norm(segment)][1])
        if current not in model["types"]:
            return True, []
    return True, []


_VALUE_HEADER = re.compile(r"(?i)^(valeurs?|values?|attendus?|expected)$")


def _control_tables(text: str) -> list:
    """[(ligne, step, lignes de table)] des steps de vérification (Then) à DataTable d'une feature."""
    out, phase, current = [], "given", None
    for number, line in enumerate(text.splitlines(), 1):
        step = _STEP_LINE.match(line)
        if step:
            phase = _PHASES.get(step.group(1).lower(), phase)
            current = [number, step.group(2), []] if phase == "then" else None
            if current:
                out.append(current)
            continue
        stripped = line.strip()
        if stripped.startswith("|") and current is not None:
            current[2].append([c.strip() for c in stripped.strip("|").split("|")])
        elif stripped and not stripped.startswith("#"):
            current = None
            phase = "given" if _SCENARIO_LINE.match(line) else phase
    return [(n, s, rows) for n, s, rows in out if rows]


def _checked_paths(rows: list) -> list:
    """Chemins de champs contrôlés d'une table : en-têtes, ou 1re colonne d'une table « champ | valeur »."""
    header = rows[0]
    if len(header) == 2 and _KEY_HEADER.match(header[0]) and _VALUE_HEADER.match(header[1]):
        return [row[0] for row in rows[1:] if row and row[0]]
    return [c for c in header if c]


def merge_models(*models) -> dict:
    """Modèles Java fusionnés : le premier l'emporte (projet client avant la bibliothèque)."""
    merged = {"steps": {}, "pojos": {}, "types": {}, "parents": {}}
    for model in reversed(models):
        for key in merged:
            merged[key].update(model.get(key) or {})
    return merged


def check_response_fields(worktree: str, base: str, features: list, cucumber_std_dir: str,
                          library_version: str = "") -> list:
    """
    Colonnes des steps de vérification générés (tables absentes de `base`) non mappées par le POJO de réponse du
    step : une erreur par colonne, avec les champs disponibles. POJO résolus sur les classes du projet client (sur
    disque) puis de la bibliothèque (référence v<library_version> du dépôt Standard, à défaut son HEAD). Un step
    sans POJO identifié n'est pas contrôlé.
    """
    if not features:
        return []
    root = os.path.join(worktree, "src", "test", "java")
    files = {os.path.relpath(os.path.join(d, n), worktree).replace("\\", "/"):
             _read_text_file(os.path.join(d, n)) for d, _, names in os.walk(root) for n in names
             if n.endswith(".java")} if os.path.isdir(root) else {}
    library = {}
    if cucumber_std_dir and os.path.isdir(cucumber_std_dir):
        library = std_snapshot(cucumber_std_dir, library_ref(cucumber_std_dir, library_version) or "HEAD")
    model = merge_models(java_model(files) if files else {}, library)
    match, errors = step_matcher(list(model["steps"])), []
    for rel in features:
        before = {(step, tuple(rows[0])) for _, step, rows in
                  _control_tables(_git(worktree, "show", f"{base}:{rel}").stdout)}
        for number, step, rows in _control_tables(_read_text_file(os.path.join(worktree, *rel.split("/")))):
            expr = match(step)
            roots = model["steps"].get(expr, {}).get("pojos") or [] if expr else []
            if not roots or (step, tuple(rows[0])) in before:
                continue
            for path in (p for p in _checked_paths(rows) if "<" not in p):
                results = [resolve_path(model, pojo, path) for pojo in roots]
                if any(ok for ok, _ in results):
                    continue
                available = sorted({f for _, fields in results for f in fields})
                closest = difflib.get_close_matches(path.rsplit(".", 1)[-1], available, n=1, cutoff=0.6)
                errors.append(f"{rel}:{number} colonne « {path} » du contrôle « {step} » : champ non mappé par le POJO "
                              f"de réponse ({', '.join(roots)}) — le contrôle lirait null"
                              + (f" ; champ le plus proche « {closest[0]} »" if closest else "")
                              + (f" ; champs disponibles : {', '.join(available[:15])}" if available else ""))
    return errors


def todo_markers(worktree: str, ref: str = "") -> list:
    """Marqueurs TODO-VERSION: du dépôt client (à `ref`, sinon fichiers du worktree) : [{fichier, ligne, texte}]."""
    out = _git(worktree, "grep", "-n", "-F", TODO_MARKER, *([ref] if ref else []), "--").stdout
    markers = []
    for line in out.splitlines():
        line = line[len(ref) + 1:] if ref and line.startswith(f"{ref}:") else line
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            text = parts[2].split(TODO_MARKER, 1)[-1].strip()
            markers.append({"fichier": parts[0], "ligne": int(parts[1]), "texte": text})
    return markers
