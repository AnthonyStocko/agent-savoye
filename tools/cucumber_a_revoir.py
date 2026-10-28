"""
Scénarios à revoir (RÈGLE 3, 0 token) : un scénario n'est jamais supprimé.

- Une cause côté test (step, colonnes, données, ordre, attente, assertion) se corrige ; les corrections sont
  contrôlées (forbidden_changes) : pas de scénario supprimé, de Then retiré, d'assertion en moins, de @ignore ni de
  valeur attendue changée sans référence du code (« # Réaligné sur le code : <fichier> »).
- Une cause hors du test, prouvée par le message d'échec (environnement, version, donnée de référence absente après
  correction, défaut produit probable, déploiement), désactive le scénario : tag générique CUCUMBER_TAG_A_REVOIR au
  niveau du scénario, commentaire de cause au-dessus, tag client déplacé sur les seuls scénarios actifs.
- Seuil CUCUMBER_MAX_RETRACT_PCT : au-delà (ou si un spécifique perdrait tous ses scénarios actifs, ou si un échec
  côté test persiste), rien n'est désactivé et la génération s'arrête pour une décision humaine.
- A_REVOIR.md à la racine du dépôt client (section générée entre deux marqueurs, le reste conservé) : défauts
  détectés en tête, puis scénarios désactivés (cause, preuve, condition de réactivation) et scénarios faibles. Une
  cause déduite (défaut produit derrière une erreur serveur, cascade d'une mise en place, déploiement) est notée NON
  PROUVÉE, avec la vérification à faire (export de logs serveur sur une fenêtre étroite autour du step).
- Aucun nettoyage ne supprime un step, un fichier de données ou une règle encore utilisé (check_deletions).
"""
import os
import re

from tools import cucumber_checks
from tools.cucumber_tools import (_GHERKIN_STEP, _expression_regex, _git, _read_text_file, _write_text,
                                  a_revoir_tag, constat_line, excludes_tag, step_expressions)
from tools.cucumber_verify import (VERSION_GAP, _title_key, failure_kind, is_setup_scenario, log_request,
                                   parse_scenarios)

DEFAULT_MAX_RETRACT_PCT = 30
A_REVOIR_FILE = "A_REVOIR.md"
BEGIN = "<!-- agent-savoye : début de la section générée -->"
END = "<!-- agent-savoye : fin de la section générée -->"
REALIGNED = "# Réaligné sur le code :"
DISABLED_COMMENT = "# À revoir :"
UNPROVEN = "NON PROUVÉE"
PROVEN = "cause PROUVÉE"
# Désactivation proposée, en attente d'une décision humaine (D2) : marque du commentaire « # À revoir : »
PENDING = " — décision en attente"
SETUP_LABEL = "mise en place des seuls scénarios désactivés"
FORBIDDEN_TAGS = ("@ignore", "@Ignore", "@wip", "@skip")
# Cause hors test : libellé et condition de réactivation
CAUSES = {
    "environnement": ("environnement", "réactiver quand l'environnement de vérification répond (initialisation, "
                                       "service, paramétrage)"),
    "version": ("écart de version bibliothèque / serveur", "réactiver quand la bibliothèque de steps du client est "
                                                          "alignée sur la version du serveur"),
    "donnee_reference": ("donnée de référence absente", "réactiver quand la donnée existe après l'initialisation ou "
                                                         "peut être créée par l'API dans le Background"),
    "bug_applicatif": ("défaut produit probable (erreur serveur)", "réactiver quand le défaut est corrigé et "
                                                                   "déployé"),
    "deploiement": ("déploiement en retard sur le dépôt", "réactiver quand le code et les personnalisations "
                                                          "déployés sont à jour"),
    # Autonomie : échec côté test que les corrections n'ont pas résolu, désactivé par l'agent (jamais supprimé)
    "test_persistant": ("échec côté test persistant après les corrections de l'agent",
                        "réactiver après correction du scénario : message d'échec, base et logs du run cités dans "
                        "A_REVOIR.md"),
}
_STATUS_CAUSE = {"environnement": "environnement", "donnee_reference": "donnee_reference",
                 "bug_applicatif": "bug_applicatif", "deploiement": "deploiement"}


