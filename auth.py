"""用户认证与角色权限：管理员 / 质检员 / 只读。

设计要点
--------
- 登录成功后签发 ``token``（itsdangerous 签名，Flask 自带依赖，无需额外安装）；
- 业务接口通过请求头 ``Authorization: Bearer <token>`` 鉴权；
- 三个角色的能力差异集中在 ``ROLE_PERMISSIONS``：
    * 管理员 admin     —— 查看 / 写入 / 复核 / 用户管理
    * 质检员 inspector —— 查看 / 写入 / 复核
    * 只读   viewer    —— 仅查看
- 令牌无状态，8 小时后过期；账号被停用后令牌立即失效（每次请求回查数据库）。
"""

from __future__ import annotations

import os
import secrets
from functools import wraps
from typing import Any, Callable

from flask import Blueprint, current_app, g, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from models import AppUser, db

ROLE_ADMIN = "admin"
ROLE_INSPECTOR = "inspector"
ROLE_VIEWER = "viewer"

PERM_VIEW = "data:view"
PERM_WRITE = "data:write"
PERM_REVIEW = "data:review"
PERM_MANAGE = "user:manage"

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: frozenset({PERM_VIEW, PERM_WRITE, PERM_REVIEW, PERM_MANAGE}),
    ROLE_INSPECTOR: frozenset({PERM_VIEW, PERM_WRITE, PERM_REVIEW}),
    ROLE_VIEWER: frozenset({PERM_VIEW}),
}

ROLE_LABELS: dict[str, str] = {
    ROLE_ADMIN: "管理员",
    ROLE_INSPECTOR: "质检员",
    ROLE_VIEWER: "只读",
}

PERMISSION_LABELS: dict[str, str] = {
    PERM_VIEW: "查看数据",
    PERM_WRITE: "录入与修改",
    PERM_REVIEW: "复核",
    PERM_MANAGE: "用户管理",
}

# 演示账号，首次启动自动创建；生产环境请立即改密（或改用环境变量注入）。
DEFAULT_USERS: tuple[tuple[str, str, str, str], ...] = (
    ("admin", "admin123", ROLE_ADMIN, "系统管理员"),
    ("inspector", "inspector123", ROLE_INSPECTOR, "质检员"),
    ("viewer", "viewer123", ROLE_VIEWER, "只读用户"),
)

TOKEN_SALT = "xm02-auth-token"
TOKEN_MAX_AGE_SECONDS = 8 * 60 * 60

auth_bp = Blueprint("auth", __name__)


# --------------------------------------------------------------------------- #
# token 签发与校验
# --------------------------------------------------------------------------- #
def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"], salt=TOKEN_SALT
    )


def issue_token(user: AppUser) -> str:
    """为用户签发访问令牌。"""
    return _serializer().dumps(
        {"username": user.username, "role": user.role}
    )


def verify_token(token: str) -> dict[str, Any] | None:
    """校验令牌，返回载荷；无效或过期返回 None。"""
    try:
        payload = _serializer().loads(token, max_age=TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict) or "username" not in payload:
        return None
    return payload


# --------------------------------------------------------------------------- #
# 角色与权限
# --------------------------------------------------------------------------- #
def permissions_for(role: str) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def _identity_payload(user: AppUser) -> dict[str, Any]:
    permissions = permissions_for(user.role)
    return {
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": user.role,
        "role_label": ROLE_LABELS.get(user.role, user.role),
        "permissions": sorted(permissions),
        "permission_labels": [
            PERMISSION_LABELS[item]
            for item in sorted(permissions)
            if item in PERMISSION_LABELS
        ],
    }


def ensure_default_users() -> list[str]:
    """创建缺失的演示账号；返回本次新建的用户名列表。"""
    created: list[str] = []
    for username, password, role, display_name in DEFAULT_USERS:
        exists = (
            db.session.query(AppUser).filter_by(username=username).first()
        )
        if exists is not None:
            continue
        db.session.add(
            AppUser(
                username=username,
                password_hash=generate_password_hash(password),
                role=role,
                display_name=display_name,
            )
        )
        created.append(username)
    if created:
        db.session.commit()
    return created


