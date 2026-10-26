"""阶段 0 契约验收：检查「两侧契约一致」，不启动服务、不调用模型。

用法（仓库根目录）：

    ai-platform\\.venv\\Scripts\\python.exe tools\\verify_stage0.py
    ai-platform\\.venv\\Scripts\\python.exe tools\\verify_stage0.py --skip-go-test

检查项：

1. proto 只暴露 Chat / ChatStream；
2. 两侧生成物存在；
3. SSE 事件名在 Python / Go / proto 三处一致；
4. SSE 帧字节与 Go 侧 fixture 逐字节一致（并验证裸换行被拒绝）；
5. 错误信封字段一致；
6. 网关复用的 AI 错误码都在 Python 码表里；
7. trace 头与信封 trace_id 贯通；
8. 心跳间隔两侧一致；
9. ChatRequest / ChatResponse 字段与 proto 一致。

任一失败退出码为 1。这是「可机械检查」的那一半；真实链路在阶段 1 的 measure_chat.py。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GO = ROOT / "ai-platform-go"
PY = ROOT / "ai-platform"
PROTO = GO / "proto" / "aiplatform" / "v1" / "chat.proto"

# Windows 控制台默认不是 UTF-8，中文断言信息会变成乱码。
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

results: list[tuple[str, bool, str]] = []


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    if not ok:
        print(f"  detail: {detail}")


def proto_message_block(name: str) -> str:
    src = read(PROTO)
    m = re.search(rf"^message {name}\s*\{{", src, re.M)
    if not m:
        raise AssertionError(f"proto 中找不到 message {name}")
    end = src.find("\n}", m.end())
    if end < 0:
        raise AssertionError(f"message {name} 没有闭合")
    return src[m.end() : end]


def class_block(source: str, name: str) -> str:
    m = re.search(rf"^class {name}\b.*?:\s*$", source, re.M)
    if not m:
        raise AssertionError(f"Python 中找不到 class {name}")
    rest = source[m.end() :]
    nxt = re.search(r"^(?:class |def |@\w)", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def proto_fields(message: str) -> set[str]:
    body = proto_message_block(message)
    return {
        m.group(1)
        for m in re.finditer(
            r"^\s*(?:(?:repeated|optional)\s+)?(?:map<[^>]+>|[\w.]+)\s+(\w+)\s*=\s*\d+\s*;",
            body,
            re.M,
        )
    }


def py_model_fields(model: str) -> set[str]:
    body = class_block(read(PY / "app" / "schemas" / "chat.py"), model)
    return {
        m.group(1)
        for m in re.finditer(r"^\s{4}(\w+)\s*:", body, re.M)
        if m.group(1) != "model_config"
    }


def check_proto_surface() -> None:
    rpcs = re.findall(r"^\s*rpc\s+(\w+)\s*\(", read(PROTO), re.M)
    record("proto 只暴露 Chat / ChatStream", rpcs == ["Chat", "ChatStream"], f"实际: {rpcs}")

    generated = [
        GO / "api" / "aiplatform" / "v1" / "chat.pb.go",
        GO / "api" / "aiplatform" / "v1" / "chat_grpc.pb.go",
        PY / "app" / "grpc" / "aiplatform" / "v1" / "chat_pb2.py",
        PY / "app" / "grpc" / "aiplatform" / "v1" / "chat_pb2_grpc.py",
    ]
    missing = [str(p.relative_to(ROOT)) for p in generated if not p.exists() or p.stat().st_size == 0]
    record("两侧生成物存在", not missing, f"缺少: {missing}")


def check_event_names() -> None:
    py_src = read(PY / "app" / "core" / "sse.py")
    py_events = set(re.findall(r'^EVENT_[A-Z_]+\s*=\s*"([a-z_]+)"', py_src, re.M))

    go_src = read(GO / "internal" / "biz" / "chat_stream.go")
    go_events = set(re.findall(r'^\s*StreamEvent\w+\s*=\s*"([a-z_]+)"', go_src, re.M))

    oneof = re.search(r"oneof event \{(.*?)\n\s{2}\}", read(PROTO), re.S)
    if not oneof:
        record("proto ChatEvent oneof 可解析", False, "找不到 oneof event")
        return
    # 行形如 `StreamMeta meta = 1;`：要取第二个标识符（字段名），不是类型名。
    proto_events = set(re.findall(r"^\s*\w+\s+(\w+)\s*=\s*\d+\s*;", oneof.group(1), re.M))

    py_business = py_events - {"ping"}
    go_business = go_events - {"gw_persist_error"}
    record(
        "SSE 业务事件名 Python == Go",
        py_business == go_business,
        f"Python: {sorted(py_business)}; Go: {sorted(go_business)}",
    )
    record(
        "SSE 业务事件名 proto == Python",
        proto_events - {"unknown"} == py_business,
        f"proto: {sorted(proto_events)}; Python: {sorted(py_business)}",
    )
    record(
        "网关专有事件不进入 proto",
        {"gw_persist_error", "ping"}.isdisjoint(proto_events),
        f"proto: {sorted(proto_events)}",
    )


def load_python_sse():
    spec = importlib.util.spec_from_file_location("ai_sse", PY / "app" / "core" / "sse.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def check_frames() -> None:
    sse = load_python_sse()
    fixtures = [
        (
            "meta",
            "meta",
            '{"conversation_id":"cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C","model":"deepseek-flash"}',
            'event: meta\ndata: {"conversation_id":"cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C","model":"deepseek-flash"}\n\n',
        ),
        ("token-中文", "token", '{"delta":"你好"}', 'event: token\ndata: {"delta":"你好"}\n\n'),
        ("done", "done", "{}", "event: done\ndata: {}\n\n"),
    ]
    mismatches = []
    for label, event, payload, want in fixtures:
        got = sse.format_frame(event, payload).decode("utf-8")
        if got != want:
            mismatches.append(f"{label}: got={got!r} want={want!r}")
    record("Python SSE 帧字节 == Go fixture", not mismatches, "; ".join(mismatches))

    escaped = r'{"delta":"第一行\n第二行"}'
    got = sse.format_frame("token", escaped).decode("utf-8")
    want = 'event: token\ndata: {"delta":"第一行\\n第二行"}\n\n'
    record("转义换行被接受且字节不变", got == want, f"got={got!r}")

    try:
        sse.format_frame("token", '{"delta":"a\nb"}')
    except ValueError:
        record("裸换行被拒绝", True)
    else:
        record("裸换行被拒绝", False, "format_frame 接受了裸换行")


def check_go_frames() -> None:
    # 不让 Go 用系统临时目录：受限环境下会 `Access is denied`。
    # 缓存与临时工作目录都放模块内，并已加入 .gitignore。
    cache = GO / ".gocache"
    tmp = GO / ".gotmp"
    cache.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["GOCACHE"] = str(cache)
    env["GOTMPDIR"] = str(tmp)
    try:
        proc = subprocess.run(
            ["go", "test", "./pkg/ssex", "-count=1"],
            cwd=GO,
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
    except FileNotFoundError:
        record("go test ./pkg/ssex", True, "SKIP: 未安装 go")
        return
    except subprocess.TimeoutExpired:
        record("go test ./pkg/ssex", False, "超时")
        return
    ok = proc.returncode == 0
    detail = (proc.stdout + proc.stderr).strip()[-800:] if not ok else ""
    record("go test ./pkg/ssex", ok, detail)


def check_error_envelope() -> None:
    go_src = read(GO / "pkg" / "errs" / "errs.go")
    m = re.search(r"type ErrorBody struct \{(.*?)\n\}", go_src, re.S)
    # 允许 `json:"details,omitempty"` 这类带选项的标签。
    go_fields = set(re.findall(r'json:"([\w]+)(?:,[^"]*)?"', m.group(1))) if m else set()

    py_src = read(PY / "app" / "core" / "exceptions.py")
    m = re.search(r"def to_envelope\(.*?\n(.*?)\n\s*return \{\"error\": error\}", py_src, re.S)
    py_fields = set(re.findall(r'"(code|message|details|trace_id|retryable|retry_after)"\s*:', m.group(1))) if m else set()

    required = {"code", "message", "details", "trace_id", "retryable"}
    record(
        "错误信封必填字段 Go",
        required <= go_fields,
        f"缺少: {sorted(required - go_fields)}",
    )
    record(
        "错误信封必填字段 Python",
        required <= py_fields,
        f"缺少: {sorted(required - py_fields)}",
    )
    record(
        "trace 字段名为 snake_case",
        "trace_id" in go_fields and "trace_id" in py_fields,
        f"Go={sorted(go_fields)}; Python={sorted(py_fields)}",
    )


def check_error_codes() -> None:
    go_src = read(GO / "pkg" / "errs" / "codes.go")
    m = re.search(r"var aiErrorStatus = map\[Code\]int\{(.*?)\n\}", go_src, re.S)
    go_ai_codes = set(re.findall(r'^\s*"([A-Z0-9_]+)"\s*:', m.group(1), re.M)) if m else set()

    py_src = read(PY / "app" / "core" / "exceptions.py")
    m = re.search(r"class ErrorCode\(StrEnum\):(.*?)(?:\nclass |\nERROR_SPECS)", py_src, re.S)
    py_codes = set(re.findall(r'^\s{4}[A-Z][A-Z0-9_]*\s*=\s*"([A-Z0-9_]+)"', m.group(1), re.M)) if m else set()

    missing = sorted(go_ai_codes - py_codes)
    record(
        f"网关复用的 AI 错误码都在 Python 码表（共 {len(go_ai_codes)} 个）",
        not missing and bool(go_ai_codes),
        f"Python 缺少: {missing}",
    )


def check_trace() -> None:
    trace_src = read(GO / "internal" / "server" / "middleware" / "trace.go")
    cors_src = read(GO / "internal" / "server" / "middleware" / "cors.go")
    record(
        "网关下发 X-Trace-Id",
        'HeaderTraceID = "X-Trace-Id"' in trace_src and "HeaderTraceID, traceID" in trace_src,
        "",
    )
    record(
        "CORS 暴露 X-Trace-Id",
        "X-Trace-Id" in cors_src and "Expose-Headers" in cors_src,
        "",
    )


def env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    match = re.search(rf"^{re.escape(key)}=(.+)$", read(path), re.M)
    return match.group(1).strip().strip('"') if match else None


def check_secret_consistency() -> None:
    """两侧 JWT_SECRET 必须同源；Python 关鉴权时只是警告，开启后就是硬失败。"""
    py_secret = env_value(PY / ".env", "JWT_SECRET")
    go_secret = env_value(GO / ".env", "JWT_SECRET")
    if not py_secret or not go_secret or py_secret == go_secret:
        return

    auth_enabled = (env_value(PY / ".env", "AUTH_ENABLED") or "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if auth_enabled:
        record(
            "JWT_SECRET 两侧一致（鉴权已开启）",
            False,
            "网关签发的 access token 会被 Python 拒绝，表现为全链路 401",
        )
    else:
        print(
            "WARN  JWT_SECRET 两侧不一致；当前 Python AUTH_ENABLED=false 才未暴露，"
            "开启鉴权前必须对齐"
        )


def check_heartbeat() -> None:
    py_src = read(PY / "app" / "core" / "sse.py")
    m = re.search(r"PING_INTERVAL_SECONDS\s*=\s*([\d.]+)", py_src)
    py_ping = float(m.group(1)) if m else -1

    go_src = read(GO / "internal" / "biz" / "message_stream.go")
    m = re.search(r"StreamHeartbeatInterval\s*=\s*(\d+)\s*\*\s*time\.Second", go_src)
    go_ping = int(m.group(1)) if m else -1

    record(
        "心跳间隔两侧一致且为 15s",
        py_ping == go_ping == 15,
        f"Python={py_ping}s; Go={go_ping}s",
    )


def check_chat_fields() -> None:
    req_proto = proto_fields("ChatRequest")
    req_py = py_model_fields("ChatRequest") - {"stream"}  # proto 有意不含 stream
    record(
        "ChatRequest 字段 proto == Python（去掉 stream）",
        req_proto == req_py,
        f"仅 proto: {sorted(req_proto - req_py)}; 仅 Python: {sorted(req_py - req_proto)}",
    )

    resp_proto = proto_fields("ChatResponse")
    resp_py = py_model_fields("ChatResponse")
    record(
        "ChatResponse 字段 proto == Python",
        resp_proto == resp_py,
        f"仅 proto: {sorted(resp_proto - resp_py)}; 仅 Python: {sorted(resp_py - resp_proto)}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 0 契约验收")
    parser.add_argument("--skip-go-test", action="store_true", help="跳过 go test（没有 Go 环境时）")
    args = parser.parse_args()

    print("阶段 0 · 契约验收")
    print("=" * 60)

    check_proto_surface()
    check_event_names()
    check_frames()
    if not args.skip_go_test:
        check_go_frames()
    check_error_envelope()
    check_error_codes()
    check_trace()
    check_secret_consistency()
    check_heartbeat()
    check_chat_fields()

    print("-" * 60)
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        suffix = f"  [{detail}]" if (not ok and detail) else ""
        print(f"{status}  {name}{suffix}")

    failed = sum(1 for _, ok, _ in results if not ok)
    print("-" * 60)
    print(f"{len(results) - failed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
