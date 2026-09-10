"""用 MuPDF 的字体解码提取文字；原文完整留存，不通过重绘生成标签。"""

import re
from pathlib import Path

import pymupdf

NUMBER_RE = re.compile(r"(?<![\w-])[0-9]+-[0-9]{4}-[0-9](?![\w-])")
SERVICE_RE = re.compile(r"\bGUOO\s+Economy\s+(Budget|Small)\b")


def shipment_numbers(text: str):
    return NUMBER_RE.findall(text)


def tail_four(number: str):
    # 与 9.7 核对清单一致：取第一段的后四位，不是中间四位。
    return number.split("-")[0][-4:] if number else ""


def parse_text(text: str, page_number: int, width=0, height=0):
    numbers = shipment_numbers(text)
    services = SERVICE_RE.findall(text)
    issues = []
    if len(numbers) != 1:
        issues.append("缺少发货号码" if not numbers else "多个发货号码")
    if len(services) != 1:
        issues.append("配送服务未识别" if not services else "多个配送服务")
    number = numbers[0] if len(numbers) == 1 else ""
    global_match = re.search(r"OZON\s+GLOBAL:\s*([0-9]+)", text)
    china_start = text.find("China,")
    service_match = SERVICE_RE.search(text)
    china_end = service_match.start() if service_match else len(text)
    number_match = NUMBER_RE.search(text)
    middle = text[number_match.end():china_start] if number_match and china_start >= number_match.end() else ""
    lines = middle.strip().splitlines()
    sorting = []
    while lines and re.fullmatch(r"(?:[A-Z]|ПВЗ)(?:\s*/\s*ПВЗ)?", lines[0].strip()):
        sorting.append(lines.pop(0).strip())
    return {
        "原标签页码": page_number, "完整发货号码": number, "尾四位": tail_four(number),
        "Ozon Global号": global_match.group(1) if global_match else "",
        "配送服务": f"GUOO Economy {services[0]}" if len(services) == 1 else "",
        "分拣标识": " / ".join(sorting), "收货地址原文": "\n".join(lines).strip(),
        "中国地址原文": text[china_start:china_end].strip() if china_start >= 0 else "",
        "二维码内容": "", "解析状态": "；".join(issues) if issues else "通过",
        "原始文本": text, "页面宽pt": width, "页面高pt": height,
    }


def extract_labels(path: Path):
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise ValueError("标签 PDF 已加密，需要可直接读取的原始 PDF")
        return [parse_text(page.get_text(sort=False), index, page.mediabox.width, page.mediabox.height)
                for index, page in enumerate(document, 1)]

