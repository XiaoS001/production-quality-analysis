"""质量管理系统的 SQLAlchemy 数据模型定义。"""

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


class Product(db.Model):
    __tablename__ = "product"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(128), nullable=False)
    model = db.Column(db.String(128))
    created_at = db.Column(db.DateTime, default=datetime.now)


class DefectType(db.Model):
    __tablename__ = "defect_type"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.now)


class Batch(db.Model):
    __tablename__ = "batch"

    id = db.Column(db.Integer, primary_key=True)
    batch_no = db.Column(db.String(64), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    production_date = db.Column(db.Date)
    planned_qty = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.now)


class InspectionItem(db.Model):
    __tablename__ = "inspection_item"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    standard = db.Column(db.String(255))
    unit = db.Column(db.String(32))
    created_at = db.Column(db.DateTime, default=datetime.now)


class InspectionRecord(db.Model):
    __tablename__ = "inspection_record"

    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batch.id"), nullable=False)
    item_id = db.Column(
        db.Integer,
        db.ForeignKey("inspection_item.id"),
        nullable=False,
    )
    inspected_qty = db.Column(db.Integer, nullable=False)
    passed_qty = db.Column(db.Integer, nullable=False)
    failed_qty = db.Column(db.Integer, nullable=False)
    defect_type_id = db.Column(db.Integer, db.ForeignKey("defect_type.id"))
    inspected_at = db.Column(db.DateTime, default=datetime.now)


class QualityIssue(db.Model):
    __tablename__ = "quality_issue"

    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("batch.id"), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(32), default="待处理")
    handler = db.Column(db.String(128))
    created_at = db.Column(db.DateTime, default=datetime.now)
    closed_at = db.Column(db.DateTime)


class DefectImage(db.Model):
    __tablename__ = "defect_image"
    __table_args__ = (
        db.CheckConstraint(
            "is_synthetic = 1",
            name="ck_defect_image_is_synthetic_true",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    file_name = db.Column(db.String(255), nullable=False)
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    batch_id = db.Column(db.Integer, db.ForeignKey("batch.id"))
    label_status = db.Column(
        db.String(32),
        nullable=False,
        default="未标注",
    )
    labeled_by = db.Column(db.String(128))
    labeled_at = db.Column(db.DateTime)
    reviewed_by = db.Column(db.String(128))
    reviewed_at = db.Column(db.DateTime)
    is_synthetic = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
    )

    annotations = db.relationship(
        "DefectAnnotation",
        back_populates="image",
        cascade="all, delete-orphan",
        order_by="DefectAnnotation.id",
    )


class DefectAnnotation(db.Model):
    __tablename__ = "defect_annotation"

    id = db.Column(db.Integer, primary_key=True)
    image_id = db.Column(
        db.Integer,
        db.ForeignKey("defect_image.id"),
        nullable=False,
    )
    defect_type_id = db.Column(
        db.Integer,
        db.ForeignKey("defect_type.id"),
        nullable=False,
    )
    x = db.Column(db.Integer, nullable=False)
    y = db.Column(db.Integer, nullable=False)
    w = db.Column(db.Integer, nullable=False)
    h = db.Column(db.Integer, nullable=False)
    annotator = db.Column(db.String(128))
    created_at = db.Column(db.DateTime, default=datetime.now)

    image = db.relationship("DefectImage", back_populates="annotations")
    defect_type = db.relationship("DefectType")


class AppUser(db.Model):
    """系统用户（用于角色权限：管理员 / 质检员 / 只读）。"""

    __tablename__ = "app_user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), nullable=False, unique=True)
    display_name = db.Column(db.String(128))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(32), nullable=False, default="viewer")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
