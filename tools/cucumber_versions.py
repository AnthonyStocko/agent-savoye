"""
Écarts de version du Cucumber Standard (0 token).

Le client exécute ses features avec SA bibliothèque de steps (test-jar du Standard en version `library_version`),
alors que la génération s'appuie sur le Standard aligné sur le serveur. Pour les steps réellement utilisés par les
features du client, ce module compare les deux versions du dépôt Standard (référence v<library_version> et
référence de génération) : step absent d'un côté, signature de la méthode, endpoints appelés, champs des POJO
référencés par la méthode du step. Il inventorie aussi les marqueurs « TODO-VERSION: » laissés dans le dépôt client.

Instantané d'une référence en cache : .tmp/cucumber/_standard/versions_<commit>.json.
"""
import os
import re

from tools.cucumber_tools import (STD_CACHE_DIR, _GHERKIN_STEP, _expression_regex, _git, _java_methods, _mask_java,
                                  _read_json, _step_prefix, _string_constants, _test_java_files, _write_json,
                                  extract_std_endpoints, java_classes, rev_parse)

SNAPSHOT_FORMAT = 1
TODO_MARKER = "TODO-VERSION:"
KINDS = {"absent_bibliotheque": "step absent de la bibliothèque du client",
         "absent_standard": "step absent du Standard aligné",
         "signature": "signature du step modifiée",
         "endpoint": "endpoints appelés modifiés",
         "pojo": "champs POJO modifiés"}
_FIELD = re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:private|protected|public|final|transient|volatile)\s+)+"
                    r"(?!static\b)[\w.<>\[\]?,\s]+?\s+(\w+)\s*(?:=[^;]*)?;", re.M)
_JSON_PROPERTY = re.compile(r'@JsonProperty\s*\(\s*(?:value\s*=\s*)?"([^"]+)"')


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
    classes = java_classes(files)
    pojos = {c["name"]: pojo_fields(files[c["path"]]) for c in classes if c["category"] == "pojo"}
    endpoints, _ = extract_std_endpoints(files)
    called = {}
    for e in endpoints:
        for expr in e["steps"]:
            called.setdefault(expr, set()).add(f"{e['methode']} {e['chemin']}")
    constants = {p: _string_constants(t) for p, t in files.items()}
    by_class = {p.rsplit("/", 1)[-1][:-5]: p for p in files}
    steps = {}
    for c in classes:
        if c["category"] != "steps":
            continue
        params = {m["name"]: m["params"] for m in c["members"] if not m["constructor"]}
        for method in _java_methods(files[c["path"]], lambda cls: constants.get(by_class.get(cls, ""), {})):
            used = sorted(set(re.findall(r"\b([A-Z]\w+)\b", method["masked"])) & set(pojos))
            for expr in method["steps"]:
                steps.setdefault(expr, {"classe": c["name"], "methode": method["name"],
                                        "params": params.get(method["name"], ""),
                                        "endpoints": sorted(called.get(expr, ())), "pojos": used})
    snapshot = {"commit": commit, "format": SNAPSHOT_FORMAT, "ref": ref, "steps": steps, "pojos": pojos}
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
        found = []
        if not old_expr:
            found.append(("absent_bibliotheque", f"défini dans le Standard aligné ({aligned['steps'][new_expr]['classe']}"
                                                 f"#{aligned['steps'][new_expr]['methode']}) : step non défini à "
                                                 f"l'exécution"))
        elif not new_expr:
            found.append(("absent_standard", f"défini dans la bibliothèque ({library['steps'][old_expr]['classe']}#"
                                             f"{library['steps'][old_expr]['methode']}), plus dans le Standard aligné"))
        else:
            old, new = library["steps"][old_expr], aligned["steps"][new_expr]
            if old["params"] != new["params"]:
                found.append(("signature", f"{old['methode']}({old['params']}) → {new['methode']}({new['params']})"))
            if old["endpoints"] != new["endpoints"]:
                gone, added = sorted(set(old["endpoints"]) - set(new["endpoints"])), sorted(
                    set(new["endpoints"]) - set(old["endpoints"]))
                found.append(("endpoint", "; ".join(filter(None, [f"bibliothèque seule : {', '.join(gone)}" if gone
                                                                  else "", f"Standard aligné seul : {', '.join(added)}"
                                                                  if added else ""]))))
            for pojo in sorted(set(old["pojos"]) & set(new["pojos"])):
                before, after = library["pojos"].get(pojo, []), aligned["pojos"].get(pojo, [])
                gone, added = [f for f in before if f not in after], [f for f in after if f not in before]
                if gone or added:
                    found.append(("pojo", f"{pojo} : " + "; ".join(filter(None, [
                        f"champs absents du Standard aligné : {', '.join(gone)}" if gone else "",
                        f"champs absents de la bibliothèque : {', '.join(added)}" if added else ""]))))
        expression = new_expr or old_expr
        for nature, detail in found:
            key = (expression, nature, detail)
            if key not in seen:
                seen.add(key)
                differences.append({"step": text, "expression": expression, "nature": nature, "detail": detail})
    return differences


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
