"""检验记录 CSV 导入与校验。"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from models import Batch, DefectType, InspectionItem, InspectionRecord, db

REQUIRED_FIELDS = (
    "batch_no",
    "item_name",
    "inspected_qty",
    "passed_qty",
    "failed_qty",
)

QUANTITY_FIELDS = (
    "inspected_qty",
    "passed_qty",
    "failed_qty",
)

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _as_text_stream(file_stream: Any) -> tuple[Any, bool]:
    """把二进制文件流转成 UTF-8 文本流，并标记是否由本函数包装。"""
    sample = file_stream.read(0)
    if isinstance(sample, bytes):
        return (
            io.TextIOWrapper(file_stream, encoding="utf-8-sig", newline=""),
            True,
        )
    return file_stream, False


def _normalize_fieldnames(reader: csv.DictReader) -> None:
    """移除表头可能携带的 BOM 和首尾空白。"""
    if reader.fieldnames is None:
        raise ValueError("CSV 为空，缺少表头")

    reader.fieldnames = [
        field_name.lstrip("\ufeff").strip() for field_name in reader.fieldnames
    ]

    missing_fields = [
        field_name
        for field_name in REQUIRED_FIELDS
        if field_name not in reader.fieldnames
    ]
    if missing_fields:
        raise ValueError(f"CSV 缺少必填列：{', '.join(missing_fields)}")


def _row_values(
    row: dict[str | None, str | None],
    fieldnames: list[str],
) -> dict[str, str]:
    """清理当前 CSV 行中的字符串值。"""
    values: dict[str, str] = {}
    for field_name in fieldnames:
        value = row.get(field_name)
        values[field_name] = value.strip() if isinstance(value, str) else ""
    return values


def _parse_non_negative_integer(
    value: str,
    field_name: str,
) -> tuple[int | None, str | None]:
    """解析非负整数字段，并返回中文错误原因。"""
    try:
        number = int(value)
    except ValueError:
        return None, f"字段 {field_name} 必须是整数，当前值为“{value}”"

    if number < 0:
        return None, (
            f"字段 {field_name} 必须大于等于 0，当前值为 {number}"
        )
    return number, None


def _validate_format(
    values: dict[str, str],
) -> tuple[dict[str, int] | None, datetime | None, list[str]]:
    """执行不查库的格式校验，并解析数量和时间字段。"""
    errors: list[str] = []

    for field_name in REQUIRED_FIELDS:
        if not values.get(field_name, ""):
            errors.append(f"必填字段 {field_name} 不能为空")

    quantity_values: dict[str, int] = {}
    for field_name in QUANTITY_FIELDS:
        value = values.get(field_name, "")
        if not value:
            continue

        number, error = _parse_non_negative_integer(value, field_name)
        if error:
            errors.append(error)
        else:
            quantity_values[field_name] = number

    if (
        len(quantity_values) == len(QUANTITY_FIELDS)
        and quantity_values["passed_qty"] + quantity_values["failed_qty"]
        != quantity_values["inspected_qty"]
    ):
        errors.append(
            "passed_qty + failed_qty 必须等于 inspected_qty"
            f"（当前为 {quantity_values['passed_qty']} + "
            f"{quantity_values['failed_qty']} != "
            f"{quantity_values['inspected_qty']}）"
        )

    inspected_at: datetime | None = None
    inspected_at_value = values.get("inspected_at", "")
    if inspected_at_value:
        try:
            inspected_at = datetime.strptime(
                inspected_at_value,
                DATETIME_FORMAT,
            )
        except ValueError:
            errors.append(
                "字段 inspected_at 格式必须为 YYYY-MM-DD HH:MM:SS，"
                f"当前值为“{inspected_at_value}”"
            )

    if errors or len(quantity_values) != len(QUANTITY_FIELDS):
        return None, inspected_at, errors

    return quantity_values, inspected_at, errors


def import_inspection_csv(file_stream: Any) -> dict[str, Any]:
    """逐行导入检验记录 CSV，返回成功、失败数量和行级错误。"""
    text_stream, should_detach = _as_text_stream(file_stream)
    success_count = 0
    errors: list[dict[str, Any]] = []

    try:
        reader = csv.DictReader(text_stream)
        _normalize_fieldnames(reader)
        fieldnames = list(reader.fieldnames or [])

        batch_by_no = {
            batch.batch_no: batch
            for batch in Batch.query.all()
        }
        item_by_name = {
            item.name: item
            for item in InspectionItem.query.all()
        }
        defect_by_code = {
            defect.code: defect
            for defect in DefectType.query.all()
        }

        for row in reader:
            row_number = reader.line_num
            values = _row_values(row, fieldnames)

            quantity_values, inspected_at, format_errors = _validate_format(
                values
            )
            if format_errors:
                errors.append(
                    {
                        "row": row_number,
                        "reason": "；".join(format_errors),
                    }
                )
                continue

            reference_errors: list[str] = []
            batch_no = values["batch_no"]
            item_name = values["item_name"]
            defect_code = values.get("defect_code", "")

            batch = batch_by_no.get(batch_no)
            if batch is None:
                reference_errors.append(
                    f"batch_no “{batch_no}” 不存在"
                )

            item = item_by_name.get(item_name)
            if item is None:
                reference_errors.append(
                    f"item_name “{item_name}” 不存在"
                )

            defect_type = None
            if defect_code:
                defect_type = defect_by_code.get(defect_code)
                if defect_type is None:
                    reference_errors.append(
                        f"defect_code “{defect_code}” 不存在"
                    )

            if reference_errors:
                errors.append(
                    {
                        "row": row_number,
                        "reason": "；".join(reference_errors),
                    }
                )
                continue

            if quantity_values is None or batch is None or item is None:
                errors.append(
                    {
                        "row": row_number,
                        "reason": "校验结果不完整，无法写入数据库",
                    }
                )
                continue

            db.session.add(
                InspectionRecord(
                    batch_id=batch.id,
                    item_id=item.id,
                    inspected_qty=quantity_values["inspected_qty"],
                    passed_qty=quantity_values["passed_qty"],
                    failed_qty=quantity_values["failed_qty"],
                    defect_type_id=(
                        defect_type.id if defect_type is not None else None
                    ),
                    inspected_at=inspected_at,
                )
            )
            success_count += 1

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    finally:
        if should_detach:
            text_stream.detach()

    return {
        "success": success_count,
        "failed": len(errors),
        "errors": errors,
    }
