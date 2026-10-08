"""检验记录 CSV 导入测试。"""

import io

from importer import import_inspection_csv
from models import InspectionRecord

HEADER = (
    "batch_no,item_name,inspected_qty,passed_qty,failed_qty,"
    "defect_code,inspected_at"
)


def test_import_three_valid_and_two_invalid_rows(seeded_data, app):
    csv_text = "\n".join(
        [
            HEADER,
            "B001,外观检查,100,95,5,D01,2026-09-01 10:00:00",
            "B002,尺寸测量,200,190,10,D02,2026-09-02 11:00:00",
            ",装配检验,100,90,10,D03,2026-09-03 12:00:00",
            "B001,通电测试,100,95,5,D04,2026-09-04 13:00:00",
            "B002,包装检查,100,90,20,,2026-09-05 14:00:00",
        ]
    )

    with app.app_context():
        result = import_inspection_csv(io.StringIO(csv_text))

    assert result == {
        "success": 3,
        "failed": 2,
        "errors": [
            {
                "row": 4,
                "reason": "必填字段 batch_no 不能为空",
            },
            {
                "row": 6,
                "reason": (
                    "passed_qty + failed_qty 必须等于 inspected_qty"
                    "（当前为 90 + 20 != 100）"
                ),
            },
        ],
    }

    with app.app_context():
        assert InspectionRecord.query.count() == 3


def test_failed_quantity_greater_than_inspected_is_rejected(
    seeded_data,
    app,
):
    csv_text = "\n".join(
        [
            HEADER,
            "B001,外观检查,100,95,20,D01,2026-09-01 10:00:00",
        ]
    )

    with app.app_context():
        result = import_inspection_csv(io.StringIO(csv_text))
        record_count = InspectionRecord.query.count()

    assert result == {
        "success": 0,
        "failed": 1,
        "errors": [
            {
                "row": 2,
                "reason": (
                    "passed_qty + failed_qty 必须等于 inspected_qty"
                    "（当前为 95 + 20 != 100）"
                ),
            }
        ],
    }
    assert record_count == 0


def test_unknown_batch_is_rejected(seeded_data, app):
    csv_text = "\n".join(
        [
            HEADER,
            "MISSING,外观检查,100,95,5,D01,2026-09-01 10:00:00",
        ]
    )

    with app.app_context():
        result = import_inspection_csv(io.StringIO(csv_text))
        record_count = InspectionRecord.query.count()

    assert result == {
        "success": 0,
        "failed": 1,
        "errors": [
            {
                "row": 2,
                "reason": "batch_no “MISSING” 不存在",
            }
        ],
    }
    assert record_count == 0
