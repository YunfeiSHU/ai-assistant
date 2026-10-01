"""隔离档端到端验真：真实基础设施（Redis/Kafka/MySQL/Milvus/MinIO）+ 独立 Worker。

与 ``tools/kafka_e2e_check.py`` 的分工：那个脚本证"消息能跨进程流转"，本脚本证
**隔离是否成立**，即"大文件入库期间，在线请求有没有被拖垮"。验三件事：

1. 出厂档（``INFRA_BACKEND=real`` + ``TASK_RUNNER=kafka``）能真的跑通：
   API 只入队 → Worker 独立进程消费 → 任务到 ``INDEXED``；
   ⚠️ **不启动 Worker 就不会有终态**（任务停在 ``QUEUED``），这正是"隔离"的代价：
   必须有一个独立进程在跑；
2. **隔离度的观测量**：一边入库，一边每 0.4s 打一次 ``GET /health``，报中位 / P95 / 最大；
3. UP-03 在**真实持久化**下的事实：片数、``truncated``、切片长度分布（从接口读回来算）。

另外两个开关用于 A/B（都不改源码）：
* ``--old-merge``：在 import 主模块前把 ``chunking.MERGE_FILL_RATIO`` 打回 0.3，
  等价于 UP-03 之前的停止条件；
* ``--torch-threads N``：覆盖 Worker 的 ``TORCH_NUM_THREADS``，量"吃满核 vs 一半核"
  对**在线请求**与**自身耗时**的影响（⚠️ 只对 ``--embedding bge`` 有意义：
  云端档下 Worker 进程不跑 torch，这一项不影响任何东西）。

用法：
    python tools/worker_e2e_check.py --mb 8 --embedding siliconflow
    python tools/worker_e2e_check.py --mb 8 --embedding siliconflow --old-merge
    python tools/worker_e2e_check.py --mb 8 --embedding hash          # 零依赖档（不联网）
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
API = "/api/v1"
LINE = "# 上传流式转发测试\n\n这是第 {0} 段占位文本，用于把文件撑到指定大小。\n"


def make_text(target_bytes: int) -> str:
    line_bytes = len(LINE.format(0).encode("utf-8"))
    raw = "".join(LINE.format(i) for i in range(math.ceil(target_bytes / line_bytes))).encode(
        "utf-8"
    )
    if len(raw) > target_bytes:
        cut = min(target_bytes, len(raw) - 1)
        while cut > 0 and raw[cut] != 10:
            cut -= 1
        raw = raw[: cut + 1]
    return raw.decode("utf-8")


def req(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict | None = None,
    timeout: float = 300.0,
) -> tuple[int, object, float]:
    r = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw, code = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, code = exc.read(), exc.code
    ms = (time.perf_counter() - t0) * 1000
    try:
        return code, json.loads(raw.decode("utf-8")), ms
    except Exception:
        return code, raw.decode("utf-8", "replace"), ms


def multipart_text(text: str, doc_name: str) -> tuple[bytes, str]:
    b = "----workerE2E" + str(int(time.time() * 1000))
    head = (
        f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{doc_name}"\r\n'
        f"Content-Type: text/plain\r\n\r\n"
    )
    return (head + text + f"\r\n--{b}--\r\n").encode("utf-8"), f"multipart/form-data; boundary={b}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=float, default=8.0)
    ap.add_argument(
        "--embedding", default="siliconflow", choices=["siliconflow", "hash", "bge", "ark"]
    )
    ap.add_argument("--port", type=int, default=8794)
    ap.add_argument("--poll-cap", type=float, default=1800.0)
    ap.add_argument(
        "--old-merge",
        action="store_true",
        help="把 MERGE_FILL_RATIO 打回 0.3（＝旧停止条件）跑一遍做 A/B",
    )
    ap.add_argument(
        "--torch-threads", type=int, default=0, help="覆盖 TORCH_NUM_THREADS（0 = 用 .env/默认值）"
    )
    args = ap.parse_args()

    env = dict(os.environ)
    env.update(
        {
            "HOST": "127.0.0.1",
            "AUTH_ENABLED": "false",
            "OTEL_ENABLED": "false",
            "METRICS_ENABLED": "false",
            "METRICS_PORT": "0",
            "LOG_LEVEL": "INFO",
            "LOG_FORMAT": "console",
            "PYTHONUNBUFFERED": "1",
            "INFRA_BACKEND": "real",
            "TASK_RUNNER": "kafka",
            "EMBEDDING_PROVIDER": args.embedding,
            "RERANKER_ENABLED": "false",
            "WORKER_EMBED_CONCURRENCY": "1",
        }
    )
    base = f"http://127.0.0.1:{args.port}"
    # 日志句柄必须活到子进程结束（子进程边跑边写），所以不能用 with：
    log_dir = Path(tempfile.gettempdir()) / "ai_platform_worker_e2e"
    log_dir.mkdir(exist_ok=True)
    api_log = (log_dir / "api.log").open("w", encoding="utf-8", errors="replace")
    worker_log_path = log_dir / "worker.log"
    worker_log = worker_log_path.open("w", encoding="utf-8", errors="replace")

    print(f"== 隔离档 E2E：{args.mb:g}MB / EMBEDDING={args.embedding} ==")
    api = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=api_log,
        stderr=subprocess.STDOUT,
    )
    worker: subprocess.Popen | None = None
    try:
        t0 = time.perf_counter()
        health = None
        while time.perf_counter() - t0 < 180:
            try:
                code, body, _ = req("GET", base + API + "/health", timeout=5)
                if code == 200 and isinstance(body, dict):
                    health = body
                    break
            except Exception:
                pass
            if api.poll() is not None:
                print(f"!! API 退出 code={api.returncode}")
                return
        if health is None:
            print("!! /health 未就绪")
            return
        deps = health.get("dependencies", {})
        print(f"   API boot={time.perf_counter() - t0:.1f}s status={health.get('status')}")
        for name, info in deps.items():
            if isinstance(info, dict):
                print(
                    f"     {name:9} ok={info.get('ok')} skipped={info.get('skipped', False)} "
                    f"latency={info.get('latency_ms')}ms backend={info.get('backend', '')}"
                    f"{' reason=' + str(info.get('reason')) if info.get('reason') else ''}"
                )

        # ---- Worker 独立进程 ----
        wenv = dict(env)
        wenv["METRICS_PORT"] = "0"
        if args.torch_threads > 0:
            wenv["TORCH_NUM_THREADS"] = str(args.torch_threads)
            print(f"   [A/B] Worker TORCH_NUM_THREADS={args.torch_threads}")
        if args.old_merge:
            # 不改源码：在 import 主模块之前把 chunking 的填充目标打回 0.3。
            # `merge_target_tokens` 读的是模块全局，所以这一句就等于旧停止条件
            # （旧实现是 floor=0.3 当停止条件 + 1.5× 上限；碎段场景下上限不生效）。
            worker_cmd = [
                sys.executable,
                "-u",
                "-c",
                "import app.rag.chunking as c; c.MERGE_FILL_RATIO = 0.3; "
                "import runpy; runpy.run_module('app.worker', run_name='__main__')",
            ]
            print("   [A/B] Worker 使用旧停止条件（MERGE_FILL_RATIO=0.3）")
        else:
            worker_cmd = [sys.executable, "-u", "-m", "app.worker"]
        worker = subprocess.Popen(
            worker_cmd, cwd=str(ROOT), env=wenv, stdout=worker_log, stderr=subprocess.STDOUT
        )
        time.sleep(6)
        if worker.poll() is not None:
            print(f"!! Worker 退出 code={worker.returncode}")
            worker_log.flush()
            print(worker_log_path.read_text(encoding="utf-8", errors="replace")[-1500:])
            return
        print(f"   Worker 已启动 pid={worker.pid}")

        # ---- 建 KB + 上传 ----
        code, kb, _ = req(
            "POST",
            base + API + "/knowledge-bases",
            body=json.dumps(
                {"name": f"iso-{int(time.time())}", "chunk_size": 512, "chunk_overlap": 64}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        if code not in (200, 201):
            print(f"!! 建 KB -> {code} {str(kb)[:300]}")
            return
        kb_id = kb["id"]

        text = make_text(int(args.mb * 1024 * 1024))
        body, ctype = multipart_text(text, f"iso-{args.mb:g}mb.txt")
        code, accepted, up_ms = req(
            "POST",
            f"{base}{API}/knowledge-bases/{kb_id}/documents",
            body=body,
            headers={"Content-Type": ctype},
        )
        print(
            f"   上传 {len(text.encode('utf-8'))} bytes -> {code} ({up_ms:.0f}ms) "
            f"status={accepted.get('status') if isinstance(accepted, dict) else accepted}"
        )
        if code not in (200, 201, 202):
            print("   body:", str(accepted)[:500])
            return
        task_id, doc_id = accepted["task_id"], accepted["doc_id"]

        # ---- 边等任务边采样 API 延迟（隔离是否成立的观测量）----
        t_start = time.perf_counter()
        health_ms: list[float] = []
        timeline: list[str] = []
        final = None
        next_probe = 0.0
        while time.perf_counter() - t_start < args.poll_cap:
            now = time.perf_counter()
            if now >= next_probe:
                next_probe = now + 0.4
                try:
                    code, _hb, ms = req("GET", base + API + "/health", timeout=10)
                    if code == 200:
                        health_ms.append(ms)
                except Exception:
                    pass
                code, task, _ = req("GET", f"{base}{API}/tasks/{task_id}", timeout=10)
                if code == 200 and isinstance(task, dict):
                    mark = (
                        f"{task.get('status')}/{task.get('stage')}:{task.get('chunks_done')}"
                        f"/{task.get('chunks_total')}@{task.get('progress')}%"
                    )
                    if not timeline or timeline[-1] != mark:
                        timeline.append(mark)
                    if task.get("status") in ("SUCCEEDED", "FAILED", "CANCELED"):
                        final = task
                        break
            time.sleep(0.05)
        elapsed = time.perf_counter() - t_start
        print(f"   终态 {final and final.get('status')}｜上传->终态 {elapsed:.1f}s")
        print("   轨迹: " + " -> ".join(timeline[:8]) + (" ..." if len(timeline) > 8 else ""))
        if health_ms:
            health_ms.sort()
            p50 = statistics.median(health_ms)
            p95 = health_ms[min(len(health_ms) - 1, int(len(health_ms) * 0.95))]
            print(
                f"   入库期间 /health：n={len(health_ms)} 中位={p50:.1f}ms "
                f"P95={p95:.1f}ms 最大={health_ms[-1]:.1f}ms"
            )
        if final is None:
            print("   !! 未在时限内结束（Kafka/Worker 链路有问题）")
            return

        code, doc, _ = req("GET", f"{base}{API}/documents/{doc_id}")
        print(
            f"   document: chunk_count={doc.get('chunk_count')} chunks_total={doc.get('chunks_total')} "
            f"truncated={doc.get('truncated')} chars={doc.get('char_count')}"
        )
        from app.core.tokens import count_tokens

        lens: list[int] = []
        cursor = None
        for _ in range(3):
            url = f"{base}{API}/documents/{doc_id}/chunks?limit=100"
            if cursor:
                url += "&cursor=" + urllib.parse.quote(str(cursor))
            code, chunks, _ = req("GET", url)
            if not isinstance(chunks, dict) or "items" not in chunks:
                print(f"   chunks -> {code} {str(chunks)[:150]}")
                break
            lens.extend(count_tokens(it["content"]) for it in chunks["items"])
            cursor = chunks.get("next_cursor")
            if not cursor:
                break
        if lens:
            lens.sort()
            print(
                f"   接口读回 {len(lens)} 片：min={lens[0]} 中位={lens[len(lens) // 2]} max={lens[-1]}"
            )
    finally:
        for proc in (worker, api):
            if proc is None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        api_log.close()
        worker_log.flush()
        worker_log.close()
        wlog = worker_log_path.read_text(encoding="utf-8", errors="replace")
        print(f"\n   Worker 日志（{worker_log_path.name}，{len(wlog)} bytes）关键行：")
        for line in wlog.splitlines():
            if any(k in line for k in ("worker.", "task.", "ingest.", "ERROR", "embedding.")):
                print("     " + line[:210])


if __name__ == "__main__":
    main()
