from copy import deepcopy

import pytest

from src.match_records import match_records
from src.validate_records import source_hashes, validate_records, verify_sources
from tests.conftest import confirmed
from tests.test_matching import inputs


def test_healthy_batch(batch):
    labels, shipments, products = inputs(batch)
    assert validate_records(labels, shipments, match_records(labels, shipments, products)) == []


@pytest.mark.parametrize("mutation", ["duplicate_label", "duplicate_shipment", "missing_shipment", "extra_shipment", "service", "quantity", "invalid_quantity"])
def test_corrupt_batch_is_blocked(batch, mutation):
    labels, shipments, products = inputs(batch)
    if mutation == "duplicate_label":
        labels[1]["完整发货号码"] = labels[0]["完整发货号码"]
    elif mutation == "duplicate_shipment":
        shipments[1]["发货号码"] = shipments[0]["发货号码"]
    elif mutation == "missing_shipment":
        shipments.pop()
    elif mutation == "extra_shipment":
        extra = deepcopy(shipments[0])
        extra["发货号码"] = "999999-0099-1"
        shipments.append(extra)
    elif mutation == "service":
        shipments[0]["配送服务"] = "GUOO Economy Budget"
    elif mutation == "quantity":
        shipments[0]["数量"], shipments[1]["数量"] = 2, 0  # 总数未变仍必须阻断
    else:
        shipments[0]["数量"] = "1.5"
    matches = match_records(labels, shipments, products)
    assert validate_records(labels, shipments, matches)


def test_orphan_and_duplicate_overrides_block(batch):
    labels, shipments, products = inputs(batch)
    orphan = confirmed("999999-0099-1", "0017573")
    issues = validate_records(labels, shipments, match_records(labels, shipments, products), [orphan, orphan])
    assert {i["问题类型"] for i in issues} == {"人工覆盖无对应订单", "人工覆盖重复"}


def test_hash_check_detects_source_change(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"original")
    hashes = source_hashes([source])
    verify_sources(hashes)
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="源文件"):
        verify_sources(hashes)

