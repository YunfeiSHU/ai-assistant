"""阶段 1 出口验收：真实流式 + 首 token 基线 + 断连取消。

前置：Python 服务已启动，且指标端口可访问（默认 9105）。

    # 直连 Python（AUTH_ENABLED=false）
    ai-platform\\.venv\\Scripts\\python.exe tools\\verify_stage1.py --rounds 1

    # 经网关
    ai-platform\\.venv\\Scripts\\python.exe tools\\verify_stage1.py `
        --base-url http://127.0.0.1:8080/api/v1 --token <ACCESS_TOKEN>

检查：
1. `/health` 返回 200；
2. 基线脚本至少一条成功（首 token 有数字）；
3. 流式响应头正确；
4. 首 token 后断开，`ai_chat_canceled_total{reason="client_disconnect"}` 增加。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

DISCONNECT_PROMPT = "请写一篇大约八百字的短文，说明流式输出为什么能改善聊天产品的体验。"
CANCEL_METRIC = re.compile(r'ai_chat_canceled_total\{reason="client_disconnect"\}\s+([\d.]+)')


def health(base_url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(base_url.rstrip("/") + "/health", timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def scrape_cancel(metrics_url: str) -> float:
    with urllib.request.urlopen(metrics_url, timeout=5) as response:
        text = response.read().decode("utf-8", errors="replace")
    match = CANCEL_METRIC.search(text)
    return float(match.group(1)) if match else 0.0


def wait_for_cancel(before: float, metrics_url: str, timeout: float = 8.0) -> float:
    deadline = time.monotonic() + timeout
    after = before
    while time.monotonic() < deadline:
        after = scrape_cancel(metrics_url)
        if after > before:
            return after
        time.sleep(0.5)
    return after


def run_baseline(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(ROOT / "tools" / "measure_chat.py"),
        "--base-url",
        args.base_url,
        "--rounds",
        str(args.rounds),
        "--timeout",
        str(args.timeout),
    ]
    if args.token:
        command += ["--token", args.token]
    if args.debug_user:
        command += ["--debug-user", args.debug_user]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=args.timeout * 2,
    )
    print((proc.stdout or "").strip())
    if (proc.stderr or "").strip():
        print(proc.stderr.strip(), file=sys.stderr)
    return proc.returncode


def disconnect_after_first_token(args: argparse.Namespace) -> dict[str, str]:
    parsed = urllib.parse.urlsplit(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"base-url 不可解析: {args.base_url}")

    path = (parsed.path.rstrip("/") + "/chat/stream") or "/chat/stream"
    connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = connection_cls(parsed.hostname, parsed.port, timeout=30)
    body = json.dumps(
        {"query": DISCONNECT_PROMPT, "use_rag": False, "use_memory": False, "use_tools": False},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    if args.debug_user:
        headers["X-Debug-User-Id"] = args.debug_user

    try:
        conn.request("POST", path, body, headers)
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeError(f"流式请求失败: HTTP {response.status}")
        captured = {
            "Content-Type": response.getheader("Content-Type", ""),
            "Cache-Control": response.getheader("Cache-Control", ""),
            "X-Accel-Buffering": response.getheader("X-Accel-Buffering", ""),
        }
        for _ in range(200):
            line = response.readline()
            if not line:
                break
            if line.startswith(b"event: token"):
                return captured
        raise RuntimeError("未收到 token 帧")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 1 出口验收")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--metrics-url", default="http://127.0.0.1:9105/metrics")
    parser.add_argument("--token", default="")
    parser.add_argument("--debug-user", default="")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--skip-cancel", action="store_true")
    args = parser.parse_args()

    failures: list[str] = []

    print("阶段 1 · 真实流式验收")
    print("=" * 60)

    try:
        status, payload = health(args.base_url)
        ok = status == 200 and payload.get("status") == "ok"
        print(f"{'PASS' if ok else 'FAIL'}  /health 200 且 status=ok（实际 {status}）")
        if not ok:
            failures.append("health")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  /health 不可达: {exc}")
        return 1

    baseline_code = run_baseline(args)
    print(f"{'PASS' if baseline_code == 0 else 'FAIL'}  基线至少一条成功")
    if baseline_code != 0:
        failures.append("baseline")

    if not args.skip_cancel:
        try:
            before = scrape_cancel(args.metrics_url)
            headers = disconnect_after_first_token(args)
            content_type = headers["Content-Type"].lower()
            headers_ok = (
                content_type.startswith("text/event-stream")
                and "no-transform" in headers["Cache-Control"]
                and headers["X-Accel-Buffering"].lower() == "no"
            )
            print(f"{'PASS' if headers_ok else 'FAIL'}  流式响应头（{headers['Content-Type']}）")
            if not headers_ok:
                failures.append("headers")

            after = wait_for_cancel(before, args.metrics_url)
            canceled = after > before
            print(
                f"{'PASS' if canceled else 'FAIL'}  断连后取消上游"
                f"（metric {before:g} → {after:g}）"
            )
            if not canceled:
                failures.append("cancel")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  断连检查失败: {exc}")
            failures.append("cancel")

    print("-" * 60)
    print("全部通过" if not failures else f"失败项: {', '.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
