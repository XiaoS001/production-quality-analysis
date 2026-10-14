"""pytest 全局配置与数据库隔离。"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_DATABASE_PATH = PROJECT_ROOT / "quality.db"

_TEMP_DIRECTORY_PATH = Path(
    tempfile.mkdtemp(prefix="xm02-tests-")
)
TEST_DATABASE_PATH = _TEMP_DIRECTORY_PATH / "test_quality.db"

os.environ["QUALITY_DATABASE_PATH"] = str(TEST_DATABASE_PATH)

assert TEST_DATABASE_PATH.resolve() != REAL_DATABASE_PATH.resolve()

from app import app as flask_app
from auth import (
    ROLE_ADMIN,
    ROLE_INSPECTOR,
    ROLE_VIEWER,
    ensure_default_users,
)
from models import Batch, DefectType, InspectionItem, Product, db

DEFAULT_PASSWORDS = {
    ROLE_ADMIN: ("admin", "admin123"),
    ROLE_INSPECTOR: ("inspector", "inspector123"),
    ROLE_VIEWER: ("viewer", "viewer123"),
}


def pytest_sessionfinish(session, exitstatus):
    """释放数据库连接后清理临时测试库。"""
    with flask_app.app_context():
        db.session.remove()
        db.engine.dispose()
    shutil.rmtree(_TEMP_DIRECTORY_PATH, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_database():
    """每个测试使用干净、隔离的数据库。"""
    with flask_app.app_context():
        db.drop_all()
        db.create_all()

    yield

    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def app():
    return flask_app


def _login(app, role: str):
    """用默认演示账号登录，返回带鉴权头的测试客户端。"""
    client = app.test_client()
    username, password = DEFAULT_PASSWORDS[role]
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    token = response.get_json()["token"]
    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    return client


@pytest.fixture()
def anon_client(app):
    """未登录的客户端（用于验证 401）。仍会准备好演示账号，便于测试登录流程。"""
    with app.app_context():
        ensure_default_users()
    return app.test_client()


@pytest.fixture()
def client(app):
    """默认客户端：管理员身份。

    2026-10-14 引入角色权限后，写接口需要令牌；本夹具自动以默认管理员登录，
    使原有测试用例的断言保持不变（未登录行为由 test_auth.py 单独覆盖）。
    """
    with app.app_context():
        ensure_default_users()
    return _login(app, ROLE_ADMIN)


@pytest.fixture()
def inspector_client(app):
    """质检员身份：可写、可复核。"""
    with app.app_context():
        ensure_default_users()
    return _login(app, ROLE_INSPECTOR)


@pytest.fixture()
def viewer_client(app):
    """只读身份：只能查看。"""
    with app.app_context():
        ensure_default_users()
    return _login(app, ROLE_VIEWER)


@pytest.fixture()
def seeded_data(app):
    """创建测试 CSV 和异常接口所需的基础数据。"""
    with app.app_context():
        product = Product(code="P1001", name="智能门锁 A 型")
        batches = [
            Batch(batch_no="B001", product_id=0, planned_qty=1000),
            Batch(batch_no="B002", product_id=0, planned_qty=500),
        ]
        items = [
            InspectionItem(name=name)
            for name in (
                "外观检查",
                "尺寸测量",
                "装配检验",
                "通电测试",
                "包装检查",
            )
        ]
        defects = [
            DefectType(code=code, name=name)
            for code, name in (
                ("D01", "外观划痕"),
                ("D02", "尺寸超差"),
                ("D03", "装配不到位"),
                ("D04", "功能失效"),
            )
        ]

        db.session.add(product)
        db.session.flush()
        for batch in batches:
            batch.product_id = product.id
            db.session.add(batch)
        db.session.add_all(items)
        db.session.add_all(defects)
        db.session.commit()

        return {
            "product_id": product.id,
            "batch_1_id": batches[0].id,
            "batch_1_no": batches[0].batch_no,
            "batch_2_id": batches[1].id,
            "batch_2_no": batches[1].batch_no,
        }
