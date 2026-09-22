import pymupdf

from src.load_sources import load_shipments


def test_pdf_shipment_reads_wrapped_sku_and_continuation_page(tmp_path):
    path = tmp_path / "shipment.pdf"
    with pymupdf.open() as document:
        first = document.new_page(width=595, height=842)
        first.insert_text((80, 80), "GUOO Economy Small")
        first.insert_text((45, 140), "49033338-0213-1")
        first.insert_text((190, 136), "Mercedes product first row")
        first.insert_text((402, 136), "Mercedes-Benz_E-Cl")
        first.insert_text((402, 145), "ass_3XL")
        first.insert_text((488, 140), "1")
        first.insert_text((45, 191), "0123969393-0126-2")
        first.insert_text((190, 187), "Chery product second row")
        first.insert_text((402, 187), "CHERY-TIGGO-ALL-S-")
        first.insert_text((402, 196), "PEVA")
        first.insert_text((488, 191), "1")

        continuation = document.new_page(width=595, height=842)
        continuation.insert_text((45, 45), "60447475-0387-1")
        continuation.insert_text((190, 41), "Lada product")
        continuation.insert_text((402, 41), "LADA2121-05-E")
        continuation.insert_text((488, 45), "1")
        document.save(path)

    _, rows = load_shipments(path)
    assert [row["货号"] for row in rows] == [
        "Mercedes-Benz_E-Class_3XL", "CHERY-TIGGO-ALL-S-PEVA", "LADA2121-05-E"
    ]
    assert [row["商品"] for row in rows] == [
        "Mercedes product first row", "Chery product second row", "Lada product"
    ]
    assert [row["配送服务"] for row in rows] == ["GUOO Economy Small"] * 3
    assert [row["数量"] for row in rows] == [1, 1, 1]
