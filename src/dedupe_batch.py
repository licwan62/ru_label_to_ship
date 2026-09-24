"""比较新旧批次的原始标签 PDF 与发货单，剔除新批次中已在旧批次出现过的记录。

用于同一批标签被重复导出的情况：新文件在旧文件基础上追加了新订单，
但仍包含旧批次已发货的全部原页。按“完整发货号码”/“发货号码”做精确去重，
输出只含新增记录的标签 PDF 和发货单 CSV/PDF，交由 audit/build 正常走完整流程。
"""

import re
from pathlib import Path

import pymupdf
from pypdf import PdfReader, PdfWriter

from .extract_labels import extract_labels, tail_four
from .load_sources import load_shipments, write_csv

SHIPMENT_OUTPUT_FIELDS = ["NO", "原发货单序号", "发货号码", "照片", "商品", "货号", "数量", "标签", "配送服务"]

# 发货单 PDF 最左列打印的序号，与货号/数量列一样是版式固定的位置，不依赖发货号码正则，
# 因此在发货号码格式异常导致该行未被识别时仍能提供“这是原表第几行”的线索。
SERIAL_COLUMN_X_MAX = 40
SERIAL_RE = re.compile(r"\d{1,4}")
NUMBER_RE = re.compile(r"\d+-\d{4}-\d+")
ROW_Y_TOLERANCE = 6


def dedupe_labels(old_labels_path: Path, new_labels_path: Path):
    """按完整发货号码精确去重；返回 (旧标签行, 新标签行, 保留行, 剔除行)。"""
    old_rows = extract_labels(old_labels_path)
    new_rows = extract_labels(new_labels_path)
    old_numbers = {row["完整发货号码"] for row in old_rows if row["完整发货号码"]}
    kept, removed = [], []
    for row in new_rows:
        (removed if row["完整发货号码"] in old_numbers else kept).append(row)
    return old_rows, new_rows, kept, removed


def write_label_subset(source: Path, target: Path, kept_rows):
    """按保留行的原页码从新标签 PDF 直接复制页面，不重绘、不缩放、不改变顺序。"""
    reader = PdfReader(source)
    writer = PdfWriter()
    for row in kept_rows:
        writer.add_page(reader.pages[row["原标签页码"] - 1])
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as stream:
        writer.write(stream)
    writer.close()


def dedupe_shipment(old_shipment_path: Path, new_shipment_path: Path):
    """按发货号码精确去重；返回 (旧发货单行, 新发货单行, 保留行, 剔除行)。"""
    _, old_rows = load_shipments(old_shipment_path)
    _, new_rows = load_shipments(new_shipment_path)
    old_numbers = {row["发货号码"] for row in old_rows if row["发货号码"]}
    kept, removed = [], []
    for row in new_rows:
        (removed if row["发货号码"] in old_numbers else kept).append(row)
    return old_rows, new_rows, kept, removed


def split_suspect_shipments(rows):
    """按“标签”须等于发货号码第一段后四位这一业务约定筛出可疑行。

    PDF 版发货单按坐标切列：当同页某一行的发货号码因格式异常未被识别时，
    相邻行的单元格范围会据此计算错位，把两行的货号/数量/标签文字拼到一行里
    （标签变成两串数字相连）。用这条业务不变量拦截，避免把拼接后的脏数据
    当成正常记录送入 audit/build；命中的行需要人工核对原发货单 PDF。
    """
    clean, suspect = [], []
    for row in rows:
        expected = tail_four(row["发货号码"])
        label = str(row.get("标签", "")).strip()
        (suspect if expected and label != expected else clean).append(row)
    return clean, suspect


