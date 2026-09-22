from pathlib import Path

import pymupdf
import pytest

from src.load_sources import write_csv
from src.schema import OVERRIDE_FIELDS

ROOT = Path(__file__).resolve().parents[1]
SHIP_FIELDS = ["NO", "发货号码", "照片", "配送服务", "商品", "货号", "数量", "标签"]
PRODUCT_FIELDS = ["序号", "来源工作表", "货号", "发货尺码", "材质", "无尺码备注（车型/尺寸）"]


def make_pdf(path, numbers):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        for number in numbers:
            page = document.new_page(width=164.25, height=113.25)
            page.insert_text((6, 12), f"OZON GLOBAL: 4103119\n{number}\nC\nRussia, Test\nChina, Yiwu\nGUOO Economy Small", fontsize=8)
            page.draw_rect(pymupdf.Rect(130, 70, 150, 90), color=(0, 0, 0), fill=(0, 0, 0))
        document.save(path)


@pytest.fixture
def batch(tmp_path):
    batch = tmp_path / "batch"
    numbers = ["0012345678-0001-1", "9912345678-0002-1", "0000000009-0003-1"]
    make_pdf(batch / "input/labels/input.pdf", numbers)
    shipments = []
    for index, number in enumerate(numbers):
        shipments.append({"NO": index + 1, "发货号码": number, "照片": '=DISPIMG("test",1)',
                          "配送服务": "GUOO Economy Small", "商品": '商品,含逗号\n和"引号"',
                          "货号": ["0017573", "SKU-B", "SKU-C"][index], "数量": "1", "标签": number.split("-")[0][-4:]})
    write_csv(batch / "input/发货单/input.csv", shipments[::-1], SHIP_FIELDS)
    products = [{"序号": index + 1, "来源工作表": "测试", "货号": row["货号"],
                 "发货尺码": "M" if index != 1 else "L", "材质": "测试材质", "无尺码备注（车型/尺寸）": "原始备注"}
                for index, row in enumerate(shipments)]
    write_csv(batch / "input/信息表/input.csv", products, PRODUCT_FIELDS)
    return batch


@pytest.fixture
def flat_batch(tmp_path):
    """扁平输入目录：文件平铺一处，靠文件名关键字区分标签/发货单/信息表。"""
    input_dir = tmp_path / "input" / "0000"
    numbers = ["0012345678-0001-1", "9912345678-0002-1", "0000000009-0003-1"]
    make_pdf(input_dir / "货物标签.pdf", numbers)
    shipments = []
    for index, number in enumerate(numbers):
        shipments.append({"NO": index + 1, "发货号码": number, "照片": '=DISPIMG("test",1)',
                          "配送服务": "GUOO Economy Small", "商品": '商品,含逗号\n和"引号"',
                          "货号": ["0017573", "SKU-B", "SKU-C"][index], "数量": "1", "标签": number.split("-")[0][-4:]})
    write_csv(input_dir / "0000发货单.csv", shipments[::-1], SHIP_FIELDS)
    products = [{"序号": index + 1, "来源工作表": "测试", "货号": row["货号"],
                 "发货尺码": "M" if index != 1 else "L", "材质": "测试材质", "无尺码备注（车型/尺寸）": "原始备注"}
                for index, row in enumerate(shipments)]
    write_csv(input_dir / "Ozon上架信息表.csv", products, PRODUCT_FIELDS)
    artifacts_dir = tmp_path / "artifacts" / "0000"
    return input_dir, artifacts_dir


def confirmed(number, sku, size="M", material="测试材质"):
    return dict(zip(OVERRIDE_FIELDS, [number, sku, size, material, "测试程序（非业务确认）", "2026-09-10", "仅用于临时测试"]), 源记录号=1)