def max_retract_pct() -> float:
    try:
        return float(os.getenv("CUCUMBER_MAX_RETRACT_PCT", "").strip() or DEFAULT_MAX_RETRACT_PCT)
    except ValueError:
        return DEFAULT_MAX_RETRACT_PCT


# ---------------------------------------------------------------------------
# Blocs de scénarios et contrôle des corrections (RÈGLE 3a, 3c)
# ---------------------------------------------------------------------------

def scenario_blocks(text: str) -> dict:
    """{titre normalisé: (scénario, lignes du bloc)} : tags et commentaires au-dessus compris, jusqu'au suivant."""
    lines = text.splitlines()
    parsed = parse_scenarios(text)
    starts = []
    for scenario in parsed:
        start = scenario["ligne"] - 1
        while start > 0 and lines[start - 1].strip().startswith(("@", "#")):
            start -= 1
        starts.append(start)
    blocks = {}
    for i, scenario in enumerate(parsed):
        end = starts[i + 1] if i + 1 < len(parsed) else len(lines)
        blocks[_title_key(scenario["titre"])] = (scenario, lines[starts[i]:end])
    return blocks


def _assertions(scenario: dict) -> list:
    """Steps de vérification (phase then) : (texte, {(colonne, valeur)} des lignes de leur table)."""
    out = []
    for step in scenario["steps"]:
        if step["phase"] != "then":
            continue
        rows = [[c.strip() for c in row.strip().strip("|").split("|")] for row in step.get("table") or []]
        pairs = {(h, v) for row in rows[1:] for h, v in zip(rows[0], row)} if rows else set()
        out.append((step["texte"], pairs))
    return out


def _kept(old: tuple, now: list) -> bool:
    """Assertion conservée : même texte, et sa table garde toutes ses valeurs (colonnes ou lignes ajoutées permises)."""
    return any(text == old[0] and old[1] <= pairs for text, pairs in now)


def snapshot(worktree: str, files: list) -> dict:
    """{feature: texte} des features du lot, avant une correction."""
    return {rel: _read_text_file(os.path.join(worktree, *rel.split("/"))) for rel in files
            if rel.endswith(".feature") and os.path.exists(os.path.join(worktree, *rel.split("/")))}


def forbidden_changes(before: str, after: str, rel: str, allowed_removals=()) -> list:
    """
    Interdits d'une correction sur une feature : scénario supprimé (sauf `allowed_removals`, titres normalisés),
    Then retiré, assertions en moins, tag @ignore / à revoir ajouté, valeur attendue changée sans
    « # Réaligné sur le code : <fichier> » dans le bloc du scénario. Une erreur par violation.
    """
    errors, old, new = [], scenario_blocks(before), scenario_blocks(after)
    forbidden_tags = set(FORBIDDEN_TAGS) | {a_revoir_tag()}
    for key, (scenario, _) in old.items():
        if key not in new:
            if key not in allowed_removals:
                errors.append(f"{rel} / {scenario['titre']} : scénario supprimé par la correction — interdit, un "
                              f"scénario se corrige (RÈGLE 3a)")
            continue
        changed, block = new[key]
        added = (set(changed["tags"]) - set(scenario["tags"])) & forbidden_tags
        if added:
            errors.append(f"{rel} / {scenario['titre']} : tag {', '.join(sorted(added))} ajouté — interdit")
        was, now = _assertions(scenario), _assertions(changed)
        if len(now) < len(was):
            errors.append(f"{rel} / {scenario['titre']} : {len(was) - len(now)} assertion(s) (Then) retirée(s) — "
                          f"interdit, une assertion ne s'affaiblit pas")
        elif not all(_kept(a, now) for a in was) and not any(line.strip().startswith(REALIGNED) and line.strip() != REALIGNED
                                    for line in block):
            errors.append(f"{rel} / {scenario['titre']} : valeur attendue modifiée sans justification par le code — "
                          f"ajoute « {REALIGNED} <fichier du code> » au-dessus du scénario, ou rétablis l'attendu")
    return errors


