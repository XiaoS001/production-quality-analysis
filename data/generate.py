"""生成模拟质检基础数据，并输出正常与异常两份 CSV。"""

from __future__ import annotations

import csv
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

random.seed(42)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import app
from models import Batch, DefectType, InspectionItem, Product, db

DATA_DIR = Path(__file__).resolve().parent
SAMPLE_CSV = DATA_DIR / "sample_inspection.csv"
ERROR_CSV = DATA_DIR / "with_errors.csv"

CSV_FIELDS = (
    "batch_no",
    "item_name",
    "inspected_qty",
    "passed_qty",
    "failed_qty",
    "defect_code",
    "inspected_at",
)

ERROR_ROW_INDEXES = (0, 30, 60, 90, 120, 150)

PRODUCTS = (
    ("P1001", "智能门锁 A 型", "A"),
    ("P1002", "智能门锁 B 型", "B"),
    ("P1003", "摄像头模组", "C"),
)

DEFECT_TYPES = (
    ("D01", "外观划痕"),
    ("D02", "尺寸超差"),
    ("D03", "装配不到位"),
    ("D04", "功能失效"),
)

INSPECTION_ITEMS = (
    "外观检查",
    "尺寸测量",
    "装配检验",
    "通电测试",
    "包装检查",
)

INSPECTION_TIME_START = datetime(2026, 9, 1, 0, 0, 0)
INSPECTION_TIME_END = datetime(2026, 9, 21, 23, 59, 59)


def ensure_base_data() -> tuple[list[str], list[str]]:
    """确保基础数据存在，并返回批次号和检验项名称。"""
    product_by_code: dict[str, Product] = {}

    for code, name, _ in PRODUCTS:
        product = Product.query.filter_by(code=code).first()
        if product is None:
            product = Product(code=code, name=name)
            db.session.add(product)
            db.session.flush()
        product_by_code[code] = product

    for code, name in DEFECT_TYPES:
        defect_type = DefectType.query.filter_by(code=code).first()
        if defect_type is None:
            db.session.add(DefectType(code=code, name=name))

    item_names: list[str] = []
    for item_name in INSPECTION_ITEMS:
        item = InspectionItem.query.filter_by(name=item_name).first()
        if item is None:
            db.session.add(InspectionItem(name=item_name))
        item_names.append(item_name)

    batch_numbers: list[str] = []
    start_date = date(2026, 9, 1)

    for index in range(15):
        product_code, _, product_suffix = PRODUCTS[index % len(PRODUCTS)]
        production_date = start_date + timedelta(days=index)
        batch_sequence = index // len(PRODUCTS) + 1
        batch_no = (
            f"{production_date:%Y%m%d}-{product_suffix}-{batch_sequence:02d}"
        )
        planned_qty = random.randint(500, 2000)

        batch = Batch.query.filter_by(batch_no=batch_no).first()
        if batch is None:
            batch = Batch(
                batch_no=batch_no,
                product_id=product_by_code[product_code].id,
                production_date=production_date,
                planned_qty=planned_qty,
            )
            db.session.add(batch)

        batch_numbers.append(batch_no)

    db.session.commit()
    return batch_numbers, item_names


def random_inspected_at() -> str:
    """返回指定时间范围内随机到秒的时间字符串。"""
    seconds = int(
        (INSPECTION_TIME_END - INSPECTION_TIME_START).total_seconds()
    )
    inspected_at = INSPECTION_TIME_START + timedelta(
        seconds=random.randint(0, seconds)
    )
    return inspected_at.strftime("%Y-%m-%d %H:%M:%S")


def generate_normal_rows(
    count: int,
    batch_numbers: list[str],
    item_names: list[str],
) -> list[dict[str, Any]]:
    """按批次良率生成正常检验记录。"""
    low_yield_batches = set(random.sample(batch_numbers, 2))
    batch_yield_rates = {
        batch_no: (
            random.uniform(0.895, 0.905)
            if batch_no in low_yield_batches
            else random.uniform(0.96, 0.99)
        )
        for batch_no in batch_numbers
    }

    rows: list[dict[str, Any]] = []
    for _ in range(count):
        batch_no = random.choice(batch_numbers)
        inspected_qty = random.randint(50, 300)
        passed_qty = round(inspected_qty * batch_yield_rates[batch_no])
        failed_qty = inspected_qty - passed_qty
        defect_code = (
            random.choice(DEFECT_TYPES)[0] if failed_qty > 0 else ""
        )

        rows.append(
            {
                "batch_no": batch_no,
                "item_name": random.choice(item_names),
                "inspected_qty": inspected_qty,
                "passed_qty": passed_qty,
                "failed_qty": failed_qty,
                "defect_code": defect_code,
                "inspected_at": random_inspected_at(),
            }
        )

    return rows


def inject_error_rows(normal_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保留正常数据，并追加 6 行分别包含指定错误的记录。"""
    error_rows: list[dict[str, Any]] = []

    blank_batch_row = dict(normal_rows[ERROR_ROW_INDEXES[0]])
    blank_batch_row["batch_no"] = ""
    error_rows.append(blank_batch_row)

    text_quantity_row = dict(normal_rows[ERROR_ROW_INDEXES[1]])
    text_quantity_row["inspected_qty"] = "一百"
    error_rows.append(text_quantity_row)

    quantity_mismatch_row = dict(normal_rows[ERROR_ROW_INDEXES[2]])
    quantity_mismatch_row["passed_qty"] = (
        int(quantity_mismatch_row["passed_qty"]) + 1
    )
    error_rows.append(quantity_mismatch_row)

    unknown_batch_row = dict(normal_rows[ERROR_ROW_INDEXES[3]])
    unknown_batch_row["batch_no"] = "20260101-X-99"
    error_rows.append(unknown_batch_row)

    unknown_defect_row = dict(normal_rows[ERROR_ROW_INDEXES[4]])
    unknown_defect_row["defect_code"] = "D99"
    if int(unknown_defect_row["failed_qty"]) == 0:
        unknown_defect_row["failed_qty"] = 1
        unknown_defect_row["passed_qty"] = (
            int(unknown_defect_row["inspected_qty"]) - 1
        )
    error_rows.append(unknown_defect_row)

    negative_quantity_row = dict(normal_rows[ERROR_ROW_INDEXES[5]])
    negative_quantity_row["inspected_qty"] = -100
    negative_quantity_row["passed_qty"] = -96
    negative_quantity_row["failed_qty"] = -4
    error_rows.append(negative_quantity_row)

    return [dict(row) for row in normal_rows] + error_rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """使用 UTF-8 编码写入检验记录 CSV。"""
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    """生成基础数据和两份 CSV。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with app.app_context():
        batch_numbers, item_names = ensure_base_data()

    normal_rows = generate_normal_rows(200, batch_numbers, item_names)
    error_rows = inject_error_rows(normal_rows)

    write_csv(SAMPLE_CSV, normal_rows)
    write_csv(ERROR_CSV, error_rows)

    print(f"产品：{len(PRODUCTS)} 个")
    print(f"批次：{len(batch_numbers)} 个")
    print(f"sample_inspection.csv：{len(normal_rows)} 行（不含表头）")
    print(
        f"with_errors.csv：{len(error_rows)} 行（不含表头，"
        f"其中正常 {len(normal_rows)} 行、错误 {len(ERROR_ROW_INDEXES)} 行）"
    )


if __name__ == "__main__":
    main()
