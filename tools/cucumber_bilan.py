"""
Bilan après chaque import de logs (onglet Cucumber, étape Bilan ; main.py --import-run), 0 token.

Sections, défauts en tête :
- Défauts détectés dans le code spécifique ou le Studio : fichier, ligne, extrait, effet observé, preuve d'exécution,
  correction suggérée (constats « defaut_code » des lots et des corrections, défauts produit des désactivations).
- Corrigés depuis le run précédent : scénario, cause, correction, fichiers modifiés (lots « correction »).
- Désactivés : cause, preuve, condition de réactivation.
- Doutes sur le code fourni en spécification (écarts ticket / code, comportements surprenants) et leur certitude.
- Corrections manuelles de l'environnement, détectées (échec hors test devenu vert sans aucune correction des
  tests) ou signalées dans l'IHM : interdites dans la durée, chacune avec la façon de la recréer dans l'init.
- Prochaine action proposée.

Écrit .tmp/cucumber/<client>/imports/<n>/bilan.json et bilan.md (bilan puis analyse), remplace la section « Bilan
du dernier run importé » d'A_REVOIR.md (worktree, commit dédié, patch réécrit s'il existe) et se dépose dans les
connaissances partagées (share_bilan).
"""
import os
from datetime import datetime

from tools import cucumber_decisions, cucumber_diagnostic, cucumber_import, knowledge_tools
from tools.cucumber_a_revoir import A_REVOIR_FILE, CAUSES, UNPROVEN
from tools.cucumber_tools import (CONSTAT_TYPES, _git, _read_json, _write_json, _write_text, cache_dir, patch_path,
                                  save_state, write_patch)
from tools.cucumber_verify import failure_category

BEGIN = "<!-- agent-savoye : début du bilan du dernier run importé -->"
END = "<!-- agent-savoye : fin du bilan du dernier run importé -->"
MANUAL_FILE = "corrections_manuelles.json"
RECREATION_HINTS = {
    "donnee_reference": "recréer la donnée par l'init du projet (fichier d'intégration du module spécifique, champ "
                        "« fichiers du module spécifique » du profil, ou step de création de l'init) ; à défaut, la "
                        "créer dans le Background des features qui l'utilisent",
    "environnement": "ajouter ce paramétrage à un fichier d'intégration de l'init du projet (champ « fichiers du "
                     "module spécifique » du profil) ; s'il est effacé par le clear, l'ajouter à la recréation de "
                     "l'init",
}


# ---------------------------------------------------------------------------
# Corrections manuelles de l'environnement signalées dans l'IHM
# ---------------------------------------------------------------------------

def manual_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), MANUAL_FILE)


def manual_corrections(client_name: str) -> list:
    """Corrections manuelles signalées : [{date, auteur, description}]."""
    return (_read_json(manual_path(client_name), {}) or {}).get("corrections", [])


def add_manual_correction(client_name: str, description: str) -> list:
    """Historise une correction manuelle signalée (qui, quand, quoi). Renvoie la liste à jour."""
    corrections = manual_corrections(client_name)
    if description.strip():
        corrections.append({"date": datetime.now().isoformat(timespec="seconds"), "auteur": knowledge_tools.author(),
                            "description": " ".join(description.split())[:500]})
        _write_json(manual_path(client_name), {"corrections": corrections})
    return corrections


def recreation_hint(category: str) -> str:
    """Façon de recréer dans l'init du projet une correction faite à la main sur l'environnement."""
    return RECREATION_HINTS.get(category, RECREATION_HINTS["environnement"])


# ---------------------------------------------------------------------------
# Sections du bilan
# ---------------------------------------------------------------------------

def _constats(state: dict) -> list:
    """Constats des lots et des corrections : [(constat, origine)] dédoublonnés par (type, fichier, détail)."""
    seen, out = set(), []
    for lot in state.get("lots") or []:
        found = [(c, f"lot {lot['numero']}") for r in lot.get("resultats") or [] for c in r.get("constats") or []]
        found += [(c, f"correction du lot {lot['numero']}") for c in (lot.get("correction") or {}).get("constats", [])]
        for constat, origin in found:
            key = (constat.get("type"), constat.get("fichier"), constat.get("detail"))
            if key not in seen:
                seen.add(key)
                out.append((constat, origin))
    return out


