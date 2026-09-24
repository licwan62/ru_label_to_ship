"""python -m src.cli audit|build --batch sample/9.10
python -m src.cli audit|build --input-dir input/0911 [--artifacts-dir artifacts/0911]"""

import argparse
import csv
import json
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from pypdf.errors import PyPdfError

from .build_package import build_package
from .dedupe_batch import dedupe_batch
from .extract_labels import extract_labels
from .load_sources import discover_flat, discover_one, discover_shipment, load_fuzzy_matches, load_overrides, load_products, load_shipments, write_csv
from .match_records import match_records
from .schema import ISSUE_FIELDS, LABEL_FIELDS, MATCH_FIELDS, OVERRIDE_FIELDS, PENDING_FIELDS, PASS_STATUSES, SKIP_STATUS
from .validate_records import source_hashes, validate_records, verify_sources


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def new_output(path, inputs):
    path = path.resolve()
    if path.exists():
        raise ValueError(f"输出目录已存在，为保留历史结果请换一个 --output：{path}")
    if any(p == path or p.is_relative_to(path) for p in inputs):
        raise ValueError("输出目录不能包含源文件")
    return path


def concise_pending(rows):
    result = []
    for row in rows:
        if not row["货号"]:
            problem, advice = "发货单缺少货号", "补充货号后重新审计"
        elif row["匹配状态"] == "货号未匹配":
            problem, advice = "信息表和规则包均无此货号", "在规则包添加尺码、材质，或逐单确认"
        elif row["匹配状态"] == "材质缺失":
            problem, advice = "信息表材质为空", "确认材质"
        elif "原始尺码含说明" in row["验证信息"]:
            problem, advice = "尺码含说明文字，不能自动分组", "确认标准尺码和材质"
        else:
            problem, advice = row["匹配状态"], row["验证信息"]
        result.append({
            "完整发货号码": row["完整发货号码"], "货号": row["货号"],
            "发货尺码": row["原始发货尺码"], "材质": row["原始材质"],
            "问题": problem, "处理建议": advice,
        })
    return result


