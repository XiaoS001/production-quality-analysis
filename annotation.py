"""缺陷图像标注、复核与进度统计接口。"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    current_app,
    jsonify,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from sqlalchemy import func

from auth import PERM_REVIEW, PERM_WRITE, require_permission
from models import DefectAnnotation, DefectImage, DefectType, db

annotation_bp = Blueprint("annotation", __name__)

LABEL_STATUS_UNLABELED = "未标注"
LABEL_STATUS_LABELED = "已标注"
LABEL_STATUS_REVIEWED = "已复核"

DATETIME_DISPLAY_FORMAT = "%Y-%m-%d %H:%M:%S"


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime(DATETIME_DISPLAY_FORMAT)


def _required_text(
    payload: dict[str, Any],
    field_name: str,
    label: str,
    *,
    max_length: int = 128,
) -> tuple[str | None, str | None]:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        return None, f"{label}不能为空"
    value = value.strip()
    if len(value) > max_length:
        return None, f"{label}不能超过 {max_length} 个字符"
    return value, None


def _serialize_annotation(
    annotation: DefectAnnotation,
) -> dict[str, Any]:
    defect_type = annotation.defect_type
    return {
        "id": annotation.id,
        "image_id": annotation.image_id,
        "defect_type_id": annotation.defect_type_id,
        "defect_type_code": defect_type.code,
        "defect_type_name": defect_type.name,
        "x": annotation.x,
        "y": annotation.y,
        "w": annotation.w,
        "h": annotation.h,
        "annotator": annotation.annotator,
        "created_at": _format_datetime(annotation.created_at),
    }


def _serialize_image(image: DefectImage) -> dict[str, Any]:
    return {
        "id": image.id,
        "file_name": image.file_name,
        "width": image.width,
        "height": image.height,
        "batch_id": image.batch_id,
        "label_status": image.label_status,
        "labeled_by": image.labeled_by,
        "labeled_at": _format_datetime(image.labeled_at),
        "reviewed_by": image.reviewed_by,
        "reviewed_at": _format_datetime(image.reviewed_at),
        "is_synthetic": bool(image.is_synthetic),
        "image_url": url_for(
            "annotation.get_image_file",
            image_id=image.id,
        ),
        "annotations": [
            _serialize_annotation(annotation)
            for annotation in image.annotations
        ],
    }


def _as_int(
    value: Any,
    field_name: str,
    *,
    minimum: int | None = None,
) -> tuple[int | None, str | None]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"{field_name} 必须是整数"
    if minimum is not None and value < minimum:
        return None, f"{field_name} 必须大于等于 {minimum}"
    return value, None


def _validate_annotations(
    payload: dict[str, Any],
    image: DefectImage,
) -> tuple[list[dict[str, int]] | None, str | None]:
    raw_annotations = payload.get("annotations")
    if not isinstance(raw_annotations, list):
        return None, "annotations 必须是数组"
    if len(raw_annotations) > 1000:
        return None, "annotations 最多允许 1000 个标注框"

    defect_type_ids = {
        defect_type_id
        for (defect_type_id,) in db.session.query(DefectType.id).all()
    }
    annotations: list[dict[str, int]] = []

    for index, raw_annotation in enumerate(raw_annotations):
        prefix = f"annotations[{index}]"
        if not isinstance(raw_annotation, dict):
            return None, f"{prefix} 必须是 JSON 对象"

        defect_type_id, error = _as_int(
            raw_annotation.get("defect_type_id"),
            f"{prefix}.defect_type_id",
            minimum=1,
        )
        if error:
            return None, error
        if defect_type_id not in defect_type_ids:
            return None, f"{prefix}.defect_type_id 对应的缺陷类型不存在"

        coordinates: dict[str, int] = {}
        for field_name in ("x", "y", "w", "h"):
            minimum = 1 if field_name in {"w", "h"} else 0
            coordinate, coordinate_error = _as_int(
                raw_annotation.get(field_name),
                f"{prefix}.{field_name}",
                minimum=minimum,
            )
            if coordinate_error:
                return None, coordinate_error
            coordinates[field_name] = coordinate

        if coordinates["x"] + coordinates["w"] > image.width:
            return None, f"{prefix} 的横向边界超出图片范围"
        if coordinates["y"] + coordinates["h"] > image.height:
            return None, f"{prefix} 的纵向边界超出图片范围"

        annotations.append(
            {
                "defect_type_id": defect_type_id,
                **coordinates,
            }
        )

    return annotations, None


@annotation_bp.get("/annotation")
def annotation_page():
    return render_template("annotation.html")


@annotation_bp.get("/api/annotation/defect-types")
def list_defect_types():
    rows = DefectType.query.order_by(DefectType.id.asc()).all()
    return jsonify([
        {
            "id": defect_type.id,
            "code": defect_type.code,
            "name": defect_type.name,
        }
        for defect_type in rows
    ])


@annotation_bp.get("/api/annotation/images")
def list_annotation_images():
    images = DefectImage.query.order_by(DefectImage.id.asc()).all()
    return jsonify({
        "is_synthetic": True,
        "images": [_serialize_image(image) for image in images],
    })


@annotation_bp.get("/api/annotation/image/<int:image_id>")
def get_annotation_image(image_id: int):
    image = db.session.get(DefectImage, image_id)
    if image is None:
        return jsonify({"error": "图片不存在"}), 404
    return jsonify(_serialize_image(image))


@annotation_bp.get("/api/annotation/image/<int:image_id>/file")
def get_image_file(image_id: int):
    image = db.session.get(DefectImage, image_id)
    if image is None:
        return jsonify({"error": "图片不存在"}), 404

    dataset_dir = os.environ.get(
        "SYNTH_DATASET_DIR",
        str(
            Path(current_app.root_path)
            / "data"
            / "defect_synth"
        ),
    )
    images_dir = Path(dataset_dir) / "images"
    file_name = Path(image.file_name).name
    if file_name != image.file_name:
        return jsonify({"error": "图片文件名非法"}), 400
    return send_from_directory(images_dir, file_name)


@annotation_bp.post("/api/annotation/image/<int:image_id>")
@require_permission(PERM_WRITE)
def save_annotations(image_id: int):
    image = db.session.get(DefectImage, image_id)
    if image is None:
        return jsonify({"error": "图片不存在"}), 404
    if image.label_status == LABEL_STATUS_REVIEWED:
        return jsonify({
            "error": "已复核图片不能直接修改，请先撤销复核"
        }), 409

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400

    labeled_by, error = _required_text(
        payload,
        "labeled_by",
        "标注人",
    )
    if error:
        return jsonify({"error": error}), 400

    annotations, error = _validate_annotations(payload, image)
    if error:
        return jsonify({"error": error}), 400

    image.annotations.clear()
    for annotation_data in annotations or []:
        image.annotations.append(
            DefectAnnotation(
                defect_type_id=annotation_data["defect_type_id"],
                x=annotation_data["x"],
                y=annotation_data["y"],
                w=annotation_data["w"],
                h=annotation_data["h"],
                annotator=labeled_by,
            )
        )

    image.label_status = LABEL_STATUS_LABELED
    image.labeled_by = labeled_by
    image.labeled_at = datetime.now()
    image.reviewed_by = None
    image.reviewed_at = None
    db.session.commit()

    return jsonify(_serialize_image(image))


@annotation_bp.post("/api/annotation/image/<int:image_id>/review")
@require_permission(PERM_REVIEW)
def review_annotations(image_id: int):
    image = db.session.get(DefectImage, image_id)
    if image is None:
        return jsonify({"error": "图片不存在"}), 404
    if image.label_status == LABEL_STATUS_REVIEWED:
        return jsonify({"error": "图片已经复核"}), 409
    if image.label_status != LABEL_STATUS_LABELED:
        return jsonify({"error": "图片尚未标注，不能复核"}), 400

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400
    reviewed_by, error = _required_text(
        payload,
        "reviewed_by",
        "复核人",
    )
    if error:
        return jsonify({"error": error}), 400

    image.label_status = LABEL_STATUS_REVIEWED
    image.reviewed_by = reviewed_by
    image.reviewed_at = datetime.now()
    db.session.commit()

    return jsonify(_serialize_image(image))


@annotation_bp.get("/api/annotation/stats")
def annotation_stats():
    total = DefectImage.query.count()
    labeled = DefectImage.query.filter_by(
        label_status=LABEL_STATUS_LABELED
    ).count()
    reviewed = DefectImage.query.filter_by(
        label_status=LABEL_STATUS_REVIEWED
    ).count()
    unlabeled = DefectImage.query.filter_by(
        label_status=LABEL_STATUS_UNLABELED
    ).count()

    rows = (
        db.session.query(
            DefectType.code.label("code"),
            DefectType.name.label("name"),
            func.count(DefectAnnotation.id).label("count"),
        )
        .join(
            DefectAnnotation,
            DefectAnnotation.defect_type_id == DefectType.id,
        )
        .group_by(DefectType.id, DefectType.code, DefectType.name)
        .order_by(DefectType.id.asc())
        .all()
    )
    defect_distribution = [
        {
            "defect_type_code": row.code,
            "defect_type_name": row.name,
            "count": int(row.count or 0),
        }
        for row in rows
    ]

    return jsonify({
        "is_synthetic": True,
        "total": total,
        "labeled": labeled,
        "reviewed": reviewed,
        "unlabeled": unlabeled,
        "defect_distribution": defect_distribution,
    })