def defects(state: dict) -> list:
    """Défauts du code spécifique ou du Studio : constats « defaut_code », puis défauts produit désactivés."""
    out = [{"fichier": c.get("fichier", ""), "ligne": c.get("ligne", ""), "extrait": c.get("extrait", ""),
            "effet": c.get("effet") or c["detail"], "preuve": c.get("preuve", ""), "correction": c.get("correction", ""),
            "certitude": c.get("certitude", "à vérifier"), "origine": origin}
           for c, origin in _constats(state) if c.get("type") == "defaut_code"]
    out += [{"fichier": e["fichier"], "ligne": "", "extrait": "", "effet": f"{e['titre']} : {CAUSES[e['cause']][0]}",
             "preuve": e.get("preuve", ""), "correction": "",
             "certitude": "prouvé" if e.get("prouvee", True) else "à vérifier", "origine": "scénario désactivé"}
            for lot in state.get("lots") or [] for e in lot.get("a_revoir") or [] if e.get("cause") == "bug_applicatif"]
    return out


def doubts(state: dict) -> list:
    """Doutes sur le code fourni : écarts ticket / code et comportements surprenants, avec leur certitude."""
    return [{"type": CONSTAT_TYPES[c["type"]], "detail": c["detail"], "fichier": c.get("fichier", ""),
             "certitude": c.get("certitude", "à vérifier"), "preuve": c.get("preuve", ""), "origine": origin}
            for c, origin in _constats(state) if c.get("type") in ("ecart_spec_code", "comportement_surprenant")]


def disabled(state: dict) -> list:
    """Scénarios désactivés : cause, preuve, condition de réactivation."""
    return [{"scenario": f"{e['fichier']} / {e['titre']}", "cause": CAUSES[e["cause"]][0], "preuve": e.get("preuve", ""),
             "reactivation": e.get("reactivation", ""), "prouvee": e.get("prouvee", True)}
            for lot in state.get("lots") or [] for e in lot.get("a_revoir") or []]


def _correction_of(identifier: str, state: dict) -> dict:
    """Dernier lot « correction » qui a traité ce scénario : {lot, cause, correction, fichiers} ; {} sinon."""
    for lot in sorted(state.get("lots") or [], key=lambda x: -x["numero"]):
        item = next((s for s in lot.get("corrections") or [] if s.get("id") == identifier), None)
        if item:
            done = next((c for c in (lot.get("correction") or {}).get("corrections", [])
                         if c.get("tag") == item.get("tag")), {})
            proof = next((p for p in (lot.get("correction") or {}).get("preuves_base", [])
                          if p.get("tag") == item.get("tag")), {})
            text = done.get("correction") or f"corrigé par le lot de correction {lot['numero']}"
            if proof:
                text += (f" (preuve en base : {', '.join(proof.get('tables') or [])} → {proof.get('resultat', '')}"
                         f"{' ; ' + proof['conclusion'] if proof.get('conclusion') else ''})")
            return {"lot": lot["numero"], "cause": done.get("cause") or item.get("message", ""),
                    "correction": text,
                    "fichiers": lot.get("fichiers") or [item.get("fichier", "")]}
    return {}


def fixed_and_manual(analysis: dict, previous: dict, state: dict) -> tuple:
    """
    (corrigés, corrections manuelles détectées) depuis le run précédent. Un échec hors test devenu vert sans
    aucune correction des tests révèle une correction manuelle de l'environnement.
    """
    fixed, manual = [], []
    for identifier in (analysis.get("comparaison") or {}).get("corriges", []):
        before = previous.get(identifier) or {}
        category = "test" if before.get("statut") == "indefini" else failure_category(before.get("nature", ""))
        correction = _correction_of(identifier, state)
        if correction:
            fixed.append({"scenario": identifier, **correction})
        elif category != "test":
            manual.append({"description": f"{identifier} : « {before.get('message', '')[:160]} » a disparu sans "
                                          f"correction des tests", "origine": "détectée", "categorie": category,
                           "recreation": recreation_hint(category)})
        else:
            fixed.append({"scenario": identifier, "lot": "", "cause": before.get("message", ""),
                          "correction": "passe sans correction de l'agent (modification manuelle des tests ?)",
                          "fichiers": []})
    return fixed, manual


