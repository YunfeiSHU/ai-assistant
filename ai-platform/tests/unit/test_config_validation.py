"""单元测试：启动期配置校验。

覆盖 ``AC-NFR-12``（缺关键配置 → 明确失败且指出变量名）与 ``AC-NFR-05``（prod 强制鉴权）。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.config import ConfigurationError, Settings


def test_baseline_local_config_is_valid(make_settings: Callable[..., Settings]) -> None:
    """本地基线配置必须通过（否则开发者开局就卡住）。"""
    make_settings().validate_for_startup()


def test_missing_jwt_secret_fails_with_variable_name(
    make_settings: Callable[..., Settings],
) -> None:
    """``AC-NFR-12``：缺 ``JWT_SECRET`` 时启动失败且报出变量名。"""
    settings = make_settings(jwt_secret="")

    with pytest.raises(ConfigurationError, match="JWT_SECRET"):
        settings.validate_for_startup()


def test_missing_openai_key_fails_outside_local(
    make_settings: Callable[..., Settings],
) -> None:
    """非 local 环境缺 ``OPENAI_API_KEY`` 必须拒绝启动（避免上线才发现）。"""
    settings = make_settings(app_env="staging", jwt_secret="s", openai_api_key="")

    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        settings.validate_for_startup()


def test_local_allows_missing_openai_key(make_settings: Callable[..., Settings]) -> None:
    """local 环境允许暂时没有 key（便于只跑单测 / 契约测试）。"""
    make_settings(openai_api_key="").validate_for_startup()


def test_prod_forces_auth_even_when_disabled(make_settings: Callable[..., Settings]) -> None:
    """``AC-NFR-05``：``APP_ENV=prod`` + ``AUTH_ENABLED=false`` 仍强制校验 JWT。"""
    settings = make_settings(
        app_env="prod",
        auth_enabled=False,
        infra_backend="real",
        cors_origins=["https://app.example.com"],
    )

    assert settings.auth_required is True


def test_prod_rejects_memory_backend(make_settings: Callable[..., Settings]) -> None:
    """生产不允许用进程内后端（数据不落库 = 静默丢数据）。"""
    settings = make_settings(app_env="prod", infra_backend="memory")

    with pytest.raises(ConfigurationError, match="INFRA_BACKEND"):
        settings.validate_for_startup()


def test_prod_rejects_wildcard_cors(make_settings: Callable[..., Settings]) -> None:
    """生产 ``CORS_ORIGINS=['*']`` 与 ``allow_credentials`` 组合是危险的。"""
    settings = make_settings(app_env="prod", infra_backend="real", cors_origins=["*"])

    with pytest.raises(ConfigurationError, match="CORS_ORIGINS"):
        settings.validate_for_startup()


def test_invalid_chunk_strategy_fails(make_settings: Callable[..., Settings]) -> None:
    """``CHUNK_OVERLAP >= CHUNK_SIZE`` 会让切分器死循环，必须拒绝。"""
    settings = make_settings(chunk_size=128, chunk_overlap=128)

    with pytest.raises(ConfigurationError, match="CHUNK_OVERLAP"):
        settings.validate_for_startup()


def test_context_budget_must_exceed_system_plus_output(
    make_settings: Callable[..., Settings],
) -> None:
    """``CONTEXT_TOKEN_BUDGET`` 必须给 system + 输出留出空间。"""
    settings = make_settings(context_token_budget=1000)

    with pytest.raises(ConfigurationError, match="CONTEXT_TOKEN_BUDGET"):
        settings.validate_for_startup()


def test_all_errors_are_reported_together(make_settings: Callable[..., Settings]) -> None:
    """一次报出全部问题，避免「改一个错、再来一个」。"""
    settings = make_settings(jwt_secret="", chunk_size=64, chunk_overlap=64)

    with pytest.raises(ConfigurationError) as excinfo:
        settings.validate_for_startup()

    message = str(excinfo.value)
    assert "JWT_SECRET" in message
    assert "CHUNK_OVERLAP" in message


def test_negative_summary_keep_recent_turns_is_rejected(
    make_settings: Callable[..., Settings],
) -> None:
    """``SUMMARY_KEEP_RECENT_TURNS`` 不能为负（会被 ``max(0, ...)`` 静默吞掉）。"""
    settings = make_settings(summary_keep_recent_turns=-1)

    with pytest.raises(ConfigurationError, match="SUMMARY_KEEP_RECENT_TURNS"):
        settings.validate_for_startup()


def test_csv_style_list_is_accepted() -> None:
    """``.env`` 里写 ``CORS_ORIGINS=a,b`` 也应被接受（比 JSON 数组好写）。"""
    settings = Settings(
        _env_file=None,  # type: ignore[arg-type]
        cors_origins="https://a.example.com, https://b.example.com",
        jwt_secret="s",
    )

    assert settings.cors_origins == ["https://a.example.com", "https://b.example.com"]


def test_upload_max_bytes_derived() -> None:
    """``upload_max_bytes`` 是 ``UPLOAD_MAX_MB`` 的派生值（避免两处各写一遍换算）。"""
    settings = Settings(_env_file=None, upload_max_mb=50, jwt_secret="s")  # type: ignore[arg-type]

    assert settings.upload_max_bytes == 50 * 1024 * 1024


def test_derived_flags() -> None:
    """``is_local`` / ``debug_tools_enabled`` 决定调试接口是否开放（``AC-AGENT-08``）。"""
    local = Settings(_env_file=None, app_env="local", jwt_secret="s")  # type: ignore[arg-type]
    prod = Settings(_env_file=None, app_env="prod", jwt_secret="s")  # type: ignore[arg-type]

    assert local.is_local is True
    assert local.debug_tools_enabled is True
    assert prod.is_local is False
    assert prod.debug_tools_enabled is False
