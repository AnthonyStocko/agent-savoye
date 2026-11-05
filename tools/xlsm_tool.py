"""
Fichiers de données Excel des tests Cucumber (.xlsm / .xlsx, binaires) : lecture et copie adaptée, pour l'agent.

Les classeurs du Standard (ex. TOW-846-WmsStockInitDraft.xlsm) portent ses codes de référence (site VDA, donneur
d'ordres SO-1…) ; un test du client a besoin de SA version, avec des codes vérifiés dans la base de l'environnement.
L'agent ne sait pas éditer un fichier binaire : cet outil le fait, sans toucher au classeur d'origine.

  python tools/xlsm_tool.py show <classeur> [--max 300]
      feuilles, puis cellules non vides (« Feuille!A1 = valeur »), tronquées à --max par feuille.
  python tools/xlsm_tool.py copy <source> <cible> [--replace ANCIEN=NOUVEAU …] [--contains] [--set "Feuille!B5=VALEUR" …]
      copie <source> vers <cible> en remplaçant les cellules dont la valeur vaut exactement ANCIEN (--contains :
      qui contiennent ANCIEN), puis les cellules désignées par --set ; macros conservées. La cible doit être sous un
      dossier de données de test (…/src/test/resources/cucumber/files/…), jamais la source elle-même.
"""
import argparse
import os
import sys

DATA_DIR = "src/test/resources/cucumber/files/"


def _load(path: str):
    import openpyxl
    return openpyxl.load_workbook(path, keep_vba=path.lower().endswith(".xlsm"))


def show(path: str, limit: int = 300) -> list:
    book = _load(path)
    lines = [f"Feuilles : {', '.join(book.sheetnames)}"]
    for sheet in book.worksheets:
        count = 0
        lines.append(f"--- {sheet.title} ({sheet.max_row} ligne(s), {sheet.max_column} colonne(s))")
        for row in sheet.iter_rows():
            for cell in row:
                if cell.value not in (None, ""):
                    lines.append(f"{sheet.title}!{cell.coordinate} = {cell.value}")
                    count += 1
                    if count >= limit:
                        lines.append(f"… (tronqué à {limit} cellules)")
                        break
            if count >= limit:
                break
    return lines


def copy(source: str, target: str, replacements: dict, contains: bool = False, cells: dict = None) -> int:
    normalized = os.path.abspath(target).replace("\\", "/")
    if DATA_DIR not in normalized:
        raise ValueError(f"cible hors d'un dossier de données de test ({DATA_DIR}) : {target}")
    if os.path.abspath(source) == os.path.abspath(target):
        raise ValueError("la cible doit être une copie, jamais le classeur d'origine")
    book, changed = _load(source), 0
    for sheet in book.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                value = cell.value
                if not isinstance(value, str):
                    if value is not None and str(value) in replacements and not contains:
                        cell.value, changed = replacements[str(value)], changed + 1
                    continue
                if not contains and value in replacements:
                    cell.value, changed = replacements[value], changed + 1
                elif contains:
                    new = value
                    for old, repl in replacements.items():
                        new = new.replace(old, repl)
                    if new != value:
                        cell.value, changed = new, changed + 1
    for ref, value in (cells or {}).items():
        sheet_name, _, coordinate = ref.rpartition("!")
        sheet = book[sheet_name] if sheet_name else book.active
        sheet[coordinate] = value
        changed += 1
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    book.save(target)
    return changed


def _pairs(items: list, sep: str) -> dict:
    out = {}
    for item in items or []:
        key, found, value = item.partition(sep)
        if not found or not key:
            raise ValueError(f"attendu CLÉ{sep}VALEUR : {item}")
        out[key] = value
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    shown = sub.add_parser("show")
    shown.add_argument("path")
    shown.add_argument("--max", type=int, default=300)
    copied = sub.add_parser("copy")
    copied.add_argument("source")
    copied.add_argument("target")
    copied.add_argument("--replace", action="append", default=[])
    copied.add_argument("--contains", action="store_true")
    copied.add_argument("--set", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            print("\n".join(show(args.path, args.max)))
        else:
            count = copy(args.source, args.target, _pairs(args.replace, "="), args.contains, _pairs(args.set, "="))
            print(f"{count} cellule(s) modifiée(s) ; classeur écrit : {args.target}")
    except Exception as e:  # noqa: BLE001 - message lisible pour l'agent
        print(f"XLSM_ERROR={type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