def check_corrections(worktree: str, before: dict, allowed_removals=()) -> list:
    """
    Contrôle des features après une correction ; une feature en infraction est remise à son état d'avant la
    correction (`before`). Renvoie les erreurs, à renvoyer en correction.
    """
    errors = []
    for rel, text in before.items():
        path = os.path.join(worktree, *rel.split("/"))
        found = forbidden_changes(text, _read_text_file(path), rel, allowed_removals)
        if found:
            _write_text(path, text)
            errors += [f"{e} (correction annulée pour ce fichier)" for e in found]
    return errors


# ---------------------------------------------------------------------------
# Nettoyage : rien d'utilisé n'est supprimé (RÈGLE 3g)
# ---------------------------------------------------------------------------

def _feature_texts(worktree: str) -> dict:
    out = _git(worktree, "ls-files", "-co", "--exclude-standard", "--", "*.feature").stdout.splitlines()
    return {rel: _read_text_file(os.path.join(worktree, *rel.split("/"))) for rel in out
            if os.path.exists(os.path.join(worktree, *rel.split("/")))}


def check_deletions(worktree: str, base: str) -> list:
    """
    Fichiers supprimés depuis `base` encore utilisés par une feature (scénario actif ou désactivé) : fichier de
    données ou règle référencé, classe Java dont un step est employé ; step retiré d'une classe conservée alors
    qu'une feature l'emploie. Une erreur bloquante par fichier ou par step.
    """
    deleted = [p for p in _git(worktree, "diff", "--name-only", "--diff-filter=D", "--no-renames", base, "--")
               .stdout.splitlines() if p]
    modified = [p for p in _git(worktree, "diff", "--name-only", "--diff-filter=M", "--no-renames", base, "--",
                                "*.java").stdout.splitlines() if p]
    if not deleted and not modified:
        return []
    features = _feature_texts(worktree)
    referenced = {cucumber_checks.resolved_path(rel, ref) for rel, text in features.items()
                  for ref in cucumber_checks.data_references(text)}
    step_lines = [m.group(1) for text in features.values() for line in text.splitlines()
                  for m in [_GHERKIN_STEP.match(line)] if m]
    errors = []
    for rel in deleted:
        if rel.endswith(".java"):
            expressions, _ = step_expressions(_git(worktree, "show", f"{base}:{rel}").stdout)
            regexes = [r for r in map(_expression_regex, expressions) if r]
            used = next((line for line in step_lines if any(r.match(line) for r in regexes)), "")
            if used:
                errors.append(f"{rel} : classe de steps supprimée alors que « {used} » l'utilise — interdit, "
                              f"restaure-la (RÈGLE 3g)")
        elif rel in referenced or any(r == rel.rsplit(".", 1)[0] for r in referenced):
            errors.append(f"{rel} : fichier supprimé alors qu'une feature le référence — interdit, restaure-le "
                          f"(RÈGLE 3g)")
    for rel in modified:
        before, _ = step_expressions(_git(worktree, "show", f"{base}:{rel}").stdout)
        now, _ = step_expressions(_read_text_file(os.path.join(worktree, *rel.split("/"))))
        for expression in sorted(set(before) - set(now)):
            regex = _expression_regex(expression)
            used = next((line for line in step_lines if regex and regex.match(line)), "")
            if used:
                errors.append(f"{rel} : step « {expression} » retiré alors que « {used} » l'utilise — interdit, "
                              f"restaure-le (RÈGLE 3g)")
    return errors


