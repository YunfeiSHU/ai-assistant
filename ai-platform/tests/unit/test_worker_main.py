"""Worker 进程入口的自检（``app/worker/__main__.py``）。

这里只覆盖**不需要真 Kafka 就能验证的那一部分**：配置不满足前提时，进程必须
**拒绝启动**（退出码 2），而不是带着进程私有的任务存储跑起来。

为什么值得单独钉住：这个自检漏了的表现是「消息被消费了、任务永远停在 ``QUEUED``」——
不报错、不退出，只能靠人盯日志发现。手工真机验证是一次性的，这里把它变成回归测试。

完整的启动链路（连 Kafka、拿消息、跑 handler）依赖真 broker，见
``tools/kafka_e2e_check.py`` 与 ``docs/11`` §4 的 E2E-12。
"""

from __future__ import annotations

import logging

import pytest
from tests.conftest import build_settings

from app.worker import __main__ as worker_main


@pytest.fixture
def quiet_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """把启动副作用（日志 / 指标 / tracing 的全局重配）换成空操作。

    被测的是自检而不是启动链路；而且 ``setup_logging`` 是「先摘干净再挂自己」
    的实现（``app/core/logging.py``），留着它会把根 logger 上的 caplog handler
    一起摘掉，影响同进程里其它用例。
    """
    monkeypatch.setattr(worker_main, "setup_logging", lambda **_kwargs: None)
    monkeypatch.setattr(worker_main, "setup_tracing", lambda _settings: None)
    monkeypatch.setattr(worker_main, "configure_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker_main, "configure_tracing", lambda *_args, **_kwargs: None)


async def test_run_worker_refuses_when_task_store_is_not_shared(
    caplog: pytest.LogCaptureFixture, quiet_bootstrap: None
) -> None:
    """``INFRA_BACKEND=memory`` 下必须直接返回退出码 2，并留下可自查的日志。"""
    settings = build_settings(infra_backend="memory", task_runner="kafka")

    with caplog.at_level(logging.ERROR, logger="app.worker"):
        code = await worker_main.run_worker(settings)

    assert code == worker_main.EXIT_MISCONFIGURED
    assert code != 0, "非 0 才能让编排系统感知到这是配置问题"
    records = [record for record in caplog.records if record.message == "worker.misconfigured"]
    assert records, "拒绝启动时必须留下日志，否则运维只能看到「进程秒退」"
    # ``extra`` 里的两个字段是排障的抓手：原因 + 可以直接照做的修法
    assert "任务存储不是共享的" in records[0].reason  # type: ignore[attr-defined]
    assert "INFRA_BACKEND=real" in records[0].hint  # type: ignore[attr-defined]


def test_main_returns_exit_code_two_for_misconfigured_process(
    monkeypatch: pytest.MonkeyPatch, quiet_bootstrap: None
) -> None:
    """同步入口 ``main()`` 也必须把自检结果变成退出码（而不是吞掉）。"""
    monkeypatch.setattr(
        worker_main,
        "get_settings",
        lambda: build_settings(infra_backend="memory", task_runner="kafka"),
    )

    assert worker_main.main() == worker_main.EXIT_MISCONFIGURED
    assert worker_main.EXIT_MISCONFIGURED == 2, "退出码是被文档与编排系统依赖的契约"
