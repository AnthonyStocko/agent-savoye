"""
Décisions humaines de l'onglet Cucumber (point 7, D2, D3), 0 token.

Les propositions qui changent le sens d'un test ne sont pas appliquées d'office : réalignement d'un attendu sur le
code (arbitré d'abord par le ticket Jira, cucumber_arbitrage), changement des données d'un step existant,
désactivation, test jugé tautologique, écart spécification / code.
- En attente (D2) : le scénario d'origine reste actif (en échec), précédé du commentaire « # Décision en attente : … »
  (désactivation : « décision en attente » dans son commentaire « # À revoir : … »). La modification proposée (bloc
  du scénario corrigé) est gardée à part, dans la décision. L'agent poursuit le reste.
- Valider : applique la modification (réalignement, données : scénario corrigé, actif ; désactivation : définitive) ;
  Refuser : rétablit le scénario d'origine, actif, et le commentaire accompagne sa prochaine correction ; Commenter :
  ajoute un avis sans trancher. Test tautologique et écart spécification / code : décisions d'information.
- Historique (D3) : .tmp/cucumber/<client>/decisions.json, chaque action avec son auteur (AGENT_KNOWLEDGE_AUTHOR,
  sinon l'utilisateur), sa date et son commentaire ; résumé dans le bilan (A_REVOIR.md, répertoire partagé).
Une décision appliquée au worktree y est commitée, et le patch réécrit s'il existe (sans diagnostic temporaire).
"""
import os
import re
from datetime import datetime

from tools import cucumber_arbitrage, knowledge_tools
from tools.cucumber_a_revoir import DISABLED_COMMENT, PENDING, REALIGNED, scenario_blocks
from tools.cucumber_diagnostic import temporary_steps
from tools.cucumber_tools import (_git, _read_json, _read_text_file, _write_json, _write_text, a_revoir_tag,
                                  cache_dir, patch_path, write_patch)
from tools.cucumber_verify import _title_key, parse_scenarios

TYPES = {"realignement": "réalignement d'un attendu sur le code", "donnees": "changement de données de test",
         "desactivation": "désactivation", "tautologique": "test jugé tautologique",
         "ecart_spec_code": "écart spécification / code"}
# Décisions qui gardent une modification à part (scénario d'origine actif en attendant)
HELD_TYPES = ("realignement", "donnees")


def autonomous() -> bool:
    """
    Autonomie (CUCUMBER_AUTONOMY=1, défaut) : aucune décision n'est demandée à l'utilisateur. Chaque choix que
    l'agent aurait soumis (réalignement, données de test changées, désactivation, arrêt) est appliqué et enregistré
    comme « décision prise par l'agent » (historique, bilan), à relire et réversible plus tard. 0 : décisions
    humaines comme avant.
    """
    return os.getenv("CUCUMBER_AUTONOMY", "1").strip() != "0"


AGENT_REASON = "décision de l'agent (autonomie : aucune décision demandée à l'utilisateur)"


def record_agent_decision(client_name: str, kind: str, proposal: str, **fields) -> dict:
    """Décision prise par l'agent : enregistrée validée, avec son historique (bilan, revue ultérieure)."""
    decisions = load_decisions(client_name)
    decision = {"id": f"d-{len(decisions) + 1}", "type": kind, "proposition": " ".join(proposal.split())[:600],
                "statut": "validee", "auto": True,
                "historique": [_event("prise par l'agent", AGENT_REASON, author="agent")], **fields}
    decisions.append(decision)
    save_decisions(client_name, decisions)
    return decision


def auto_resolve_pending(client_name: str, worktree: str, base: str = "", client_tag: str = "") -> list:
    """Autonomie : décisions techniques encore en attente validées par l'agent. Renvoie celles traitées."""
    if not autonomous():
        return []
    done = []
    # Seules les décisions techniques (données de test) sont prises par l'agent ; un réalignement, une désactivation
    # ou un écart restent à l'humain
    for decision in [d for d in pending(client_name) if d["type"] == "donnees"]:
        resolved = resolve(client_name, decision["id"], "valider", AGENT_REASON, worktree, base, client_tag, "agent")
        if resolved:
            resolved["auto"] = True
            done.append(resolved)
    if done:
        decisions = load_decisions(client_name)
        for d in decisions:
            if d["id"] in {x["id"] for x in done}:
                d["auto"] = True
        save_decisions(client_name, decisions)
    return done


