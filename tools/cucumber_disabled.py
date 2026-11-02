"""
Désactivations prouvées (0 token) : un scénario désactivé pour une cause hors test prouvée (code non déployé,
personnalisation absente, environnement) ne redevient actif que si la cause a disparu.

- Tag de désactivation : le tag générique (CUCUMBER_TAG_A_REVOIR, @A_REVOIR) et ses variantes écrites par un lot
  (@A_REVOIR_ENV…). Le runner n'exclut que le tag générique : une variante seule est complétée par lui, et un
  scénario désactivé ne porte jamais le tag client (il serait exécuté par le job filtré sur ce tag).
- Preuve de disparition de la cause : date de déploiement (deploiement.json, cucumber_job.deployment) — toutes les
  tâches dont le commit cite le ticket de la cause sont « déployé » (commit antérieur à la publication testée).
  Une personnalisation absente n'a pas de preuve automatique : elle se réactive à la main.
- Sans preuve, le scénario reste désactivé : tag client retiré, tag de désactivation remis, ce que l'agent fait
  lui-même (règle rétroactive du tag client, correction d'un lot, livraison).
"""
import re

from tools.cucumber_tools import a_revoir_tag, disabled_tags
from tools.cucumber_verify import scenario_units

_TICKET = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
# Commentaire d'une désactivation pour cause hors test (prouvée par l'agent ou écrite par un lot)
_OUT_OF_TEST = re.compile(r"non\s+d[ée]ploy|personnalisation|ENVIRONNEMENT|d[ée]ploiement|environnement", re.I)
_CUSTOMIZATION = re.compile(r"personnalisation", re.I)
REACTIVATED = "# Réactivé : cause disparue —"


def _tag_lines(lines: list, scenario: int) -> list:
    """Index des lignes de tags juste au-dessus de la ligne Scenario (index 0)."""
    out, index = [], scenario - 1
    while index >= 0 and lines[index].strip().startswith("@"):
        out.insert(0, index)
        index -= 1
    return out


def cause_comment(lines: list, scenario: int) -> str:
    """Commentaires au-dessus des tags du scénario (index 0 de sa ligne Scenario) : la cause de sa désactivation."""
    tags = _tag_lines(lines, scenario)
    index, out = (tags[0] if tags else scenario) - 1, []
    while index >= 0 and lines[index].strip().startswith("#"):
        out.insert(0, lines[index].strip().lstrip("#").strip())
        index -= 1
    return " ".join(out)


def reactivation_proof(cause: str, deployment: dict) -> str:
    """
    Preuve que la cause `cause` a disparu ('' sinon) : chaque tâche dont le dernier commit cite un ticket de la
    cause est déployée (commit antérieur à la publication de la version testée). Personnalisation : jamais prouvée.
    """
    tickets = set(_TICKET.findall(cause or ""))
    if not tickets or _CUSTOMIZATION.search(cause or ""):
        return ""
    tasks = [t for t in ((deployment or {}).get("taches") or {}).values()
             if any(k in str((t.get("commit") or {}).get("sujet", "")) for k in tickets)]
    if not tasks or any(t.get("etat") != "deploye" for t in tasks):
        return ""
    return " ; ".join(t.get("preuve", "") for t in tasks)


def _set_tags(line: str, add=(), remove=()) -> str:
    indent = line[:len(line) - len(line.lstrip())]
    tags = [t for t in line.split() if t not in remove]
    tags += [t for t in add if t not in tags]
    return indent + " ".join(tags)


def enforce(text: str, client_tag: str, deployment: dict = None, disabled: str = "") -> tuple:
    """
    Désactivations de la feature `text` rendues effectives : un scénario désactivé (tag générique ou variante) qui
    porte le tag client est réactivé si la cause a disparu (preuve), sinon il le perd ; une variante seule reçoit
    le tag générique. Renvoie (texte, [{titre, ligne, action: maintenu | reactive | normalise, preuve}]).
    """
    disabled = disabled or a_revoir_tag()
    eol = "\r\n" if "\r\n" in text else "\n"
    lines, actions = text.splitlines(), []
    # Du bas vers le haut : une ligne insérée ne décale pas les scénarios restant à traiter
    for unit in reversed(scenario_units(text)):
        off = disabled_tags(unit["tags"], disabled)
        if not off:
            continue
        scenario, tags = unit["ligne"] - 1, _tag_lines(lines, unit["ligne"] - 1)
        if client_tag in unit["propres"] and tags:
            cause = cause_comment(lines, scenario)
            proof = reactivation_proof(cause, deployment) if _OUT_OF_TEST.search(cause) else ""
            if proof:
                for index in tags:
                    lines[index] = _set_tags(lines[index], remove=off)
                indent = lines[tags[0]][:len(lines[tags[0]]) - len(lines[tags[0]].lstrip())]
                lines.insert(tags[0], f"{indent}{REACTIVATED} {proof}")
                actions.append({"titre": unit["titre"], "ligne": unit["ligne"], "action": "reactive", "preuve": proof})
                continue
            for index in tags:
                lines[index] = _set_tags(lines[index], add=[disabled] if index == tags[-1] else (),
                                         remove=[client_tag])
            actions.append({"titre": unit["titre"], "ligne": unit["ligne"], "action": "maintenu", "preuve": "",
                            "cause": cause})
        elif disabled not in unit["tags"] and tags:
            lines[tags[-1]] = _set_tags(lines[tags[-1]], add=[disabled])
            actions.append({"titre": unit["titre"], "ligne": unit["ligne"], "action": "normalise", "preuve": ""})
    if not actions:
        return text, []
    return eol.join(lines) + (eol if text.endswith(("\n", "\r\n")) else ""), actions


