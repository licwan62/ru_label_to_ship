"""真实样本回归。模拟确认仅写入 pytest 临时目录，绝不作为业务确认。"""

from collections import Counter
import json
import shutil

from src.cli import main
from src.extract_labels import extract_labels
from src.load_sources import discover_one, load_products, load_shipments, read_csv, write_csv
from src.match_records import match_records
from src.schema import OVERRIDE_FIELDS
from src.validate_records import source_hashes, validate_records
from tests.conftest import ROOT, confirmed


def test_real_910_matches_startup(tmp_path):
    batch = ROOT / "sample/9.10"
    pdf = discover_one(batch / "input/labels", ".pdf")
    shipment = discover_one(batch / "input/发货单", ".csv")
    products_path = discover_one(batch / "input/信息表", ".csv")
    before = source_hashes([pdf, shipment, products_path])
    labels = extract_labels(pdf)
    shipments = load_shipments(shipment)[1]
    products = load_products(products_path)
    matches = match_records(labels, shipments, products)
    assert len(labels) == len(shipments) == len(matches) == 55
    assert len(products) == 134
    assert sum(row["数量"] for row in shipments) == 55
    assert len({row["货号"] for row in shipments}) == 28
    assert Counter(row["配送服务"] for row in labels) == {"GUOO Economy Budget": 23, "GUOO Economy Small": 32}
    assert labels[0]["分拣标识"] == "C / ПВЗ"
    assert "Россия" in labels[0]["收货地址原文"]
    assert matches[0]["货号"] == "20260803013"
    assert Counter(row["匹配状态"] for row in matches) == {"自动匹配通过": 53, "货号未匹配": 2}
    assert {row["货号"] for row in matches if row["匹配状态"] == "货号未匹配"} == {"VESTA-2M", "0017573"}
    assert len(validate_records(labels, shipments, matches)) == 2
    # 在隔离目录验证完整链路，包括真实二维码和俄文的逐页像素不变。
    isolated = tmp_path / "simulated_910_NOT_FOR_SHIPPING"
    for source, folder in [(pdf, "labels"), (shipment, "发货单"), (products_path, "信息表")]:
        (isolated / "input" / folder).mkdir(parents=True)
        shutil.copyfile(source, isolated / "input" / folder / source.name)
    overrides = [confirmed(row["完整发货号码"], row["货号"], "TEST", "测试专用禁止发货")
                 for row in matches if row["匹配状态"] != "自动匹配通过"]
    write_csv(isolated / "manual_overrides.csv", overrides, OVERRIDE_FIELDS)
    assert main(["build", "--batch", str(isolated)]) == 0
    output = isolated / "output/发货包"
    summary = json.loads(next((isolated / "审计").rglob("统计摘要.json")).read_text(encoding="utf-8"))
    assert summary["输出复核"]["复核页数"] == 55
    _, checklist = read_csv(output / "逐单核对清单.csv")
    assert len(checklist) == len({row["完整发货号码"] for row in checklist}) == 55
    assert source_hashes([pdf, shipment, products_path]) == before
