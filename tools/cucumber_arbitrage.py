"""
Arbitrage Jira d'un écart entre l'attendu d'un test et le code (CUCUMBER_SPEC_CODE_POLICY=jira, défaut), 0 token.

Un réalignement d'attendu sur le code (« # Réaligné sur le code : <fichier> ») n'est jamais appliqué sur la seule foi
du code : l'arbitre est le critère d'acceptation du ticket Jira (outil Jira, lecture du ticket en direct).
- Ticket conforme au code (sa valeur figure dans le critère, pas celle du test) → réalignement appliqué.
- Ticket conforme au test (l'inverse) → « défaut détecté » : scénario d'origine gardé actif, en échec, marqué
  « # Défaut détecté : … » ; constat « defaut_code » avec le critère en preuve (bilan, A_REVOIR.md). Jamais réaligné.
- Pas de critère exploitable (aucun ticket, pas de section « Critères d'acceptation », les deux valeurs ou aucune
  citées, tickets contradictoires) → décision humaine.
Tickets consultés : clés citées dans le scénario ou l'en-tête de sa feature, sinon celles des extraits Jira de ses
spécifiques (jira_<spec>.md). CUCUMBER_SPEC_CODE_POLICY=decision : toujours une décision humaine.
"""
import os
import re

from tools.cucumber_tools import _read_text_file, cache_dir

VERDICTS = {"code": "ticket conforme au code : réalignement appliqué",
            "test": "ticket conforme au test : défaut détecté",
            "aucun": "pas de critère d'acceptation exploitable : décision humaine"}
DEFECT_COMMENT = "# Défaut détecté :"
_KEY = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_CRITERIA_TITLE = re.compile(r"(?i)^\W*(crit[eè]res?\s+d['’]\s*acceptation|acceptance\s+criteria|\bCA\b\s*:?$)")
_OTHER_TITLE = re.compile(r"(?i)^\W*(#+\s|r[eè]gles?\s+de\s+gestion|contexte|description|release\s+note|cadrage|"
                          r"hors\s+p[eé]rim[eè]tre|notes?|remarques?|tests?\s+[àa]\s+r[eé]aliser)\b")
_QUOTED = re.compile(r"\"([^\"]+)\"|'([^']+)'|«\s*([^»]+?)\s*»")
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)?(?![\w.])")


def policy() -> str:
    """« jira » (défaut) ou « decision » (tout écart attendu / code soumis à une décision humaine)."""
    return "decision" if os.getenv("CUCUMBER_SPEC_CODE_POLICY", "").strip().lower() == "decision" else "jira"


def acceptance_criteria(description: str) -> str:
    """Texte de la section « Critères d'acceptation » d'une description de ticket ('' si absente)."""
    lines, out, inside = (description or "").splitlines(), [], False
    for line in lines:
        stripped = line.strip()
        if _CRITERIA_TITLE.match(stripped):
            inside = True
            rest = _CRITERIA_TITLE.sub("", stripped).strip(" :")
            if rest:
                out.append(rest)
            continue
        if inside and stripped and _OTHER_TITLE.match(stripped) and len(stripped) < 60:
            inside = False
        if inside and stripped:
            out.append(stripped)
    return "\n".join(out)


def _values(lines: list) -> set:
    """Valeurs d'assertion d'un groupe de lignes Gherkin : cellules de table, chaînes citées, nombres."""
    out = set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|"):
            out.update(c.strip() for c in stripped.strip("|").split("|") if c.strip())
            continue
        for match in _QUOTED.finditer(stripped):
            out.add(next(g for g in match.groups() if g is not None).strip())
        out.update(_NUMBER.findall(stripped))
    return {v for v in out if v}


def changed_values(before: list, after: list) -> tuple:
    """(valeurs attendues par le test seul, valeurs du code seul) entre le bloc d'origine et le bloc réaligné."""
    ignore = lambda line: line.strip().startswith(("#", "@")) or not line.strip()  # noqa: E731
    old = [line for line in before if not ignore(line) and line not in after]
    new = [line for line in after if not ignore(line) and line not in before]
    test, code = _values(old), _values(new)
    return sorted(test - code), sorted(code - test)