def next_action(analysis: dict, bilan: dict) -> str:
    """Prochaine action proposée, de la plus bloquante à la livraison."""
    if analysis.get("non_analyse"):
        return f"Importer le bon run : {analysis['non_analyse']}"
    init = analysis.get("init_en_echec") or {}
    if init:
        return f"Corriger l'initialisation du client ({init['step'] or init['id']} : {init['message'][:160]}), puis relancer"
    if bilan.get("decisions_en_attente"):
        return f"Trancher les {bilan['decisions_en_attente']} décision(s) en attente (étape Correction)"
    if analysis.get("propositions"):
        return f"Lancer la correction ciblée ({len(analysis['propositions'])} lot(s), étape Correction)"
    if analysis.get("a_prouver"):
        return (f"Apporter une preuve pour {len(analysis['a_prouver'])} cause(s) restées en échec après "
                f"{cucumber_import.max_attempts()} corrections (méthodes proposées dans le bilan)")
    regressions = (analysis.get("comparaison") or {}).get("regressions", [])
    if regressions:
        return f"Examiner les {len(regressions)} régression(s) : scénarios verts au run précédent"
    exports = [e for e in analysis.get("logs") or [] if e.get("a_exporter")]
    if exports:
        return f"Exporter les logs serveur des {len(exports)} échec(s) (fenêtres indiquées) puis les importer"
    if bilan["manuelles"]:
        return (f"Générer l'initialisation client (étape Projet) : elle recrée les {len(bilan['manuelles'])} "
                f"correction(s) manuelle(s) de l'environnement")
    green, failed = run_counts(analysis)
    if failed:
        return f"Traiter les {failed} échec(s) hors test (environnement, données, défauts) : voir A_REVOIR.md"
    return "Livrer le patch : tous les scénarios exécutés sont verts"


def run_counts(analysis: dict) -> tuple:
    """(verts, échecs) d'une analyse d'import."""
    results = (analysis.get("resultats") or {}).values()
    green = sum(1 for r in results if r.get("statut") == "passe")
    return green, len(results) - green


def build_bilan(client_name: str, analysis: dict, state: dict) -> dict:
    """Bilan de l'import `analysis` (voir docstring du module)."""
    comparison = analysis.get("comparaison") or {}
    previous = (_read_json(os.path.join(cucumber_import.imports_dir(client_name), str(comparison["import"]),
                                        "analyse.json"), {}) or {}).get("resultats", {}) if comparison else {}
    fixed, manual = fixed_and_manual(analysis, previous, state)
    manual += [{"description": m["description"], "origine": f"signalée par {m['auteur']} le {m['date'][:10]}",
                "categorie": "", "recreation": recreation_hint("")} for m in manual_corrections(client_name)]
    green, failed = run_counts(analysis)
    bilan = {"import": analysis["numero"], "date": datetime.now().isoformat(timespec="seconds"),
             "verts": green, "echecs": failed, "regressions": comparison.get("regressions", []),
             "init_en_echec": analysis.get("init_en_echec") or {}, "non_analyse": analysis.get("non_analyse", ""),
             "lents": (analysis.get("durees") or {}).get("lents", []), "defauts": defects(state),
             "corriges": fixed, "desactives": disabled(state), "doutes": doubts(state), "manuelles": manual,
             "a_prouver": analysis.get("a_prouver") or [], "diagnostics": analysis.get("diagnostics") or {},
             "decisions": cucumber_decisions.summary_lines(cucumber_decisions.load_decisions(client_name)),
             "decisions_en_attente": len(cucumber_decisions.pending(client_name))}
    bilan["prochaine_action"] = next_action(analysis, bilan)
    return bilan


# ---------------------------------------------------------------------------
# Rendus et dépôts
# ---------------------------------------------------------------------------

def _defect_lines(defect: dict) -> list:
    where = defect["fichier"] + (f":{defect['ligne']}" if defect["ligne"] else "")
    lines = [f"- **{where or 'fichier non précisé'}** — {defect['effet']} ({defect['certitude']}, {defect['origine']})"]
    lines += [f"  - Extrait : `{defect['extrait']}`"] if defect["extrait"] else []
    lines += [f"  - Preuve d'exécution : `{defect['preuve']}`"] if defect["preuve"] else [
        f"  - Preuve d'exécution : aucune — {UNPROVEN}"]
    lines += [f"  - Correction suggérée : {defect['correction']}"] if defect["correction"] else []
    return lines


