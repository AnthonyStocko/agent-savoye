"""
Fichiers de test écrits par l'agent HORS de l'espace de travail du dépôt <client>-cucumber (0 token). L'agent tourne
depuis .tmp/cucumber/<client>/ : un chemin relatif mal formé (ex. « cucumber_apparel_agent2/src/test/… ») y crée un
dossier parasite, et ces fichiers manqueraient au livrable. Après chaque appel, tout fichier récent dont le chemin
contient « src/test/ » et qui n'est pas dans un dépôt git (worktree, build, autres copies) est rapatrié au même
chemin dans le worktree, puis le dossier parasite est supprimé.
"""
import os
import shutil

MARK = "src/test/"
SKIPPED = {".git", "target", "node_modules", "imports", "verification", "build"}


def _is_repository(path: str) -> bool:
    return os.path.exists(os.path.join(path, ".git"))


def relocate(run_dir: str, worktree: str, since: float = 0.0) -> list:
    """
    Rapatrie dans `worktree` les fichiers de test écrits depuis `since` hors d'un dépôt (0 : quelle que soit leur
    date, mais jamais par-dessus une version plus récente du worktree). Renvoie [(source, cible)].
    """
    if not (run_dir and worktree and os.path.isdir(run_dir) and os.path.isdir(worktree)):
        return []
    moved, parasites = [], set()
    worktree = os.path.abspath(worktree)
    for root, dirs, files in os.walk(run_dir):
        dirs[:] = [d for d in dirs if d not in SKIPPED and not _is_repository(os.path.join(root, d))
                   and os.path.abspath(os.path.join(root, d)) != worktree]
        for name in files:
            source = os.path.join(root, name)
            rel = os.path.relpath(source, run_dir).replace(os.sep, "/")
            index = rel.find(MARK)
            try:
                recent = os.path.getmtime(source) >= since
            except OSError:
                continue
            if index < 0 or not recent:
                continue
            target = os.path.join(worktree, *rel[index:].split("/"))
            if not since and os.path.exists(target) and os.path.getmtime(target) >= os.path.getmtime(source):
                continue   # rattrapage : la version du dépôt est plus récente, l'égarée est périmée
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)
            os.remove(source)
            moved.append((rel, rel[index:]))
            if index:
                parasites.add(os.path.join(run_dir, *rel[:index].rstrip("/").split("/")[:1]))
    for folder in parasites:
        # Dossier parasite vidé de ses fichiers de test : supprimé s'il ne contient plus rien d'autre
        if os.path.isdir(folder) and not any(files for _, _, files in os.walk(folder)):
            shutil.rmtree(folder, ignore_errors=True)
    return moved
