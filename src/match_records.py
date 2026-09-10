"""两段精确匹配与逐单人工覆盖；绝不使用标题、尾号或行位置推断。"""

import re
from collections import defaultdict
from datetime import date

from .schema import MATCH_FIELDS, OVERRIDE_FIELDS, SKIP_STATUS


def index_rows(rows, key):
    result = defaultdict(list)
    for row in rows:
        if row.get(key):
            result[row[key]].append(row)
    return result


def standard_size(value):
    """仅接受独立尺码代码；带说明文字的值必须由人工给出标准代码。"""
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]*", value))


def override_errors(override, sku):
    errors = []
    action = override.get("处理方式") or "发货"
    required = [f for f in OVERRIDE_FIELDS[:-1] if action != "跳过" or f not in ("确认发货尺码", "确认材质")]
    if action not in {"发货", "跳过"}:
        errors.append("处理方式只能为发货或跳过")
    if any(not override.get(field) for field in required):
        errors.append("人工覆盖字段未填全")
    if override.get("货号") != sku:
        errors.append("人工覆盖货号与发货单不一致")
    try:
        value = override.get("确认日期", "")
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        errors.append("确认日期须为 YYYY-MM-DD 有效日期")
    if action != "跳过" and not standard_size(override.get("确认发货尺码", "")):
        errors.append("确认发货尺码必须是独立尺码代码")
    return errors


def match_records(labels, shipments, products, overrides=(), fuzzy_matches=None):
    material_aliases = (fuzzy_matches or {}).get("材质", {})
    shipment_index = index_rows(shipments, "发货号码")
    product_index = index_rows(products, "货号")
    override_index = index_rows(overrides, "完整发货号码")
    result = []
    for label in labels:
        row = dict.fromkeys(MATCH_FIELDS, "")
        for key in ("原标签页码", "完整发货号码", "尾四位", "Ozon Global号"):
            row[key] = label[key]
        row["标签配送服务"] = label["配送服务"]
        errors = []
        if label["解析状态"] != "通过":
            errors.append(label["解析状态"])
        candidates = shipment_index.get(label["完整发货号码"], [])
        if len(candidates) != 1:
            errors.append("发货号码未匹配" if not candidates else "发货号码重复匹配")
        else:
            shipment = candidates[0]
            for field in ("货号", "商品", "数量"):
                row[field] = shipment[field]
            row["发货单记录号"] = shipment["源记录号"]
            row["发货单配送服务"] = shipment["配送服务"]
            if shipment.get("标准化信息"):
                errors.append(shipment["标准化信息"])
            if type(shipment["数量"]) is not int or shipment["数量"] != 1:
                errors.append("数量必须为1，其他数量的标签规则尚未定义")
            if label["配送服务"] != shipment["配送服务"]:
                errors.append("配送服务不一致")
            if not shipment["货号"]:
                errors.append("货号缺失")
            products_found = product_index.get(shipment["货号"], [])
            if len(products_found) == 1:
                product = products_found[0]
                row.update({
                    "信息表记录号": product["源记录号"], "信息表序号": product.get("序号", ""),
                    "来源工作表": product.get("来源工作表", ""),
                    "原始发货尺码": product["发货尺码"], "原始材质": product["材质"],
                    "标准发货尺码": product["发货尺码"], "标准材质": product["材质"],
                    "无尺码备注": product.get("无尺码备注（车型/尺寸）", ""),
                    "对应依据": f"完整发货号码精确匹配发货单记录{shipment['源记录号']}；货号精确匹配信息表记录{product['源记录号']}",
                })
                mapped_material = material_aliases.get(row["标准材质"])
                if mapped_material:
                    row["标准材质"] = mapped_material
                    row["对应依据"] += f"；模糊匹配JSON材质映射：{product['材质']}→{mapped_material}"
            elif len(products_found) > 1:
                row["信息表记录号"] = ";".join(str(p["源记录号"]) for p in products_found)

            manual = override_index.get(label["完整发货号码"], [])
            confirmed = False
            skipped = False
            if len(manual) > 1:
                errors.append("人工覆盖重复")
            elif len(manual) == 1:
                override = manual[0]
                invalid = override_errors(override, shipment["货号"])
                if invalid:
                    errors.append("等待人工确认")
                    errors.extend(invalid)
                else:
                    skipped = override.get("处理方式") == "跳过"
                    confirmed = not skipped
                    row["标准发货尺码"] = "" if skipped else override["确认发货尺码"]
                    row["标准材质"] = "" if skipped else override["确认材质"]
                    mapped_material = material_aliases.get(row["标准材质"])
                    if mapped_material:
                        original_confirmed = row["标准材质"]
                        row["标准材质"] = mapped_material
                        row["对应依据"] += f"；模糊匹配JSON材质映射：{original_confirmed}→{mapped_material}"
                row["对应依据"] += (
                    f"；manual_overrides记录{override['源记录号']}"
                    f"；确认人={override['确认人']}；确认日期={override['确认日期']}"
                    f"；确认依据={override['确认依据']}"
                    f"；处理方式={override.get('处理方式') or '发货'}"
                )
            if not skipped:
                if not products_found and not confirmed:
                    errors.insert(0, "货号未匹配")
                if len(products_found) > 1:
                    errors.insert(0, "货号重复匹配")
                if not row["标准发货尺码"]:
                    errors.append("尺码缺失")
                elif not standard_size(row["标准发货尺码"]):
                    errors.append("等待人工确认")
                    errors.append("原始尺码含说明或非标准字符，需逐单确认标准尺码及材质")
                if not row["标准材质"]:
                    errors.append("材质缺失")
            row["匹配状态"] = SKIP_STATUS if skipped else ("人工确认通过" if confirmed else "自动匹配通过")
        if errors:
            row["匹配状态"] = errors[0]
        row["验证信息"] = "；".join(dict.fromkeys(errors))
        result.append(row)
    return result
