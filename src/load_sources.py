"""严格 CSV 读取：支持字段内换行，禁止数值化货号或解包 Excel 公式。"""

import csv
import io
import re
from pathlib import Path

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


def load_products(path: Path):
    _, rows = read_csv(path, ("货号", "发货尺码", "材质"))
    for index, row in enumerate(rows, 1):
        row["源记录号"] = index
    return rows


def load_overrides(path: Path):
    if not path.exists():
        return []
    _, rows = read_csv(path, OVERRIDE_FIELDS[:-1])
    for index, row in enumerate(rows, 1):
        row["源记录号"] = index
        row["处理方式"] = row.get("处理方式", "") or "发货"
    return rows


def discover_one(directory: Path, suffix: str):
    candidates = sorted(p for p in directory.glob("*") if p.is_file() and p.suffix.lower() == suffix)
    if len(candidates) != 1:
        raise ValueError(f"{directory} 必须恰有一个 {suffix} 文件，实际 {len(candidates)} 个；可用命令行指定")
    return candidates[0].resolve()
