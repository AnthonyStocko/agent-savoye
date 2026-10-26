"""
Recréation, par l'initialisation du client, des données d'initializers serveur effacées par le clear (point 5).

Données à recréer : statut « effacee » du profil (server_initialized_data), ou « inconnu » quand les données des
initializers ne survivent pas au clear (initializers_survive_clear = faux) ; jamais une donnée qui survit ou que le
serveur recrée, jamais une création « partiel » (code non résolu). Placées dans chaque feature d'init entre
l'initialisation globale par fichiers et les fichiers du module spécifique.

Hybride : Python retient d'abord un step de création du Standard reconnu pour l'entité (expression de création
qui nomme l'entité, table avec une colonne de code ; ligne d'exemple du Standard, code remplacé) ; sinon une étape
du projet, « INIT- <Client> recréation de <Entité> {string} », dont la méthode est écrite par un appel Claude borné
puis contrôlée en Python (expression définie une fois, absente du Standard, initializer cité, aucune méthode vide).
Une recréation non livrable reste dans la feature, commentée sous « # TODO-INITIALIZER: », jamais supprimée.
"""
import os
import re

from tools.cucumber_tools import (_java_methods, _read_text_file, java_class_suffix, java_package_segment,
                                  step_expressions)
from tools.cucumber_verify import step_matcher, step_tables

TODO_MARKER = "TODO-INITIALIZER:"
HEADER = "# Recréation des données des initializers serveur effacées par le clear"
_CREATION = re.compile(r"cr[ée]ation|cr[ée]er|\bcr[ée]e\b", re.I)
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
# Préfixes techniques des entités, sans valeur pour reconnaître un step (RefSite -> site)
_ENTITY_NOISE = {"ref", "entity", "model", "data", "dto"}


def to_recreate(profile: dict) -> list:
    """Données d'initializers à recréer par l'init (voir docstring du module), dans l'ordre du profil."""
    survive = (profile or {}).get("initializers_survive_clear", "")
    return [d for d in (profile or {}).get("server_initialized_data") or []
            if d.get("code") and d.get("certitude") != "partiel"
            and (d.get("statut") == "effacee" or (d.get("statut") == "inconnu" and survive == "faux"))]


def entity_tokens(entity: str) -> set:
    """Mots d'un nom d'entité Java (RefSiteGroup -> {site, group}), préfixes techniques écartés."""
    return {t.lower() for t in _CAMEL.findall(entity or "")} - _ENTITY_NOISE


def _code_column(header: list, tokens: set) -> int:
    """Index de la colonne de code d'une table (« code », « <entité>.code »), -1 si aucune."""
    for i, column in enumerate(header):
        name = column.strip().lower()
        if name == "code" or (name.endswith(".code") and entity_tokens(column.rsplit(".", 1)[0]) <= tokens):
            return i
    return -1


def std_creation_steps(std_steps: dict, feature_texts) -> list:
    """
    Steps de création du Standard utilisables pour recréer une donnée : expression de création à DataTable, avec
    un exemple d'usage dans ses features : [{expression, entete, ligne}].
    """
    # Expression sans paramètre : seul le code de la table change
    creation = [e for e, s in std_steps.items() if _CREATION.search(e) and "{" not in e
                and "DataTable" in (s.get("params") or "")]
    match, examples = step_matcher(creation), {}
    for text in feature_texts:
        lines = text.splitlines()
        for number, step, header in step_tables(text):
            expr = match(step)
            if expr and expr not in examples and number < len(lines) - 1:
                row = [c.strip() for c in lines[number + 1].strip().strip("|").split("|")]
                if len(row) == len(header):
                    examples[expr] = {"expression": expr, "entete": header, "ligne": row}
    return list(examples.values())


def match_std_step(entity: str, candidates: list) -> dict:
    """Step de création du Standard reconnu pour `entity` : expression qui nomme l'entité, colonne de code."""
    tokens = entity_tokens(entity)
    for candidate in candidates:
        words = set(re.findall(r"\w+", candidate["expression"].lower()))
        if tokens and tokens <= words and _code_column(candidate["entete"], tokens) >= 0:
            return candidate
    return {}


def project_expression(client_name: str, entity: str) -> str:
    return f"INIT- {java_class_suffix(client_name)} recréation de {entity} {{string}}"


def java_path(client_name: str) -> str:
    return (f"src/test/java/cucumber/features/{java_package_segment(client_name)}/"
            f"Init{java_class_suffix(client_name)}InitializerSteps.java")