# --------------------------------------------------------------------------- #
# 身份解析与装饰器
# --------------------------------------------------------------------------- #
def _bearer_token() -> str | None:
    header = request.headers.get("Authorization", "")
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def resolve_identity() -> tuple[dict[str, Any] | None, tuple[Any, int] | None]:
    """解析当前请求身份；返回 (身份, 错误响应元组)。"""
    token = _bearer_token()
    if token is None:
        return None, (jsonify({"error": "缺少访问令牌，请在请求头携带 Authorization: Bearer <token>"}), 401)

    payload = verify_token(token)
    if payload is None:
        return None, (jsonify({"error": "访问令牌无效或已过期，请重新登录"}), 401)

    user = (
        db.session.query(AppUser)
        .filter_by(username=payload["username"])
        .first()
    )
    if user is None or not user.is_active:
        return None, (jsonify({"error": "账号不存在或已停用"}), 401)

    return _identity_payload(user), None


def login_required(view: Callable) -> Callable:
    """要求已登录（任意角色）。"""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        identity, error = resolve_identity()
        if error is not None:
            body, status = error
            return body, status
        g.current_identity = identity
        return view(*args, **kwargs)

    return wrapper


def require_permission(permission: str) -> Callable:
    """要求具备指定权限，否则返回 403。"""

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            identity, error = resolve_identity()
            if error is not None:
                body, status = error
                return body, status
            if permission not in identity["permissions"]:
                return (
                    jsonify(
                        {
                            "error": (
                                f"当前角色（{identity['role_label']}）"
                                "没有该操作的权限"
                            ),
                            "required_permission": permission,
                            "role": identity["role"],
                        }
                    ),
                    403,
                )
            g.current_identity = identity
            return view(*args, **kwargs)

        return wrapper

    return decorator


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@auth_bp.post("/api/auth/login")
def login():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400

    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(username, str) or not username.strip():
        return jsonify({"error": "username 不能为空"}), 400
    if not isinstance(password, str) or not password:
        return jsonify({"error": "password 不能为空"}), 400

    user = (
        db.session.query(AppUser)
        .filter_by(username=username.strip())
        .first()
    )
    if (
        user is None
        or not user.is_active
        or not check_password_hash(user.password_hash, password)
    ):
        return jsonify({"error": "用户名或密码错误"}), 401

    identity = _identity_payload(user)
    return jsonify(
        {
            "token": issue_token(user),
            "token_type": "Bearer",
            "expires_in": TOKEN_MAX_AGE_SECONDS,
            **identity,
        }
    )


@auth_bp.get("/api/auth/me")
@login_required
def whoami():
    return jsonify(g.current_identity)


@auth_bp.get("/api/auth/users")
@require_permission(PERM_MANAGE)
def list_users():
    """仅管理员可见：账号与角色清单。"""
    rows = (
        db.session.query(AppUser)
        .order_by(AppUser.id.asc())
        .all()
    )
    return jsonify(
        [
            {
                "username": user.username,
                "display_name": user.display_name,
                "role": user.role,
                "role_label": ROLE_LABELS.get(user.role, user.role),
                "is_active": bool(user.is_active),
            }
            for user in rows
        ]
    )


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #
def init_auth(app) -> None:
    """注册认证蓝图，并确保 SECRET_KEY 可用。

    ⚠️ 不在代码里放「可用的默认密钥」——否则未配置 ``XM02_SECRET_KEY`` 的部署，
    任何人都能用源码里公开的值伪造令牌（2026-10-14 质检实证：伪造管理员令牌直接 200）。
    未配置时生成**随机密钥**并告警：安全优先，代价是重启后旧令牌失效。
    """
    if not app.config.get("SECRET_KEY"):
        configured = os.environ.get("XM02_SECRET_KEY")
        if configured:
            app.config["SECRET_KEY"] = configured
        else:
            app.config["SECRET_KEY"] = secrets.token_hex(32)
            app.logger.warning(
                "未配置 XM02_SECRET_KEY：已生成临时随机密钥，重启后所有登录令牌失效。"
                "生产环境请通过环境变量固定配置。"
            )
    app.register_blueprint(auth_bp)