# ---------------------------------------------------------------------------
# Décision : désactiver, arrêter pour décision humaine (RÈGLE 3b, 3d)
# ---------------------------------------------------------------------------

def cause_of(item: dict) -> str:
    """Cause hors test d'un résultat de vérification (clé de CAUSES), '' pour un échec côté test ou un succès."""
    status = item.get("statut", "")
    if status == "environnement" and VERSION_GAP in item.get("motif", ""):
        return "version"
    if status == "cascade":
        cause = item.get("cause", "")
        return "version" if cause == "version" else _STATUS_CAUSE.get(cause, "")
    return _STATUS_CAUSE.get(status, "")


def decide(scenarios: dict, spec_of: dict, max_pct: float = None, autonomous: bool = False) -> dict:
    """
    Décision sur les scénarios vérifiés d'un lot ({tag: résultat}) : {"desactiver": {tag: cause}, "faibles": [tags],
    "bloquants": [tags], "arret": bool, "motifs": [...]}. Une désactivation exige une cause hors test ET une preuve
    (message d'échec) ; arrêt (rien n'est désactivé) si un échec côté test persiste, si les désactivations
    dépassent `max_pct` % du lot ou si un spécifique n'aurait plus aucun scénario actif.
    """
    max_pct = max_retract_pct() if max_pct is None else max_pct
    disable = {t: cause_of(v) for t, v in scenarios.items() if cause_of(v) and v.get("motif")}
    blocking = sorted(t for t, v in scenarios.items() if v.get("statut") == "invalide")
    weak = sorted(t for t, v in scenarios.items() if v.get("statut") == "faible")
    reasons = []
    if blocking:
        reasons.append(f"{len(blocking)} scénario(s) toujours en échec pour une cause côté test après les corrections "
                       f"(jamais désactivés)")
    pct = 100 * len(disable) / len(scenarios) if scenarios else 0
    if disable and pct > max_pct:
        reasons.append(f"{len(disable)} désactivation(s) sur {len(scenarios)} scénario(s) ({pct:.0f} % > "
                       f"CUCUMBER_MAX_RETRACT_PCT={max_pct:g} %)")
    by_spec = {}
    for tag, item in scenarios.items():
        by_spec.setdefault(spec_of.get(item.get("id", ""), ""), []).append(tag)
    emptied = sorted(s for s, tags in by_spec.items() if s and all(t in disable or t in blocking for t in tags))
    if disable and emptied:
        reasons.append(f"spécifique(s) sans aucun scénario actif après désactivation : {', '.join(emptied)}")
    if autonomous:
        # Aucun arrêt : les échecs côté test persistants sont désactivés eux aussi (cause NON PROUVÉE, listés), les
        # seuils deviennent des alertes du bilan
        disable.update({t: "test_persistant" for t in blocking if scenarios[t].get("motif")})
        return {"desactiver": disable, "faibles": weak, "bloquants": blocking, "arret": False, "motifs": [],
                "alertes": reasons}
    stop = bool(reasons)
    return {"desactiver": {} if stop else disable, "faibles": weak, "bloquants": blocking, "arret": stop,
            "motifs": reasons, "alertes": []}


# ---------------------------------------------------------------------------
# Désactivation par tag, jamais de suppression (RÈGLE 3f)
# ---------------------------------------------------------------------------

