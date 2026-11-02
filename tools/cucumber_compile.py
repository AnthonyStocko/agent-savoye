"""
Compilation du worktree Cucumber d'un client, appelée par l'agent d'un lot pendant qu'il écrit (Bash autorisé sur
cette seule commande) : il voit ses erreurs et les corrige lui-même, avec tout le contexte du lot, au lieu de les
laisser à un appel de correction séparé.

    python tools/cucumber_compile.py <client>            compile (test-compile) et vérifie les appels à la bibliothèque
    python tools/cucumber_compile.py <client> --api <Classe>   méthodes de la classe dans la version du pom du client

Le contexte (worktree, clone du Cucumber Standard, commande Maven) est écrit par main.py dans
.tmp/cucumber/<client>/compilation.json avant les lots. Rien n'est exécuté : compilation seule (test-compile). La
bibliothèque de référence est celle du pom.xml du dépôt client (pas celle du Standard ni d'un autre client).
Compilation impossible (Maven, dépendances, contexte absent) : ARRÊT, code 3 — jamais « poursuis sans compiler ».
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import cucumber_library, cucumber_tools  # noqa: E402

CONTEXT_FILE = "compilation.json"
STOPPED = 3


def context_path(client_name: str) -> str:
    return os.path.join(cucumber_tools.cache_dir(client_name), CONTEXT_FILE)


def write_context(client_name: str, worktree: str, cucumber_std_dir: str, compile_cmd: str) -> str:
    path = context_path(client_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"worktree": worktree, "cucumber_std_dir": cucumber_std_dir, "compile_cmd": compile_cmd}, f)
    return path


def api_errors(worktree: str) -> tuple:
    """(version du pom, appels du code Java du worktree absents de la bibliothèque de cette version)."""
    version = cucumber_library.repo_library_version(worktree)
    api = cucumber_library.load_api(version) if version else {}
    errors = []
    for path in glob.glob(os.path.join(worktree, "src", "test", "java", "**", "*.java"), recursive=True):
        rel = os.path.relpath(path, worktree).replace("\\", "/")
        with open(path, encoding="utf-8", errors="replace") as f:
            errors += cucumber_library.check_java(f.read(), api, rel)
    return version, errors


def main(argv: list) -> int:
    if len(argv) not in (1, 3) or (len(argv) == 3 and argv[1] != "--api"):
        print("Usage : cucumber_compile.py <client> [--api <Classe>]")
        return 2
    client = argv[0].strip().lower()
    path = context_path(client)
    try:
        with open(path, encoding="utf-8") as f:
            context = json.load(f)
    except (OSError, ValueError):
        print(f"ARRÊT — COMPILATION IMPOSSIBLE : contexte absent ({path}). Ne livre rien : signale-le dans ta réponse.")
        return STOPPED
    if len(argv) == 3:
        version = cucumber_library.repo_library_version(context["worktree"])
        api = cucumber_library.load_api(version) if version else {}
        if not api:
            print(f"API NON DISPONIBLE : bibliothèque {version or '(pas de version dans le pom)'} absente du dépôt "
                  f"Maven local ; compile d'abord (sans --api) pour la télécharger.")
            return STOPPED
        print(cucumber_library.describe(api, argv[2].strip()))
        return 0
    try:
        build = cucumber_tools.build_dir(client, context["worktree"], context["cucumber_std_dir"])
    except RuntimeError as e:
        print(f"ARRÊT — COMPILATION IMPOSSIBLE (environnement) : {e}. Ne livre rien : signale-le dans ta réponse.")
        return STOPPED
    check = cucumber_tools.verify_build(build, context["compile_cmd"] or cucumber_tools.DEFAULT_COMPILE_CMD)
    version, wrong_calls = api_errors(context["worktree"])
    if check["environnement"]:
        print("ARRÊT — COMPILATION IMPOSSIBLE (Maven indisponible ou dépendances introuvables, pas une erreur de "
              "code) : ne relance pas, ne livre rien, signale-le dans ta réponse.")
        print("\n".join(f"  {e}" for e in check["erreurs"][:5]))
        return STOPPED
    if check["ok"] and not wrong_calls:
        print(f"COMPILATION OK (bibliothèque {version or '?'} du pom du client)")
        return 0
    errors = list(dict.fromkeys(wrong_calls + check["erreurs"]))
    print(f"COMPILATION KO : {len(errors)} erreur(s) (chemins relatifs au worktree ; bibliothèque {version or '?'} du "
          f"pom du client)")
    print("\n".join(f"- {e}" for e in errors))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
