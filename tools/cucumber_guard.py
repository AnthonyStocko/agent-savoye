"""
Garde-fou de non-régression des corrections d'un run importé (0 token).

Une correction n'est livrée que si des runs COMPLETS de la suite active, lancés par l'agent APRÈS la correction,
comptent chacun strictement moins d'échecs que le run de référence (l'import corrigé) et aucun nouvel échec. La
suite est lancée deux fois de suite (CUCUMBER_GUARD_RUNS) : une donnée créée qui ne se rejoue pas échoue au 2e run.
Sinon — ou si l'agent ne peut pas lancer de run complet — les corrections sont annulées automatiquement (commit
d'annulation dans le worktree : fichiers remis à leur état d'avant les corrections), le patch n'en contient rien, et
le bilan l'explique (imports/<n>/garde_fou.md, etat.json « garde_fou »).
"""
import os
from datetime import datetime

from tools.cucumber_tools import _git, _write_text

DEFAULT_RUNS = 2
REPORT = "garde_fou.md"


def runs() -> int:
    try:
        return max(1, int(os.getenv("CUCUMBER_GUARD_RUNS", "").strip() or DEFAULT_RUNS))
    except ValueError:
        return DEFAULT_RUNS


def failing(analysis: dict) -> set:
    """Scénarios en échec d'une analyse d'import."""
    return {i for i, r in ((analysis or {}).get("resultats") or {}).items() if r.get("statut") != "passe"}


def verdict(reference: dict, run: dict) -> dict:
    """
    Run `run` lancé après les corrections face au run de référence : {ok, motif, avant, apres, nouveaux,
    corriges, import}. ok si run complet analysé, sans écart console, strictement moins d'échecs, aucun nouveau.
    """
    before, after = failing(reference), failing(run)
    out = {"ok": False, "avant": len(before), "apres": len(after), "nouveaux": sorted(after - before),
           "corriges": sorted(before - after), "import": run.get("numero"), "build": run.get("build", "")}
    if run.get("erreur") or run.get("non_analyse"):
        return {**out, "apres": None, "motif": f"run complet non obtenu : {run.get('erreur') or run['non_analyse']}"}
    if not run.get("run_complet"):
        return {**out, "motif": "le run lancé n'est pas un run complet de la suite active (filtre de tags partiel)"}
    if run.get("ecart_console"):
        return {**out, "motif": f"run illisible : {run['ecart_console']}"}
    if out["nouveaux"]:
        return {**out, "motif": f"{len(out['nouveaux'])} nouvel(s) échec(s) : " + ", ".join(
            i.split("::", 1)[-1] for i in out["nouveaux"][:5])}
    if len(after) >= len(before):
        return {**out, "motif": f"pas d'amélioration : {len(after)} échec(s) pour {len(before)} au run de "
                                f"référence"}
    return {**out, "ok": True, "motif": f"{len(before)} → {len(after)} échec(s), aucun nouvel échec"}


def changed_files(worktree: str, start: str) -> list:
    out = _git(worktree, "diff", "--name-only", "--no-renames", "-z", start, "HEAD", "--").stdout
    return sorted(p for p in out.split("\0") if p)


def revert(worktree: str, start: str, reason: str) -> list:
    """
    Annule les corrections commitées depuis `start` : chaque fichier remis à son état de `start` (supprimé s'il
    n'y existait pas), un commit d'annulation. Renvoie les fichiers remis.
    """
    files = changed_files(worktree, start)
    for rel in files:
        if _git(worktree, "cat-file", "-e", f"{start}:{rel}").returncode == 0:
            _git(worktree, "checkout", start, "--", rel)
        else:
            _git(worktree, "rm", "-q", "-f", "--", rel)
    if files:
        _git(worktree, "add", "-A", "--", *files)
        _git(worktree, "commit", "-q", "-m", f"Cucumber : corrections annulées (garde-fou de non-régression) — "
                                             f"{reason[:150]}")
    return files


def report_markdown(result: dict) -> str:
    lines = [f"# Garde-fou de non-régression — corrections de l'import n° {result['reference']}", "",
             f"- Décision : **{'corrections livrées' if result['ok'] else 'corrections annulées'}** — "
             f"{result['motif']}",
             f"- Règle : chaque run complet lancé par l'agent après les corrections compte strictement moins "
             f"d'échecs que le run de référence ({result['avant']}) et aucun nouvel échec ; "
             f"{result['runs_voulus']} run(s) de suite."]
    for number, run in enumerate(result["runs"], 1):
        lines.append(f"- Run {number} : " + (f"import n° {run['import']}" if run.get("import") else "non obtenu")
                     + (f", build {run['build']}" if run.get("build") else "")
                     + f" — {'✅' if run['ok'] else '⛔'} {run['motif']}")
        lines += [f"  - nouvel échec : {i}" for i in run["nouveaux"][:20]]
        lines += [f"  - corrigé : {i}" for i in run["corriges"][:20]]
    if result.get("annules"):
        lines += ["", "## Fichiers remis à leur état d'avant les corrections", ""]
        lines += [f"- {f}" for f in result["annules"]]
    return "\n".join(lines) + "\n"


def guard(state: dict, reference: dict, worktree: str, start: str, launch, imports_folder: str = "",
          can_run: bool = True, log=print) -> dict:
    """
    Garde-fou des corrections commitées depuis `start` pour l'import `reference` (son analyse). `launch()` lance un
    run complet de la suite et renvoie l'analyse de son import ({"erreur"} s'il n'aboutit pas). Renvoie
    {ok, motif, runs, annules, ...} ; enregistré dans state["garde_fou"] et imports/<n>/garde_fou.md.
    """
    wanted = runs()
    result = {"reference": reference.get("numero"), "debut": start, "date": datetime.now().isoformat(timespec="seconds"),
              "avant": len(failing(reference)), "runs_voulus": wanted, "runs": [], "annules": [], "ok": False,
              "motif": ""}
    if not changed_files(worktree, start):
        return {**result, "ok": True, "motif": "aucune correction commitée"}
    if not reference.get("run_complet"):
        result["motif"] = ("le run de référence n'est pas un run complet de la suite : rien ne permet de prouver "
                           "l'absence de régression")
    elif not can_run:
        result["motif"] = ("l'agent ne peut pas lancer de run complet (CUCUMBER_AGENT_JENKINS=0 ou identifiants "
                           "Jenkins absents) : aucune preuve de non-régression")
    else:
        for number in range(1, wanted + 1):
            log(f"[GARDE-FOU] Run complet {number}/{wanted} lancé après les corrections…")
            run = verdict(reference, launch() or {"erreur": "aucun résultat"})
            result["runs"].append(run)
            log(f"[GARDE-FOU] Run {number} : {'✅' if run['ok'] else '⛔'} {run['motif']}")
            if not run["ok"]:
                result["motif"] = f"run {number}/{wanted} : {run['motif']}"
                break
        else:
            result.update(ok=True, motif=f"{wanted} run(s) complet(s) : " + " ; ".join(r["motif"]
                                                                                    for r in result["runs"]))
    if not result["ok"]:
        result["annules"] = revert(worktree, start, result["motif"])
        log(f"[GARDE-FOU] ⛔ Corrections annulées ({len(result['annules'])} fichier(s) remis) : {result['motif']}")
    state.setdefault("garde_fou", []).append({k: v for k, v in result.items() if k != "runs"}
                                             | {"runs": [{k: r[k] for k in ("ok", "motif", "avant", "apres", "import")}
                                                         for r in result["runs"]]})
    if imports_folder:
        _write_text(os.path.join(imports_folder, REPORT), report_markdown(result))
    return result
