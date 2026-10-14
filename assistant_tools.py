"""AI 助手可调用的工具集。

每个工具都**直接查询真实数据库**，并复用 ``metrics.py`` 的统计口径，
保证「AI 助手说的数字」与「看板显示的数字」来自同一套逻辑。

工具返回统一结构::

    {
        "tool": "query_yield_by_batch",
        "params": {...},
        "row_count": 3,
        "summary": "3 个批次，整体良率 96.2%",
        "data": [...],
    }
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func

from metrics import (
    defect_distribution as metrics_defect_distribution,
    yield_by_batch as metrics_yield_by_batch,
    yield_by_product as metrics_yield_by_product,
    yield_trend as metrics_yield_trend,
)
from models import (
    Batch,
    DefectAnnotation,
    DefectImage,
    DefectType,
    InspectionItem,
    InspectionRecord,
    Product,
    QualityIssue,
    db,
)

MAX_LIMIT = 50


def _clamp(value: Any, default: int, low: int = 1, high: int = MAX_LIMIT) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _result(
    name: str,
    params: dict[str, Any],
    data: list[dict[str, Any]],
    summary: str,
) -> dict[str, Any]:
    return {
        "tool": name,
        "params": params,
        "row_count": len(data),
        "summary": summary,
        "data": data,
    }


def _rate(inspected: int, passed: int) -> float:
    if inspected <= 0:
        return 0.0
    return round(passed / inspected * 100, 1)


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #
def query_overall_yield(limit: int = 5) -> dict[str, Any]:
    """整体质量总览：受检/合格/不合格总量、整体良率、批次概览。"""
    limit = _clamp(limit, 5)
    totals = (
        db.session.query(
            func.sum(InspectionRecord.inspected_qty),
            func.sum(InspectionRecord.passed_qty),
            func.sum(InspectionRecord.failed_qty),
            func.count(InspectionRecord.id),
        )
        .one()
    )
    inspected = int(totals[0] or 0)
    passed = int(totals[1] or 0)
    failed = int(totals[2] or 0)
    records = int(totals[3] or 0)

    batches = metrics_yield_by_batch()[:limit]
    issues_open = (
        db.session.query(func.count(QualityIssue.id))
        .filter(QualityIssue.status != "已关闭")
        .scalar()
        or 0
    )

    data = [
        {
            "inspected_qty": inspected,
            "passed_qty": passed,
            "failed_qty": failed,
            "record_count": records,
            "overall_yield_rate": _rate(inspected, passed),
            "open_issue_count": int(issues_open),
        },
        *batches,
    ]
    if records == 0:
        summary = "当前数据库还没有检验记录，无法计算良率。"
    else:
        summary = (
            f"共 {records} 条检验记录，累计受检 {inspected} 件，"
            f"整体良率 {_rate(inspected, passed)}%，"
            f"未关闭质量异常 {int(issues_open)} 条。"
        )
    return _result(
        "query_overall_yield",
        {"limit": limit},
        data,
        summary,
    )


def query_yield_by_batch(
    batch_no: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """按批次查询良率，可指定批次号精确过滤。"""
    limit = _clamp(limit, 10)
    rows = metrics_yield_by_batch()
    if isinstance(batch_no, str) and batch_no.strip():
        keyword = batch_no.strip().lower()
        rows = [row for row in rows if keyword in str(row["batch_no"]).lower()]
    rows = rows[:limit]

    if not rows:
        summary = (
            f"没有找到批次 {batch_no!r} 的检验数据。"
            if batch_no
            else "当前没有可用批次数据。"
        )
    else:
        worst = min(rows, key=lambda item: item["yield_rate"])
        summary = (
            f"命中 {len(rows)} 个批次；良率最低的是 {worst['batch_no']}"
            f"（{worst['yield_rate']}%，不合格 {worst['failed_qty']} 件）。"
        )
    return _result(
        "query_yield_by_batch",
        {"batch_no": batch_no, "limit": limit},
        rows,
        summary,
    )


def query_yield_by_product(limit: int = 10) -> dict[str, Any]:
    """按产品查询良率。"""
    limit = _clamp(limit, 10)
    rows = metrics_yield_by_product()[:limit]
    if not rows:
        summary = "当前没有可用的产品良率数据。"
    else:
        worst = min(rows, key=lambda item: item["yield_rate"])
        summary = (
            f"共 {len(rows)} 个产品；良率最低的是 {worst['product_name']}"
            f"（{worst['yield_rate']}%）。"
        )
    return _result("query_yield_by_product", {"limit": limit}, rows, summary)


def query_defect_distribution(limit: int = 10) -> dict[str, Any]:
    """按缺陷类型统计不合格数量与占比。"""
    limit = _clamp(limit, 10)
    rows = metrics_defect_distribution()[:limit]
    total = sum(item["count"] for item in rows)
    data = [
        {
            **item,
            "ratio": (
                round(item["count"] / total * 100, 1) if total else 0.0
            ),
        }
        for item in rows
    ]
    if not data:
        summary = "当前没有不合格品记录，缺陷分布为空。"
    else:
        top = data[0]
        summary = (
            f"不合格合计 {total} 件，占比最高的是 {top['defect_name']}"
            f"（{top['count']} 件，{top['ratio']}%）。"
        )
    return _result("query_defect_distribution", {"limit": limit}, data, summary)


def query_yield_trend(days: int = 7) -> dict[str, Any]:
    """按天查询良率趋势。"""
    days = _clamp(days, 7, 1, 60)
    rows = metrics_yield_trend(days)
    if not rows:
        summary = "最近没有可用于趋势统计的检验记录。"
    else:
        latest = rows[-1]
        summary = (
            f"最近 {len(rows)} 个有数据的工作日，最新一天"
            f"（{latest['date']}）良率 {latest['yield_rate']}%。"
        )
    return _result("query_yield_trend", {"days": days}, rows, summary)


def query_quality_issues(
    status: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """查询质量异常清单（待处理 / 处理中 / 已关闭）。"""
    limit = _clamp(limit, 10)
    query = (
        db.session.query(QualityIssue, Batch.batch_no)
        .join(Batch, QualityIssue.batch_id == Batch.id)
    )
    if isinstance(status, str) and status.strip():
        query = query.filter(QualityIssue.status == status.strip())
    rows = (
        query.order_by(
            QualityIssue.created_at.desc(), QualityIssue.id.desc()
        )
        .limit(limit)
        .all()
    )

    counter = dict(
        db.session.query(QualityIssue.status, func.count(QualityIssue.id))
        .group_by(QualityIssue.status)
        .all()
    )
    data = [
        {
            "id": issue.id,
            "batch_no": batch_no,
            "title": issue.title,
            "status": issue.status,
            "handler": issue.handler,
            "created_at": (
                issue.created_at.strftime("%Y-%m-%d %H:%M:%S")
                if issue.created_at
                else None
            ),
        }
        for issue, batch_no in rows
    ]
    status_text = "、".join(
        f"{name} {count}" for name, count in sorted(counter.items())
    ) or "无"
    summary = (
        f"异常状态统计：{status_text}；"
        f"本次列出 {len(data)} 条"
        + (f"（筛选状态：{status}）" if status else "")
        + "。"
    )
    return _result(
        "query_quality_issues",
        {"status": status, "limit": limit},
        data,
        summary,
    )


def query_annotation_progress() -> dict[str, Any]:
    """缺陷图像标注进度（未标注 / 已标注 / 已复核）与标注框数量。"""
    counts = dict(
        db.session.query(DefectImage.label_status, func.count(DefectImage.id))
        .group_by(DefectImage.label_status)
        .all()
    )
    total_images = sum(counts.values())
    total_annotations = (
        db.session.query(func.count(DefectAnnotation.id)).scalar() or 0
    )
    synthetic_images = (
        db.session.query(func.count(DefectImage.id))
        .filter(DefectImage.is_synthetic.is_(True))
        .scalar()
        or 0
    )

    data = [
        {
            "total_images": total_images,
            "未标注": int(counts.get("未标注", 0)),
            "已标注": int(counts.get("已标注", 0)),
            "已复核": int(counts.get("已复核", 0)),
            "annotation_count": int(total_annotations),
            "synthetic_images": int(synthetic_images),
        }
    ]
    if total_images == 0:
        summary = "当前没有缺陷图像，标注进度为空。"
    else:
        summary = (
            f"共 {total_images} 张缺陷图像（合成图 {int(synthetic_images)} 张）："
            f"未标注 {int(counts.get('未标注', 0))}、"
            f"已标注 {int(counts.get('已标注', 0))}、"
            f"已复核 {int(counts.get('已复核', 0))}；"
            f"标注框合计 {int(total_annotations)} 个。"
        )
    return _result("query_annotation_progress", {}, data, summary)


def query_recent_inspections(days: int = 7, limit: int = 20) -> dict[str, Any]:
    """最近若干天的检验记录明细。"""
    days = _clamp(days, 7, 1, 60)
    limit = _clamp(limit, 20)
    since = datetime.now() - timedelta(days=days)

    rows = (
        db.session.query(
            InspectionRecord,
            Batch.batch_no,
            Product.name,
            InspectionItem.name,
            DefectType.name,
        )
        .join(Batch, InspectionRecord.batch_id == Batch.id)
        .join(Product, Batch.product_id == Product.id)
        .join(
            InspectionItem,
            InspectionRecord.item_id == InspectionItem.id,
        )
        .outerjoin(DefectType, InspectionRecord.defect_type_id == DefectType.id)
        .filter(
            InspectionRecord.inspected_at.is_not(None),
            InspectionRecord.inspected_at >= since,
        )
        .order_by(InspectionRecord.inspected_at.desc())
        .limit(limit)
        .all()
    )

    data = [
        {
            "batch_no": batch_no,
            "product_name": product_name,
            "item_name": item_name,
            "inspected_qty": record.inspected_qty,
            "passed_qty": record.passed_qty,
            "failed_qty": record.failed_qty,
            "yield_rate": _rate(record.inspected_qty, record.passed_qty),
            "defect_name": defect_name,
            "inspected_at": (
                record.inspected_at.strftime("%Y-%m-%d %H:%M:%S")
                if record.inspected_at
                else None
            ),
        }
        for record, batch_no, product_name, item_name, defect_name in rows
    ]
    if not data:
        summary = f"最近 {days} 天没有检验记录。"
    else:
        failed = sum(item["failed_qty"] for item in data)
        summary = (
            f"最近 {days} 天共 {len(data)} 条检验记录，"
            f"不合格合计 {failed} 件。"
        )
    return _result(
        "query_recent_inspections",
        {"days": days, "limit": limit},
        data,
        summary,
    )


# --------------------------------------------------------------------------- #
# 注册表与执行入口
# --------------------------------------------------------------------------- #
TOOL_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "query_overall_yield": query_overall_yield,
    "query_yield_by_batch": query_yield_by_batch,
    "query_yield_by_product": query_yield_by_product,
    "query_defect_distribution": query_defect_distribution,
    "query_yield_trend": query_yield_trend,
    "query_quality_issues": query_quality_issues,
    "query_annotation_progress": query_annotation_progress,
    "query_recent_inspections": query_recent_inspections,
}

# 给大模型看的工具说明（OpenAI function calling 格式）
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_overall_yield",
            "description": "整体质量总览：累计受检/合格/不合格数量、整体良率、未关闭异常数、批次概览。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "返回的批次条数，默认 5，最大 50",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_yield_by_batch",
            "description": "按批次查询良率；可传入批次号做模糊过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "batch_no": {
                        "type": "string",
                        "description": "批次号，例如 B001；不传则返回全部批次",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回条数，默认 10",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_yield_by_product",
            "description": "按产品统计良率与不合格数量。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "最多返回条数，默认 10",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_defect_distribution",
            "description": "按缺陷类型统计不合格数量与占比（不良分布）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "最多返回条数，默认 10",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_yield_trend",
            "description": "按天查询良率趋势（最近若干个有数据的工作日）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "天数，默认 7，范围 1-60",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_quality_issues",
            "description": "查询质量异常清单及状态统计（待处理 / 处理中 / 已关闭）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "状态过滤，例如 待处理；不传则返回全部",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回条数，默认 10",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_annotation_progress",
            "description": "缺陷图像标注进度统计（未标注 / 已标注 / 已复核、标注框数量）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_recent_inspections",
            "description": "最近若干天的检验记录明细。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "天数，默认 7，范围 1-60",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回条数，默认 20",
                    },
                },
            },
        },
    },
]


def execute_tool(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """按白名单执行工具；未知工具或参数异常都转成结构化错误，不抛出 500。"""
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        return {
            "tool": name,
            "params": params or {},
            "row_count": 0,
            "summary": f"未知工具：{name}",
            "data": [],
            "error": "unknown_tool",
        }

    safe_params = params if isinstance(params, dict) else {}
    try:
        return tool(**safe_params)
    except TypeError as exc:
        return {
            "tool": name,
            "params": safe_params,
            "row_count": 0,
            "summary": f"工具参数不正确：{exc}",
            "data": [],
            "error": "bad_parameters",
        }
    except Exception as exc:  # noqa: BLE001 - 工具失败必须降级而不是 500
        return {
            "tool": name,
            "params": safe_params,
            "row_count": 0,
            "summary": f"工具执行失败：{exc}",
            "data": [],
            "error": "tool_failed",
        }
