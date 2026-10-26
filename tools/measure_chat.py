"""阶段 1 基线测量：向流式对话接口发固定 prompt，记录首 token / 总时长 / tokens。

只依赖标准库，默认打本机 Python 服务（鉴权关闭时可直接跑）；也可指向网关：

    # Python 直连（AUTH_ENABLED=false）
    ai-platform\\.venv\\Scripts\\python.exe tools\\measure_chat.py --rounds 2

    # 经网关（需要 access token）
    ai-platform\\.venv\\Scripts\\python.exe tools\\measure_chat.py `
        --base-url http://127.0.0.1:8080/api/v1 --token <ACCESS_TOKEN>

输出一份 JSON 到 tools/out/，并打印首 token 的中位数。没有基线数字的优化不合并。
"""

from __future__ import annotations

import argparse
import json
import statistics
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

PROMPTS = [
    "用一句话解释什么是 SSE。",
    "把 128 乘以 1.15，只给结果。",
    "用三句话说明 Redis 的 LTRIM 用途。",
    "什么是向量数据库？一句话回答。",
    "写一个 Python 函数，判断字符串是否是回文。",
]


def parse_frames(response):
    """逐帧解析 SSE；返回 (event, data)。"""
    event: str | None = None
    data_lines: list[str] = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line == "":
            if event and data_lines:
                yield event, "\n".join(data_lines)
            event, data_lines = None, []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())

    if event and data_lines:
        yield event, "\n".join(data_lines)


def run_once(url: str, prompt: str, args: argparse.Namespace, index: int) -> dict:
    body = {
        "query": prompt,
        "use_rag": args.rag,
        "use_memory": args.memory,
        "use_tools": args.tools,
    }
    if args.model:
        body["model"] = args.model

    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "ai-assistant-stage1/1.0",
    }
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    if args.debug_user:
        headers["X-Debug-User-Id"] = args.debug_user
    for item in args.header:
        key, _, value = item.partition(":")
        headers[key.strip()] = value.strip()

    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    started = time.perf_counter()
    first_token_at: float | None = None
    first_event_at: float | None = None
    finished_at: float | None = None
    content: list[str] = []
    meta: dict = {}
    usage: dict = {}
    done: dict = {}
    error: dict | None = None
    references = 0
    tool_calls = 0

    def elapsed(mark: float | None) -> float | None:
        return None if mark is None else round((mark - started) * 1000, 1)

    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type:
                raise RuntimeError(f"响应不是 SSE：Content-Type={content_type!r}")

            for event, data in parse_frames(response):
                now = time.perf_counter()
                if first_event_at is None:
                    first_event_at = now
                try:
                    payload = json.loads(data) if data else {}
                except json.JSONDecodeError:
                    payload = {"_raw": data[:200]}

                if event == "meta":
                    meta = payload
                elif event == "reference":
                    references = len(payload.get("references") or [])
                elif event == "token":
                    if first_token_at is None:
                        first_token_at = now
                    content.append(payload.get("delta", ""))
                elif event in {"tool_call", "tool_result"}:
                    tool_calls += 1
                elif event == "usage":
                    usage = payload
                elif event == "done":
                    done = payload
                    finished_at = now
                elif event == "error":
                    error = payload

    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            envelope = {"_raw": raw[:500]}
        return {
            "index": index,
            "prompt": prompt,
            "status": "http_error",
            "http_status": exc.code,
            "error": (envelope.get("error") if isinstance(envelope, dict) else None) or envelope,
            "first_token_ms": None,
            "total_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as exc:  # noqa: BLE001 - 基线脚本需要把任何失败都记成一条记录
        return {
            "index": index,
            "prompt": prompt,
            "status": "exception",
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "first_token_ms": None,
            "total_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    if error is not None:
        status = "stream_error"
    elif done:
        status = "ok"
    else:
        status = "closed_without_done"

    return {
        "index": index,
        "prompt": prompt,
        "status": status,
        "model": meta.get("model") or args.model,
        "first_event_ms": elapsed(first_event_at),
        "first_token_ms": elapsed(first_token_at),
        "total_ms": elapsed(finished_at or time.perf_counter()),
        "finish_reason": done.get("finish_reason"),
        "partial": done.get("partial"),
        "usage": usage,
        "chars": len("".join(content)),
        "references": references,
        "tool_events": tool_calls,
        "degraded": meta.get("degraded"),
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 1 流式对话基线")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--endpoint", default="/chat/stream")
    parser.add_argument("--token", default="")
    parser.add_argument("--debug-user", default="", help="AUTH_ENABLED=false 时使用的 X-Debug-User-Id")
    parser.add_argument("--header", action="append", default=[], help='额外请求头，如 "X-Foo: bar"')
    parser.add_argument("--model", default="")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--rag", action="store_true")
    parser.add_argument("--memory", action="store_true")
    parser.add_argument("--tools", action="store_true")
    parser.add_argument("--prompts", type=Path, help="每行一条 prompt 的文件")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    prompts = (
        [line.strip() for line in args.prompts.read_text(encoding="utf-8").splitlines() if line.strip()]
        if args.prompts
        else PROMPTS
    )
    url = args.base_url.rstrip("/") + args.endpoint

    records: list[dict] = []
    index = 0
    for round_no in range(1, args.rounds + 1):
        for prompt in prompts:
            index += 1
            record = run_once(url, prompt, args, index)
            record["round"] = round_no
            records.append(record)
            first = record.get("first_token_ms")
            total = record.get("total_ms")
            usage = record.get("usage") or {}
            print(
                f"[{index:02d}] {record['status']:<18} "
                f"first_token={first if first is not None else '-':>8}ms "
                f"total={total if total is not None else '-':>8}ms "
                f"tokens={usage.get('total_tokens', '-')} | {prompt[:24]}"
            )

    successes = [r for r in records if r["status"] == "ok"]
    first_tokens = [r["first_token_ms"] for r in successes if r.get("first_token_ms") is not None]
    totals = [r["total_ms"] for r in successes if r.get("total_ms") is not None]

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "url": url,
        "model": args.model or None,
        "rounds": args.rounds,
        "success": len(successes),
        "total": len(records),
        "first_token_ms_p50": round(statistics.median(first_tokens), 1) if first_tokens else None,
        "first_token_ms_p95": (
            round(sorted(first_tokens)[max(0, int(len(first_tokens) * 0.95) - 1)], 1) if first_tokens else None
        ),
        "total_ms_p50": round(statistics.median(totals), 1) if totals else None,
        "records": records,
    }

    print("-" * 72)
    print(
        f"成功 {summary['success']}/{summary['total']} | "
        f"首 token p50={summary['first_token_ms_p50']}ms "
        f"p95={summary['first_token_ms_p95']}ms | "
        f"总时长 p50={summary['total_ms_p50']}ms"
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output = args.output or OUT_DIR / f"measure-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {output.relative_to(ROOT)}")

    return 0 if successes else 1


if __name__ == "__main__":
    sys.exit(main())
