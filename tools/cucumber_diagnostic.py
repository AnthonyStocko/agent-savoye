"""
Diagnostic des échecs que le message ne tranche pas (point 7, 0 token côté Python).

- Diagnostic temporaire (D7) : pendant une correction, l'agent peut ajouter un step du projet dont l'expression
  commence par « TEMPORAIRE- » et qui AFFICHE l'état réel (scenario.log, aucune assertion). Le scénario est exécuté
  une fois avec ce step ; sa sortie, lue dans le rapport Cucumber, est rendue à la correction suivante. Python
  retire ensuite le step des features et sa méthode Java. Contrôle bloquant : aucun step TEMPORAIRE dans le patch.
- Nature « déploiement » (D8) : jamais déduite du seul message d'échec. Elle n'est retenue que sur un constat de
  l'agent (« constats » du JSON de correction) dont l'extrait cité est présent mot pour mot dans le journal de
  l'exécution du scénario (message d'échec, sorties des steps).
"""
import json
import os
import re

from tools.cucumber_tools import _git, _mask_java, _matching, _read_text_file, _write_text
from tools.cucumber_verify import _STEP_LINE

TEMP_PREFIX = "TEMPORAIRE-"
DEPLOYMENT = "déploiement en retard sur le dépôt"
# Extrait de preuve trop court : il se trouverait n'importe où
MIN_PROOF_CHARS = 15
_TEMP_ANNOTATION = re.compile(r"@(?:Given|When|Then|And|But|Soit|Quand|Alors|Et|Mais)\s*\(\s*(?:value\s*=\s*)?\""
                              + re.escape(TEMP_PREFIX))


def _is_temporary_step(line: str) -> bool:
    step = _STEP_LINE.match(line)
    return bool(step) and step.group(2).startswith(TEMP_PREFIX)


def _test_files(worktree: str) -> list:
    """Features et classes Java suivies ou nouvelles du worktree."""
    out = _git(worktree, "ls-files", "-co", "--exclude-standard", "--", "*.feature", "*.java").stdout.splitlines()
    return [p for p in out if os.path.exists(os.path.join(worktree, *p.split("/")))]


def temporary_steps(worktree: str) -> list:
    """Steps TEMPORAIRE encore présents (« fichier:ligne ») : lignes de features et définitions Java."""
    found = []
    for rel in _test_files(worktree):
        text = _read_text_file(os.path.join(worktree, *rel.split("/")))
        if TEMP_PREFIX not in text:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if (rel.endswith(".feature") and _is_temporary_step(line)) or (
                    rel.endswith(".java") and _TEMP_ANNOTATION.search(line)):
                found.append(f"{rel}:{number}")
    return found


def _strip_feature(text: str) -> str:
    """Feature sans ses steps TEMPORAIRE (avec leur table ou docstring)."""
    out, skipping, docstring = [], False, False
    for line in text.splitlines():
        stripped = line.strip()
        if skipping and (docstring or stripped.startswith(('"""', "|"))):
            if stripped.startswith('"""'):
                docstring = not docstring
            continue
        skipping = _is_temporary_step(line)
        if not skipping:
            out.append(line)
    return "\n".join(out) + "\n"


def _strip_java(text: str) -> str:
    """Classe sans ses méthodes de step TEMPORAIRE (annotations comprises)."""
    masked = _mask_java(text)
    for match in reversed(list(_TEMP_ANNOTATION.finditer(text))):
        brace = masked.find("{", match.end())
        if brace < 0:
            continue
        start = text.rfind("\n", 0, match.start()) + 1
        end = _matching(masked, brace)
        end = end + 1 if text[end:end + 1] == "\n" else end
        text, masked = text[:start] + text[end:], masked[:start] + masked[end:]
    return text


def remove_temporary(worktree: str) -> list:
    """Retire les steps TEMPORAIRE des features et leurs méthodes Java. Renvoie les fichiers modifiés."""
    changed = []
    for rel in _test_files(worktree):
        path = os.path.join(worktree, *rel.split("/"))
        text = _read_text_file(path)
        if TEMP_PREFIX not in text:
            continue
        stripped = _strip_feature(text) if rel.endswith(".feature") else _strip_java(text)
        if stripped != text:
            _write_text(path, stripped)
            changed.append(rel)
    return changed


def diagnostic_lines(item: dict, max_chars: int = 600) -> list:
    """Sorties des steps (diagnostic temporaire compris) d'un scénario, rendues à la correction suivante."""
    outputs = " | ".join(item.get("sorties") or [])
    return [f"  sortie du diagnostic : {' '.join(outputs.split())[:max_chars]}"] if outputs else []


def parse_fix_result(text: str) -> dict:
    """Bloc JSON final d'une correction ({corrigees, restantes, realignes, constats}), {} s'il est absent."""
    for candidate in re.findall(r"```(?:json)?\s*(.*?)```", text or "", flags=re.S)[::-1] + [text or ""]:
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            try:
                data = json.loads(candidate[start:end + 1])
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return {}


def _normalize(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def deployment_findings(fix: dict, scenarios: dict) -> tuple:
    """
    Constats « deploiement » d'une correction appliqués aux scénarios ({tag: résultat}) : retenus seulement si
    l'extrait cité figure dans le journal de la dernière exécution du scénario. Renvoie ({tag: mise à jour du
    résultat}, [constats refusés, avec la raison]).
    """
    accepted, refused = {}, []
    for constat in fix.get("constats") or []:
        if not isinstance(constat, dict) or str(constat.get("type", "")).strip().lower() != "deploiement":
            continue
        tag, proof = str(constat.get("tag", "")).strip(), str(constat.get("preuve", "")).strip()
        item = scenarios.get(tag)
        if not item:
            refused.append(f"{tag or '(sans tag)'} : constat de déploiement sur un scénario inconnu du lot")
        elif len(_normalize(proof)) < MIN_PROOF_CHARS:
            refused.append(f"{tag} : constat de déploiement sans extrait de preuve exploitable")
        elif _normalize(proof) not in _normalize(item.get("journal", "")):
            refused.append(f"{tag} : extrait « {proof[:120]} » absent du journal de l'exécution — constat de "
                           f"déploiement refusé")
        else:
            detail = " ".join(str(constat.get("detail", "")).split())[:200]
            accepted[tag] = {"statut": "deploiement", "preuve_verifiee": True,
                             "motif": f"{DEPLOYMENT} : {detail} — extrait vérifié dans le log : « {proof[:200]} »"}
    return accepted, refused
