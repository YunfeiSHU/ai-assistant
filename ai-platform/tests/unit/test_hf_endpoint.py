"""单元测试：``HF_ENDPOINT`` 的生效路径（``docs/12-§13.1``）。

存在的理由：``huggingface_hub`` 在 **import 期**就把 ``HF_ENDPOINT`` 固化进
``constants.ENDPOINT`` / ``constants.HUGGINGFACE_CO_URL_TEMPLATE``，而
``import app.main`` 期间 ``langchain_text_splitters → transformers`` 会提前把它
拉进来 —— 所以「设了环境变量」并不等于「下载会走镜像」。

这组用例把四件事固定住：

1. 配了 ``HF_ENDPOINT`` → ``os.environ`` **与已导入的库常量都被改写**；
2. 没配（或只有空白）→ 什么都不做，不往环境里写空值；
3. 真实环境变量优先于 ``.env``（与 pydantic-settings 的优先级一致）；
4. 重复调用结果一致（``create_app`` 在测试里会被反复调用）。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest

from app.config import Settings, apply_hf_endpoint

HF_ENDPOINT_ENV = "HF_ENDPOINT"
MIRROR = "https://hf-mirror.com"


@pytest.fixture
def clean_env() -> Iterator[None]:
    """备份并清空 ``HF_ENDPOINT``，用例结束后原样恢复（避免串味到其它用例）。"""
    saved = os.environ.get(HF_ENDPOINT_ENV)
    os.environ.pop(HF_ENDPOINT_ENV, None)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(HF_ENDPOINT_ENV, None)
        else:
            os.environ[HF_ENDPOINT_ENV] = saved


@pytest.fixture
def hf_constants() -> Iterator[object]:
    """把 ``huggingface_hub.constants`` 的两个端点常量在用例后还原。

    它们是**模块级常量**，被改写后会在整个进程里一直生效 ——
    不还原的话，「镜像指向」这件事会泄漏给同进程内的其它用例。
    """
    module = pytest.importorskip("huggingface_hub.constants")
    saved = (module.ENDPOINT, module.HUGGINGFACE_CO_URL_TEMPLATE)
    try:
        yield module
    finally:
        module.ENDPOINT, module.HUGGINGFACE_CO_URL_TEMPLATE = saved


def test_sets_environment_variable(make_settings: Callable[..., Settings], clean_env: None) -> None:
    """配了镜像就写进 ``os.environ``（对后续才导入的模块与子进程有效）。"""
    settings = make_settings(hf_endpoint=MIRROR)

    assert apply_hf_endpoint(settings) == MIRROR
    assert os.environ[HF_ENDPOINT_ENV] == MIRROR


def test_rewrites_huggingface_hub_constants(
    make_settings: Callable[..., Settings], clean_env: None, hf_constants: object
) -> None:
    """已导入的库常量也必须被改写 —— 这是导入顺序不可控时唯一确定性生效的办法。

    断言的是**库自己的运行时状态**，而不是「我们自己的配置对象读到了值」：
    真正的判据是 ``hf_hub_url()`` 生成的下载地址（它用的是
    ``HUGGINGFACE_CO_URL_TEMPLATE``；只改 ``ENDPOINT`` 是不够的，
    因为 ``hf_hub_url`` 仅在显式传 ``endpoint=`` 时才重写域名）。
    """
    from huggingface_hub import hf_hub_url

    apply_hf_endpoint(make_settings(hf_endpoint=MIRROR))

    assert hf_constants.ENDPOINT == MIRROR  # type: ignore[attr-defined]
    assert hf_constants.HUGGINGFACE_CO_URL_TEMPLATE.startswith(  # type: ignore[attr-defined]
        MIRROR + "/"
    )
    assert hf_hub_url("BAAI/bge-m3", "config.json").startswith(MIRROR + "/")


def test_noop_when_not_configured(make_settings: Callable[..., Settings], clean_env: None) -> None:
    """没配就什么都不做：返回 ``None``，且**不**往环境里写空值。"""
    assert apply_hf_endpoint(make_settings(hf_endpoint="")) is None
    assert HF_ENDPOINT_ENV not in os.environ


def test_blank_configured_value_is_treated_as_absent(
    make_settings: Callable[..., Settings], clean_env: None
) -> None:
    """只有空白字符等同于没配（``.env`` 里手滑留空格不该把镜像置空）。"""
    assert apply_hf_endpoint(make_settings(hf_endpoint="   ")) is None
    assert HF_ENDPOINT_ENV not in os.environ


def test_real_environment_variable_wins(
    make_settings: Callable[..., Settings], clean_env: None
) -> None:
    """真实环境变量优先于 ``.env``（容器编排注入的镜像不该被 .env 覆盖）。"""
    os.environ[HF_ENDPOINT_ENV] = "https://mirror.example.com"

    assert apply_hf_endpoint(make_settings(hf_endpoint=MIRROR)) == "https://mirror.example.com"
    assert os.environ[HF_ENDPOINT_ENV] == "https://mirror.example.com"


def test_idempotent_across_calls(
    make_settings: Callable[..., Settings], clean_env: None, hf_constants: object
) -> None:
    """重复调用结果一致（同一个进程里 ``create_app`` 会被反复调用）。"""
    settings = make_settings(hf_endpoint=MIRROR)

    assert apply_hf_endpoint(settings) == MIRROR
    assert apply_hf_endpoint(settings) == MIRROR
    assert os.environ[HF_ENDPOINT_ENV] == MIRROR
