"""
Compilation du worktree Cucumber d'un client, appelée par l'agent d'un lot pendant qu'il écrit (Bash autorisé sur
cette seule commande) : il voit ses erreurs et les corrige lui-même, avec tout le contexte du lot, au lieu de les
laisser à un appel de correction séparé.

    python tools/cucumber_compile.py <client>

Le contexte (worktree, clone du Cucumber Standard, commande Maven) est écrit par main.py dans
.tmp/cucumber/<client>/compilation.json avant les lots. Rien n'est exécuté : compilation seule (test-compile).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import cucumber_tools  # noqa: E402

CONTEXT_FILE = "compilation.json"


def context_path(client_name: str) -> str:
    return os.path.join(cucumber_tools.cache_dir(client_name), CONTEXT_FILE)


def write_context(client_name: str, worktree: str, cucumber_std_dir: str, compile_cmd: str) -> str:
    path = context_path(client_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"worktree": worktree, "cucumber_std_dir": cucumber_std_dir, "compile_cmd": compile_cmd}, f)
    return path


def main(argv: list) -> int:
    if len(argv) != 1:
        print("Usage : cucumber_compile.py <client>")
        return 2
    path = context_path(argv[0].strip().lower())
    try:
        with open(path, encoding="utf-8") as f:
            context = json.load(f)
    except (OSError, ValueError):
        print(f"COMPILATION NON VÉRIFIABLE : contexte absent ({path}). Poursuis sans compiler.")
        return 0
    try:
        build = cucumber_tools.build_dir(argv[0].strip().lower(), context["worktree"], context["cucumber_std_dir"])
    except RuntimeError as e:
        print(f"COMPILATION NON VÉRIFIABLE (environnement) : {e}. Poursuis sans compiler.")
        return 0
    check = cucumber_tools.verify_build(build, context["compile_cmd"])
    if check["ok"]:
        print("COMPILATION OK")
        return 0
    if check["environnement"]:
        print("COMPILATION NON VÉRIFIABLE (environnement, pas une erreur de code) : poursuis sans compiler.")
        print("\n".join(f"  {e}" for e in check["erreurs"][:5]))
        return 0
    print(f"COMPILATION KO : {len(check['erreurs'])} erreur(s) (chemins relatifs au worktree)")
    print("\n".join(f"- {e}" for e in check["erreurs"]))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
