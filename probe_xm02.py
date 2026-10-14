"""XM02 质检探测脚本。

覆盖：鉴权矩阵（4 身份 × 受保护接口）、令牌安全、输入边界、空库边界、
读接口暴露面、路由与方法错误、SQL 注入尝试。

- 使用 ``quality.db`` 的**临时副本**，绝不写真实库；
- 所有结论都打印「检查项 / 期望 / 实际 / 结论」，最后给出分级汇总。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

_temp_dir = Path(tempfile.mkdtemp(prefix="xm02-probe-"))
_db_copy = _temp_dir / "probe.db"
shutil.copy(ROOT / "quality.db", _db_copy)

os.environ["QUALITY_DATABASE_PATH"] = str(_db_copy)
os.environ["XM02_SECRET_KEY"] = "probe-secret-key"
os.environ.pop("DEEPSEEK_API_KEY", None)

from app import app  # noqa: E402
from assistant import offline_plan  # noqa: E402
from assistant_tools import execute_tool  # noqa: E402
from auth import ensure_default_users  # noqa: E402
from models import QualityIssue, db  # noqa: E402

BUGS: list[str] = []
RISKS: list[str] = []
PASSED: list[str] = []


def check(name: str, ok: bool, detail: str = "", level: str = "bug") -> None:
    if ok:
        PASSED.append(name)
        print(f"[PASS] {name}")
    else:
        target = BUGS if level == "bug" else RISKS
        target.append(f"{name} —— {detail}")
        print(f"[{'BUG ' if level == 'bug' else 'RISK'}] {name} -> {detail}")


def note(name: str, detail: str) -> None:
    RISKS.append(f"{name} —— {detail}")
    print(f"[RISK] {name} -> {detail}")


def login(username: str, password: str) -> str:
    with app.test_client() as client:
        resp = client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        return resp.get_json()["token"]


def call(
    method: str,
    path: str,
    *,
    body: Any = None,
    token: str | None = None,
    raw_body: Any = None,
    content_type: str | None = None,
    headers: dict[str, str] | None = None,
):
    client = app.test_client()
    merged = dict(headers or {})
    if token:
        merged["Authorization"] = f"Bearer {token}"
    func = getattr(client, method.lower())
    kwargs: dict[str, Any] = {}
    if body is not None:
        kwargs["json"] = body
    if raw_body is not None:
        kwargs["data"] = raw_body
    if content_type:
        kwargs["content_type"] = content_type
    if merged:
        kwargs["headers"] = merged
    return func(path, **kwargs)


def status(method: str, path: str, **kwargs) -> int:
    return call(method, path, **kwargs).status_code


def main() -> int:
    with app.app_context():
        ensure_default_users()

    tokens = {
        "admin": login("admin", "admin123"),
        "inspector": login("inspector", "inspector123"),
        "viewer": login("viewer", "viewer123"),
    }
    print("=" * 72)
    print("【A】鉴权矩阵：4 种身份 × 受保护接口")
    print("=" * 72)

    matrix: list[tuple[str, str, Any, dict[str, int]]] = [
        (
            "POST",
            "/api/issue",
            {"batch_id": 1, "title": "质检探测"},
            {"anon": 401, "viewer": 403, "inspector": 200, "admin": 200},
        ),
        (
            "POST",
            "/api/issue/999999/advance",
            None,
            {"anon": 401, "viewer": 403, "inspector": 404, "admin": 404},
        ),
        (
            "POST",
            "/api/annotation/image/999999",
            {"labeled_by": "探测", "annotations": []},
            {"anon": 401, "viewer": 403, "inspector": 404, "admin": 404},
        ),
        (
            "POST",
            "/api/annotation/image/999999/review",
            None,
            {"anon": 401, "viewer": 403, "inspector": 404, "admin": 404},
        ),
        (
            "POST",
            "/api/assistant/ask",
            {"question": "整体质量情况"},
            {"anon": 401, "viewer": 200, "inspector": 200, "admin": 200},
        ),
        (
            "GET",
            "/api/auth/me",
            None,
            {"anon": 401, "viewer": 200, "inspector": 200, "admin": 200},
        ),
        (
            "GET",
            "/api/auth/users",
            None,
            {"anon": 401, "viewer": 403, "inspector": 403, "admin": 200},
        ),
    ]

    for method, path, body, expected in matrix:
        for role, want in expected.items():
            got = status(
                method,
                path,
                body=body,
                token=None if role == "anon" else tokens[role],
            )
            check(
                f"{role:9s} {method} {path} -> {want}",
                got == want,
                f"实际 {got}",
            )

    print()
    print("=" * 72)
    print("【B】令牌安全")
    print("=" * 72)

    admin_token = tokens["admin"]
    cases = [
        ("无 Authorization 头", {}),
        ("Bearer 无值", {"Authorization": "Bearer"}),
        ("Bearer 空值", {"Authorization": "Bearer "}),
        ("方案名错误 Token", {"Authorization": f"Token {admin_token}"}),
        ("令牌截断", {"Authorization": f"Bearer {admin_token[:-2]}"}),
        ("令牌尾追加字符", {"Authorization": f"Bearer {admin_token}zz"}),
        ("令牌非法字符", {"Authorization": "Bearer !!!not-base64!!!"}),
    ]
    for name, headers in cases:
        got = status("GET", "/api/auth/me", headers=headers)
        check(f"{name} -> 401", got == 401, f"实际 {got}")

    # 换密钥后旧令牌应失效
    original_secret = app.config["SECRET_KEY"]
    app.config["SECRET_KEY"] = "attacker-secret"
    forged = login("admin", "admin123")
    app.config["SECRET_KEY"] = original_secret
    got = status("GET", "/api/auth/me", token=forged)
    check("其他密钥签发的令牌被拒 -> 401", got == 401, f"实际 {got}")

    # 过期校验生效（把 max_age 设为 -1 等价于已过期）
    from itsdangerous import SignatureExpired

    from auth import _serializer

    with app.app_context():
        expired = False
        try:
            _serializer().loads(admin_token, max_age=-1)
        except SignatureExpired:
            expired = True
    check("过期时间校验生效（max_age 判定）", expired, "未触发过期")

    print()
    print("=" * 72)
    print("【C】输入边界")
    print("=" * 72)

    checks = [
        ("登录：非 JSON 体", status("POST", "/api/auth/login", raw_body="not-json", content_type="text/plain"), 400),
        ("登录：JSON 数组", status("POST", "/api/auth/login", body=[1, 2]), 400),
        ("登录：用户名为空", status("POST", "/api/auth/login", body={"username": "  ", "password": "x"}), 400),
        ("登录：密码为空", status("POST", "/api/auth/login", body={"username": "admin", "password": ""}), 400),
        ("登录：用户名非字符串", status("POST", "/api/auth/login", body={"username": 123, "password": "x"}), 400),
        ("登录：超长用户名", status("POST", "/api/auth/login", body={"username": "a" * 5000, "password": "x"}), 401),
        ("建异常：JSON 数组", status("POST", "/api/issue", body=[1], token=tokens["admin"]), 400),
        ("建异常：batch_id 非数字", status("POST", "/api/issue", body={"batch_id": "abc", "title": "t"}, token=tokens["admin"]), 400),
        ("建异常：batch_id 布尔", status("POST", "/api/issue", body={"batch_id": True, "title": "t"}, token=tokens["admin"]), 400),
        ("建异常：title 非字符串", status("POST", "/api/issue", body={"batch_id": 1, "title": 123}, token=tokens["admin"]), 400),
        ("建异常：批次不存在", status("POST", "/api/issue", body={"batch_id": 999999, "title": "t"}, token=tokens["admin"]), 400),
        ("助手：null 体", status("POST", "/api/assistant/ask", raw_body="null", content_type="application/json", token=tokens["admin"]), 400),
        ("助手：question 非字符串", status("POST", "/api/assistant/ask", body={"question": 123}, token=tokens["admin"]), 400),
        ("助手：question 空", status("POST", "/api/assistant/ask", body={"question": "   "}, token=tokens["admin"]), 400),
        ("助手：question 超长", status("POST", "/api/assistant/ask", body={"question": "良" * 501}, token=tokens["admin"]), 400),
    ]
    for name, got, want in checks:
        check(f"{name} -> {want}", got == want, f"实际 {got}")

    # SQL 注入尝试
    inject = "'; DROP TABLE quality_issue; --"
    resp = call("POST", "/api/assistant/ask", body={"question": inject}, token=tokens["admin"])
    with app.app_context():
        table_alive = (
            db.session.query(QualityIssue).count() >= 0
        ) and "quality_issue" in db.engine.dialect.get_table_names(
            db.engine.connect()
        )
    check(
        "SQL 注入串提问后表仍存在",
        resp.status_code == 200 and table_alive,
        f"HTTP {resp.status_code} 表存在={table_alive}",
    )

    with app.app_context():
        for name, params in (
            ("limit 负数", {"limit": -5}),
            ("limit 超大", {"limit": 99999}),
            ("batch_no 数字", {"batch_no": 123}),
            ("batch_no 空串", {"batch_no": ""}),
            ("batch_no 注入串", {"batch_no": "' OR 1=1 --"}),
            ("days 负数", {"days": -1}),
            ("status 注入串", {"status": "' OR 1=1 --"}),
        ):
            tool = (
                "query_yield_trend"
                if "days" in params
                else ("query_quality_issues" if "status" in params else "query_yield_by_batch")
            )
            result = execute_tool(tool, params)
            ok = "error" not in result or result.get("error") in (None,)
            check(f"工具 {tool} 参数 {name} 不崩溃", ok, str(result.get("error")))

    print()
    print("=" * 72)
    print("【D】读接口暴露面（事实记录）")
    print("=" * 72)

    open_reads = []
    for path in (
        "/api/yield",
        "/api/issues",
        "/api/report/week",
        "/api/annotation/stats",
        "/api/annotation/images",
        "/api/annotation/defect-types",
    ):
        got = status("GET", path)
        if got == 200:
            open_reads.append(path)
    if open_reads:
        note(
            "读接口未鉴权（匿名可访问）",
            "、".join(open_reads) + " —— 现有测试依赖此行为；如需完整权限控制应统一加 data:view",
        )
    else:
        check("读接口均已鉴权", True)

    print()
    print("=" * 72)
    print("【E】路由与方法错误")
    print("=" * 72)

    check("GET /api/issue -> 405", status("GET", "/api/issue", token=tokens["admin"]) == 405, f"实际 {status('GET', '/api/issue', token=tokens['admin'])}")
    check("POST /api/report/week -> 405", status("POST", "/api/report/week", token=tokens["admin"]) == 405)
    check("GET /不存在 -> 404", status("GET", "/no-such-page") == 404)
    check("GET / -> 200 ok", call("GET", "/").data == b"ok")
    check(
        "GET /login /assistant /dashboard -> 200",
        all(status("GET", p) == 200 for p in ("/login", "/assistant", "/dashboard")),
    )

    print()
    print("=" * 72)
    print("【F】空库边界（清空数据后不应报 500）")
    print("=" * 72)

    with app.app_context():
        db.drop_all()
        db.create_all()
        ensure_default_users()

    for name, method, path, body, want in (
        ("空库 /api/yield", "GET", "/api/yield", None, 200),
        ("空库 /api/issues", "GET", "/api/issues", None, 200),
        ("空库 /api/annotation/stats", "GET", "/api/annotation/stats", None, 200),
        ("空库 /api/report/week", "GET", "/api/report/week", None, 200),
        ("空库 助手：整体情况", "POST", "/api/assistant/ask", {"question": "整体质量情况"}, 200),
        ("空库 助手：批次良率", "POST", "/api/assistant/ask", {"question": "各批次良率"}, 200),
        ("空库 助手：标注进度", "POST", "/api/assistant/ask", {"question": "标注进度如何"}, 200),
    ):
        got = status(method, path, body=body, token=tokens["admin"])
        check(f"{name} -> {want}", got == want, f"实际 {got}")

    with app.app_context():
        empty_tool = execute_tool("query_overall_yield", {})
    check(
        "空库时工具返回友好提示而非报错",
        "error" not in empty_tool and "还没有" in empty_tool["summary"],
        str(empty_tool.get("summary"))[:60],
    )
    check(
        "空库时助手回答不含 500 痕迹",
        "Traceback" not in json.dumps(
            call("POST", "/api/assistant/ask", body={"question": "整体情况"}, token=tokens["admin"]).get_json(),
            ensure_ascii=False,
        ),
        "",
    )

    print()
    print("=" * 72)
    print("【G】离线路由覆盖（不依赖大模型）")
    print("=" * 72)
    for question, expect in (
        ("批次 B001 良率", "query_yield_by_batch"),
        ("不良分布", "query_defect_distribution"),
        ("未关闭异常", "query_quality_issues"),
        ("标注进度", "query_annotation_progress"),
        ("最近检验记录", "query_recent_inspections"),
        ("随便是啥", "query_overall_yield"),
    ):
        picked = offline_plan(question)[0]["name"]
        check(f"离线路由「{question}」-> {expect}", picked == expect, f"实际 {picked}")

    print()
    print("=" * 72)
    print(f"汇总：通过 {len(PASSED)} · BUG {len(BUGS)} · 风险/观察 {len(RISKS)}")
    if BUGS:
        print("\n🔴 BUG：")
        for item in BUGS:
            print(f"  - {item}")
    if RISKS:
        print("\n🟡 风险 / 观察：")
        for item in RISKS:
            print(f"  - {item}")
    print("=" * 72)

    shutil.rmtree(_temp_dir, ignore_errors=True)
    return 0 if not BUGS else 1


if __name__ == "__main__":
    raise SystemExit(main())