def spec_code_rule() -> str:
    """Règle donnée au correcteur (politique de qualité par défaut, CUCUMBER_SPEC_CODE_POLICY=jira)."""
    rule = ("Corrections TECHNIQUES (fichier, colonne, champ non mappé, brouillon, longueur, step, tag, runner) : "
            "applique-les sans attendre de décision. ÉCART entre l'attendu du test et le code : n'aligne un attendu sur "
            "le code qu'avec le commentaire « # Réaligné sur le code : <fichier> » ; ce réalignement est ARBITRÉ par le "
            "critère d'acceptation du ticket Jira (cite sa clé dans le scénario si tu la connais) : ticket conforme au "
            "code → appliqué ; ticket conforme au test → défaut détecté, le scénario d'origine reste en échec ; pas de "
            "critère exploitable → décision humaine. Ne désactive jamais un scénario : seule une cause hors test "
            "PROUVÉE le désactive, et ce n'est pas à toi de le faire.")
    if cucumber_arbitrage.policy() == "decision":
        rule += " (CUCUMBER_SPEC_CODE_POLICY=decision : tout réalignement est soumis à une décision humaine.)"
    return rule


STATUSES = {"en_attente": "en attente", "validee": "validée", "refusee": "refusée"}
PENDING_COMMENT = "# Décision en attente :"


# ---------------------------------------------------------------------------
# Stockage et historique
# ---------------------------------------------------------------------------

def decisions_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "decisions.json")


def load_decisions(client_name: str) -> list:
    return (_read_json(decisions_path(client_name), {}) or {}).get("decisions", [])


def save_decisions(client_name: str, decisions: list) -> None:
    _write_json(decisions_path(client_name), {"decisions": decisions})


def _event(action: str, comment: str = "", author: str = "") -> dict:
    return {"date": datetime.now().isoformat(timespec="seconds"), "auteur": author or knowledge_tools.author(),
            "action": action, "commentaire": " ".join(str(comment or "").split())[:500]}


def pending(client_name: str) -> list:
    return [d for d in load_decisions(client_name) if d["statut"] == "en_attente"]


def propose(client_name: str, kind: str, proposal: str, **fields) -> dict:
    """
    Ajoute une décision en attente ({type, proposition, fichier, titre, scenario, lot, cause, preuve, reactivation,
    avant, apres…}) ; une décision en attente du même type sur le même scénario (ou le même détail) est réutilisée.
    """
    decisions = load_decisions(client_name)
    key = fields.get("scenario") or proposal
    found = next((d for d in decisions if d["statut"] == "en_attente" and d["type"] == kind
                  and (d.get("scenario") or d["proposition"]) == key), None)
    if found:
        return found
    decision = {"id": f"d-{len(decisions) + 1}", "type": kind, "proposition": " ".join(proposal.split())[:600],
                "statut": "en_attente", "historique": [_event("proposée par l'agent", author="agent")], **fields}
    decisions.append(decision)
    save_decisions(client_name, decisions)
    return decision


def refused_comments(client_name: str, identifier: str) -> list:
    """Commentaires des décisions refusées sur un scénario : ils accompagnent sa prochaine correction."""
    return [e["commentaire"] for d in load_decisions(client_name) if d.get("scenario") == identifier
            and d["statut"] == "refusee" for e in d["historique"] if e["action"] == "refusée" and e["commentaire"]]


# ---------------------------------------------------------------------------
# Blocs de scénarios dans une feature
# ---------------------------------------------------------------------------

def _block_range(lines: list, title: str) -> tuple:
    """(début, fin) du bloc d'un scénario : tags et commentaires au-dessus compris ; (-1, -1) s'il est absent."""
    parsed = parse_scenarios("\n".join(lines))
    starts = []
    for scenario in parsed:
        start = scenario["ligne"] - 1
        while start > 0 and lines[start - 1].strip().startswith(("@", "#")):
            start -= 1
        starts.append(start)
    for i, scenario in enumerate(parsed):
        if _title_key(scenario["titre"]) == _title_key(title):
            return starts[i], starts[i + 1] if i + 1 < len(parsed) else len(lines)
    return -1, -1


def _replace_block(worktree: str, rel: str, title: str, block: list) -> bool:
    path = os.path.join(worktree, *rel.split("/"))
    lines = _read_text_file(path).splitlines()
    start, end = _block_range(lines, title)
    if start < 0:
        return False
    # Lignes vides de fin de bloc conservées (séparation avec le scénario suivant)
    trailing = [line for line in lines[start:end][::-1] if not line.strip()]
    body = [line for line in block]
    while body and not body[-1].strip():
        body.pop()
    _write_text(path, "\n".join(lines[:start] + body + trailing + lines[end:]) + "\n")
    return True


