import json

import pymupdf

from src.cli import main
from src.dedupe_batch import dedupe_batch, read_original_serials, split_suspect_shipments
from src.extract_labels import extract_labels
from src.load_sources import load_shipments, write_csv
from tests.conftest import SHIP_FIELDS, make_pdf


def make_shipment_csv(path, numbers):
    rows = [{"NO": index + 1, "发货号码": number, "照片": "", "配送服务": "GUOO Economy Small",
             "商品": f"Product {index}", "货号": f"SKU-{index}", "数量": "1", "标签": number.split("-")[0][-4:]}
            for index, number in enumerate(numbers)]
    write_csv(path, rows, SHIP_FIELDS)


def make_shipment_pdf_with_serials(path, rows):
    """按 load_shipments_pdf 的固定列坐标造一份最小发货单 PDF，另加最左列打印序号。"""
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=842)
        page.insert_text((80, 80), "GUOO Economy Small")
        y = 140
        for serial, number, sku in rows:
            page.insert_text((31, y), serial)
            page.insert_text((45, y), number)
            page.insert_text((402, y), sku)
            page.insert_text((488, y), "1")
            y += 51
        document.save(path)


def test_read_original_serials_matches_by_row_position(tmp_path):
    path = tmp_path / "shipment_with_serials.pdf"
    make_shipment_pdf_with_serials(path, [
        ("14", "0145918409-0060-1", "LADA20-01-A"),
        ("16", "0128902775-0190-1", "LADA20-01-A"),
    ])
    serials = read_original_serials(path)
    assert serials == {"0145918409-0060-1": "14", "0128902775-0190-1": "16"}


def test_read_original_serials_empty_for_csv(tmp_path):
    path = tmp_path / "shipment.csv"
    make_shipment_csv(path, ["0184236205-0036-1"])
    assert read_original_serials(path) == {}


def test_split_suspect_shipments_flags_tail_mismatch():
    clean_row = {"发货号码": "0145918409-0060-1", "标签": "8409", "货号": "LADA20-01-A", "数量": 1,
                 "商品": "ok", "配送服务": "GUOO Economy Small"}
    corrupted_row = {"发货号码": "0145918409-0060-1", "标签": "84093611", "货号": "LADA20-01-A20260802012",
                      "数量": "11", "商品": "merged text", "配送服务": "GUOO Economy Small"}
    clean, suspect = split_suspect_shipments([clean_row, corrupted_row])
    assert clean == [clean_row]
    assert suspect == [corrupted_row]


def test_dedupe_removes_pages_and_rows_already_in_old_batch(tmp_path):
    old_numbers = ["0184236205-0036-1", "41031190195822918-0034-2"]
    new_numbers = old_numbers + ["9999999999-0099-1"]
    make_pdf(tmp_path / "old_labels.pdf", old_numbers)
    make_pdf(tmp_path / "new_labels.pdf", new_numbers)
    make_shipment_csv(tmp_path / "old_shipment.csv", old_numbers)
    make_shipment_csv(tmp_path / "new_shipment.csv", new_numbers)

    output = tmp_path / "out"
    report = dedupe_batch(tmp_path / "old_labels.pdf", tmp_path / "old_shipment.csv",
                           tmp_path / "new_labels.pdf", tmp_path / "new_shipment.csv", output)

    assert report["旧标签页数"] == 2
    assert report["新标签页数"] == 3
    assert report["新增标签页数"] == 1
    assert report["重复标签页数"] == 2
    assert report["新增发货单行数"] == 1
    assert report["重复发货单行数"] == 2
    assert report["剔除的标签号码"] == sorted(old_numbers)
    assert report["剔除的发货号码"] == sorted(old_numbers)
    assert report["新增标签有号码但发货单无对应记录"] == []
    assert report["新增发货单有号码但标签无对应页"] == []
    assert report["新增标签中解析异常页（号码格式不标准，需人工核对原PDF）"] == []

    kept_labels = [row["完整发货号码"] for row in extract_labels(output / "新增标签.pdf")]
    assert kept_labels == ["9999999999-0099-1"]
    _, shipment_rows = load_shipments(output / "新增发货单.csv")
    assert [row["发货号码"] for row in shipment_rows] == ["9999999999-0099-1"]
    # 发货单来源是 CSV，没有可对照的打印序号列。
    assert shipment_rows[0]["原发货单序号"] == ""


def test_dedupe_cli_writes_report(tmp_path):
    old_numbers = ["0184236205-0036-1"]
    new_numbers = old_numbers + ["1234567890-0001-1"]
    make_pdf(tmp_path / "old_labels.pdf", old_numbers)
    make_pdf(tmp_path / "new_labels.pdf", new_numbers)
    make_shipment_csv(tmp_path / "old_shipment.csv", old_numbers)
    make_shipment_csv(tmp_path / "new_shipment.csv", new_numbers)

    output = tmp_path / "dedupe_out"
    code = main([
        "dedupe",
        "--old-labels", str(tmp_path / "old_labels.pdf"),
        "--old-shipment", str(tmp_path / "old_shipment.csv"),
        "--new-labels", str(tmp_path / "new_labels.pdf"),
        "--new-shipment", str(tmp_path / "new_shipment.csv"),
        "--output", str(output),
    ])
    assert code == 0
    assert (output / "新增标签.pdf").is_file()
    assert (output / "新增发货单.csv").is_file()
    report = json.loads((output / "去重报告.json").read_text(encoding="utf-8"))
    assert report["新增标签页数"] == 1
    assert report["新增发货单行数"] == 1
