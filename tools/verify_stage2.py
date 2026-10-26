"""阶段 2 出口验收：会话与消息台账。

分两段跑，中间重启网关，用来验证「重启后历史一致」：

    # 1. 写：断连 partial、10 并发 seq、55 会话分页、跨用户 404、归档 409
    ai-platform\\.venv\\Scripts\\python.exe tools\\verify_stage2.py --phase write

    # 2. 重启网关（Python gRPC 可不动），再读
    ai-platform\\.venv\\Scripts\\python.exe tools\\verify_stage2.py --phase read

前置：网关 + Python gRPC + MySQL。为了 55 次建会话不被 30/min 挡住，网关用
`CONV_RATE_PER_MINUTE=100`、`MSG_RATE_PER_MINUTE=100` 启动。
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "tools" / "out" / "stage2-state.json"

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

PASSWORD = "Stage2!Passw0rd2026"
LONG_PROMPT = "请写一篇大约七百字的短文，说明流式输出在聊天产品里的价值，不要用列表。"
PARTIAL_STATUSES = {"partial", "failed", "completed"}


class Gateway:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def call(self, method: str, path: str, body: dict | None = None, token: str | None = None, timeout: float = 90):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"_raw": raw[:500]}

    def login_or_register(self, email: str, password: str) -> str:
        status, payload = self.call("POST", "/auth/register", {"email": email, "password": password, "nickname": "stage2"})
        code = (payload.get("error") or {}).get("code")
        if status >= 400 and code != "EMAIL_ALREADY_EXISTS":
            raise RuntimeError(f"注册失败: {status} {payload}")
        if status == 201 and payload.get("access_token"):
            return payload["access_token"]

        status, payload = self.call("POST", "/auth/login", {"email": email, "password": password, "device_name": "stage2"})
        token = payload.get("access_token")
        if status != 200 or not token:
            raise RuntimeError(f"登录失败: {status} {payload}")
        return token

    def create_conversation(self, token: str, title: str) -> str:
        status, payload = self.call("POST", "/conversations", {"title": title}, token)
        if status != 201 or not payload.get("id"):
            raise RuntimeError(f"建会话失败: {status} {payload}")
        return payload["id"]

    def stream_and_abort(self, path: str, body: dict, token: str, timeout: float = 60) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = connection_cls(parsed.hostname, parsed.port, timeout=timeout)
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        try:
            conn.request("POST", parsed.path.rstrip("/") + path, json.dumps(body, ensure_ascii=False).encode(), headers)
            response = conn.getresponse()
            if response.status != 200:
                raise RuntimeError(f"流式请求失败: HTTP {response.status}")
            for _ in range(500):
                line = response.readline()
                if not line:
                    break
                if line.startswith(b"event: token"):
                    return
            raise RuntimeError("未收到 token 帧")
        finally:
            conn.close()


def record(results: list[tuple[str, bool, str]], name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


def wait_for_assistant(gw: Gateway, conversation_id: str, token: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        status, page = gw.call("GET", f"/conversations/{conversation_id}/messages?limit=20&order=desc", token=token)
        if status == 200:
            assistants = [item for item in page.get("items", []) if item.get("role") == "assistant"]
            if assistants:
                last = assistants[0]
                if last.get("status") in PARTIAL_STATUSES:
                    return last
        time.sleep(0.5)
    return last


def paginate_conversations(gw: Gateway, token: str, limit: int = 20) -> tuple[list[int], list[str]]:
    pages: list[int] = []
    ids: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        path = f"/conversations?limit={limit}"
        if cursor:
            path += "&cursor=" + urllib.parse.quote(cursor)
        status, page = gw.call("GET", path, token=token)
        if status != 200:
            raise RuntimeError(f"会话分页失败: {status} {page}")
        items = page.get("items", [])
        pages.append(len(items))
        ids.extend(item["id"] for item in items)
        cursor = page.get("next_cursor")
        if not page.get("has_more"):
            break
    return pages, ids


def unique_email(prefix: str) -> str:
    return f"{prefix}+{int(time.time())}-{uuid.uuid4().hex[:6]}@example.com"


def write_phase(gw: Gateway, results: list[tuple[str, bool, str]]) -> dict:
    email_a = unique_email("stage2-a")
    token_a = gw.login_or_register(email_a, PASSWORD)
    record(results, "用户 A 注册/登录", bool(token_a))

    # 1. 断连 → partial
    partial_cid = gw.create_conversation(token_a, "stage2-partial")
    gw.stream_and_abort(
        f"/conversations/{partial_cid}/messages/stream",
        {"content": LONG_PROMPT, "use_rag": False, "use_memory": False, "use_tools": False},
        token_a,
    )
    partial = wait_for_assistant(gw, partial_cid, token_a)
    partial_ok = partial.get("status") == "partial" and bool(partial.get("content"))
    record(
        results,
        "断连后 assistant 落 partial 且有正文",
        partial_ok,
        f"status={partial.get('status')} chars={len(partial.get('content') or '')}",
    )

    # 2. 10 并发 → seq 唯一且连续
    seq_cid = gw.create_conversation(token_a, "stage2-seq")

    def send(index: int):
        return gw.call(
            "POST",
            f"/conversations/{seq_cid}/messages",
            {
                "content": f"只回答数字 {index}，不要解释。",
                "use_rag": False,
                "use_memory": False,
                "use_tools": False,
            },
            token_a,
            timeout=120,
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        sent = list(pool.map(send, range(10)))
    bad = [(status, payload.get("error")) for status, payload in sent if status != 200]
    record(results, "10 条并发消息全部 200", not bad, f"失败: {bad[:3]}")

    status, page = gw.call("GET", f"/conversations/{seq_cid}/messages?limit=100&order=asc", token=token_a)
    items = page.get("items", []) if status == 200 else []
    seqs = [item["seq"] for item in items]
    record(
        results,
        "20 条台账 seq 唯一且连续 1..20",
        len(items) == 20 and sorted(seqs) == list(range(1, 21)),
        f"count={len(items)} seqs={seqs[:8]}...",
    )

    # 3. 55 个会话 → 3 页
    email_b = unique_email("stage2-b")
    token_b = gw.login_or_register(email_b, PASSWORD)
    for index in range(55):
        gw.create_conversation(token_b, f"stage2-page-{index:02d}")
    pages, ids = paginate_conversations(gw, token_b)
    record(results, "55 条会话分 3 页（20/20/15）", pages == [20, 20, 15], f"pages={pages}")
    record(
        results,
        "55 条会话无重复无遗漏",
        len(ids) == 55 and len(set(ids)) == 55,
        f"count={len(ids)} unique={len(set(ids))}",
    )

    # 4. 跨用户 404
    status, payload = gw.call("GET", f"/conversations/{partial_cid}", token=token_b)
    code = (payload.get("error") or {}).get("code")
    record(results, "跨用户读会话返回 404", status == 404 and code == "CONVERSATION_NOT_FOUND", f"{status} {code}")

    # 5. 归档后发消息 409
    archive_cid = gw.create_conversation(token_a, "stage2-archive")
    status, _ = gw.call("POST", f"/conversations/{archive_cid}/archive", None, token_a)
    archive_ok = status in {200, 204}
    status, payload = gw.call(
        "POST",
        f"/conversations/{archive_cid}/messages",
        {"content": "归档后还能发吗？", "use_rag": False, "use_memory": False, "use_tools": False},
        token_a,
    )
    code = (payload.get("error") or {}).get("code")
    record(
        results,
        "归档会话发消息返回 409 CONVERSATION_ARCHIVED",
        archive_ok and status == 409 and code == "CONVERSATION_ARCHIVED",
        f"archive={archive_ok} send={status} {code}",
    )

    state = {
        "user_a": {"email": email_a, "password": PASSWORD},
        "user_b": {"email": email_b, "password": PASSWORD},
        "partial": {
            "conversation_id": partial_cid,
            "message_id": partial.get("id"),
            "chars": len(partial.get("content") or ""),
        },
        "seq": {"conversation_id": seq_cid, "expected": 20},
        "pagination": {"count": 55, "ids": ids, "pages": pages},
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"状态已写入 {STATE_PATH.relative_to(ROOT)}")
    return state


def read_phase(gw: Gateway, state: dict, results: list[tuple[str, bool, str]]) -> None:
    token_a = gw.login_or_register(state["user_a"]["email"], state["user_a"]["password"])
    token_b = gw.login_or_register(state["user_b"]["email"], state["user_b"]["password"])

    partial_cid = state["partial"]["conversation_id"]
    status, page = gw.call("GET", f"/conversations/{partial_cid}/messages?limit=20&order=desc", token=token_a)
    items = page.get("items", []) if status == 200 else []
    assistants = [item for item in items if item.get("role") == "assistant"]
    same = (
        bool(assistants)
        and assistants[0].get("id") == state["partial"]["message_id"]
        and assistants[0].get("status") == "partial"
        and len(assistants[0].get("content") or "") == state["partial"]["chars"]
    )
    record(results, "重启后 partial 消息仍在且内容一致", same, f"{assistants[:1]}")

    seq_cid = state["seq"]["conversation_id"]
    status, page = gw.call("GET", f"/conversations/{seq_cid}/messages?limit=100&order=asc", token=token_a)
    items = page.get("items", []) if status == 200 else []
    record(
        results,
        "重启后 20 条消息 seq 仍唯一连续",
        len(items) == state["seq"]["expected"] and sorted(item["seq"] for item in items) == list(range(1, 21)),
        f"count={len(items)}",
    )

    pages, ids = paginate_conversations(gw, token_b)
    record(
        results,
        "重启后 55 个会话分页仍无重复无遗漏",
        pages == [20, 20, 15] and len(ids) == 55 and len(set(ids)) == 55,
        f"pages={pages} count={len(ids)}",
    )

    status, payload = gw.call("GET", f"/conversations/{partial_cid}", token=token_b)
    code = (payload.get("error") or {}).get("code")
    record(results, "重启后跨用户仍 404", status == 404 and code == "CONVERSATION_NOT_FOUND", f"{status} {code}")


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 2 会话与消息台账验收")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/api/v1")
    parser.add_argument("--phase", choices=["write", "read", "all"], default="all")
    args = parser.parse_args()

    gw = Gateway(args.base_url)
    results: list[tuple[str, bool, str]] = []

    print("阶段 2 · 会话与消息台账")
    print("=" * 60)
    try:
        if args.phase in {"write", "all"}:
            state = write_phase(gw, results)
        else:
            if not STATE_PATH.exists():
                print(f"缺少状态文件 {STATE_PATH}，先跑 --phase write")
                return 1
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

        if args.phase in {"read", "all"}:
            read_phase(gw, state, results)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  执行中断: {type(exc).__name__}: {exc}")
        for name, ok, detail in results:
            print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
        return 1

    print("-" * 60)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failed = sum(1 for _, ok, _ in results if not ok)
    print("-" * 60)
    print(f"{len(results) - failed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
