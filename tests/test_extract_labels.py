import pytest

from src.extract_labels import extract_labels, parse_text, shipment_numbers, tail_four


def test_extracts_pages_and_keeps_metadata(batch):
    labels = extract_labels(batch / "input/labels/input.pdf")
    assert len(labels) == 3
    assert labels[0]["完整发货号码"] == "0012345678-0001-1"
    assert labels[0]["分拣标识"] == "C"
    assert labels[0]["中国地址原文"] == "China, Yiwu"
    assert labels[0]["收货地址原文"] == "Russia, Test"
    assert labels[0]["配送服务"] == "GUOO Economy Small"
    assert labels[0]["页面宽pt"] == 164.25
    assert labels[0]["页面高pt"] == 113.25


@pytest.mark.parametrize(("text", "status"), [
    ("GUOO Economy Small", "缺少发货号码"),
    ("00123-0001-1 00123-0001-1 GUOO Economy Small", "多个发货号码"),
    ("00123-0001-1", "配送服务未识别"),
    ("00123-0001-1 GUOO Economy Small GUOO Economy Budget", "多个配送服务"),
])
def test_bad_page_is_blocked(text, status):
    assert status in parse_text(text, 1)["解析状态"]


def test_full_identifier_boundaries_and_tail():
    assert shipment_numbers("00123-0001-12 X00123-0001-1 00123-0001-1-X") == []
    assert tail_four("40552532-0418-1") == "2532"
    assert tail_four("0000000009-0003-1") == "0009"

