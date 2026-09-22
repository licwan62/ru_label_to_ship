from copy import deepcopy

import pytest

from src.extract_labels import extract_labels
from src.load_sources import load_fuzzy_matches, load_products, load_shipments, read_csv, write_csv
from src.match_records import match_records
from tests.conftest import confirmed


def inputs(batch):
    return (extract_labels(batch / "input/labels/input.pdf"),
            load_shipments(batch / "input/发货单/input.csv")[1],
            load_products(batch / "input/信息表/input.csv"))


def test_exact_join_ignores_order_and_tail_collision(batch):
    labels, shipments, products = inputs(batch)
    rows = match_records(labels, shipments, products)
    assert [r["货号"] for r in rows] == ["0017573", "SKU-B", "SKU-C"]
    assert [r["标准发货尺码"] for r in rows] == ["M", "L", "M"]
    assert rows[0]["尾四位"] == rows[1]["尾四位"]
    assert all(r["匹配状态"] == "自动匹配通过" for r in rows)
    assert shipments[-1]["照片"] == '=DISPIMG("test",1)'
    assert shipments[-1]["商品"] == '商品,含逗号\n和"引号"'


def test_sku_does_not_drop_leading_zero(batch):
    labels, shipments, products = inputs(batch)
    products[0]["货号"] = "17573"
    assert match_records(labels, shipments, products)[0]["匹配状态"] == "货号未匹配"


def test_duplicate_products_block_even_with_override(batch):
    labels, shipments, products = inputs(batch)
    products.append(deepcopy(products[0]))
    manual = confirmed(labels[0]["完整发货号码"], "0017573")
    assert match_records(labels, shipments, products, [manual])[0]["匹配状态"] == "货号重复匹配"


def test_annotated_size_is_normalized_and_manual_override_still_wins(batch):
    labels, shipments, products = inputs(batch)
    products[0]["发货尺码"] = "S(应该发ys，但是没有就发s)"
    products[0]["材质"] = "PEVA"
    before = match_records(labels, shipments, products)[0]
    assert before["匹配状态"] == "自动匹配通过"
    assert before["标准发货尺码"] == "S"
    assert "括号说明已移除" in before["对应依据"]
    manual = confirmed(labels[0]["完整发货号码"], "0017573", "YS", "测试已确认材质")
    after = match_records(labels, shipments, products, [manual])[0]
    assert after["匹配状态"] == "人工确认通过"
    assert after["标准发货尺码"] == "YS"
    assert after["标准材质"] == "测试已确认材质"
    assert after["原始材质"] == "PEVA"
    assert "测试程序（非业务确认）" in after["对应依据"]


@pytest.mark.parametrize(("field", "value"), [
    ("确认人", ""), ("确认依据", ""), ("确认材质", ""), ("确认发货尺码", "M(备注)"),
    ("确认日期", "2026-02-30"), ("确认日期", "20260910"), ("货号", "17573"),
])
def test_invalid_override_cannot_fall_back_to_automatic_match(batch, field, value):
    labels, shipments, products = inputs(batch)
    manual = confirmed(labels[0]["完整发货号码"], "0017573")
    manual[field] = value
    assert match_records(labels, shipments, products, [manual])[0]["匹配状态"] == "等待人工确认"


def test_override_can_resolve_missing_product(batch):
    labels, shipments, products = inputs(batch)
    manual = confirmed(labels[0]["完整发货号码"], "0017573")
    row = match_records(labels, shipments, products[1:], [manual])[0]
    assert row["匹配状态"] == "人工确认通过"
    assert row["信息表记录号"] == ""
    assert "manual_overrides" in row["对应依据"]