def run(args):
    if args.artifacts_dir and not args.input_dir:
        raise ValueError("--artifacts-dir 必须配合 --input-dir 使用")
    if args.input_dir:
        input_dir = args.input_dir.resolve()
        if not input_dir.is_dir():
            raise ValueError(f"输入目录不存在：{input_dir}")
        artifacts_dir = (args.artifacts_dir or Path("artifacts") / input_dir.name).resolve()
        flat_labels, flat_shipment, flat_products = (None, None, None)
        if not (args.labels and args.shipment and args.products):
            flat_labels, flat_shipment, flat_products = discover_flat(input_dir)
        labels_path = args.labels.resolve() if args.labels else flat_labels
        shipment_path = args.shipment.resolve() if args.shipment else flat_shipment
        products_path = args.products.resolve() if args.products else flat_products
        batch = artifacts_dir
    else:
        batch = args.batch.resolve()
        if not batch.is_dir():
            raise ValueError(f"批次目录不存在：{batch}")
        labels_path = args.labels.resolve() if args.labels else discover_one(batch / "input/labels", ".pdf")
        shipment_path = args.shipment.resolve() if args.shipment else discover_shipment(batch / "input/发货单")
        products_path = args.products.resolve() if args.products else discover_one(batch / "input/信息表", ".csv")
    override_path = (args.overrides or batch / "manual_overrides.csv").resolve()
    fuzzy_path = (args.fuzzy_matches or batch / "fuzzy_matches.json").resolve()
    inputs = [labels_path, shipment_path, products_path]
    if override_path in inputs:
        raise ValueError("人工覆盖表不能与原始数据文件相同")
    if args.overrides and not override_path.is_file():
        raise ValueError(f"指定的人工覆盖表不存在：{override_path}")
    if override_path.exists():
        inputs.append(override_path)
    if args.fuzzy_matches and not fuzzy_path.is_file():
        raise ValueError(f"指定的模糊匹配 JSON 不存在：{fuzzy_path}")
    if fuzzy_path.exists():
        inputs.append(fuzzy_path)
    hashes = source_hashes(inputs)
    labels = extract_labels(labels_path)
    shipment_fields, shipments = load_shipments(shipment_path)
    products = load_products(products_path)
    overrides = load_overrides(override_path)
    fuzzy_matches = load_fuzzy_matches(fuzzy_path)
    matches = match_records(labels, shipments, products, overrides, fuzzy_matches)
    issues = validate_records(labels, shipments, matches, overrides)
    pending = [row for row in matches if row["匹配状态"] not in PASS_STATUSES | {SKIP_STATUS}]
    skipped = [row for row in matches if row["匹配状态"] == SKIP_STATUS]
    passed = [row for row in matches if row["匹配状态"] in PASS_STATUSES]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    audit_path = (args.output if args.command == "audit" and args.output else batch / "审计" / stamp)
    audit_path = new_output(audit_path, inputs + [override_path])
    audit_path.mkdir(parents=True)
    write_csv(audit_path / "labels_parsed.csv", labels, LABEL_FIELDS)
    write_csv(audit_path / "shipment_normalized.csv", shipments, shipment_fields)
    write_csv(audit_path / "完整匹配表.csv", matches, MATCH_FIELDS)
    write_csv(audit_path / "待确认/待确认清单.csv", concise_pending(pending), PENDING_FIELDS)
    write_csv(audit_path / "跳过清单.csv", skipped, MATCH_FIELDS)
    write_csv(audit_path / "验证问题.csv", issues, ISSUE_FIELDS)
    if overrides:
        write_csv(audit_path / "manual_overrides.csv", overrides, OVERRIDE_FIELDS)
    # 只首次创建待填模板，后续运行绝不覆盖人工确认。
    if not override_path.exists():
        template = []
        seen = set()
        for row in pending:
            number = row["完整发货号码"]
            if number and row["货号"] and number not in seen:
                template.append({"完整发货号码": number, "货号": row["货号"]})
                seen.add(number)
        override_path.parent.mkdir(parents=True, exist_ok=True)
        # x 模式保障并发时也不会覆盖刚由用户创建的确认文件。
        try:
            with override_path.open("x", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=OVERRIDE_FIELDS)
                writer.writeheader()
                writer.writerows(template)
        except FileExistsError:
            raise ValueError("人工覆盖表在运行期间被创建，请重跑以读取最新确认") from None
    groups = Counter((r["标准材质"], r["标准发货尺码"]) for r in passed)
    summary = {
        "模式": args.command, "状态": "阻断" if issues else "审计通过",
        "输入页数": len(labels), "发货单行数": len(shipments), "信息表行数": len(products),
        "通过数": len(passed), "待确认数": len(pending), "跳过数": len(skipped), "验证问题数": len(issues),
        "状态分布": dict(Counter(r["匹配状态"] for r in matches)),
        "配送服务分布": dict(Counter(r["配送服务"] for r in labels)),
        "可分组统计": [{"材质": m, "发货尺码": s, "页数": count} for (m, s), count in sorted(groups.items())],
        "源文件SHA256": hashes, "源文件未改变": True,
        "审计目录": str(audit_path), "人工覆盖表": str(override_path), "发货包目录": None,
        "模糊匹配档案": str(fuzzy_path) if fuzzy_path.exists() else None,
    }
    verify_sources(hashes)
    write_json(audit_path / "统计摘要.json", summary)
    if issues:
        print(f"审计完成：{len(labels)}页，{len(passed)}条通过，{len(pending)}条待确认；全局问题见验证问题.csv。")
        print(f"审计目录：{audit_path}")
        print(f"人工覆盖表：{override_path}")
        if args.command == "build":
            print("存在阻断项，未生成正式发货包或分组 PDF。")
        return 2
    if args.command == "build":
        output = new_output(args.output or batch / "output/发货包", inputs + [override_path])
        output.parent.mkdir(parents=True, exist_ok=True)
        # 同一卷内暂存并验收，只有全部成功后才以正式目录名发布。
        with tempfile.TemporaryDirectory(prefix=".发货包暂存-", dir=output.parent) as temporary:
            staged = Path(temporary) / "package"
            staged.mkdir()
            checks = build_package(labels_path, staged, matches, audit_path)
            verify_sources(hashes)
            summary.update({"状态": "发货包已生成", "发货包目录": str(output), "输出复核": checks})
            staged.rename(output)
        write_json(audit_path / "统计摘要.json", summary)
        print(f"发货包已生成：{output}（发货{len(passed)}页，明确跳过{len(skipped)}页，逐页文字、尺寸和像素复核通过）")
    else:
        print(f"审计通过：{len(matches)}条。审计目录：{audit_path}")
    return 0