def _held_block(block: list, note: str) -> list:
    """Bloc d'origine, actif (jamais désactivé en attendant), précédé du commentaire de décision en attente."""
    keyword = next(i for i, line in enumerate(block) if re.match(r"^\s*(Scenario|Scénario|Example|Exemple|Plan)",
                                                                 line.strip()))
    indent = re.match(r"^\s*", block[keyword]).group(0)
    head = [line for line in block[:keyword] if not line.strip().startswith(PENDING_COMMENT)]
    first_tag = next((i for i, line in enumerate(head) if line.strip().startswith("@")), len(head))
    return head[:first_tag] + [f"{indent}{PENDING_COMMENT} {note}"] + head[first_tag:] + block[keyword:]


def _default_arbiter(worktree: str):
    """Arbitre Jira sans spécifique connu : tickets cités par le scénario ou l'en-tête de sa feature."""
    return lambda rel, before, after: cucumber_arbitrage.arbitrate(
        before, after, cucumber_arbitrage.ticket_keys("", worktree, rel, before + after))


def held_changes(worktree: str, before: dict, client_tag: str = "", arbiter=None) -> list:
    """
    Modifications d'une correction qui changent le sens d'un scénario existant (features `before` {rel: texte}) :
    attendu réaligné sur le code (« # Réaligné sur le code : »), ou valeur d'un step existant (Given / When, table
    comprise) changée. Renvoie [{type, fichier, titre, avant, apres, proposition, applique, code, arbitrage}].
    - Données changées (correction technique) : appliquées en autonomie, sinon gardées à part.
    - Réalignement : arbitré par le critère d'acceptation du ticket Jira (`arbiter(rel, avant, apres)`, voir
      cucumber_arbitrage) : « code » → appliqué ; « test » → défaut détecté, bloc d'origine actif marqué
      « # Défaut détecté : » (type « defaut ») ; « aucun » → décision humaine.
    Un bloc non appliqué est remplacé par son bloc d'origine, actif (en échec), avec le commentaire de décision.
    """
    arbiter = arbiter or _default_arbiter(worktree)
    held = []
    for rel, text in before.items():
        path = os.path.join(worktree, *rel.split("/"))
        if not os.path.exists(path):
            continue
        old, new = scenario_blocks(text), scenario_blocks(_read_text_file(path))
        for key, (scenario, block) in old.items():
            if key not in new or new[key][1] == block:
                continue
            changed, new_block = new[key]
            kind = _change_kind(scenario, changed, new_block)
            if not kind:
                continue
            reference = next((line.strip()[len(REALIGNED):].strip() for line in new_block
                              if line.strip().startswith(REALIGNED)), "")
            proposal = (f"attendu réaligné sur le code ({reference})" if kind == "realignement" else
                        "valeur d'un step existant changée par la correction")
            verdict = arbiter(rel, block, new_block) if kind == "realignement" else {}
            applied = autonomous() if kind == "donnees" else verdict.get("verdict") == "code"
            entry = {"type": kind, "fichier": rel, "titre": scenario["titre"], "avant": block, "apres": new_block,
                     "proposition": f"{rel} / {scenario['titre']} : {proposal}", "applique": applied,
                     "code": reference, "arbitrage": verdict}
            if verdict.get("verdict") == "test":
                entry["type"] = "defaut"
                _replace_block(worktree, rel, scenario["titre"], cucumber_arbitrage.defect_block(block, verdict))
            elif not applied:
                _replace_block(worktree, rel, scenario["titre"], _held_block(block, TYPES[kind]))
            held.append(entry)
    return held


def _change_kind(old: dict, new: dict, new_block: list) -> str:
    """
    « donnees », « realignement » ou '' (ajout de steps, de colonnes ou de lignes : correction ordinaire). Une valeur
    de step existant changée prime : elle reste soumise à décision même dans un scénario réaligné sur le code.
    """
    given = [(s["texte"], tuple(s.get("table") or [])) for s in old["steps"] if s["phase"] != "then"]
    kept = {(s["texte"], tuple(s.get("table") or [])) for s in new["steps"] if s["phase"] != "then"}
    if not all(step in kept for step in given):
        return "donnees"
    return "realignement" if any(line.strip().startswith(REALIGNED) for line in new_block) else ""


# ---------------------------------------------------------------------------
# Valider, refuser, commenter
# ---------------------------------------------------------------------------

def _restore_active(block: list, client_tag: str) -> list:
    """Bloc réactivé : commentaire de décision ou de désactivation retiré, tag à revoir retiré, tag client remis."""
    tag = a_revoir_tag()
    out, tagged = [], False
    for line in block:
        stripped = line.strip()
        if stripped.startswith((PENDING_COMMENT, DISABLED_COMMENT)):
            continue
        if stripped.startswith("@"):
            tags = [t for t in stripped.split() if t != tag]
            if client_tag and client_tag not in tags:
                tags.append(client_tag)
            tagged = True
            if tags:
                out.append(re.match(r"^\s*", line).group(0) + " ".join(tags))
            continue
        if not tagged and client_tag and re.match(r"^\s*(Scenario|Scénario|Example|Exemple|Plan)", stripped):
            out.append(re.match(r"^\s*", line).group(0) + client_tag)
            tagged = True
        out.append(line)
    return out


