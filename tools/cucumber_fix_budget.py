"""
Plafond adaptatif des corrections après exécution (0 token) : on continue tant que la correction fait bouger
l'échec, on s'arrête scénario par scénario dès qu'il revient à l'identique.

- Un scénario est « bloqué » quand, après une correction, il échoue avec le même statut et le même message (valeurs
  variables masquées : dates, identifiants, nombres) qu'avant : une nouvelle tentative devinerait. Il n'est plus
  envoyé en correction ; il garde son statut et sa cause pour le prochain run importé (où la base et les logs
  peuvent trancher).
- Les autres (échec qui a changé, nouveau) repartent, jusqu'à CUCUMBER_VERIFY_FIX_MAX tentatives (4 par défaut :
  la file Jenkins n'exécute qu'un build à la fois). CUCUMBER_VERIFY_FIX_ATTEMPTS=0 désactive les corrections.
- Les causes prouvées hors test (environnement, déploiement) ne sont jamais renvoyées : elles ne sont pas dans
  les scénarios à corriger.
"""
import os
import re


def enabled() -> bool:
    return os.getenv("CUCUMBER_VERIFY_FIX_ATTEMPTS", "").strip() != "0"


def max_attempts() -> int:
    if not enabled():
        return 0
    try:
        return max(1, int(os.getenv("CUCUMBER_VERIFY_FIX_MAX", "").strip() or 4))
    except ValueError:
        return 4


def _volatile_free(message: str) -> str:
    """Message sans ses parties qui changent d'une exécution à l'autre (dates, heures, identifiants, nombres) ; les
    valeurs attendues et obtenues restent : un « obtenu » qui change est un progrès."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ][\d:.,]+Z?|\d{2}:\d{2}:\d{2}([.,]\d+)?", "<date>", message or "")
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{20,}\b", "<id>", text, flags=re.I)
    text = re.sub(r"(?<![\w<\"'])\d+(?![\w>\"'])", "<n>", text)
    return " ".join(text.split())[:300]


def signature(item: dict) -> tuple:
    return item.get("statut", ""), item.get("step", ""), _volatile_free(item.get("motif", ""))


def stuck_after(before: dict, after: dict) -> set:
    """
    Tags corrigés (`before` : {tag: résultat} envoyés en correction) encore à corriger après la revérification
    (`after`) avec le même échec : bloqués.
    """
    return {tag for tag, item in before.items() if tag in after and signature(after[tag]) == signature(item)}
