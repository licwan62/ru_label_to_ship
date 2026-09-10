from copy import deepcopy
import json

import pytest

from src.build_package import group_records, safe_component, verify_package
from src.cli import main
from src.load_sources import load_shipments, read_csv, write_csv
from src.match_records import match_records
from src.schema import OVERRIDE_FIELDS
from src.validate_records import source_hashes
from tests.test_matching import inputs
from tests.conftest import confirmed


def test_build_copies_every_page_and_preserves_sources(batch):
    sources = [batch / "input/labels/input.pdf", batch / "input/发货单/input.csv", batch / "input/信息表/input.csv"]
    before = source_hashes(sources)
    assert main(["build", "--batch", str(batch)]) == 0
    output = batch / "output/发货包"
    summary = json.loads(next((batch / "审计").rglob("统计摘要.json")).read_text(encoding="utf-8"))
    assert {p.name for p in output.iterdir()} == {"测试材质", "逐单核对清单.csv", "先看这里_分组说明.txt"}
    assert summary["输出复核"]["复核页数"] == 3
    assert summary["输出复核"]["复核PDF数"] == 2
    assert source_hashes(sources) == before
    _, checklist = read_csv(output / "逐单核对清单.csv")
    assert {r["完整发货号码"] for r in checklist} == {"0012345678-0001-1", "9912345678-0002-1", "0000000009-0003-1"}
    assert [r["原标签页码"] for r in checklist if r["发货尺码"] == "M"] == ["1", "3"]
    saved = source_hashes(output.rglob("*.*"))
    assert main(["build", "--batch", str(batch)]) == 1  # 不能覆盖已有正式包
    assert source_hashes(saved) == saved


def test_blocked_build_writes_audit_only(batch):
    headers, rows = read_csv(batch / "input/信息表/input.csv")
    write_csv(batch / "input/信息表/input.csv", rows[1:], headers)
    assert main(["build", "--batch", str(batch)]) == 2
    assert not (batch / "output/发货包").exists()
    assert not list((batch / "审计").rglob("*.pdf"))
    _, pending = read_csv(next((batch / "审计").rglob("待确认清单.csv")))
    assert len(pending) == 1
    manual = batch / "manual_overrides.csv"
    template_hash = source_hashes([manual])
    assert main(["audit", "--batch", str(batch)]) == 2
    assert source_hashes([manual]) == template_hash


def test_audit_does_not_create_pdfs(batch):
    assert main(["audit", "--batch", str(batch)]) == 0
    assert not (batch / "output/发货包").exists()
    assert not list((batch / "审计").rglob("*.pdf"))


def test_fixed_input_layout_ignores_backups_and_shipment_pdf(batch):
    (batch / "input/发货单/input.csv.bak").write_text("backup", encoding="utf-8")
    (batch / "input/发货单/input.pdf").write_bytes(b"reference only")
    (batch / "labels").mkdir()
    (batch / "labels/old.pdf").write_bytes(b"old layout must not be read")
    assert main(["build", "--batch", str(batch)]) == 0
    assert (batch / "output/发货包/逐单核对清单.csv").is_file()
    assert not (batch / "发货包").exists()


def test_output_cannot_contain_inputs(batch):
    assert main(["audit", "--batch", str(batch), "--output", str(batch)]) == 1
    assert len(load_shipments(batch / "input/发货单/input.csv")[1]) == 3


def test_explicit_missing_override_is_error(batch):
    assert main(["build", "--batch", str(batch), "--overrides", str(batch / "typo.csv")]) == 1
    assert not (batch / "output/发货包").exists()


def test_verifier_detects_changed_checklist(batch):
    assert main(["build", "--batch", str(batch)]) == 0
    output = batch / "output/发货包"
    headers, rows = read_csv(output / "逐单核对清单.csv")
    rows[0]["货号"] = "wrong"
    write_csv(output / "逐单核对清单.csv", rows, headers)
    labels, shipments, products = inputs(batch)
    with pytest.raises(ValueError, match="核对清单字段"):
        verify_package(batch / "input/labels/input.pdf", output, match_records(labels, shipments, products))


def test_filename_safety_and_collisions(batch):
    assert safe_component("../A:B\\C") == ".._A_B_C"
    assert safe_component("CON") == "_CON"
    assert safe_component("x. ") == "x"
    labels, shipments, products = inputs(batch)
    rows = match_records(labels, shipments, products)
    rows[0]["标准材质"] = "A/B"
    rows[1]["标准材质"] = "A:B"
    with pytest.raises(ValueError, match="重名"):
        group_records(rows)
    rows[1]["标准材质"] = "待确认"
    with pytest.raises(ValueError, match="冲突"):
        group_records(rows)


def test_explicit_skip_accounts_for_page_without_shipping_it(batch):
    headers, products = read_csv(batch / "input/信息表/input.csv")
    write_csv(batch / "input/信息表/input.csv", products[1:], headers)
    override = confirmed("0012345678-0001-1", "0017573", "", "")
    override["处理方式"] = "跳过"
    write_csv(batch / "manual_overrides.csv", [override], OVERRIDE_FIELDS)
    assert main(["build", "--batch", str(batch)]) == 0
    output = batch / "output/发货包"
    audit = next((batch / "审计").iterdir())
    summary = json.loads((audit / "统计摘要.json").read_text(encoding="utf-8"))
    assert summary["通过数"] == 2
    assert summary["跳过数"] == 1
    assert summary["输出复核"]["复核页数"] == 2
    _, skipped = read_csv(audit / "跳过清单.csv")
    assert [row["货号"] for row in skipped] == ["0017573"]
    _, matched = read_csv(audit / "完整匹配表.csv")
    assert len(matched) == 3


@pytest.mark.parametrize("failure", ["quantity", "service", "missing_confirmation"])
def test_skip_cannot_bypass_structural_errors_or_missing_confirmation(batch, failure):
    override = confirmed("0012345678-0001-1", "0017573", "", "")
    override["处理方式"] = "跳过"
    if failure == "missing_confirmation":
        override["确认人"] = ""
    else:
        headers, rows = read_csv(batch / "input/发货单/input.csv")
        row = next(r for r in rows if r["货号"] == "0017573")
        row["数量" if failure == "quantity" else "配送服务"] = "2" if failure == "quantity" else "GUOO Economy Budget"
        write_csv(batch / "input/发货单/input.csv", rows, headers)
    write_csv(batch / "manual_overrides.csv", [override], OVERRIDE_FIELDS)
    assert main(["build", "--batch", str(batch)]) == 2
    assert not (batch / "output/发货包").exists()