def proof_status(cause: str, item: dict) -> tuple:
    """
    (prouvée, vérification à faire) d'une cause hors test. Prouvée : le message d'échec du serveur la montre
    directement (route ou champ inconnu, donnée « non trouvée avec code », champ exigé). Déduite, donc NON PROUVÉE :
    défaut produit derrière une erreur serveur, scénario en cascade d'une mise en place, déploiement en retard,
    cause que le message ne montre pas ; la vérification à faire indique la fenêtre d'export des logs serveur.
    """
    logs = log_request(item)
    if item.get("statut") == "cascade":
        return False, ("le scénario n'a pas été jugé sur ses propres steps (mise en place en échec) : le rejouer seul "
                       "une fois la mise en place corrigée ; " + logs)
    if cause == "bug_applicatif":
        return False, f"erreur serveur constatée, défaut produit déduit : le confirmer par la trace serveur ; {logs}"
    if cause == "deploiement":
        # Constat de l'agent dont l'extrait a été retrouvé dans le journal de l'exécution (D8)
        return (True, "") if item.get("preuve_verifiee") else (
            False, f"comparer la version déployée au dépôt (écran reçu, valeur lue) ; {logs}")
    nature = failure_kind(item.get("motif", ""))[0]
    if nature == cause or (cause == "environnement" and nature == "version"):
        return True, ""
    return False, f"cause déduite, absente du message d'échec : la confirmer par une preuve directe ; {logs}"


def _proof(item: dict, max_chars: int = 200) -> str:
    return " ".join(str(item.get("motif", "")).split())[:max_chars]


def disabled_comment(cause: str, item: dict, pending: bool = False) -> str:
    """« # À revoir : <cause> — cause PROUVÉE | cause NON PROUVÉE [— décision en attente] — <preuve> — réactivation : … »."""
    proven = proof_status(cause, item)[0]
    return (f"{DISABLED_COMMENT} {CAUSES[cause][0]} — {PROVEN if proven else 'cause ' + UNPROVEN}"
            f"{PENDING if pending else ''} — {_proof(item)} — réactivation : {CAUSES[cause][1]}")


def _disable_at(lines: list, line: int, comment: str, client_tag: str) -> None:
    """Désactive le scénario de la ligne `line` : tag à revoir (tag client retiré), commentaire au-dessus."""
    tag = a_revoir_tag()
    indent = re.match(r"^\s*", lines[line]).group(0)
    if line > 0 and lines[line - 1].strip().startswith("@"):
        tags = [t for t in lines[line - 1].split() if t != client_tag]
        lines[line - 1] = indent + " ".join(tags + ([tag] if tag not in tags else []))
        top = line - 1
    else:
        lines.insert(line, indent + tag)
        top = line
    if not (top > 0 and lines[top - 1].strip().startswith(DISABLED_COMMENT)):
        lines.insert(top, f"{indent}{comment}")


def _disable_unused_setup(lines: list, client_tag: str) -> list:
    """
    Mises en place (« Contexte… », @agent_<client>_<lot>_0) qui ne servent plus qu'à des scénarios désactivés :
    désactivées avec eux. Renvoie leurs titres.
    """
    tag = a_revoir_tag()
    parsed = parse_scenarios("\n".join(lines))
    others = [s for s in parsed if not is_setup_scenario(s["titre"], s["tags"])]
    if not others or any(tag not in s["tags"] for s in others):
        return []
    done = []
    for scenario in sorted(parsed, key=lambda s: -s["ligne"]):
        if is_setup_scenario(scenario["titre"], scenario["tags"]) and tag not in scenario["tags"]:
            _disable_at(lines, scenario["ligne"] - 1, f"{DISABLED_COMMENT} {SETUP_LABEL} — réactivation : avec eux",
                        client_tag)
            done.append(scenario["titre"])
    return done


