"""冒烟验证：真实启动 Flask，验证鉴权 / 角色权限 / AI 助手。

要点
----
- 使用 ``quality.db`` 的**临时副本**，绝不写回真实库；
- 真实起进程、真实发 HTTP 请求（不是 test_client）；
- 每个检查项都打印 期望 / 实际 / 结论，最后给出汇总与退出码。
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / "venv" / "Scripts" / "python.exe"
PORT = 5021
BASE = f"http://127.0.0.1:{PORT}"

CHECKS: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str) -> None:
    CHECKS.append((name, passed, detail))
    flag = "PASS" if passed else "FAIL"
    print(f"[{flag}] {name} -> {detail}")


def wait_for_port(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", PORT)) == 0:
                return True
        time.sleep(0.4)
    return False


def main() -> int:
    temp_dir = Path(tempfile.mkdtemp(prefix="xm02-smoke-"))
    db_copy = temp_dir / "smoke.db"
    shutil.copy(ROOT / "quality.db", db_copy)
    print(f"临时库：{db_copy}")

    env = {
        **dict(__import__("os").environ),
        "QUALITY_DATABASE_PATH": str(db_copy),
        "XM02_SECRET_KEY": "smoke-test-secret",
    }
    env.pop("DEEPSEEK_API_KEY", None)  # 冒烟走离线模式，零 API 消耗

    log_path = temp_dir / "server.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(PYTHON), "-m", "flask", "--app", "app", "run",
             "--no-debugger", "--no-reload", "-p", str(PORT)],
            cwd=str(ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    try:
        if not wait_for_port():
            print(log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
            record("服务启动", False, "30 秒内未监听端口")
            return 1
        record("服务启动", True, f"监听 {PORT}")

        # 1) 未登录访问 AI 接口 -> 401
        resp = requests.post(
            f"{BASE}/api/assistant/ask",
            json={"question": "整体质量情况"},
            timeout=10,
        )
        record(
            "未登录访问 AI 接口返回 401",
            resp.status_code == 401,
            f"HTTP {resp.status_code} {resp.json().get('error', '')[:30]}",
        )

        # 2) 登录三个角色
        tokens: dict[str, str] = {}
        for username, password, role in (
            ("admin", "admin123", "admin"),
            ("inspector", "inspector123", "inspector"),
            ("viewer", "viewer123", "viewer"),
        ):
            resp = requests.post(
                f"{BASE}/api/auth/login",
                json={"username": username, "password": password},
                timeout=10,
            )
            ok = resp.status_code == 200
            if ok:
                tokens[role] = resp.json()["token"]
            record(
                f"登录 {role}",
                ok,
                f"HTTP {resp.status_code} 角色={resp.json().get('role_label', '-') if ok else '-'}",
            )

        # 3) 错误密码 -> 401
        resp = requests.post(
            f"{BASE}/api/auth/login",
            json={"username": "admin", "password": "bad"},
            timeout=10,
        )
        record("错误密码返回 401", resp.status_code == 401, f"HTTP {resp.status_code}")

        # 4) 只读角色写数据 -> 403
        resp = requests.post(
            f"{BASE}/api/issue",
            json={"batch_id": 1, "title": "只读越权"},
            headers={"Authorization": f"Bearer {tokens.get('viewer', '')}"},
            timeout=10,
        )
        record(
            "只读角色创建异常返回 403",
            resp.status_code == 403,
            f"HTTP {resp.status_code} {resp.json().get('required_permission', '')}",
        )

        # 5) 质检员写数据 -> 200
        resp = requests.post(
            f"{BASE}/api/issue",
            json={"batch_id": 1, "title": "冒烟验证：质检员录入"},
            headers={"Authorization": f"Bearer {tokens.get('inspector', '')}"},
            timeout=10,
        )
        record(
            "质检员创建异常返回 200",
            resp.status_code == 200,
            f"HTTP {resp.status_code}",
        )

        # 6) 管理员问 AI -> 200 且返回真实数据
        resp = requests.post(
            f"{BASE}/api/assistant/ask",
            json={"question": "各批次良率如何？"},
            headers={"Authorization": f"Bearer {tokens.get('admin', '')}"},
            timeout=30,
        )
        body = resp.json() if resp.content else {}
        calls = body.get("tool_calls") or []
        record(
            "AI 助手返回 200 且带工具调用",
            resp.status_code == 200 and bool(calls),
            f"HTTP {resp.status_code} mode={body.get('mode')} 工具={[c['tool'] for c in calls]}",
        )
        record(
            "AI 回答包含真实数字",
            any(ch.isdigit() for ch in str(body.get("answer", ""))),
            str(body.get("answer", ""))[:90],
        )

        # 7) 只读角色也能问（有查看权限）
        resp = requests.post(
            f"{BASE}/api/assistant/ask",
            json={"question": "标注进度如何？"},
            headers={"Authorization": f"Bearer {tokens.get('viewer', '')}"},
            timeout=30,
        )
        record(
            "只读角色可向助手提问",
            resp.status_code == 200,
            f"HTTP {resp.status_code}",
        )

        # 8) 页面可访问
        for path in ("/login", "/assistant", "/dashboard"):
            resp = requests.get(f"{BASE}{path}", timeout=10)
            record(
                f"页面 {path} 可访问",
                resp.status_code == 200,
                f"HTTP {resp.status_code}",
            )

        # 9) 真实库未被写入（哈希不变）
        import hashlib

        real_hash = hashlib.sha256((ROOT / "quality.db").read_bytes()).hexdigest().upper()
        record(
            "真实 quality.db 未被改动",
            real_hash == "95DD08F770FA1DB5D16E2668084282B64EC5BF6EDE74D7E83DC9B9226976E881",
            real_hash[:16] + "...",
        )

    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(temp_dir, ignore_errors=True)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print("-" * 60)
    print(f"冒烟汇总：{passed}/{total} 通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in CHECKS:
            if not ok:
                print(f"  - {name}: {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
