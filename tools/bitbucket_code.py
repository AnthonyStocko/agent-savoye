"""
Code du Standard lu à jour dans Bitbucket (API REST), pour l'agent des analyses de tickets du socle seul
(STD_ONLY_PROJECTS, ex. OXY) : aucune copie locale qui daterait de la veille.

Chaque appel résout la branche de base de chaque dépôt (STD_BASE_BRANCHES, défaut « dev,development », puis la branche
par défaut) en son DERNIER commit : le code lu est toujours celui de Bitbucket à cet instant (contenus gardés en cache
par commit, donc jamais périmés).

  python tools/bitbucket_code.py repos [--like TEXTE]
  python tools/bitbucket_code.py search "TEXTE" [--limit 25]        recherche de code (index de la branche par défaut)
  python tools/bitbucket_code.py files DEPOT [--prefix DOSSIER/] [--like TEXTE] [--max 300]
  python tools/bitbucket_code.py show DEPOT CHEMIN [--from N] [--to M]
  python tools/bitbucket_code.py grep DEPOT REGEX [--prefix DOSSIER/] [--ext .java] [--max-files 300] [--max 100]
  python tools/bitbucket_code.py checkout DEPOT
      étape patch seulement : copie locale À JOUR (clone ou fetch, puis branche de base) du seul dépôt à modifier,
      dans .tmp/std_<PROJET>_travail/<DEPOT> ; jamais de push (aucun identifiant n'est enregistré dans la copie).
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:  # pragma: no cover
    pass

from tools.bitbucket_std import BitbucketUnavailable, RemoteStandard, project_key  # noqa: E402


def base_candidates() -> list:
    names = [n.strip() for n in os.getenv("STD_BASE_BRANCHES", "dev,development").split(",") if n.strip()]
    return [f"origin/{n}" for n in names] + ["HEAD"]


def standard(project: str = "") -> RemoteStandard:
    key = project or project_key(os.getenv("BITBUCKET_STD_REPO_PATH", ""))
    if not key:
        raise BitbucketUnavailable("BITBUCKET_STD_REPO_PATH n'est pas l'adresse d'un projet Bitbucket (…/projects/CLE)")
    return RemoteStandard(key)


def work_root(std: RemoteStandard) -> str:
    return os.path.join(ROOT, ".tmp", f"std_{std.project}_travail")


def _base(std: RemoteStandard, repo: str) -> tuple:
    if repo not in std.repos():
        raise BitbucketUnavailable(f"dépôt {repo} absent du projet {std.project}")
    commit = std.ref(repo, base_candidates())
    if not commit:
        raise BitbucketUnavailable(f"{repo} : aucune branche de base ({', '.join(base_candidates())})")
    return commit, std.ref_name(repo, commit)


def _paths(std, repo, commit, prefix="", like="", ext="") -> list:
    return [p for p in std.files(repo, commit)
            if p.startswith(prefix) and (not like or like.lower() in p.lower()) and (not ext or p.endswith(ext))]


def cmd_repos(std, args) -> list:
    return [r for r in std.repos() if not args.like or args.like.lower() in r.lower()]


def cmd_search(std, args) -> list:
    return [f"{repo}/{path}" for repo, path in std.search(args.query)[:args.limit]] or ["(aucun résultat)"]


def cmd_files(std, args) -> list:
    commit, name = _base(std, args.repo)
    paths = _paths(std, args.repo, commit, args.prefix, args.like)
    head = [f"# {args.repo} @ {name} ({commit[:10]}) : {len(paths)} fichier(s)"]
    return head + paths[:args.max] + ([f"… ({len(paths) - args.max} de plus : affine --prefix / --like)"]
                                      if len(paths) > args.max else [])


def cmd_show(std, args) -> list:
    commit, name = _base(std, args.repo)
    text = std.show(args.repo, commit, args.path)
    if not text:
        return [f"(fichier absent : {args.repo}/{args.path} @ {name})"]
    lines = text.splitlines()
    start, end = max(1, args.start), min(len(lines), args.end or len(lines))
    return [f"# {args.repo}/{args.path} @ {name} ({commit[:10]}), lignes {start}-{end} sur {len(lines)}"] + \
        [f"{n}\t{lines[n - 1]}" for n in range(start, end + 1)]


def cmd_grep(std, args) -> list:
    commit, name = _base(std, args.repo)
    pattern = re.compile(args.regex)
    paths = _paths(std, args.repo, commit, args.prefix, ext=args.ext)
    out, scanned = [], paths[:args.max_files]
    for path in scanned:
        for n, line in enumerate(std.show(args.repo, commit, path).splitlines(), 1):
            if pattern.search(line):
                out.append(f"{path}:{n}: {line.strip()[:240]}")
                if len(out) >= args.max:
                    break
        if len(out) >= args.max:
            break
    head = [f"# {args.repo} @ {name} ({commit[:10]}) : {len(scanned)} fichier(s) lus sur {len(paths)}"
            + (" — affine --prefix / --ext" if len(paths) > len(scanned) else "")]
    return head + (out or ["(aucune occurrence)"])


def _git(args: list, cwd: str = None, token: str = "") -> subprocess.CompletedProcess:
    extra = ["-c", f"http.extraHeader=Authorization: Bearer {token}"] if token else []
    return subprocess.run(["git", *extra, *args], cwd=cwd, capture_output=True, text=True)


def cmd_checkout(std, args) -> list:
    commit, name = _base(std, args.repo)
    target = os.path.join(work_root(std), args.repo)
    url = f"{std.base}/scm/{std.project.lower()}/{args.repo}.git"
    if not os.path.isdir(os.path.join(target, ".git")):
        os.makedirs(work_root(std), exist_ok=True)
        cloned = _git(["clone", "-q", url, target], token=std.token)
        if cloned.returncode != 0:
            raise BitbucketUnavailable(f"clone de {args.repo} impossible : {cloned.stderr.strip()[-200:]}")
    else:
        fetched = _git(["fetch", "-q", "--prune", "origin"], cwd=target, token=std.token)
        if fetched.returncode != 0:
            raise BitbucketUnavailable(f"fetch de {args.repo} impossible : {fetched.stderr.strip()[-200:]}")
    if _git(["status", "--porcelain", "--untracked-files=no"], cwd=target).stdout.strip():
        return [f"{target} : modifications non commitées présentes, copie laissée telle quelle (fetch fait) ; "
                f"base à jour : origin/{name} ({commit[:10]})"]
    switched = _git(["checkout", "-q", "-B", name, f"origin/{name}"], cwd=target)
    if switched.returncode != 0:
        raise BitbucketUnavailable(f"{args.repo} : branche {name} impossible ({switched.stderr.strip()[-200:]})")
    return [f"Copie à jour de {args.repo} : {target}", f"Branche de base : {name} @ {commit[:10]} (crée ta branche "
            f"de travail à partir d'elle)"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("repos")
    p.add_argument("--like", default="")
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=25)
    p = sub.add_parser("files")
    p.add_argument("repo")
    p.add_argument("--prefix", default="")
    p.add_argument("--like", default="")
    p.add_argument("--max", type=int, default=300)
    p = sub.add_parser("show")
    p.add_argument("repo")
    p.add_argument("path")
    p.add_argument("--from", dest="start", type=int, default=1)
    p.add_argument("--to", dest="end", type=int, default=0)
    p = sub.add_parser("grep")
    p.add_argument("repo")
    p.add_argument("regex")
    p.add_argument("--prefix", default="")
    p.add_argument("--ext", default="")
    p.add_argument("--max-files", dest="max_files", type=int, default=300)
    p.add_argument("--max", type=int, default=100)
    p = sub.add_parser("checkout")
    p.add_argument("repo")
    args = parser.parse_args(argv)
    try:
        std = standard(args.project)
        lines = globals()[f"cmd_{args.command}"](std, args)
        print("\n".join(lines))
    except (BitbucketUnavailable, re.error) as e:
        print(f"BITBUCKET_ERROR={e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
