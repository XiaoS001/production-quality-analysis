"""质量指标统计测试。"""

from datetime import datetime

from metrics import yield_by_batch, yield_by_product
from models import Batch, InspectionItem, InspectionRecord, Product, db


def test_batch_and_product_yield(app):
    with app.app_context():
        product = Product(code="P1001", name="智能门锁 A 型")
        item = InspectionItem(name="外观检查")
        batch_1 = Batch(batch_no="B001", product_id=0)
        batch_2 = Batch(batch_no="B002", product_id=0)

        db.session.add_all([product, item])
        db.session.flush()
        batch_1.product_id = product.id
        batch_2.product_id = product.id
        db.session.add_all([batch_1, batch_2])
        db.session.flush()

        db.session.add_all(
            [
                InspectionRecord(
                    batch_id=batch_1.id,
                    item_id=item.id,
                    inspected_qty=1000,
                    passed_qty=950,
                    failed_qty=50,
                    inspected_at=datetime(2026, 9, 1, 10, 0, 0),
                ),
                InspectionRecord(
                    batch_id=batch_2.id,
                    item_id=item.id,
                    inspected_qty=500,
                    passed_qty=480,
                    failed_qty=20,
                    inspected_at=datetime(2026, 9, 2, 10, 0, 0),
                ),
            ]
        )
        db.session.commit()

        batch_rows = yield_by_batch()
        product_rows = yield_by_product()

    assert [
        (row["batch_no"], row["yield_rate"])
        for row in batch_rows
    ] == [
        ("B001", 95.0),
        ("B002", 96.0),
    ]
    assert product_rows == [
        {
            "product_name": "智能门锁 A 型",
            "inspected_qty": 1500,
            "passed_qty": 1430,
            "failed_qty": 70,
            "yield_rate": 95.3,
        }
    ]


def test_zero_inspected_quantity_returns_zero_yield(app):
    with app.app_context():
        product = Product(code="P1002", name="智能门锁 B 型")
        item = InspectionItem(name="尺寸测量")
        batch = Batch(batch_no="B003", product_id=0)

        db.session.add_all([product, item])
        db.session.flush()
        batch.product_id = product.id
        db.session.add(batch)
        db.session.flush()

        db.session.add(
            InspectionRecord(
                batch_id=batch.id,
                item_id=item.id,
                inspected_qty=0,
                passed_qty=0,
                failed_qty=0,
                inspected_at=datetime(2026, 9, 3, 10, 0, 0),
            )
        )
        db.session.commit()

        batch_rows = yield_by_batch()
        product_rows = yield_by_product()

    assert batch_rows[0]["yield_rate"] == 0.0
    assert product_rows[0]["yield_rate"] == 0.0
