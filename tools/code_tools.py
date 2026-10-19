import os
from crewai.tools import tool

@tool("Chercher du code dans le répertoire source local")
def search_source_code(query: str) -> str:
    """
    Parcourt le dossier source local pour trouver des fichiers ou méthodes correspondant à la recherche.
    - query: mot-clé, nom de classe, entité, endpoint API ou package (ex: 'OrderController', 'WmsStockService').
    """
    src_dir = os.getenv("SRC_DIRECTORY_PATH", "")
    
    if not os.path.exists(src_dir):
        return f"Erreur : Le répertoire source {src_dir} n'existe pas."

    matches = []
    # Parcourt les fichiers .java, .xml, .properties, etc.
    for root, _, files in os.walk(src_dir):
        for file in files:
            if file.endswith(('.java', '.xml', '.yml', '.properties')):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        if query.lower() in content.lower() or query.lower() in file.lower():
                            relative_path = os.path.relpath(file_path, src_dir)
                            # Extrait les 10 premières lignes contenant la correspondance
                            snippet = "\n".join([line for line in content.splitlines() if query.lower() in line.lower()][:5])
                            matches.append(f"Fichier: {relative_path}\nExtrait:\n{snippet}\n---")
                except Exception:
                    continue

    if not matches:
        return f"Aucun résultat trouvé dans {src_dir} pour la recherche : '{query}'."
    
    return "\n".join(matches[:5]) # Limite à 5 fichiers pour ne pas surcharger le contexte LLM
    
@tool("Lire le fichier de conventions de développement")
def read_dev_conventions() -> str:
    """Lit et retourne les règles et conventions de développement spécifiques au projet."""
    file_path = os.getenv("DEV_CONVENTIONS_PATH", "").strip() or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "odatio-specific-code-conventions-dev.md"
    )

    if not os.path.exists(file_path):
        return (
            f"Avertissement : Le fichier de conventions {file_path} n'a pas été"
            " trouvé."
        )

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Erreur lors de la lecture du fichier de conventions : {str(e)}"