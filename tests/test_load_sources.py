import openpyxl
import pytest

from src.load_sources import load_products


HEADERS = ["序号", "来源工作表", "货号", "发货尺码", "材质", "无尺码备注（车型/尺寸）"]


def write_workbook(path, sheets):
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        sheet.append(HEADERS)
        for row in rows:
            sheet.append(row)
    workbook.save(path)


def test_load_products_xlsx_prefers_store_one_sheet_over_summary(tmp_path):
    """汇总表可能落后于分店明细未同步新货号，因此“一店”优先。"""
    path = tmp_path / "信息表.xlsx"
    write_workbook(path, {
        "ozon跨境二店": [[1, "测试", "SKU-STORE2-ONLY", "M", "PEVA", ""]],
        "ozon跨境一店": [
            [1, "测试", "SKU-A", "M", "PEVA", ""],
            [2, "测试", "0017573", "XXL", "单层PEVA", ""],
        ],
        "Ozon上架链接汇总": [[1, "测试", "SKU-A", "旧尺码", "旧材质", ""]],
    })
    rows = load_products(path)
    assert [row["货号"] for row in rows] == ["SKU-A", "0017573"]
    assert rows[0]["发货尺码"] == "M"  # 取一店的值，不是落后的汇总值
    assert rows[1]["货号"] == "0017573"  # 前导零必须保留为文本


def test_load_products_xlsx_falls_back_to_summary_sheet(tmp_path):
    path = tmp_path / "信息表.xlsx"
    write_workbook(path, {
        "店铺明细": [[1, "店铺A", "SKU-OTHER", "M", "PEVA", ""]],
        "Ozon上架链接汇总": [
            [1, "测试", "SKU-A", "M", "PEVA", ""],
            [2, "测试", "0017573", "XXL", "单层PEVA", ""],
        ],
    })
    rows = load_products(path)
    assert [row["货号"] for row in rows] == ["SKU-A", "0017573"]


def test_load_products_xlsx_rejects_ambiguous_store_one_sheet(tmp_path):
    path = tmp_path / "信息表.xlsx"
    write_workbook(path, {
        "一店旧表": [[1, "测试", "SKU-A", "M", "PEVA", ""]],
        "一店新表": [[1, "测试", "SKU-B", "M", "PEVA", ""]],
    })
    with pytest.raises(ValueError, match="唯一的.一店.或.汇总.表"):
        load_products(path)


def test_load_products_xlsx_rejects_numeric_sku(tmp_path):
    path = tmp_path / "信息表.xlsx"
    write_workbook(path, {"汇总": [[1, "测试", 17573, "M", "PEVA", ""]]})
    with pytest.raises(ValueError, match="被 Excel 存成数字"):
        load_products(path)
