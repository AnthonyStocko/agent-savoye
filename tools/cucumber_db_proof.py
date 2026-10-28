"""
Preuve en base des corrections d'échecs liés aux données (0 token). Un échec « donnée introuvable / liste vide /
état inattendu » se tranche par une lecture de la base de test (CUCUMBER_DB_*) : donnée absente (à créer dans le
Background) ou test mal écrit (filtre, code, colonne). Quand l'accès base est accordé :
1. data_failure : l'échec relève des données (message du run) ;
2. prompt_block : consigne ajoutée à la correction — vérifier en base AVANT de corriger, citer la preuve ;
3. check : une correction d'un tel scénario sans preuve (entrée « preuves_base » du JSON final, dont les tables ont
   réellement été lues par une requête réussie de cet appel, journal AGENT_DB_LOG) est refusée — ses fichiers sont
   rétablis et le scénario reste à corriger.
Sans accès base, rien n'est refusé : la cause reste « NON PROUVÉE » et la correction est signalée comme telle.
CUCUMBER_DB_PROOF=0 désactive l'exigence.
"""
import os
import re

_DATA_FAILURE = re.compile(
    r"introuvable|n'existe pas|not found|non trouvé|empty List|from an empty|out of bounds for length 0|"
    r"Pas d'élément correspondant|aucune? .{0,40}en contexte|is not .{0,40} or already|"
    r"expected:<\d+> but was:<0>|NoSuchElementException", re.I)


def enabled() -> bool:
    return os.getenv("CUCUMBER_DB_PROOF", "1").strip() != "0"


def data_failure(message: str) -> bool:
    """Échec qui relève de l'état des données de l'environnement (à trancher par une lecture en base)."""
    return bool(_DATA_FAILURE.search(message or ""))


def data_scenarios(corrections: list) -> list:
    """Scénarios d'un lot de correction dont l'échec relève des données."""
    return [s for s in corrections or [] if data_failure(s.get("message", ""))]


def prompt_block(scenarios: list, db_available: bool) -> str:
    """Consigne de la correction pour les échecs de données (vide s'il n'y en a pas)."""
    if not scenarios or not enabled():
        return ""
    tags = ", ".join(s.get("tag") or s.get("id", "?") for s in scenarios)
    if not db_available:
        return ("\n=== ÉCHECS LIÉS AUX DONNÉES (" + tags + ") ===\nAucun accès base : tu ne peux pas prouver si la "
                "donnée existe. Une correction de ces scénarios fondée sur une supposition s'écrit, dans "
                "« restantes », « NON PROUVÉE : <cause> — vérification : <requête attendue> ».\n")
    return ("\n=== ÉCHECS LIÉS AUX DONNÉES : PREUVE EN BASE OBLIGATOIRE (" + tags + ") ===\n"
            "Ces échecs (donnée introuvable, liste vide, état inattendu) se tranchent en base AVANT toute correction : "
            "lis par une requête (accès base ci-dessus) la donnée que le step cherche, avec le filtre du step "
            "(code, statut, site, donneur d'ordres). Résultat vide : la donnée n'existe pas → crée-la dans le "
            "Background par un step existant. Résultat présent : le test est en cause (filtre, colonne, code, attente "
            "asynchrone) → corrige-le. Pour CHAQUE scénario de cette liste que tu modifies, ajoute au JSON final "
            "« preuves_base » : [{\"tag\": \"@agent_…\", \"tables\": [\"<tables lues>\"], \"requete\": \"<requête, "
            "sans valeur nominative>\", \"resultat\": \"<ce que la base a répondu : nombre de lignes, statut lu>\", "
            "\"conclusion\": \"donnée absente|donnée présente\"}]. Une modification d'un de ces scénarios sans preuve "
            "(ou dont les tables n'ont pas été lues par une requête réussie) est annulée automatiquement.\n")


def _table(name: str) -> str:
    return str(name or "").strip().strip('"[]`').rsplit(".", 1)[-1].lower()


def check(scenarios: list, fixed: dict, db_entries: list, changed_files: set) -> dict:
    """
    Corrections d'échecs de données sans preuve en base. `fixed` : JSON final de l'agent ; `db_entries` : commandes
    base de CET appel ; `changed_files` : fichiers modifiés par l'appel. Renvoie {"refuses": [scénarios],
    "fichiers": [fichiers à rétablir], "preuves": {tag: preuve acceptée}}.
    """
    read = {_table(t) for e in db_entries or [] if e.get("ok") and e.get("commande") == "query"
            for t in e.get("tables") or []}
    proofs = {}
    for proof in fixed.get("preuves_base") or []:
        if not isinstance(proof, dict) or not proof.get("tag"):
            continue
        tables = {_table(t) for t in proof.get("tables") or []}
        if tables and tables <= read and str(proof.get("resultat", "")).strip():
            proofs[str(proof["tag"]).strip()] = proof
    refused, files = [], []
    for s in scenarios:
        if s.get("fichier") in changed_files and (s.get("tag") or "") not in proofs:
            refused.append(s)
            files.append(s["fichier"])
    return {"refuses": refused, "fichiers": sorted(set(files)), "preuves": proofs}
