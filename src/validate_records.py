"""不依赖匹配顺序的集合、唯一性、数量校验及源文件哈希。"""

import hashlib
from collections import Counter
from pathlib import Path

from .match_records import override_errors
from .schema import PASS_STATUSES, SKIP_STATUS


def source_hashes(paths):
    return {str(Path(path).resolve()): hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths}


def verify_sources(hashes):
    current = source_hashes(hashes)
    if current != hashes:
        raise ValueError("源文件在执行期间发生变化，结果不能交付")


def validate_records(labels, shipments, matches, overrides=()):
    issues = []

    def issue(kind, source, record="", number="", detail=""):
        issues.append(dict(zip(("问题类型", "来源", "记录号", "完整发货号码", "说明"),
                               (kind, source, record, number, detail))))

    if not labels:
        issue("标签为空", "PDF")
    if len({len(labels), len(shipments), len(matches)}) != 1:
        issue("行数不一致", "全局", detail=f"PDF={len(labels)}，发货单={len(shipments)}，匹配表={len(matches)}")
    sources = (("PDF", labels, "完整发货号码"), ("发货单", shipments, "发货号码"), ("匹配表", matches, "完整发货号码"))
    counts = {}
    for name, rows, field in sources:
        counts[name] = Counter(row[field] for row in rows)
        for number, count in counts[name].items():
            if not number or count != 1:
                issue("号码缺失" if not number else "号码重复", name, number=number, detail=f"出现{count}次")
    for number in sorted(set(counts["发货单"]) - set(counts["PDF"])):
        issue("发货单无对应标签", "发货单", number=number)
    for number in sorted(set(counts["PDF"]) - set(counts["发货单"])):
        issue("标签无对应发货单", "PDF", number=number)
    if counts["PDF"] != counts["匹配表"]:
        issue("匹配表号码集合不一致", "匹配表")
    if [r["原标签页码"] for r in matches] != list(range(1, len(labels) + 1)):
        issue("原标签页码遗漏或重复", "匹配表")
    for row in shipments:
        if type(row["数量"]) is not int or row["数量"] != 1:
            issue("数量规则不满足", "发货单", row["源记录号"], row["发货号码"], str(row["数量"]))
    quantities = [row["数量"] for row in shipments]
    if not all(type(n) is int for n in quantities) or sum(n for n in quantities if type(n) is int) != len(labels):
        issue("数量合计不一致", "发货单", detail="数量合计必须等于标签页数")
    duplicate_numbers = {number for counter in counts.values() for number, n in counter.items() if number and n > 1}
    for row in matches:
        if row["完整发货号码"] in duplicate_numbers:
            row["匹配状态"] = "发货号码重复"
            row["验证信息"] = (row["验证信息"] + "；发货号码重复").strip("；")
        if row["匹配状态"] not in PASS_STATUSES | {SKIP_STATUS}:
            issue(row["匹配状态"], "匹配表", row["原标签页码"], row["完整发货号码"], row["验证信息"])
        elif row["匹配状态"] in PASS_STATUSES and (not row["标准发货尺码"] or not row["标准材质"] or row["标签配送服务"] != row["发货单配送服务"]):
            issue("通过记录字段不完整或冲突", "匹配表", row["原标签页码"], row["完整发货号码"])
        elif row["匹配状态"] == SKIP_STATUS:
            explicit = [o for o in overrides if o["完整发货号码"] == row["完整发货号码"]]
            if len(explicit) != 1 or explicit[0].get("处理方式") != "跳过" or override_errors(explicit[0], row["货号"]):
                issue("跳过记录缺少有效人工依据", "匹配表", row["原标签页码"], row["完整发货号码"])
    override_counts = Counter(o["完整发货号码"] for o in overrides)
    for override in overrides:
        number = override["完整发货号码"]
        if not number or number not in counts["发货单"] or number not in counts["PDF"]:
            issue("人工覆盖无对应订单", "manual_overrides", override["源记录号"], number)
        if override_counts[number] != 1:
            issue("人工覆盖重复", "manual_overrides", override["源记录号"], number)
    return issues
