"""严格 CSV 读取：支持字段内换行，禁止数值化货号或解包 Excel 公式。"""

import csv
import io
import json
import re
from pathlib import Path

import openpyxl
import pymupdf

from .schema import OVERRIDE_FIELDS


def read_csv(path: Path, required=()):
    data = path.read_bytes()
    encodings = ("utf-16",) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            content = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"无法识别 CSV 编码：{path}")
    reader = csv.reader(io.StringIO(content, newline=""), strict=True)
    try:
        headers = [v.strip() for v in next(reader)]
    except StopIteration:
        raise ValueError(f"CSV 为空：{path}") from None
    if not all(headers) or len(set(headers)) != len(headers):
        raise ValueError(f"CSV 表头为空或重复：{path}")
    missing = set(required) - set(headers)
    if missing:
        raise ValueError(f"{path} 缺少字段：{', '.join(sorted(missing))}")
    rows = []
    for values in reader:
        if not values or not any(v.strip() for v in values):
            continue
        if len(values) != len(headers):
            raise ValueError(f"{path} 第 {reader.line_num} 物理行字段数不符，检查引号/逗号")
        rows.append(dict(zip(headers, (v.strip() for v in values))))
    return headers, rows


def write_csv(path: Path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_shipments(path: Path):
    if path.suffix.lower() == ".pdf":
        return load_shipments_pdf(path)
    fields, rows = read_csv(path, ("发货号码", "货号", "数量", "配送服务", "商品"))
    for index, row in enumerate(rows, 1):
        row["源记录号"] = index
        value = row["数量"]
        row["标准化信息"] = ""
        if re.fullmatch(r"[0-9]+", value):
            row["数量"] = int(value)
        else:
            row["标准化信息"] = "数量必须是正整数文本"
    return list(dict.fromkeys(fields + ["源记录号", "标准化信息"])), rows


SHIPMENT_FIELDS = ["NO", "发货号码", "照片", "商品", "货号", "数量", "标签", "配送服务", "源记录号", "标准化信息"]
SHIPMENT_NUMBER_RE = re.compile(r"\d+-\d{4}-\d")


def load_shipments_pdf(path: Path):
    """读取 Ozon 发货编号 PDF 的固定列布局，不依赖乱码的中文表头。"""
    rows = []
    current_service = ""
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise ValueError("发货单 PDF 已加密，需要可直接读取的原始 PDF")
        for page_number, page in enumerate(document, 1):
            words = page.get_text("words", sort=True)
            service_words = [w[4] for w in words if w[1] < 90 and w[4] in {"GUOO", "Economy", "Budget", "Small", "Standard", "Big"}]
            if "GUOO" in service_words:
                start = service_words.index("GUOO")
                current_service = " ".join(service_words[start:start + 3])
            if not current_service:
                raise ValueError(f"发货单 PDF 第 {page_number} 页之前未识别配送服务")
            number_words = [w for w in words if SHIPMENT_NUMBER_RE.fullmatch(w[4])]
            for position, number_word in enumerate(number_words):
                y = number_word[1]
                previous_y = number_words[position - 1][1] if position else y - 51
                next_y = number_words[position + 1][1] if position + 1 < len(number_words) else page.rect.height
                cell_top, cell_bottom = (previous_y + y) / 2, (y + next_y) / 2
                # 货号可能在单元格内折成多行，例如 CHERY-TIGGO-ALL-S- / PEVA。
                sku = "".join(w[4] for w in words if 395 <= w[0] < 487 and cell_top <= w[1] < cell_bottom)
                quantity = "".join(w[4] for w in words if 487 <= w[0] < 520 and cell_top <= w[1] < cell_bottom)
                product = " ".join(w[4] for w in words if 180 <= w[0] < 395 and cell_top <= w[1] < cell_bottom)
                label = "".join(w[4] for w in words if w[0] >= 520 and cell_top <= w[1] < cell_bottom)
                rows.append({
                    "NO": len(rows) + 1, "发货号码": number_word[4], "照片": "", "商品": product,
                    "货号": sku, "数量": int(quantity) if quantity.isdigit() else quantity, "标签": label,
                    "配送服务": current_service, "源记录号": len(rows) + 1,
                    "标准化信息": "" if quantity.isdigit() else "数量必须是正整数文本",
                })
    if not rows:
        raise ValueError(f"发货单 PDF 未识别到任何发货记录：{path}")
    return SHIPMENT_FIELDS, rows


def load_products(path: Path):
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        rows = load_products_xlsx(path)
    else:
        _, rows = read_csv(path, ("货号", "发货尺码", "材质"))
    for index, row in enumerate(rows, 1):
        row["源记录号"] = index
    return rows


PRODUCT_REQUIRED_FIELDS = ("货号", "发货尺码", "材质")


def load_products_xlsx(path: Path):
    """读取信息表 Excel；优先取唯一含“一店”的工作表（汇总表可能落后于分店明细未同步新货号），
    没有“一店”命名时回退到唯一含“汇总”的工作表，避免误读其他分店明细。"""
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        candidates = [name for name in workbook.sheetnames if "一店" in name]
        if not candidates:
            candidates = [name for name in workbook.sheetnames if "汇总" in name]
        if len(candidates) != 1:
            raise ValueError(f"{path} 未按工作表名识别到唯一的“一店”或“汇总”表，实际候选：{candidates}；可将信息表整理为单一 CSV 后用 --products 指定")
        sheet = workbook[candidates[0]]
        rows_iter = sheet.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            raise ValueError(f"信息表工作表为空：{path}：{candidates[0]}") from None
        headers = [str(v).strip() if v is not None else "" for v in header_row]
        if not all(headers) or len(set(headers)) != len(headers):
            raise ValueError(f"信息表工作表表头为空或重复：{path}：{candidates[0]}")
        missing = set(PRODUCT_REQUIRED_FIELDS) - set(headers)
        if missing:
            raise ValueError(f"{path}：{candidates[0]} 缺少字段：{', '.join(sorted(missing))}")
        field_indexes = {field: headers.index(field) for field in PRODUCT_REQUIRED_FIELDS}
        rows = []
        for values in rows_iter:
            if values is None or not any(v not in (None, "") for v in values):
                continue
            for field, index in field_indexes.items():
                if index < len(values) and isinstance(values[index], (int, float)):
                    raise ValueError(f"{path}：{candidates[0]} 第 {len(rows) + 2} 行“{field}”被 Excel 存成数字，可能丢失前导零；请改为文本格式")
            row = {header: ("" if index >= len(values) or values[index] is None else str(values[index]).strip())
                   for index, header in enumerate(headers)}
            rows.append(row)
        return rows
    finally:
        workbook.close()


def load_overrides(path: Path):
    if not path.exists():
        return []
    _, rows = read_csv(path, OVERRIDE_FIELDS[:-1])
    # 首次审计生成的空白行只是待填占位符，不应压过后来加入的通用规则。
    rows = [row for row in rows if any(row.get(field) for field in OVERRIDE_FIELDS[2:])]
    for index, row in enumerate(rows, 1):
        row["源记录号"] = index
        row["处理方式"] = row.get("处理方式", "") or "发货"
    return rows


def load_fuzzy_matches(path: Path):
    """读取可迁移规则包：材质别名与按完整货号登记的分组规则。"""
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"模糊匹配 JSON 无效：{path}：{exc}") from None
    if not isinstance(value, dict) or set(value) - {"材质", "货号", "默认值", "货号尾段尺码", "材质括号说明"}:
        raise ValueError(f"模糊匹配 JSON 包含不支持的顶层规则：{path}")
    materials = value.get("材质", {})
    if not isinstance(materials, dict):
        raise ValueError(f"模糊匹配 JSON 的“材质”必须是对象：{path}")
    result = {}
    for source, target in materials.items():
        if not isinstance(source, str) or not source.strip() or not isinstance(target, str) or not target.strip():
            raise ValueError(f"模糊匹配 JSON 的材质原值和目标值必须是非空字符串：{path}")
        result[source.strip()] = target.strip()
    sku_rules = value.get("货号", {})
    if not isinstance(sku_rules, dict):
        raise ValueError(f"模糊匹配 JSON 的“货号”必须是对象：{path}")
    normalized_skus = {}
    for sku, rule in sku_rules.items():
        if not isinstance(sku, str) or not sku.strip() or not isinstance(rule, dict):
            raise ValueError(f"模糊匹配 JSON 的货号规则无效：{path}")
        if set(rule) != {"发货尺码", "材质"} or not all(isinstance(rule[k], str) and rule[k].strip() for k in rule):
            raise ValueError(f"货号 {sku} 必须且仅提供非空的“发货尺码”和“材质”：{path}")
        normalized_skus[sku.strip()] = {k: rule[k].strip() for k in ("发货尺码", "材质")}
    defaults = value.get("默认值", {})
    if not isinstance(defaults, dict) or set(defaults) - {"材质"}:
        raise ValueError(f"模糊匹配 JSON 的“默认值”仅支持“材质”：{path}")
    if "材质" in defaults and (not isinstance(defaults["材质"], str) or not defaults["材质"].strip()):
        raise ValueError(f"模糊匹配 JSON 的默认材质必须是非空字符串：{path}")
    suffix_size = value.get("货号尾段尺码", False)
    if not isinstance(suffix_size, bool):
        raise ValueError(f"模糊匹配 JSON 的“货号尾段尺码”必须是 true 或 false：{path}")
    material_brackets = value.get("材质括号说明", False)
    if not isinstance(material_brackets, bool):
        raise ValueError(f"模糊匹配 JSON 的“材质括号说明”必须是 true 或 false：{path}")
    return {"材质": result, "货号": normalized_skus,
            "默认值": {"材质": defaults["材质"].strip()} if defaults.get("材质") else {},
            "货号尾段尺码": suffix_size, "材质括号说明": material_brackets}


