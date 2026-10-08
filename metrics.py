"""质量数据统计函数。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func

from models import Batch, DefectType, InspectionRecord, Product, db


def _yield_rate(inspected_qty: int, passed_qty: int) -> float:
    """计算良率百分比，并处理除数为零的情况。"""
    if inspected_qty == 0:
        return 0.0
    return round(passed_qty / inspected_qty * 100, 1)


def yield_by_batch() -> list[dict[str, Any]]:
    """按批次统计检验数量和良率。"""
    rows = (
        db.session.query(
            Batch.batch_no.label("batch_no"),
            Product.name.label("product_name"),
            func.sum(InspectionRecord.inspected_qty).label("inspected_qty"),
            func.sum(InspectionRecord.passed_qty).label("passed_qty"),
            func.sum(InspectionRecord.failed_qty).label("failed_qty"),
        )
        .join(InspectionRecord, InspectionRecord.batch_id == Batch.id)
        .join(Product, Batch.product_id == Product.id)
        .group_by(Batch.id, Batch.batch_no, Product.id, Product.name)
        .order_by(Batch.batch_no.asc())
        .all()
    )

    return [
        {
            "batch_no": row.batch_no,
            "product_name": row.product_name,
            "inspected_qty": int(row.inspected_qty or 0),
            "passed_qty": int(row.passed_qty or 0),
            "failed_qty": int(row.failed_qty or 0),
            "yield_rate": _yield_rate(
                int(row.inspected_qty or 0),
                int(row.passed_qty or 0),
            ),
        }
        for row in rows
    ]


def yield_by_product() -> list[dict[str, Any]]:
    """按产品统计检验数量和良率。"""
    rows = (
        db.session.query(
            Product.name.label("product_name"),
            func.sum(InspectionRecord.inspected_qty).label("inspected_qty"),
            func.sum(InspectionRecord.passed_qty).label("passed_qty"),
            func.sum(InspectionRecord.failed_qty).label("failed_qty"),
        )
        .join(Batch, Batch.product_id == Product.id)
        .join(InspectionRecord, InspectionRecord.batch_id == Batch.id)
        .group_by(Product.id, Product.name)
        .order_by(Product.name.asc())
        .all()
    )

    return [
        {
            "product_name": row.product_name,
            "inspected_qty": int(row.inspected_qty or 0),
            "passed_qty": int(row.passed_qty or 0),
            "failed_qty": int(row.failed_qty or 0),
            "yield_rate": _yield_rate(
                int(row.inspected_qty or 0),
                int(row.passed_qty or 0),
            ),
        }
        for row in rows
    ]


def yield_trend(days: int = 30) -> list[dict[str, Any]]:
    """按天统计最近若干个有数据自然日的整体良率。"""
    if days <= 0:
        return []

    inspected_date = func.date(InspectionRecord.inspected_at)
    rows = (
        db.session.query(
            inspected_date.label("date"),
            func.sum(InspectionRecord.inspected_qty).label("inspected_qty"),
            func.sum(InspectionRecord.passed_qty).label("passed_qty"),
        )
        .filter(InspectionRecord.inspected_at.is_not(None))
        .group_by(inspected_date)
        .order_by(inspected_date.desc())
        .limit(int(days))
        .all()
    )

    trend: list[dict[str, Any]] = []
    for row in reversed(rows):
        inspected_qty = int(row.inspected_qty or 0)
        passed_qty = int(row.passed_qty or 0)
        trend.append(
            {
                "date": str(row.date),
                "yield_rate": _yield_rate(inspected_qty, passed_qty),
                "inspected_qty": inspected_qty,
            }
        )
    return trend


def defect_distribution() -> list[dict[str, Any]]:
    """按缺陷类型统计不合格数量。"""
    failed_qty = func.sum(InspectionRecord.failed_qty)
    rows = (
        db.session.query(
            DefectType.name.label("defect_name"),
            failed_qty.label("count"),
        )
        .join(
            InspectionRecord,
            InspectionRecord.defect_type_id == DefectType.id,
        )
        .filter(InspectionRecord.failed_qty > 0)
        .group_by(DefectType.id, DefectType.name)
        .order_by(failed_qty.desc(), DefectType.name.asc())
        .all()
    )

    return [
        {
            "defect_name": row.defect_name,
            "count": int(row.count or 0),
        }
        for row in rows
    ]
