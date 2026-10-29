"""
Mise de côté de tests en échec, choisie par l'utilisateur pour les analyser plus tard (0 token) : chaque scénario
est désactivé dans sa feature (tag à revoir, commentaire « # À revoir : mis de côté … — <dernière raison d'échec> »,
tag client retiré), jamais supprimé ; liste gardée dans etat.json (« mis_de_cote ») et dans A_REVOIR.md ; commit
dans l'espace de l'agent puis patch régénéré. Réactivation : bloc rétabli tel qu'avant, tag client remis.
"""
import os
from datetime import datetime

from tools import cucumber_a_revoir, cucumber_decisions
from tools.cucumber_tools import (_git, load_inventory, load_state, patch_path, save_state, worktree_dir,
                                  write_patch)


def _split(identifier: str) -> tuple:
    rel, _, title = identifier.partition("::")
    return rel, title


def _deliver(client_name: str, worktree: str, state: dict, files: list, message: str) -> bool:
    """A_REVOIR.md à jour, commit des features touchées, patch régénéré (si une génération existe)."""
    specifics = {s["id"]: s for s in load_inventory(client_name).get("specifiques") or []}
    save_state(client_name, state)
    cucumber_a_revoir.write_a_revoir(worktree, state, specifics)
    _git(worktree, "add", "--", *files)
    if os.path.exists(os.path.join(worktree, cucumber_a_revoir.A_REVOIR_FILE)):
        _git(worktree, "add", "--", cucumber_a_revoir.A_REVOIR_FILE)
    if _git(worktree, "diff", "--cached", "--quiet").returncode == 0:
        return False
    _git(worktree, "commit", "-q", "-m", message)
    if state.get("base"):
        write_patch(worktree, state["base"], patch_path(client_name), client_name)
    return True


def set_aside(client_name: str, failures: list, client_tag: str) -> list:
    """
    Met de côté les scénarios `failures` ([{id, motif}]) : désactivés avec leur dernière raison d'échec. Renvoie les
    identifiants mis de côté.
    """
    worktree, state = worktree_dir(client_name), load_state(client_name)
    by_file = {}
    for failure in failures:
        rel, title = _split(failure["id"])
        if rel and title and os.path.exists(os.path.join(worktree, *rel.split("/"))):
            by_file.setdefault(rel, {})[title] = ("mis_de_cote", {"motif": failure.get("motif", ""),
                                                                  "statut": "mis_de_cote"})
    done, entries = [], state.setdefault("mis_de_cote", [])
    for rel, items in by_file.items():
        disabled = cucumber_a_revoir.deactivate(worktree, rel, items, client_tag)
        for title in disabled:
            identifier = f"{rel}::{title}"
            motif = items.get(title, ("", {}))[1].get("motif", "")
            entries[:] = [e for e in entries if e.get("id") != identifier]
            entries.append({"id": identifier, "fichier": rel, "titre": title, "cause": "mis_de_cote",
                            "preuve": " ".join(str(motif).split())[:400], "prouvee": True,
                            "reactivation": cucumber_a_revoir.CAUSES["mis_de_cote"][1],
                            "date": datetime.now().isoformat(timespec="seconds")})
            done.append(identifier)
    if done:
        _deliver(client_name, worktree, state, list(by_file),
                 f"Cucumber : {len(done)} test(s) en échec mis de côté pour analyse ultérieure")
    return done


def reactivate(client_name: str, identifiers: list, client_tag: str) -> list:
    """Réactive des scénarios désactivés (mis de côté ou désactivés par l'agent). Renvoie ceux réactivés."""
    worktree, state = worktree_dir(client_name), load_state(client_name)
    done, files = [], []
    for identifier in identifiers:
        rel, title = _split(identifier)
        path = os.path.join(worktree, *rel.split("/"))
        if not os.path.exists(path):
            continue
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        start, end = cucumber_decisions._block_range(lines, title)
        if start < 0:
            continue
        restored = cucumber_decisions._restore_active(lines[start:end], client_tag)
        if cucumber_decisions._replace_block(worktree, rel, title, restored):
            done.append(identifier)
            files.append(rel)
    if done:
        state["mis_de_cote"] = [e for e in state.get("mis_de_cote") or [] if e.get("id") not in done]
        for lot in state.get("lots") or []:
            lot["a_revoir"] = [e for e in lot.get("a_revoir") or []
                               if f"{e.get('fichier')}::{e.get('titre')}" not in done]
        _deliver(client_name, worktree, state, sorted(set(files)), f"Cucumber : {len(done)} test(s) réactivé(s)")
    return done