def discover_one(directory: Path, suffix: str):
    candidates = sorted(p for p in directory.glob("*") if p.is_file() and p.suffix.lower() == suffix)
    if len(candidates) != 1:
        raise ValueError(f"{directory} 必须恰有一个 {suffix} 文件，实际 {len(candidates)} 个；可用命令行指定")
    return candidates[0].resolve()


def discover_shipment(directory: Path):
    """兼容旧批次 CSV；仅无 CSV 时使用唯一 PDF。"""
    csv_files = sorted(p for p in directory.glob("*") if p.is_file() and p.suffix.lower() == ".csv")
    if csv_files:
        if len(csv_files) != 1:
            raise ValueError(f"{directory} 必须恰有一个 .csv 文件，实际 {len(csv_files)} 个；可用命令行指定")
        return csv_files[0].resolve()
    return discover_one(directory, ".pdf")


LABEL_KEYWORDS = ("标签", "label")
SHIPMENT_KEYWORDS = ("发货编号", "发货单", "shipment")
PRODUCT_KEYWORDS = ("信息表", "上架", "product")


def _by_keyword(files, keywords):
    return [p for p in files if any(k.lower() in p.stem.lower() for k in keywords)]


def _pick_one(directory, role, candidates):
    if len(candidates) == 1:
        return candidates[0].resolve()
    if not candidates:
        raise ValueError(f"{directory} 未按文件名识别到{role}，请用命令行参数明确指定")
    raise ValueError(f"{directory} 有多个疑似{role}的文件：{[p.name for p in candidates]}；请用命令行参数明确指定")


def discover_flat(directory: Path):
    """扁平输入目录：按文件名关键字识别标签、发货单、信息表三份原始文件，避免固定子目录结构。"""
    files = sorted(p for p in directory.glob("*") if p.is_file())
    pdfs = [p for p in files if p.suffix.lower() == ".pdf"]
    csvs = [p for p in files if p.suffix.lower() == ".csv"]
    sheets = [p for p in files if p.suffix.lower() in (".csv", ".xlsx", ".xlsm")]
    labels_path = _pick_one(directory, "标签 PDF", _by_keyword(pdfs, LABEL_KEYWORDS))
    shipment_path = _pick_one(directory, "发货单", _by_keyword(pdfs + csvs, SHIPMENT_KEYWORDS))
    products_path = _pick_one(directory, "信息表", _by_keyword(sheets, PRODUCT_KEYWORDS))
    return labels_path, shipment_path, products_path