def bilan_markdown(bilan: dict, level: int = 2) -> str:
    """Bilan lisible (IHM, A_REVOIR.md, répertoire partagé) ; `level` : niveau de son titre (sections au suivant)."""
    title, section = "#" * level, "#" * (level + 1)
    lines = [f"{title} Bilan du run importé n° {bilan['import']}", "",
             f"**Prochaine action** : {bilan['prochaine_action']}", "",
             f"- Résultat : {bilan['verts']} vert(s), {bilan['echecs']} échec(s)"
             + (f", {len(bilan['regressions'])} régression(s)" if bilan["regressions"] else "")
             + (f", {len(bilan['lents'])} scénario(s) anormalement long(s)" if bilan["lents"] else "")]
    if bilan["non_analyse"]:
        lines.append(f"- ⛔ Run non analysé : {bilan['non_analyse']}")
    if bilan["init_en_echec"]:
        init = bilan["init_en_echec"]
        lines.append(f"- ⛔ Initialisation en échec, aucun scénario jugé : {init['step'] or init['id']} — "
                     f"{init['message']}")
    lines += ["", f"{section} Défauts détectés dans le code spécifique ou le Studio : {len(bilan['defauts'])}", ""]
    for defect in bilan["defauts"]:
        lines += _defect_lines(defect)
    lines += ["", f"{section} Corrigés depuis le run précédent : {len(bilan['corriges'])}", ""]
    lines += [f"- {f['scenario']} — cause : {f['cause'][:160] or '?'} ; correction : {f['correction']}"
              + (f" ; fichiers : {', '.join(f['fichiers'])}" if f["fichiers"] else "") for f in bilan["corriges"]]
    lines += ["", f"{section} Désactivés : {len(bilan['desactives'])}", ""]
    lines += [f"- {d['scenario']} — cause : {d['cause']}" + ("" if d["prouvee"] else f" ({UNPROVEN})")
              + f" ; preuve : `{d['preuve']}` ; réactivation : {d['reactivation']}" for d in bilan["desactives"]]
    lines += ["", f"{section} Doutes sur le code fourni en spécification : {len(bilan['doutes'])}", ""]
    lines += [f"- {d['type']} ({d['certitude']}) : {d['detail']}" + (f" — {d['fichier']}" if d["fichier"] else "")
              + (f" — preuve : `{d['preuve']}`" if d["preuve"] else "") for d in bilan["doutes"]]
    lines += ["", f"{section} Décisions humaines : {bilan.get('decisions_en_attente', 0)} en attente", ""]
    lines += bilan.get("decisions") or []
    to_prove = bilan.get("a_prouver") or []
    if to_prove:
        lines += ["", f"{section} Causes à prouver (l'agent a arrêté de deviner) : {len(to_prove)}", ""]
        for item in to_prove:
            lines.append(f"- {item['id']} — `{item['message'][:200]}` ; essayé : " + " ; ".join(
                f"lot {t['lot']} : {t['correction']}" for t in item["tentatives"]))
            lines += [f"  - Méthode de preuve : {m}" for m in item["methodes"]]
    if bilan.get("diagnostics"):
        lines += ["", f"{section} Diagnostics temporaires exécutés : {len(bilan['diagnostics'])}", ""]
        lines += [f"- {i} : {text[:300]}" for i, text in bilan["diagnostics"].items()]
        if bilan.get("diagnostics_retires"):
            lines.append(f"- Retirés du worktree après ce run : {', '.join(bilan['diagnostics_retires'])}")
    lines += ["", f"{section} Corrections manuelles de l'environnement : {len(bilan['manuelles'])}", ""]
    if bilan["manuelles"]:
        lines.append("Interdites dans la durée : l'environnement doit pouvoir être reconstruit par l'init du projet. "
                     "« Générer l'initialisation client » transforme les corrections signalées en recréation dans "
                     "l'init, suivie d'un contrôle de l'environnement en lecture seule.")
    lines += [f"- {m['description']} ({m['origine']}) → {m['recreation']}" for m in bilan["manuelles"]]
    return "\n".join(lines) + "\n"


