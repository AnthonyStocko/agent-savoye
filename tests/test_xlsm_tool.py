import openpyxl
import pytest

from tools import xlsm_tool as xt


def _book(path):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Stock"
    sheet["A1"], sheet["B1"], sheet["C1"] = "site", "owner", "article"
    sheet["A2"], sheet["B2"], sheet["C2"] = "VDA", "SO-1", "ART-VDA-1"
    sheet["D2"] = 12
    book.save(path)


def test_show_and_copy_with_database_codes(tmp_path):
    source = tmp_path / "std" / "Stock.xlsx"
    source.parent.mkdir()
    _book(source)
    lines = xt.show(str(source))
    assert lines[0] == "Feuilles : Stock" and "Stock!A2 = VDA" in lines
    target = tmp_path / "src" / "test" / "resources" / "cucumber" / "files" / "canson" / "Stock.xlsx"
    assert xt.copy(str(source), str(target), {"VDA": "01", "SO-1": "CF01"}, cells={"Stock!D2": "5"}) == 3
    sheet = openpyxl.load_workbook(target)["Stock"]
    assert (sheet["A2"].value, sheet["B2"].value, sheet["C2"].value, sheet["D2"].value) == ("01", "CF01", "ART-VDA-1", "5")
    assert xt.copy(str(source), str(target), {"VDA": "01"}, contains=True) == 2
    assert openpyxl.load_workbook(target)["Stock"]["C2"].value == "ART-01-1"
    assert openpyxl.load_workbook(source)["Stock"]["A2"].value == "VDA"


def test_copy_refuses_targets_outside_data_files(tmp_path):
    source = tmp_path / "Stock.xlsx"
    _book(source)
    with pytest.raises(ValueError):
        xt.copy(str(source), str(tmp_path / "out.xlsx"), {})
    assert xt.main(["copy", str(source), str(tmp_path / "out.xlsx")]) == 1