def reactivated_without_proof(before: str, after: str, client_tag: str, deployment: dict = None,
                              disabled: str = "") -> list:
    """
    Scénarios désactivés pour une cause hors test dans `before` et actifs dans `after` sans preuve que la cause a
    disparu : [{titre, cause, tags}] (tags de désactivation à remettre).
    """
    disabled = disabled or a_revoir_tag()
    old_lines, out = before.splitlines(), []
    now = {u["titre"]: u for u in scenario_units(after)}
    for unit in scenario_units(before):
        off = disabled_tags(unit["tags"], disabled)
        current = now.get(unit["titre"])
        if not off or not current or disabled_tags(current["tags"], disabled):
            continue
        cause = cause_comment(old_lines, unit["ligne"] - 1)
        if _OUT_OF_TEST.search(cause) and not reactivation_proof(cause, deployment):
            out.append({"titre": unit["titre"], "cause": cause, "tags": off})
    return out


def restore_disabled(after: str, items: list, client_tag: str, disabled: str = "") -> str:
    """Remet désactivés les scénarios `items` (reactivated_without_proof) : tags de désactivation, sans tag client."""
    disabled = disabled or a_revoir_tag()
    eol = "\r\n" if "\r\n" in after else "\n"
    lines, wanted = after.splitlines(), {i["titre"]: i for i in items}
    for unit in reversed(scenario_units(after)):
        item = wanted.get(unit["titre"])
        if not item:
            continue
        scenario, tags = unit["ligne"] - 1, _tag_lines(lines, unit["ligne"] - 1)
        add = list(dict.fromkeys(item["tags"] + [disabled]))
        if tags:
            lines[tags[-1]] = _set_tags(lines[tags[-1]], add=add, remove=[client_tag])
            for index in tags[:-1]:
                lines[index] = _set_tags(lines[index], remove=[client_tag])
        else:
            indent = lines[scenario][:len(lines[scenario]) - len(lines[scenario].lstrip())]
            lines.insert(scenario, indent + " ".join(add))
        cause = " ".join(item.get("cause", "").split())[:200]
        indent = lines[scenario][:len(lines[scenario]) - len(lines[scenario].lstrip())]
        if cause and cause not in "\n".join(lines[max(0, scenario - 6):scenario]):
            lines.insert(tags[0] if tags else scenario, f"{indent}# {cause}")
    return eol.join(lines) + (eol if after.endswith(("\n", "\r\n")) else "")


def enforce_repo(worktree: str, features: list, client_tag: str, deployment: dict = None, base: str = "") -> dict:
    """
    Désactivations des `features` du worktree rendues effectives (enforce) et, avec `base`, réactivations sans
    preuve depuis `base` annulées (reactivated_without_proof). Fichiers réécrits sur place, sans commit.
    Renvoie {"fichiers": [...], "actions": [{fichier, titre, action, preuve}]}.
    """
    import os
    from tools.cucumber_tools import _git, _read_text_file
    changed, actions = [], []
    for rel in features:
        path = os.path.join(worktree, *rel.split("/"))
        if not os.path.exists(path):
            continue
        text = _read_text_file(path)
        fixed, done = enforce(text, client_tag, deployment)
        if base:
            before = _git(worktree, "show", f"{base}:{rel}").stdout
            lost = reactivated_without_proof(before, fixed, client_tag, deployment) if before else []
            if lost:
                fixed = restore_disabled(fixed, lost, client_tag)
                done += [{"titre": i["titre"], "action": "maintenu", "preuve": "", "cause": i["cause"]} for i in lost]
        if fixed != text:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(fixed)
            changed.append(rel)
        actions += [{**a, "fichier": rel} for a in done]
    return {"fichiers": changed, "actions": actions}


def summary(actions: list) -> list:
    """Lignes lisibles des actions d'enforce_repo."""
    labels = {"maintenu": "reste désactivé (cause non disparue, aucune preuve)",
              "reactive": "réactivé (cause disparue)", "normalise": "tag de désactivation générique ajouté"}
    return [f"{a['fichier'].rsplit('/', 1)[-1]} « {a['titre']} » : {labels[a['action']]}"
            + (f" — {a['preuve']}" if a.get("preuve") else "") for a in actions]
