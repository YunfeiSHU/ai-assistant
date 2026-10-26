"""全栈冒烟：Gin → gRPC → Python → SSE。

前置：
- Python gRPC 进程：`GRPC_ENABLED=true python -m app.grpc`（默认 50051）
- 网关：`AI_GRPC_ENABLED=true ./bin/server.exe`（默认 8080）
- MySQL 可用；Redis 不可用时网关会告警降级，不影响本脚本

用法：

    ai-platform\\.venv\\Scripts\\python.exe tools\\smoke_gateway.py

脚本会注册（或登录）一个本地测试账号、建一个会话、发一条消息，断言：
`meta` 首帧、至少一个 `token`、`usage`、`done`、无 `error`。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "tools" / "out"

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")


def call(base_url: str, path: str, body: dict | None, token: str | None, method: str = "POST"):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"_raw": raw[:500]}


def ensure_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token

    status, payload = call(
        args.base_url,
        "/auth/register",
        {"email": args.email, "password": args.password, "nickname": "smoke"},
        None,
    )
    code = (payload.get("error") or {}).get("code")
    if status >= 400 and code != "EMAIL_ALREADY_EXISTS":
        raise RuntimeError(f"注册失败: {status} {code}")

    status, payload = call(
        args.base_url,
        "/auth/login",
        {"email": args.email, "password": args.password, "device_name": "smoke"},
        None,
    )
    token = payload.get("access_token")
    if status != 200 or not token:
        raise RuntimeError(f"登录失败: {status} {payload}")
    return token


def stream_once(args: argparse.Namespace, token: str) -> dict:
    status, conversation = call(args.base_url, "/conversations", {"title": "stage1 smoke"}, token)
    conversation_id = conversation.get("id")
    if status != 201 or not conversation_id:
        raise RuntimeError(f"建会话失败: {status} {conversation}")

    body = {
        "content": args.prompt,
        "use_rag": False,
        "use_memory": False,
        "use_tools": False,
    }
    request = urllib.request.Request(
        args.base_url.rstrip("/") + f"/conversations/{conversation_id}/messages/stream",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    started = time.perf_counter()
    events: list[str] = []
    first_token_ms: float | None = None
    meta: dict = {}
    usage: dict = {}
    done: dict = {}
    error: dict | None = None

    with urllib.request.urlopen(request, timeout=args.timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        if response.status != 200 or "text/event-stream" not in content_type:
            raise RuntimeError(f"不是 SSE: {response.status} {content_type}")

        event: str | None = None
        data_lines: list[str] = []
        for raw in response:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line == "":
                if event:
                    events.append(event)
                    payload = json.loads("\n".join(data_lines)) if data_lines else {}
                    if event == "token" and first_token_ms is None:
                        first_token_ms = round((time.perf_counter() - started) * 1000, 1)
                    elif event == "meta":
                        meta = payload
                    elif event == "usage":
                        usage = payload
                    elif event == "done":
                        done = payload
                    elif event == "error":
                        error = payload
                event, data_lines = None, []
                continue
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())

    return {
        "conversation_id": conversation_id,
        "events": events,
        "first_token_ms": first_token_ms,
        "total_ms": round((time.perf_counter() - started) * 1000, 1),
        "meta": meta,
        "usage": usage,
        "done": done,
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="全栈冒烟（Gin → gRPC → Python → SSE）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/api/v1")
    parser.add_argument("--email", default="stage1-smoke@example.com")
    parser.add_argument("--password", default="Stage1!Passw0rd2026")
    parser.add_argument("--token", default="", help="跳过注册/登录，直接用 access token")
    parser.add_argument("--prompt", default="用一句话解释什么是 SSE。")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    token = ensure_token(args)
    result = stream_once(args, token)

    events = result["events"]
    checks = {
        "首帧是 meta": bool(events) and events[0] == "meta",
        "至少一个 token": "token" in events,
        "有 usage": "usage" in events,
        "末帧是 done": bool(events) and events[-1] == "done",
        "没有 error": result["error"] is None,
        "usage.total_tokens > 0": int((result["usage"] or {}).get("total_tokens") or 0) > 0,
    }

    print("全栈冒烟 · Gin → gRPC → Python → SSE")
    print("=" * 60)
    print(
        f"conversation={result['conversation_id']} "
        f"first_token={result['first_token_ms']}ms total={result['total_ms']}ms "
        f"tokens={(result['usage'] or {}).get('total_tokens')}"
    )
    print(f"事件数={len(events)}，序列={','.join(events[:3])} ... {','.join(events[-2:])}")
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output = OUT_DIR / f"smoke-gateway-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    output.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "base_url": args.base_url,
                **result,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"已写入 {output.relative_to(ROOT)}")

    failed = sum(1 for ok in checks.values() if not ok)
    print("-" * 60)
    print("全部通过" if failed == 0 else f"{failed} 项失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