def run_dedupe(args):
    old_labels = args.old_labels.resolve()
    old_shipment = args.old_shipment.resolve()
    new_labels = args.new_labels.resolve()
    new_shipment = args.new_shipment.resolve()
    output = new_output(args.output, [old_labels, old_shipment, new_labels, new_shipment])
    output.mkdir(parents=True)
    report = dedupe_batch(old_labels, old_shipment, new_labels, new_shipment, output)
    write_json(output / "去重报告.json", report)
    print(f"去重完成：新标签{report['新标签页数']}页，剔除{report['重复标签页数']}页重复，新增{report['新增标签页数']}页。")
    print(f"新发货单{report['新发货单行数']}行，剔除{report['重复发货单行数']}行重复，新增{report['新增发货单行数']}行。")
    if report["新增标签有号码但发货单无对应记录"] or report["新增发货单有号码但标签无对应页"]:
        print("警告：新增标签与新增发货单的号码集合不一致，见去重报告.json。")
    if report["新增标签中解析异常页（号码格式不标准，需人工核对原PDF）"]:
        print("警告：存在号码格式不标准、未能自动识别的标签页，见去重报告.json，需人工核对原PDF后手动补录。")
    if report["新增发货单中疑似列错位的行（标签与发货号码尾四位不符，已从输出中剔除，需人工核对原PDF）"]:
        print("警告：存在疑似列错位（货号/商品被拼接）的发货单行，已从新增发货单.csv中剔除，见去重报告.json，需人工核对原PDF后手动补录。")
    print(f"输出目录：{output}")
    return 0


def parser():
    root = argparse.ArgumentParser(description="Ozon 原始标签转发货包；有阻断项时禁止生成 PDF")
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("audit", "build"):
        sub = commands.add_parser(command, help="只生成审计文件" if command == "audit" else "全部验证通过后生成正式发货包")
        group = sub.add_mutually_exclusive_group(required=True)
        group.add_argument("--batch", type=Path, help="固定格式批次根目录，如 sample/9.10（内含 input/labels、input/发货单、input/信息表 子目录，输出写回同一目录）")
        group.add_argument("--input-dir", type=Path, help="扁平原始文件目录，如 input/0911；按文件名关键字识别标签/发货单/信息表，输出写入 --artifacts-dir")
        sub.add_argument("--artifacts-dir", type=Path, help="配合 --input-dir 使用；默认 artifacts/<--input-dir 目录名>")
        sub.add_argument("--labels", type=Path, help="指定原标签 PDF")
        sub.add_argument("--shipment", type=Path, help="指定发货单 CSV 或 PDF")
        sub.add_argument("--products", type=Path, help="指定信息表 CSV")
        sub.add_argument("--overrides", type=Path, help="指定已存在的逐单人工覆盖表")
        sub.add_argument("--fuzzy-matches", type=Path, help="指定材质等字段的模糊匹配 JSON 档案")
        sub.add_argument("--output", type=Path, help="新输出目录；audit 为审计目录，build 为正式发货包目录")
    dedupe = commands.add_parser("dedupe", help="比较新旧批次标签/发货单，剔除新批次中已在旧批次出现过的记录")
    dedupe.add_argument("--old-labels", required=True, type=Path, help="旧批次原标签 PDF")
    dedupe.add_argument("--old-shipment", required=True, type=Path, help="旧批次发货单 CSV 或 PDF")
    dedupe.add_argument("--new-labels", required=True, type=Path, help="新批次原标签 PDF（含旧批次全部原页）")
    dedupe.add_argument("--new-shipment", required=True, type=Path, help="新批次发货单 CSV 或 PDF（含旧批次全部记录）")
    dedupe.add_argument("--output", required=True, type=Path, help="新输出目录，写入新增标签.pdf、新增发货单.csv、去重报告.json")
    return root


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        return run_dedupe(args) if args.command == "dedupe" else run(args)
    except (OSError, ValueError, RuntimeError, csv.Error, PyPdfError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