def test_fuzzy_material_mapping_preserves_original(batch, tmp_path):
    labels, shipments, products = inputs(batch)
    products[0]["材质"] = "PEVA"
    archive = tmp_path / "fuzzy_matches.json"
    archive.write_text('{"材质":{"PEVA":"单层PEVA"}}', encoding="utf-8")
    row = match_records(labels, shipments, products, fuzzy_matches=load_fuzzy_matches(archive))[0]
    assert row["原始材质"] == "PEVA"
    assert row["标准材质"] == "单层PEVA"
    assert "PEVA→单层PEVA" in row["对应依据"]


def test_fuzzy_material_mapping_applies_after_manual_override(batch):
    labels, shipments, products = inputs(batch)
    manual = confirmed(labels[0]["完整发货号码"], "0017573", material="PEVA")
    row = match_records(labels, shipments, products, [manual], {"材质": {"PEVA": "单层PEVA"}})[0]
    assert row["标准材质"] == "单层PEVA"


def test_material_brackets_are_stripped_when_enabled(batch):
    labels, shipments, products = inputs(batch)
    products[0]["材质"] = "单层PEVA（灰色无耳）"
    rules = {"材质括号说明": True}
    row = match_records(labels, shipments, products, fuzzy_matches=rules)[0]
    assert row["原始材质"] == "单层PEVA（灰色无耳）"
    assert row["标准材质"] == "单层PEVA"
    assert "材质括号说明已移除：单层PEVA（灰色无耳）→单层PEVA" in row["对应依据"]


def test_material_brackets_untouched_when_disabled(batch):
    labels, shipments, products = inputs(batch)
    products[0]["材质"] = "单层PEVA（灰色无耳）"
    row = match_records(labels, shipments, products)[0]
    assert row["标准材质"] == "单层PEVA（灰色无耳）"


def test_material_brackets_stripped_before_alias_mapping(batch):
    labels, shipments, products = inputs(batch)
    products[0]["材质"] = "PEVA（灰色无耳）"
    rules = {"材质括号说明": True, "材质": {"PEVA": "单层PEVA"}}
    row = match_records(labels, shipments, products, fuzzy_matches=rules)[0]
    assert row["标准材质"] == "单层PEVA"


def test_reusable_sku_rule_resolves_nonstandard_or_missing_product(batch):
    labels, shipments, products = inputs(batch)
    products[0]["发货尺码"] = "S(说明文字)"
    products[0]["材质"] = "PEVA"
    rules = {"材质": {"PEVA": "单层PEVA"}, "货号": {"0017573": {"发货尺码": "S", "材质": "PEVA"}}}
    row = match_records(labels, shipments, products, fuzzy_matches=rules)[0]
    assert row["匹配状态"] == "自动匹配通过"
    assert (row["标准发货尺码"], row["标准材质"]) == ("S", "单层PEVA")

    row = match_records(labels, shipments, products[1:], fuzzy_matches=rules)[0]
    assert row["匹配状态"] == "自动匹配通过"


def test_unmatched_sku_can_use_valid_final_underscore_segment_as_size(batch):
    labels, shipments, products = inputs(batch)
    next(row for row in shipments if row["发货号码"] == labels[0]["完整发货号码"])["货号"] = "Mercedes-Benz_E-Class_3XL"
    rules = {"默认值": {"材质": "单层PEVA"}, "货号尾段尺码": True}
    row = match_records(labels, shipments, products, fuzzy_matches=rules)[0]
    assert row["匹配状态"] == "自动匹配通过"
    assert (row["标准发货尺码"], row["标准材质"]) == ("3XL", "单层PEVA")


def test_csv_bom_encoding_and_malformed_headers(tmp_path):
    path = tmp_path / "data.csv"
    path.write_bytes('货号,数量\r\n0017573,1\r\n'.encode("gb18030"))
    headers, rows = read_csv(path, ["货号"])
    assert rows[0]["货号"] == "0017573"
    write_csv(path, rows, headers)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    path.write_text("货号,货号\na,b", encoding="utf-8")
    with pytest.raises(ValueError, match="表头"):
        read_csv(path)

