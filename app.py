"""Flask 应用入口。"""

import csv
import io
import os
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request
from sqlalchemy import func

from annotation import annotation_bp
from metrics import (
    defect_distribution,
    yield_by_batch,
    yield_by_product,
    yield_trend,
)
from models import (
    Batch,
    DefectType,
    InspectionItem,
    InspectionRecord,
    Product,
    QualityIssue,
    db,
)


app = Flask(__name__)

# 每个状态只能推进到下一个状态，禁止跳级。
ISSUE_STATUS_FLOW = {
    "待处理": "处理中",
    "处理中": "已关闭",
}

DATETIME_DISPLAY_FORMAT = "%Y-%m-%d %H:%M:%S"


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime(DATETIME_DISPLAY_FORMAT)

database_path = os.environ.get(
    "QUALITY_DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "quality.db"),
)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{database_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
app.register_blueprint(annotation_bp)


@app.get("/")
def index():
    return "ok"


@app.get("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.get("/api/yield")
def get_yield_metrics():
    return jsonify(
        {
            "by_batch": yield_by_batch(),
            "by_product": yield_by_product(),
            "trend": yield_trend(),
            "defects": defect_distribution(),
        }
    )


@app.get("/api/report/week")
def export_week_report():
    inspected_date = func.date(InspectionRecord.inspected_at)
    rows = (
        db.session.query(
            inspected_date.label("date"),
            func.sum(InspectionRecord.inspected_qty).label(
                "inspected_qty"
            ),
            func.sum(InspectionRecord.passed_qty).label("passed_qty"),
            func.sum(InspectionRecord.failed_qty).label("failed_qty"),
        )
        .filter(InspectionRecord.inspected_at.is_not(None))
        .group_by(inspected_date)
        .order_by(inspected_date.desc())
        .limit(7)
        .all()
    )
    rows = list(reversed(rows))
    report_dates = [str(row.date) for row in rows]

    issue_counts: dict[str, int] = {}
    if report_dates:
        issue_date = func.date(QualityIssue.created_at)
        issue_counts = {
            str(issue_day): int(issue_count)
            for issue_day, issue_count in (
                db.session.query(
                    issue_date,
                    func.count(QualityIssue.id),
                )
                .filter(issue_date.in_(report_dates))
                .group_by(issue_date)
                .all()
            )
        }

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "日期",
        "受检数",
        "合格数",
        "不合格数",
        "良率",
        "新增异常数",
    ])

    for row in rows:
        inspected_qty = int(row.inspected_qty or 0)
        passed_qty = int(row.passed_qty or 0)
        failed_qty = int(row.failed_qty or 0)
        yield_rate = (
            round(passed_qty / inspected_qty * 100, 1)
            if inspected_qty
            else 0.0
        )
        writer.writerow(
            [
                str(row.date),
                inspected_qty,
                passed_qty,
                failed_qty,
                f"{yield_rate:.1f}",
                issue_counts.get(str(row.date), 0),
            ]
        )

    filename = f"quality_report_{datetime.now():%Y%m%d}.csv"
    return Response(
        output.getvalue(),
        content_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


@app.post("/api/issue")
def create_issue():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400

    batch_id_value = payload.get("batch_id")
    if isinstance(batch_id_value, bool) or not isinstance(
        batch_id_value, (int, str)
    ):
        return jsonify({"error": "batch_id 必须是整数"}), 400

    try:
        batch_id = int(batch_id_value)
    except (TypeError, ValueError):
        return jsonify({"error": "batch_id 必须是整数"}), 400

    title_value = payload.get("title")
    if not isinstance(title_value, str) or not title_value.strip():
        return jsonify({"error": "title 不能为空"}), 400

    handler_value = payload.get("handler")
    if handler_value is None:
        handler = None
    elif isinstance(handler_value, str):
        handler = handler_value.strip() or None
    else:
        return jsonify({"error": "handler 必须是字符串或 null"}), 400

    batch = db.session.get(Batch, batch_id)
    if batch is None:
        return jsonify({"error": "batch_id 对应的批次不存在"}), 400

    issue = QualityIssue(
        batch_id=batch.id,
        title=title_value.strip(),
        status="待处理",
        handler=handler,
    )
    db.session.add(issue)
    db.session.commit()

    return jsonify({"id": issue.id})


@app.get("/api/issues")
def list_issues():
    rows = (
        db.session.query(QualityIssue, Batch.batch_no)
        .join(Batch, QualityIssue.batch_id == Batch.id)
        .order_by(QualityIssue.created_at.desc(), QualityIssue.id.desc())
        .all()
    )

    return jsonify([
        {
            "id": issue.id,
            "batch_id": issue.batch_id,
            "batch_no": batch_no,
            "title": issue.title,
            "status": issue.status,
            "handler": issue.handler,
            "created_at": (
                _format_datetime(issue.created_at)
            ),
            "closed_at": (
                _format_datetime(issue.closed_at)
            ),
        }
        for issue, batch_no in rows
    ])


@app.post("/api/issue/<int:issue_id>/advance")
def advance_issue(issue_id: int):
    issue = db.session.get(QualityIssue, issue_id)
    if issue is None:
        return jsonify({"error": "异常不存在"}), 404

    if issue.status == "已关闭":
        return jsonify({"error": "已关闭的异常不能再推进"}), 400

    next_status = ISSUE_STATUS_FLOW.get(issue.status)
    if next_status is None:
        return jsonify({"error": "异常状态非法，无法推进"}), 400

    issue.status = next_status
    if next_status == "已关闭":
        issue.closed_at = datetime.now()

    db.session.commit()

    return jsonify({"id": issue.id, "status": issue.status})


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(debug=True, port=5001)