def deactivate(worktree: str, rel: str, items: dict, client_tag: str, pending: bool = False) -> list:
    """
    Désactive dans la feature `rel` les scénarios {titre: (cause, résultat)} : commentaire « # À revoir : … » (cause
    PROUVÉE ou NON PROUVÉE, preuve, condition de réactivation ; « décision en attente » si `pending`) et tag à revoir
    au-dessus du scénario, tag client retiré du scénario ; le tag client de la feature est déplacé sur les scénarios
    restés actifs ; une mise en place qui ne sert plus qu'à des scénarios désactivés l'est aussi. Rien n'est
    supprimé. Renvoie les titres désactivés.
    """
    path = os.path.join(worktree, *rel.split("/"))
    text = _read_text_file(path)
    lines, tag = text.splitlines(), a_revoir_tag()
    wanted = {_title_key(t): v for t, v in items.items()}
    parsed = parse_scenarios(text)
    feature_index = next((i for i, line in enumerate(lines) if re.match(r"^\s*(Feature|Fonctionnalité)\s*:", line)),
                         -1)
    feature_tags = []
    index = feature_index - 1
    while index >= 0 and lines[index].strip().startswith(("@", "#")):
        if lines[index].strip().startswith("@"):
            feature_tags.append(index)
        index -= 1
    move_client = bool(client_tag) and any(client_tag in lines[i].split() for i in feature_tags)
    done = []
    for scenario in sorted(parsed, key=lambda s: -s["ligne"]):
        key = _title_key(scenario["titre"])
        line = scenario["ligne"] - 1
        indent = re.match(r"^\s*", lines[line]).group(0)
        has_tags = line > 0 and lines[line - 1].strip().startswith("@")
        if key in wanted:
            cause, item = wanted[key]
            _disable_at(lines, line, disabled_comment(cause, item, pending), client_tag)
            done.append(scenario["titre"])
        elif move_client and tag not in scenario["tags"]:
            if has_tags:
                if client_tag not in lines[line - 1].split():
                    lines[line - 1] = lines[line - 1].rstrip() + " " + client_tag
            else:
                lines.insert(line, indent + client_tag)
    if move_client and done:
        for i in feature_tags:
            kept = [t for t in lines[i].split() if t != client_tag]
            lines[i] = (re.match(r"^\s*", lines[i]).group(0) + " ".join(kept)) if kept else None
        lines = [line for line in lines if line is not None]
    if done:
        _disable_unused_setup(lines, client_tag)
    _write_text(path, "\n".join(lines) + "\n")
    return done


def apply_decision(worktree: str, files: list, scenarios: dict, decision: dict, client_tag: str,
                   located: dict, pending: bool = False) -> list:
    """
    Désactive les scénarios de decision["desactiver"] ; `located` : {tag: (feature, scénario)} (tagged_scenarios) ;
    `pending` : désactivation proposée, en attente d'une décision humaine (D2).
    Renvoie [{fichier, titre, id, cause, preuve, reactivation, prouvee, verification, heure}] pour A_REVOIR.md.
    """
    by_file = {}
    for tag, cause in decision["desactiver"].items():
        if tag in located:
            rel, scenario = located[tag]
            by_file.setdefault(rel, {})[scenario["titre"]] = (cause, scenarios[tag])
    entries = []
    for rel, items in by_file.items():
        deactivate(worktree, rel, items, client_tag, pending)
        for title, (cause, item) in items.items():
            proven, check = proof_status(cause, item)
            entries.append({"fichier": rel, "titre": title, "id": item.get("id", ""), "cause": cause,
                            "preuve": _proof(item, 400), "reactivation": CAUSES[cause][1], "prouvee": proven,
                            "verification": check, "heure": item.get("heure", "")})
    return entries


# ---------------------------------------------------------------------------
# A_REVOIR.md du dépôt client (RÈGLE 2, 3f)
# ---------------------------------------------------------------------------

