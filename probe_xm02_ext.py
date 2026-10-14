"""补充质检：默认密钥风险实证 + 并发写入稳定性。

1. 模拟「部署时忘记配置 XM02_SECRET_KEY」：验证是否能用源码里公开的默认密钥
   自行签发管理员令牌并成功访问受保护接口；
2. 并发 10 路写入质量异常，统计失败率（SQLite 锁竞争）。
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from itsdangerous import URLSafeTimedSerializer

ROOT = Path(__file__).resolve().parent
PY = ROOT / "venv" / "Scripts" / "python.exe"
PORT = 5022
BASE = f"http://127.0.0.1:{PORT}"

PUBLIC_DEFAULT_SECRET = "xm02-dev-secret-change-me"
TOKEN_SALT = "xm02-auth-token"

BUGS: list[str] = []
RISKS: list[str] = []
PASSED: list[str] = []


def check(name: str, ok: bool, detail: str, level: str = "bug") -> None:
    if ok:
        PASSED.append(name)
        print(f"[PASS] {name}")
    else:
        (BUGS if level == "bug" else RISKS).append(f"{name} —— {detail}")
        print(f"[{'BUG ' if level == 'bug' else 'RISK'}] {name} -> {detail}")


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
    temp_dir = Path(tempfile.mkdtemp(prefix="xm02-extcheck-"))
    db_copy = temp_dir / "ext.db"
    shutil.copy(ROOT / "quality.db", db_copy)

    env = {**os.environ, "QUALITY_DATABASE_PATH": str(db_copy)}
    env.pop("XM02_SECRET_KEY", None)      # ← 关键：模拟忘记配置密钥
    env.pop("DEEPSEEK_API_KEY", None)

    log_path = temp_dir / "server.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(PY), "-m", "flask", "--app", "app", "run",
             "--no-debugger", "--no-reload", "-p", str(PORT)],
            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
        )

    try:
        if not wait_for_port():
            print(log_path.read_text(encoding="utf-8", errors="replace")[-1500:])
            print("服务未能启动")
            return 1

        print("=" * 72)
        print("【H】默认密钥风险实证（部署未配置 XM02_SECRET_KEY 时）")
        print("=" * 72)

        forged = URLSafeTimedSerializer(
            PUBLIC_DEFAULT_SECRET, salt=TOKEN_SALT
        ).dumps({"username": "admin", "role": "admin"})

        resp = requests.get(
            f"{BASE}/api/auth/me",
            headers={"Authorization": f"Bearer {forged}"},
            timeout=10,
        )
        check(
            "无法用公开默认密钥伪造身份（应 401）",
            resp.status_code == 401,
            f"实际 HTTP {resp.status_code}：源码内公开的默认密钥即可签发管理员令牌，"
            "未配置 XM02_SECRET_KEY 的部署可被完全绕过鉴权",
        )

        if resp.status_code == 200:
            body = resp.json()
            print(f"      伪造令牌身份：{body.get('username')} / {body.get('role')}")

        print()
        print("=" * 72)
        print("【I】并发写入稳定性（10 路并发创建质量异常）")
        print("=" * 72)

        login = requests.post(
            f"{BASE}/api/auth/login",
            json={"username": "admin", "password": "admin123"},
            timeout=10,
        ).json()
        token = login["token"]
        headers = {"Authorization": f"Bearer {token}"}

        def create(index: int) -> int:
            try:
                return requests.post(
                    f"{BASE}/api/issue",
                    json={"batch_id": 1, "title": f"并发探测 {index}"},
                    headers=headers,
                    timeout=15,
                ).status_code
            except Exception:  # noqa: BLE001
                return -1

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(create, range(10)))

        ok_count = sum(1 for code in results if code == 200)
        check(
            "10 路并发写入无 5xx",
            ok_count == 10,
            f"成功 {ok_count}/10，状态码分布={sorted(results)}",
        )

        # 真实库的**业务数据**未被写入（app_user 为正常初始化，不计入业务数据）
        expected_counts = {
            "product": 3,
            "batch": 15,
            "inspection_item": 5,
            "inspection_record": 400,
            "defect_type": 4,
        }
        con = sqlite3.connect(ROOT / "quality.db")
        actual = {
            name: con.execute(f'select count(*) from "{name}"').fetchone()[0]
            for name in expected_counts
        }
        con.close()
        check(
            "真实库业务数据未被写入",
            actual == expected_counts,
            f"实际 {actual}",
        )

    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(temp_dir, ignore_errors=True)

    print()
    print("=" * 72)
    print(f"汇总：通过 {len(PASSED)} · BUG {len(BUGS)} · 风险 {len(RISKS)}")
    for item in BUGS:
        print(f"  🔴 {item}")
    for item in RISKS:
        print(f"  🟡 {item}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