def read_original_serials(shipment_pdf_path: Path):
    """返回 {发货号码: 原发货单PDF打印的序号}；仅对 PDF 发货单有效，按同页最近行高匹配。"""
    if shipment_pdf_path.suffix.lower() != ".pdf":
        return {}
    serial_map = {}
    with pymupdf.open(shipment_pdf_path) as document:
        for page in document:
            words = page.get_text("words", sort=True)
            serials = [(w[1], w[4]) for w in words if w[0] < SERIAL_COLUMN_X_MAX and SERIAL_RE.fullmatch(w[4])]
            numbers = [(w[1], w[4]) for w in words if NUMBER_RE.fullmatch(w[4])]
            for y, number in numbers:
                if not serials:
                    continue
                nearest_y, nearest_serial = min(serials, key=lambda item: abs(item[0] - y))
                if abs(nearest_y - y) <= ROW_Y_TOLERANCE:
                    serial_map[number] = nearest_serial
    return serial_map


def dedupe_batch(old_labels_path: Path, old_shipment_path: Path, new_labels_path: Path, new_shipment_path: Path, output_dir: Path):
    """去重并落盘：<output_dir>/新增标签.pdf、新增发货单.csv；返回统计报告字典。"""
    old_label_rows, new_label_rows, kept_labels, removed_labels = dedupe_labels(old_labels_path, new_labels_path)
    old_shipment_rows, new_shipment_rows, kept_shipments, removed_shipments = dedupe_shipment(old_shipment_path, new_shipment_path)
    clean_shipments, suspect_shipments = split_suspect_shipments(kept_shipments)
    original_serials = read_original_serials(new_shipment_path)

    output_dir = output_dir.resolve()
    label_path = output_dir / "新增标签.pdf"
    write_label_subset(new_labels_path, label_path, kept_labels)

    shipment_rows = [
        {"NO": index, "原发货单序号": original_serials.get(row["发货号码"], ""), "发货号码": row["发货号码"],
         "照片": row.get("照片", ""), "商品": row["商品"], "货号": row["货号"], "数量": row["数量"],
         "标签": row.get("标签", ""), "配送服务": row["配送服务"]}
        for index, row in enumerate(clean_shipments, 1)
    ]
    shipment_csv_path = output_dir / "新增发货单.csv"
    write_csv(shipment_csv_path, shipment_rows, SHIPMENT_OUTPUT_FIELDS)

    kept_label_numbers = {row["完整发货号码"] for row in kept_labels}
    kept_shipment_numbers = {row["发货号码"] for row in clean_shipments}
    label_parse_issues = [{"原标签页码": row["原标签页码"], "解析状态": row["解析状态"]}
                           for row in kept_labels if row["解析状态"] != "通过"]
    suspect_report = [
        {"原发货单序号": original_serials.get(row["发货号码"], ""), "发货号码": row["发货号码"],
         "货号（可能已拼接下一行）": row["货号"], "标签": row.get("标签", ""),
         "数量": row["数量"], "商品（可能已拼接下一行）": row["商品"], "配送服务": row["配送服务"]}
        for row in suspect_shipments
    ]
    report = {
        "旧标签页数": len(old_label_rows), "新标签页数": len(new_label_rows),
        "新增标签页数": len(kept_labels), "重复标签页数": len(removed_labels),
        "旧发货单行数": len(old_shipment_rows), "新发货单行数": len(new_shipment_rows),
        "新增发货单行数": len(clean_shipments), "重复发货单行数": len(removed_shipments),
        "剔除的标签号码": sorted({row["完整发货号码"] for row in removed_labels if row["完整发货号码"]}),
        "剔除的发货号码": sorted({row["发货号码"] for row in removed_shipments if row["发货号码"]}),
        "新增标签有号码但发货单无对应记录": sorted(kept_label_numbers - kept_shipment_numbers),
        "新增发货单有号码但标签无对应页": sorted(kept_shipment_numbers - kept_label_numbers),
        "新增标签中解析异常页（号码格式不标准，需人工核对原PDF）": label_parse_issues,
        "新增发货单中疑似列错位的行（标签与发货号码尾四位不符，已从输出中剔除，需人工核对原PDF）": suspect_report,
        "新增标签PDF": str(label_path), "新增发货单CSV": str(shipment_csv_path),
    }
    return report