def _cited(values: list, text: str) -> list:
    lowered = text.lower()
    return [v for v in values if re.search(r"(?<![\w])" + re.escape(v.lower()) + r"(?![\w])", lowered)]


def judge(criteria: str, test_values: list, code_values: list) -> tuple:
    """(verdict, motif) d'un critère d'acceptation face aux valeurs du test et du code."""
    if not criteria.strip():
        return "aucun", "aucune section « Critères d'acceptation » dans le ticket"
    if not test_values and not code_values:
        return "aucun", "aucune valeur d'attendu changée à comparer au critère"
    for_test, for_code = _cited(test_values, criteria), _cited(code_values, criteria)
    if for_test and not for_code:
        return "test", f"le critère cite la valeur du test ({', '.join(for_test)}), pas celle du code"
    if for_code and not for_test:
        return "code", f"le critère cite la valeur du code ({', '.join(for_code)}), pas celle du test"
    if for_test and for_code:
        return "aucun", "le critère cite à la fois la valeur du test et celle du code"
    return "aucun", "le critère ne cite ni la valeur du test ni celle du code"


def ticket_keys(client_name: str, worktree: str, rel: str, block: list, specs=()) -> list:
    """Tickets à consulter : clés citées par le scénario, sinon par l'en-tête de sa feature, sinon extraits Jira."""
    keys = list(dict.fromkeys(_KEY.findall("\n".join(block))))
    if keys:
        return keys
    text = _read_text_file(os.path.join(worktree, *rel.split("/"))) if worktree else ""
    header = re.split(r"(?m)^\s*(?:Background|Contexte|Scenario|Scénario)", text, maxsplit=1)[0]
    keys = list(dict.fromkeys(_KEY.findall(header)))
    if keys:
        return keys
    for spec in specs:
        path = os.path.join(cache_dir(client_name), f"jira_{spec}.md")
        keys += [k for k in re.findall(r"(?m)^##\s+([A-Z][A-Z0-9]+-\d+)\b", _read_text_file(path)) if k not in keys]
    return keys


def _fetch_description(key: str) -> str:
    from tools import jira_tools
    context = jira_tools.get_full_jira_issue_context(key)
    if context.get("error"):
        return ""
    return context.get("description") or ""


def arbitrate(before: list, after: list, keys: list, fetch=None) -> dict:
    """
    Verdict sur un réalignement : {verdict: code|test|aucun, ticket, critere, motif, valeurs_test, valeurs_code}.
    `fetch(key)` renvoie la description du ticket (outil Jira par défaut). Verdicts contradictoires entre tickets :
    « aucun ».
    """
    test_values, code_values = changed_values(before, after)
    base = {"valeurs_test": test_values, "valeurs_code": code_values, "ticket": "", "critere": ""}
    if policy() == "decision":
        return {**base, "verdict": "aucun", "motif": "CUCUMBER_SPEC_CODE_POLICY=decision"}
    if not keys:
        return {**base, "verdict": "aucun", "motif": "aucun ticket Jira relié au scénario"}
    fetch = fetch or _fetch_description
    found, reasons = [], []
    for key in keys:
        criteria = acceptance_criteria(fetch(key) or "")
        verdict, reason = judge(criteria, test_values, code_values)
        reasons.append(f"{key} : {reason}")
        if verdict != "aucun":
            found.append({"verdict": verdict, "ticket": key, "critere": " ".join(criteria.split())[:400],
                          "motif": reason})
    if len({f["verdict"] for f in found}) == 1:
        return {**base, **found[0]}
    if found:
        return {**base, "verdict": "aucun", "motif": "tickets contradictoires : " + " ; ".join(reasons)}
    return {**base, "verdict": "aucun", "motif": " ; ".join(reasons)}


def defect_block(block: list, verdict: dict) -> list:
    """Bloc d'origine, actif, marqué « # Défaut détecté : <ticket> — <motif> » au-dessus de ses tags."""
    comment = (f"{DEFECT_COMMENT} {verdict['ticket']} — {verdict['motif']} — attendu du test conservé, le code est "
               f"à corriger")
    kept = [line for line in block if not line.strip().startswith(DEFECT_COMMENT)]
    start = next(i for i, line in enumerate(kept) if line.strip())
    return kept[:start] + [re.match(r"^\s*", kept[start]).group(0) + comment] + kept[start:]