def plan(profile: dict, client_name: str, candidates: list) -> list:
    """[{donnee, mode: standard|projet, expression, entete, ligne}] pour chaque donnée à recréer."""
    out = []
    for item in to_recreate(profile):
        std = match_std_step(item.get("entite", ""), candidates)
        if std:
            row = list(std["ligne"])
            row[_code_column(std["entete"], entity_tokens(item["entite"]))] = item["code"]
            out.append({"donnee": item, "mode": "standard", "expression": std["expression"],
                        "entete": std["entete"], "ligne": row})
        else:
            out.append({"donnee": item, "mode": "projet",
                        "expression": project_expression(client_name, item.get("entite", "Donnee"))})
    return out


def _cells(values: list) -> str:
    return "| " + " | ".join(values) + " |"


def feature_lines(items: list, indent: str, eol: str, commented: bool = False) -> list:
    """Lignes Gherkin de recréation (commentaire citant l'initializer, step, table) ; `commented` : sous TODO."""
    if not items:
        return []
    lines = [f"{indent}{HEADER}{eol}"]
    for entry in items:
        item = entry["donnee"]
        origin = f"{item.get('entite', '?')} {item['code']} : initializer {item.get('classe', '?')} " \
                 f"({item.get('depot', '?')})"
        step = (f"And {entry['expression']}" if entry["mode"] == "standard"
                else "And " + entry["expression"].replace("{string}", f'"{item["code"]}"'))
        body = [step] + ([f"  {_cells(entry['entete'])}", f"  {_cells(entry['ligne'])}"]
                         if entry["mode"] == "standard" else [])
        if commented or entry.get("todo"):
            lines.append(f"{indent}# {TODO_MARKER} {origin} — {entry.get('todo') or 'recréation non livrée'}{eol}")
            lines += [f"{indent}# {b}{eol}" for b in body]
        else:
            lines.append(f"{indent}# {origin}{eol}")
            lines += [f"{indent}{b}{eol}" for b in body]
    return lines


def comment_project_steps(text: str, items: list, reason: str) -> str:
    """Feature d'init où les recréations « projet » non livrables sont commentées sous TODO-INITIALIZER."""
    for entry in (e for e in items if e["mode"] == "projet"):
        item = entry["donnee"]
        step = entry["expression"].replace("{string}", f'"{item["code"]}"')
        origin = f"{item.get('entite', '?')} {item['code']} : initializer {item.get('classe', '?')} " \
                 f"({item.get('depot', '?')})"
        text = re.sub(rf"(?m)^(\s*)# {re.escape(origin)}(\r?\n)\s*And {re.escape(step)}(\r?\n|$)",
                      lambda m: f"{m.group(1)}# {TODO_MARKER} {origin} — {reason}{m.group(2)}"
                                f"{m.group(1)}# And {step}{m.group(3)}", text)
    return text


def check_project_steps(worktree: str, items: list, std_expressions) -> list:
    """
    Contrôles de la classe de recréation écrite par Claude : chaque expression attendue définie exactement une fois
    dans le projet, absente du Standard ; l'initializer de chaque donnée cité dans la classe ; aucune méthode de
    recréation vide ni « PendingException ». Une erreur par écart.
    """
    expected = sorted({e["expression"] for e in items if e["mode"] == "projet"})
    if not expected:
        return []
    root = os.path.join(worktree, "src", "test", "java")
    texts = {os.path.join(d, n): _read_text_file(os.path.join(d, n)) for d, _, names in os.walk(root)
             for n in names if n.endswith(".java")} if os.path.isdir(root) else {}
    defined = [e for text in texts.values() for e in step_expressions(text)[0]]
    errors = []
    for expression in expected:
        count = defined.count(expression)
        if count != 1:
            errors.append(f"step « {expression} » défini {count} fois dans le projet (attendu : une fois)")
        if expression in set(std_expressions):
            errors.append(f"step « {expression} » déjà défini par le Standard : nom à rendre unique")
    recreation = "\n".join(t for t in texts.values() if any(e in t for e in expected))
    for classe in sorted({e["donnee"].get("classe", "") for e in items if e["mode"] == "projet"} - {""}):
        if classe not in recreation:
            errors.append(f"initializer {classe} non cité dans la classe de recréation (commentaire attendu)")
    for text in (t for t in texts.values() if any(e in t for e in expected)):
        for method in _java_methods(text):
            if set(method["steps"]) & set(expected) and (
                    "PendingException" in method["body"] or not method["masked"].strip("{} \n\r\t")):
                errors.append(f"méthode {method['name']} vide ou en attente (PendingException) : à implémenter")
    return errors