def generated_section(state: dict, specifics: dict) -> str:
    """Section générée d'A_REVOIR.md : défauts détectés, scénarios désactivés, scénarios faibles, arrêts."""
    lots = state.get("lots", [])
    label = lambda i: specifics.get(i, {}).get("libelle", i)  # noqa: E731
    disabled = [e for lot in lots for e in lot.get("a_revoir") or []]
    weak = [e for lot in lots for e in lot.get("faibles") or []]
    # Défauts avérés (preuve directe) en tête
    defects = sorted((e for e in disabled if e["cause"] == "bug_applicatif"),
                     key=lambda e: not e.get("prouvee", True))
    defects += [{"constat": c, "spec": r["id"]} for lot in lots for r in lot.get("resultats") or []
                for c in r.get("constats") or [] if c.get("type") == "ecart_spec_code"]
    stops = [lot for lot in lots if lot.get("statut") == "decision_humaine"]
    lines = [BEGIN, "", "# À revoir (généré par l'agent Cucumber)", "",
             "Rien n'a été supprimé : chaque scénario listé reste dans sa feature, désactivé par le tag "
             f"`{a_revoir_tag()}` et un commentaire `{DISABLED_COMMENT} …` au-dessus. Un humain décide de sa "
             "réactivation.", "", f"## Défauts détectés : {len(defects)}", ""]
    for d in defects:
        if "constat" in d:
            lines.append(f"- {label(d['spec'])} — {constat_line(d['constat'])} ({UNPROVEN} : constat de l'agent, à "
                         f"confirmer par une trace serveur, un écran reçu ou une valeur lue)")
        else:
            lines.append(f"- {d['fichier']} / {d['titre']} — {d['preuve']}" + (
                "" if d.get("prouvee", True) else f" ({UNPROVEN} : {d.get('verification', '')})"))
    lines += ["", f"## Scénarios désactivés : {len(disabled)}", ""]
    for e in disabled:
        lines += [f"### {e['fichier']} / {e['titre']}", "",
                  f"- Cause : {CAUSES[e['cause']][0]}" + ("" if e.get("prouvee", True) else f" — **{UNPROVEN}**"),
                  f"- Preuve : `{e['preuve']}`"]
        if not e.get("prouvee", True):
            lines.append(f"- Vérification à faire : {e.get('verification', '')}")
        lines += [
                  f"- Condition de réactivation : {e['reactivation']} ; retirer alors le tag `{a_revoir_tag()}` et "
                  f"le commentaire, et remettre le tag client.", ""]
    if weak:
        lines += [f"## Scénarios faibles, restés actifs : {len(weak)}", ""]
        lines += [f"- {e['fichier']} / {e['titre']} : {e['motif']}" for e in weak]
        lines.append("")
    if stops:
        lines += [f"## Lots arrêtés pour décision humaine : {len(stops)}", ""]
        lines += [f"- Lot {lot['numero']} : {' ; '.join(lot.get('arret') or [])}" for lot in stops]
        lines.append("")
    return "\n".join(lines + [END])


def write_a_revoir(worktree: str, state: dict, specifics: dict) -> str:
    """
    Met à jour A_REVOIR.md à la racine du worktree : section générée remplacée, contenu existant conservé. Aucun
    fichier créé s'il n'y a rien à revoir. Renvoie le chemin relatif ('' si rien n'est écrit).
    """
    lots = state.get("lots", [])
    if not any(lot.get("a_revoir") or lot.get("faibles") or lot.get("statut") == "decision_humaine"
               or any(c.get("type") == "ecart_spec_code" for r in lot.get("resultats") or []
                      for c in r.get("constats") or []) for lot in lots):
        return ""
    path = os.path.join(worktree, A_REVOIR_FILE)
    current = _read_text_file(path)
    section = generated_section(state, specifics)
    if BEGIN in current and END in current:
        text = current[:current.index(BEGIN)] + section + current[current.index(END) + len(END):]
    else:
        text = (current.rstrip() + "\n\n" if current.strip() else "") + section + "\n"
    _write_text(path, text)
    return A_REVOIR_FILE


def runner_excludes_a_revoir(runner: dict) -> bool:
    """Le runner exclut les scénarios désactivés : sinon ils restent exécutés (le filtre du job peut ne pas
    atteindre Cucumber)."""
    return excludes_tag((runner or {}).get("tags") or [], a_revoir_tag())