def write_a_revoir_bilan(worktree: str, markdown: str) -> bool:
    """Remplace (ou ajoute) la section du dernier bilan dans A_REVOIR.md du worktree. True si le fichier a changé."""
    if not worktree or not os.path.isdir(worktree):
        return False
    path = os.path.join(worktree, A_REVOIR_FILE)
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else "# À revoir\n"
    block = f"{BEGIN}\n\n{markdown}\n{END}"
    if BEGIN in text and END in text:
        updated = text[:text.index(BEGIN)] + block + text[text.index(END) + len(END):]
    else:
        updated = text.rstrip("\n") + "\n\n" + block + "\n"
    if updated == text:
        return False
    _write_text(path, updated)
    return True


def commit_bilan(worktree: str, client_name: str, state: dict, numero: int) -> bool:
    """Commit dédié d'A_REVOIR.md, puis patch réécrit s'il existe (le bilan est livré). True si commité."""
    _git(worktree, "add", "--", A_REVOIR_FILE)
    if _git(worktree, "diff", "--cached", "--quiet").returncode == 0:
        return False
    if _git(worktree, "commit", "-m", f"A_REVOIR.md : bilan du run importé n° {numero}").returncode != 0:
        return False
    if state.get("base") and os.path.exists(patch_path(client_name)):
        write_patch(worktree, state["base"], patch_path(client_name), client_name)
    return True


def after_import(client_name: str, analysis: dict, state: dict, worktree: str = "") -> dict:
    """
    Bilan d'un import analysé : bilan.json et bilan.md (bilan puis analyse) dans le dossier de l'import, section
    d'A_REVOIR.md commitée. Renvoie le bilan, avec "fichier" (bilan.md) et "a_revoir" (A_REVOIR.md mis à jour).
    """
    bilan = build_bilan(client_name, analysis, state)
    bilan["diagnostics_retires"] = remove_run_diagnostics(client_name, analysis, state, worktree)
    folder = os.path.join(cucumber_import.imports_dir(client_name), str(analysis["numero"]))
    markdown = bilan_markdown(bilan)
    bilan["fichier"] = os.path.join(folder, "bilan.md")
    _write_text(bilan["fichier"], markdown + "\n---\n\n" + cucumber_import.analysis_markdown(analysis))
    bilan["a_revoir"] = write_a_revoir_bilan(worktree, markdown) and commit_bilan(worktree, client_name, state,
                                                                                 analysis["numero"])
    _write_json(os.path.join(folder, "bilan.json"), bilan)
    return bilan


def remove_run_diagnostics(client_name: str, analysis: dict, state: dict, worktree: str) -> list:
    """
    Diagnostics temporaires gardés pour ce run (D7) : exécutés (sortie lue dans le rapport), ils sont retirés du
    worktree (commit dédié) et les lots ne les attendent plus. Renvoie les fichiers modifiés.
    """
    waiting = [lot for lot in state.get("lots") or [] if lot.get("diagnostics_en_attente")]
    if not analysis.get("diagnostics") or not waiting or not worktree or not os.path.isdir(worktree):
        return []
    removed = cucumber_diagnostic.remove_temporary(worktree)
    if removed:
        cucumber_decisions.commit_decision(worktree, client_name, state.get("base", ""), removed,
                                           f"Retrait des diagnostics temporaires exécutés au run importé n° "
                                           f"{analysis['numero']}")
    for lot in waiting:
        lot.pop("diagnostics_en_attente", None)
    save_state(client_name, state)
    return removed


def latest_bilan(client_name: str) -> dict:
    """Bilan du dernier import ({} s'il n'y en a pas)."""
    index = cucumber_import.load_index(client_name)
    if not index.get("imports"):
        return {}
    numero = max(e["numero"] for e in index["imports"])
    return _read_json(os.path.join(cucumber_import.imports_dir(client_name), str(numero), "bilan.json"), {}) or {}


def share_bilan(client_name: str, bilan: dict, project: str = "") -> dict:
    """Dépôt du bilan (et de l'analyse) dans les connaissances partagées ; {} si la fonction est désactivée."""
    if not knowledge_tools.enabled() or not bilan.get("fichier") or not os.path.exists(bilan["fichier"]):
        return {}
    with open(bilan["fichier"], encoding="utf-8") as f:
        body = f.read()
    return knowledge_tools.publish_knowledge(body, project or client_name.upper(), "cucumber-diagnostic-run",
                                             f"Bilan du run importé n° {bilan['import']}", client=client_name,
                                             tags=["cucumber"], resume=bilan["prochaine_action"])
