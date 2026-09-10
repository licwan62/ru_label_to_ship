"""复制原 PDF 页面，写核对清单，并重新打开输出做独立验收。"""

import re
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf
from pypdf import PdfReader, PdfWriter

from .extract_labels import shipment_numbers
from .load_sources import read_csv, write_csv
from .schema import CHECK_FIELDS, MATCH_FIELDS, PASS_STATUSES, SKIP_STATUS


def safe_component(value: str):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().rstrip(". ")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "_"
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", cleaned, re.IGNORECASE):
        cleaned = "_" + cleaned
    # 留出文件前后缀余量，清洗碰撞在分组阶段显式拒绝。
    return cleaned[:60].rstrip(". ")


def group_records(matches):
    groups = defaultdict(list)
    for row in matches:
        if row["匹配状态"] == SKIP_STATUS:
            continue
        if row["匹配状态"] not in PASS_STATUSES:
            raise ValueError("存在未通过记录，禁止生成部分发货包")
        if not row["标准材质"] or not row["标准发货尺码"]:
            raise ValueError("分组值不能为空")
        groups[(row["标准材质"], row["标准发货尺码"])].append(row)
    paths = {}
    material_paths = {}
    reserved = {"待确认"}
    for material, size in sorted(groups):
        directory = safe_component(material)
        if directory.casefold() in reserved:
            raise ValueError(f"材质目录与系统目录冲突：{material}")
        previous = material_paths.setdefault(directory.casefold(), material)
        if previous != material:
            raise ValueError(f"材质目录清洗后重名：{previous} / {material}")
        rows = sorted(groups[(material, size)], key=lambda row: int(row["原标签页码"]))
        path = f"{directory}/发{safe_component(size)}码_{directory}_{len(rows)}张.pdf"
        # 同材质尺码即使页数不同也不能依赖页数来消除碰撞。
        identity = (directory.casefold(), safe_component(size).casefold())
        if identity in paths:
            raise ValueError(f"尺码文件名清洗后重名：{material} / {size}")
        paths[identity] = (path, rows)
    return [value for _, value in sorted(paths.items())]


def page_geometry(page):
    return (tuple(page.mediabox), tuple(page.cropbox), tuple(page.trimbox),
            tuple(page.bleedbox), tuple(page.artbox), page.rotation, page.get("/UserUnit", 1))