def _current_block(worktree: str, decision: dict) -> list:
    lines = _read_text_file(os.path.join(worktree, *decision["fichier"].split("/"))).splitlines()
    start, end = _block_range(lines, decision["titre"])
    return lines[start:end] if start >= 0 else []


def _apply(worktree: str, decision: dict, accepted: bool, client_tag: str) -> bool:
    """Modifie le worktree selon la décision ; True si une feature a changé."""
    if decision["type"] not in HELD_TYPES + ("desactivation",) or not decision.get("fichier"):
        return False
    current = _current_block(worktree, decision)
    if not current:
        return False
    if decision["type"] in HELD_TYPES:
        block = decision["apres"] if accepted else _restore_active(decision["avant"], client_tag)
    elif accepted:
        block = [line.replace(PENDING, "") if line.strip().startswith(DISABLED_COMMENT) else line for line in current]
    else:
        block = _restore_active(current, client_tag)
    return block != current and _replace_block(worktree, decision["fichier"], decision["titre"], block)


def commit_decision(worktree: str, client_name: str, base: str, files: list, message: str) -> bool:
    """Commit dédié des features modifiées, puis patch réécrit s'il existe et qu'aucun diagnostic n'est en cours."""
    _git(worktree, "add", "--", *files)
    if _git(worktree, "diff", "--cached", "--quiet").returncode == 0:
        return False
    if _git(worktree, "commit", "-m", message).returncode != 0:
        return False
    if base and os.path.exists(patch_path(client_name)) and not temporary_steps(worktree):
        write_patch(worktree, base, patch_path(client_name), client_name)
    return True


def resolve(client_name: str, decision_id: str, action: str, comment: str = "", worktree: str = "",
            base: str = "", client_tag: str = "", author: str = "") -> dict:
    """
    « valider », « refuser » ou « commenter » une décision : worktree modifié et commité selon le type (voir
    docstring du module), action historisée. Renvoie la décision à jour ({} si elle est inconnue).
    """
    decisions = load_decisions(client_name)
    decision = next((d for d in decisions if d["id"] == decision_id), None)
    if not decision:
        return {}
    if action == "commenter" or decision["statut"] != "en_attente":
        decision["historique"].append(_event("commentée", comment, author))
    else:
        accepted = action == "valider"
        changed = bool(worktree) and os.path.isdir(worktree) and _apply(worktree, decision, accepted, client_tag)
        decision["statut"] = "validee" if accepted else "refusee"
        decision["historique"].append(_event("validée" if accepted else "refusée", comment, author))
        if changed:
            commit_decision(worktree, client_name, base, [decision["fichier"]],
                            f"Décision {'validée' if accepted else 'refusée'} ({TYPES[decision['type']]}) : "
                            f"{decision['titre']}")
    save_decisions(client_name, decisions)
    return decision


def held_scenario_ids(client_name: str) -> set:
    """Scénarios désactivés provisoirement en attendant une décision."""
    return {d.get("scenario", "") for d in pending(client_name)} - {""}


def summary_lines(decisions: list, limit: int = 20) -> list:
    """Résumé pour le bilan : décisions en attente, puis décisions prises (qui, quand, pourquoi)."""
    lines = []
    for d in sorted(decisions, key=lambda x: x["statut"] != "en_attente")[:limit]:
        last = d["historique"][-1]
        lines.append(f"- {TYPES[d['type']]} — {d['proposition']} : **{STATUSES[d['statut']]}**"
                     + ("" if d["statut"] == "en_attente" else
                        f" par {last['auteur']} le {last['date'][:10]}"
                        + (f" — « {last['commentaire']} »" if last["commentaire"] else "")))
    return lines


def sync_state(state: dict, decision: dict) -> None:
    """
    État de la génération après une décision : le scénario n'attend plus de décision ; une désactivation refusée
    sort des scénarios désactivés (A_REVOIR.md, bilan).
    """
    for lot in state.get("lots") or []:
        if decision.get("scenario") in (lot.get("decisions_en_attente") or []) and decision["statut"] != "en_attente":
            lot["decisions_en_attente"] = [i for i in lot["decisions_en_attente"] if i != decision["scenario"]]
        if decision["type"] == "desactivation" and decision["statut"] == "refusee":
            lot["a_revoir"] = [e for e in lot.get("a_revoir") or []
                               if (e["fichier"], e["titre"]) != (decision.get("fichier"), decision.get("titre"))]