def verify_package(source: Path, output: Path, matches, audit_path: Path | None = None):
    """对落盘 PDF 和 CSV 重新读取，按实际页序核对每个号码、分组及图像。"""
    _, checklist = read_csv(output / "逐单核对清单.csv", CHECK_FIELDS)
    shipping = [row for row in matches if row["匹配状态"] in PASS_STATUSES]
    expected = {row["完整发货号码"]: row for row in shipping}
    if len({row["完整发货号码"] for row in matches}) != len(matches) or not matches:
        raise ValueError("验收失败：匹配表号码重复或为空")
    if len(checklist) != len(shipping):
        raise ValueError("验收失败：核对清单/匹配表行数不符")
    skips = [row for row in matches if row["匹配状态"] == SKIP_STATUS]
    if audit_path is not None:
        for filename, expected_rows in (("完整匹配表.csv", matches), ("跳过清单.csv", skips)):
            _, saved = read_csv(audit_path / filename, MATCH_FIELDS)
            if saved != [{field: str(row[field]) for field in MATCH_FIELDS} for row in expected_rows]:
                raise ValueError(f"验收失败：{filename}与审计结果不一致")
    listed = defaultdict(list)
    for row in checklist:
        listed[row["分组文件"]].append(row)
    actual_files = {p.relative_to(output).as_posix() for p in output.rglob("*.pdf")}
    planned = {path: rows for path, rows in group_records(matches)}
    if actual_files != set(listed) or actual_files != set(planned):
        raise ValueError("验收失败：PDF 文件集合不符")
    seen = []
    source_reader = PdfReader(source)
    with pymupdf.open(source) as source_document:
        if len(source_reader.pages) != len(matches):
            raise ValueError("验收失败：原 PDF 页数不符")
        for relative in sorted(actual_files):
            pdf = PdfReader(output / relative)
            rows = listed[relative]
            if len(pdf.pages) != len(rows) or len(rows) != len(planned[relative]):
                raise ValueError(f"验收失败：{relative} 页数不符")
            with pymupdf.open(output / relative) as rendered:
                for index, (page, entry, planned_row) in enumerate(zip(pdf.pages, rows, planned[relative]), 1):
                    number = entry["完整发货号码"]
                    expected_row = expected.get(number)
                    if expected_row is None or expected_row != planned_row:
                        raise ValueError(f"验收失败：{relative} 组内号码或顺序错误")
                    if entry["文件内页码"] != str(index):
                        raise ValueError(f"验收失败：{relative} 清单页码不符")
                    mapping = {"发货尺码": "标准发货尺码", "材质": "标准材质", "状态": "匹配状态",
                               "原标签页码": "原标签页码", "货号": "货号", "数量": "数量",
                               "尾四位": "尾四位", "对应依据": "对应依据", "发货备注": "无尺码备注"}
                    if any(entry[out] != str(expected_row[src]) for out, src in mapping.items()):
                        raise ValueError(f"验收失败：{number} 核对清单字段不符")
                    origin = int(expected_row["原标签页码"]) - 1
                    if page_geometry(page) != page_geometry(source_reader.pages[origin]):
                        raise ValueError(f"验收失败：{number} 页面尺寸/裁剪框/旋转发生变化")
                    text = rendered[index - 1].get_text(sort=False)
                    if shipment_numbers(text) != [number] or text != source_document[origin].get_text(sort=False):
                        raise ValueError(f"验收失败：{number} 标签文字不符")
                    # 全页 144 DPI 像素对比覆盖二维码、字体和其他非文字图形。
                    actual_image = rendered[index - 1].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                    original_image = source_document[origin].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                    if (actual_image.width, actual_image.height, actual_image.samples) != (
                        original_image.width, original_image.height, original_image.samples
                    ):
                        raise ValueError(f"验收失败：{number} 页面渲染与原页不同")
                    seen.append(number)
    if Counter(seen) != Counter(expected.keys()):
        raise ValueError("验收失败：重复或遗漏标签")
    return {"复核页数": len(seen), "明确跳过页数": len(skips), "复核PDF数": len(actual_files), "文字尺寸顺序复核": "通过", "逐页144DPI像素复核": "通过"}


def build_package(source: Path, output: Path, matches, audit_path: Path | None = None):
    groups = group_records(matches)
    reader = PdfReader(source)
    if sorted(int(row["原标签页码"]) for row in matches) != list(range(1, len(reader.pages) + 1)):
        raise ValueError("原始页面必须全部且仅使用一次")
    checklist = []
    for relative, rows in groups:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        writer = PdfWriter()
        for index, row in enumerate(rows, 1):
            writer.add_page(reader.pages[int(row["原标签页码"]) - 1])
            checklist.append({
                "分组文件": relative, "文件内页码": index, "发货尺码": row["标准发货尺码"],
                "材质": row["标准材质"], "完整发货号码": row["完整发货号码"],
                "尾四位": row["尾四位"], "货号": row["货号"], "数量": row["数量"],
                "原标签页码": row["原标签页码"], "对应依据": row["对应依据"],
                "状态": row["匹配状态"], "发货备注": row["无尺码备注"],
            })
        with target.open("wb") as stream:
            writer.write(stream)
        writer.close()
    write_csv(output / "逐单核对清单.csv", checklist, CHECK_FIELDS)
    skipped = [row for row in matches if row["匹配状态"] == SKIP_STATUS]
    lines = ["发货包分组说明", "", f"原标签 {len(matches)} 张，本次发货 {len(checklist)} 张，用户明确跳过 {len(skipped)} 张。",
             "按标准材质、标准发货尺码分组；组内按原标签页码升序。",
             "直接复制原标签页，无缩放、裁剪、旋转、重绘或封面。",
             "尾四位取完整发货号码第一段的后四位，仅辅助人工查看。",
             "CSV 为 UTF-8 BOM；用 Excel 导入时请把号码、尾四位、货号列设为文本。", ""]
    for relative, rows in groups:
        lines.append(f"{relative}：{len(rows)} 张")
        notes = sorted({row["无尺码备注"] for row in rows if row["无尺码备注"]})
        lines.extend(f"  发货备注：{note}" for note in notes)
    (output / "先看这里_分组说明.txt").write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return verify_package(source, output, matches, audit_path)
