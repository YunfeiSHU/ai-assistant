"""Agent 与工具的契约测试（``AC-AGENT-01..09`` / E2E-3，``docs/04`` §4）。

契约层只验证「经路由 + SSE 之后行为没走样」：循环内部的护栏细节在
``tests/unit/test_agent_loop.py`` 里逐条测过，这里重复一遍只会让失败定位变难。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.support.fake_llm import FakeLLM, tool_call

from app.agent.loop import FINISH_MAX_STEPS
from app.config import Settings
from app.llm.base import LLMToolCall
from app.tools import build_tool_registry
from app.tools.registry import ToolRegistrationError

AGENT = "/api/v1/agent/run"
AGENT_STREAM = "/api/v1/agent/run/stream"
TOOLS = "/api/v1/tools"


def _events(body: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 文本解析成 ``(event, data)`` 列表。"""
    parsed: list[tuple[str, dict[str, Any]]] = []
    for block in body.strip().split("\n\n"):
        name = ""
        data: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name:
            parsed.append((name, data))
    return parsed


def _body(**overrides: Any) -> dict[str, Any]:
    """默认请求体：关掉 RAG/Memory，让用例只考验工具行为。"""
    payload: dict[str, Any] = {
        "query": "帮我算一下",
        "use_rag": False,
        "use_memory": False,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# AC-AGENT-01 / E2E-3
# ---------------------------------------------------------------------------


def test_agent_runs_tool_then_answers(agent_client: TestClient, fake_llm: FakeLLM) -> None:
    """``AC-AGENT-01``：模型先要 calculator，再给文本 → ``steps=2``。"""
    fake_llm.replies = ["", "1+1 等于 2"]
    fake_llm.tool_scripts = [[tool_call("calculator", {"expression": "1+1"})]]

    response = agent_client.post(AGENT, json=_body())
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["steps"] == 2
    assert payload["finish_reason"] == "stop"
    assert len(payload["tool_calls"]) == 1
    trace = payload["tool_calls"][0]
    assert trace["name"] == "calculator"
    assert trace["status"] == "ok"
    assert trace["arguments"] == {"expression": "1+1"}
    assert "2" in payload["answer"]
    assert payload["usage"]["total_tokens"] > 0


def test_agent_multistep_retrieve_then_calculate(
    agent_client: TestClient, fake_llm: FakeLLM
) -> None:
    """``E2E-3``：``kb_retrieve`` → ``calculator`` → 文本 → ``steps=3``，两条轨迹都 ok。

    这条链路同时验证「工具结果能进入下一轮上下文」：第二轮能看到第一轮的检索结果，
    否则计算类追问无从下手。
    """
    fake_llm.replies = ["", "", "综合答案是 4"]
    fake_llm.tool_scripts = [
        [tool_call("kb_retrieve", {"query": "销售额", "kb_ids": []})],
        [tool_call("calculator", {"expression": "2+2"})],
    ]

    response = agent_client.post(AGENT, json=_body(use_rag=True))
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["steps"] == 3
    assert [item["name"] for item in payload["tool_calls"]] == ["kb_retrieve", "calculator"]
    assert all(item["status"] == "ok" for item in payload["tool_calls"])


# ---------------------------------------------------------------------------
# AC-AGENT-02
# ---------------------------------------------------------------------------


def test_agent_stops_at_max_steps_with_http_200(
    agent_client: TestClient, fake_llm: FakeLLM
) -> None:
    """``AC-AGENT-02``：模型永远要求调工具时停在步数上限，HTTP 仍是 200。"""
    fake_llm.replies = ["", "", "", "", "", "", "", "", "已尽力回答"]
    fake_llm.tool_scripts = [
        [tool_call("calculator", {"expression": f"{index}+1"}, call_id=f"c{index}")]
        for index in range(10)
    ]

    response = agent_client.post(AGENT, json=_body(max_steps=8))
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["steps"] == 8
    assert payload["finish_reason"] == FINISH_MAX_STEPS
    assert len(payload["tool_calls"]) == 8
    assert payload["degraded"] is True
    assert "max_steps" in payload["degraded_reasons"]


def test_agent_request_cannot_exceed_configured_max_steps(
    agent_client: TestClient, fake_llm: FakeLLM
) -> None:
    """请求只能调小上限；``max_steps`` 超过配置时按配置执行（运维的最后一道闸门）。"""
    fake_llm.replies = ["", "", "", "", "", "", "", "", "收尾"]
    fake_llm.tool_scripts = [
        [tool_call("calculator", {"expression": f"{index}+1"}, call_id=f"c{index}")]
        for index in range(12)
    ]

    response = agent_client.post(AGENT, json=_body(max_steps=16))
    assert response.status_code == 200, response.text
    # 默认配置 ``agent_max_steps=8``；请求写的 16 不得生效
    assert response.json()["steps"] == 8


# ---------------------------------------------------------------------------
# AC-AGENT-03 / 04
# ---------------------------------------------------------------------------


def test_agent_skips_duplicate_tool_calls(agent_client: TestClient, fake_llm: FakeLLM) -> None:
    """``AC-AGENT-03``：同一工具 + 同一参数第二次出现时不再执行。"""
    fake_llm.replies = ["", "", "好"]
    fake_llm.tool_scripts = [
        [tool_call("calculator", {"expression": "1+1"}, call_id="c1")],
        [tool_call("calculator", {"expression": "1+1"}, call_id="c2")],
    ]

    response = agent_client.post(AGENT, json=_body())
    assert response.status_code == 200, response.text
    payload = response.json()

    executed = [item for item in payload["tool_calls"] if item["status"] == "ok"]
    assert len(executed) == 1
    skipped = [item for item in payload["tool_calls"] if item["summary"].startswith("重复调用")]
    assert len(skipped) == 1


def test_agent_rejects_dangerous_calculator_expression(
    agent_client: TestClient, fake_llm: FakeLLM
) -> None:
    """``AC-AGENT-04``：``__import__('os')`` 不得被执行，且返回可回复的失败。"""
    fake_llm.replies = ["", "换不了就不算了"]
    fake_llm.tool_scripts = [
        [tool_call("calculator", {"expression": "__import__('os').system('ls')"})]
    ]

    response = agent_client.post(AGENT, json=_body())
    assert response.status_code == 200, response.text
    payload = response.json()

    trace = payload["tool_calls"][0]
    assert trace["status"] == "error"
    assert "参数不符合工具 Schema" in trace["summary"]
    assert payload["answer"] == "换不了就不算了"


# ---------------------------------------------------------------------------
# AC-AGENT-06 / 07
# ---------------------------------------------------------------------------


def test_tools_list_contains_required_builtins(agent_client: TestClient) -> None:
    """``AC-AGENT-06``：至少包含三个内置工具，且 ``parameters`` 是合法 JSON Schema。"""
    response = agent_client.get(TOOLS)
    assert response.status_code == 200, response.text
    payload = response.json()

    names = {item["name"] for item in payload["items"]}
    assert {"kb_retrieve", "calculator", "current_time"} <= names
    for item in payload["items"]:
        schema = item["parameters"]
        assert schema["type"] == "object"
        assert isinstance(schema.get("properties"), dict)
        # 描述必须告诉模型「何时使用 / 何时不适用」
        assert 0 < len(item["description"]) <= 512
        # 默认关闭的工具不应出现在默认列表里
        assert item["enabled"] is True
    assert "http_fetch" not in names


def test_tools_list_can_include_disabled(agent_client: TestClient) -> None:
    """显式 ``enabled=false`` 时可以查到默认关闭的工具（便于运维排查）。"""
    response = agent_client.get(TOOLS, params={"enabled": "false"})
    assert response.status_code == 200
    assert [item["name"] for item in response.json()["items"]] == ["http_fetch"]


def test_tools_list_filters_by_source(agent_client: TestClient) -> None:
    response = agent_client.get(TOOLS, params={"source": "mcp"})
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_tools_list_rejects_bad_cursor(agent_client: TestClient) -> None:
    response = agent_client.get(TOOLS, params={"cursor": "not-a-cursor"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_duplicate_builtin_tool_name_fails_fast(settings: Settings) -> None:
    """``AC-AGENT-07``：内置重名必须让启动失败，而不是静默覆盖。"""
    from app.rag.base import NullRetriever
    from app.tools.builtin import CalculatorTool

    class _DuplicateCalculator(CalculatorTool):
        """与内置 ``calculator`` 同名（模拟两个模块各注册了一次）。"""

    with pytest.raises(ToolRegistrationError, match="工具名冲突"):
        build_tool_registry(
            settings,
            retriever=NullRetriever(),
            extra_tools=[_DuplicateCalculator()],
        )


def test_build_tool_registry_validates_each_spec(settings: Settings) -> None:
    """装配期逐项校验：名字、描述长度、``parameters`` 形状、MCP 必填字段。"""
    from app.rag.base import NullRetriever
    from app.tools.base import ToolSpec
    from app.tools.builtin import CalculatorTool

    class _RawTool:
        def __init__(self, spec: ToolSpec) -> None:
            self._spec = spec

        @property
        def spec(self) -> ToolSpec:
            return self._spec

        @property
        def enabled(self) -> bool:
            return True

        def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
            return arguments

        async def invoke(self, arguments: Any, ctx: Any) -> Any:  # pragma: no cover - 不执行
            raise AssertionError

    cases: list[tuple[ToolSpec, str]] = [
        (ToolSpec(name="BadName", description="x", parameters={"type": "object"}), "不合法"),
        (ToolSpec(name="ok_tool", description="  ", parameters={"type": "object"}), "缺少描述"),
        (
            ToolSpec(name="ok_tool", description="描述" * 300, parameters={"type": "object"}),
            "描述过长",
        ),
        (ToolSpec(name="ok_tool", description="x", parameters={"type": "array"}), "parameters"),
        (
            ToolSpec(name="ok_tool", description="x", parameters={"type": "object"}, source="mcp"),
            "mcp_server",
        ),
    ]
    for spec, pattern in cases:
        with pytest.raises(ToolRegistrationError, match=pattern):
            build_tool_registry(settings, retriever=NullRetriever(), extra_tools=[_RawTool(spec)])

    # 对照：合法的额外工具能注册成功，且内置工具仍在
    builtin_spec = CalculatorTool().spec
    assert builtin_spec.source == "builtin"
    assert builtin_spec.side_effect == "read"
    registry = build_tool_registry(
        settings,
        retriever=NullRetriever(),
        extra_tools=[
            _RawTool(
                ToolSpec(name="ok_tool", description="可以做某事", parameters={"type": "object"})
            )
        ],
    )
    assert "ok_tool" in registry


# ---------------------------------------------------------------------------
# AC-AGENT-08
# ---------------------------------------------------------------------------


def test_tool_invoke_available_locally(agent_client: TestClient) -> None:
    """``AC-AGENT-08``（前半）：local 环境下调试调用可用。"""
    response = agent_client.post(
        f"{TOOLS}/calculator/invoke", json={"arguments": {"expression": "1+1"}}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["name"] == "calculator"
    assert payload["status"] == "ok"
    assert payload["result"]["result"] == 2


def test_tool_invoke_hidden_in_prod(make_settings: Any) -> None:
    """``AC-AGENT-08``（后半）：prod 下必须 404（连「存在」都不暴露）。"""
    from app.core.security import create_access_token
    from app.main import create_app

    prod = make_settings(
        app_env="prod",
        infra_backend="real",
        jwt_secret="p" * 32,
        openai_api_key="sk-prod",
        cors_origins=["https://example.com"],
    )
    headers = {"Authorization": f"Bearer {create_access_token('u_prod', prod)}"}
    with TestClient(create_app(prod)) as client:
        response = client.post(
            f"{TOOLS}/calculator/invoke",
            json={"arguments": {"expression": "1+1"}},
            headers=headers,
        )
        # 同一个 app 的列接口仍然可用（否则下面的 404 只能归因于「整个路由挂了」）
        listed = client.get(TOOLS, headers=headers)
    assert listed.status_code == 200
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TOOL_NOT_FOUND"


def test_tool_invoke_unknown_tool_locally(agent_client: TestClient) -> None:
    response = agent_client.post(f"{TOOLS}/nope/invoke", json={"arguments": {}})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TOOL_NOT_FOUND"


def test_tool_invoke_reports_bad_arguments(agent_client: TestClient) -> None:
    response = agent_client.post(
        f"{TOOLS}/calculator/invoke",
        json={"arguments": {"expression": "__import__('os')"}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_tool_invoke_dry_run_skips_execution(agent_client: TestClient) -> None:
    response = agent_client.post(
        f"{TOOLS}/calculator/invoke",
        json={"arguments": {"expression": "1+1"}, "dry_run": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    # dry_run 不执行：结果体为空（只有执行过才会有 payload）
    assert payload["result"] == {}


# ---------------------------------------------------------------------------
# AC-AGENT-09
# ---------------------------------------------------------------------------


def test_agent_stream_pairs_tool_frames_before_done(
    agent_client: TestClient, fake_llm: FakeLLM
) -> None:
    """``AC-AGENT-09``：每个 ``tool_call`` 都有同一 ``call_id`` 的 ``tool_result``，
    且都在 ``done`` 之前。"""
    fake_llm.replies = ["", "最终答案"]
    fake_llm.tool_scripts = [[tool_call("calculator", {"expression": "2+2"}, call_id="call_x")]]

    response = agent_client.post(AGENT_STREAM, json=_body())
    assert response.status_code == 200, response.text
    events = _events(response.text)

    names = [name for name, _ in events]
    assert names[0] == "meta"
    assert names[-1] == "done"

    done_at = names.index("done")
    calls = [data for name, data in events if name == "tool_call"]
    results = [data for name, data in events if name == "tool_result"]
    assert len(calls) == 1
    assert len(results) == 1
    assert calls[0]["call_id"] == results[0]["call_id"] == "call_x"
    assert calls[0]["arguments"] == {"expression": "2+2"}
    assert results[0]["status"] == "ok"
    assert next(index for index, name in enumerate(names) if name == "tool_call") < done_at
    assert next(index for index, name in enumerate(names) if name == "tool_result") < done_at

    # 工具调用与结果必须**成对相邻**，且顺序是 call 在前
    assert names.index("tool_call") < names.index("tool_result")
    tokens = "".join(data["delta"] for name, data in events if name == "token")
    assert "最终答案" in tokens


def test_agent_stream_emits_references_when_retrieval_happens(
    agent_client: TestClient, fake_llm: FakeLLM
) -> None:
    """检索类工具的片段必须出现在 ``reference`` 帧里，且早于 ``done``。"""
    from tests.support.rag import create_kb, ingest_text

    kb = create_kb(agent_client)
    ingest_text(agent_client, kb["id"], "退款政策：审核通过后 3 个工作日内原路退回。" * 5)
    fake_llm.replies = ["", "答案见 [1]"]
    fake_llm.tool_scripts = [
        [tool_call("kb_retrieve", {"query": "退款几天到账", "kb_ids": [kb["id"]]})]
    ]

    response = agent_client.post(AGENT_STREAM, json=_body(use_rag=True))
    assert response.status_code == 200, response.text
    events = _events(response.text)
    names = [name for name, _ in events]
    references = [data for name, data in events if name == "reference"]
    assert references, "检索命中后必须下发 reference 帧"
    assert references[0]["references"][0]["doc_name"]
    assert names.index("reference") < names.index("done")
    # 引用紧跟在产生它的 tool_result 之后（早于 tool_call 就会引用一个还没回流的调用）
    assert names.index("tool_call") < names.index("tool_result") < names.index("reference")


def test_agent_stream_rejects_empty_query(agent_client: TestClient) -> None:
    """准备阶段必须在 SSE 开始**之前**抛错，否则只能用一个 error 帧表达 400。"""
    response = agent_client.post(AGENT_STREAM, json=_body(query="   "))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "QUERY_EMPTY"


def test_agent_stream_unknown_tool_name_is_400(agent_client: TestClient) -> None:
    response = agent_client.post(AGENT_STREAM, json=_body(allowed_tools=["nope"]))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


# ---------------------------------------------------------------------------
# 工具范围
# ---------------------------------------------------------------------------


def test_agent_restricts_tools_to_allowlist(agent_client: TestClient, fake_llm: FakeLLM) -> None:
    """白名单必须同时体现在下发给上游的 ``tools`` 与执行授权上。"""
    response = agent_client.post(AGENT, json=_body(allowed_tools=["current_time"]))
    assert response.status_code == 200, response.text
    offered = fake_llm.tools_seen[0]
    assert offered is not None
    assert [item["function"]["name"] for item in offered] == ["current_time"]


def test_agent_denied_tools_override_allowlist(agent_client: TestClient, fake_llm: FakeLLM) -> None:
    response = agent_client.post(
        AGENT,
        json=_body(allowed_tools=["calculator", "current_time"], denied_tools=["calculator"]),
    )
    assert response.status_code == 200, response.text
    offered = fake_llm.tools_seen[0]
    assert offered is not None
    assert [item["function"]["name"] for item in offered] == ["current_time"]


def test_agent_rejects_empty_tool_set(agent_client: TestClient) -> None:
    """工具集合为空时无法执行 Agent 请求——必须明确拒绝而不是「不带工具硬答」。"""
    response = agent_client.post(
        AGENT,
        json=_body(
            allowed_tools=["calculator", "current_time", "kb_retrieve"],
            denied_tools=["calculator", "current_time", "kb_retrieve"],
        ),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_agent_call_outside_allowlist_is_rejected(
    agent_client: TestClient, fake_llm: FakeLLM
) -> None:
    """模型仍然要求调用未授权工具时，不得执行（``docs/04`` §5 第 1 行）。"""
    fake_llm.replies = ["", "换个办法"]
    fake_llm.tool_scripts = [[tool_call("calculator", {"expression": "1+1"})]]

    response = agent_client.post(AGENT, json=_body(allowed_tools=["current_time"]))
    assert response.status_code == 200, response.text
    trace = response.json()["tool_calls"][0]
    assert trace["status"] == "error"
    assert "未在本次请求的允许范围内" in trace["summary"]


# ---------------------------------------------------------------------------
# 鉴权（结构性保护：新路由必须挂 ``get_current_user``）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [TOOLS, AGENT, AGENT_STREAM])
def test_agent_routes_require_auth(client: TestClient, path: str) -> None:
    response = client.post(path, json=_body()) if path != TOOLS else client.get(path)
    assert response.status_code == 401


def test_agent_schemas_accept_chat_request_fields() -> None:
    """``AgentRunRequest`` 继承 ``ChatRequest``：新增字段时不能重复实现校验。"""
    from app.core.ids import new_id
    from app.schemas.agent import AgentRunRequest

    request = AgentRunRequest(query="你好", kb_ids=[new_id("kb")])
    assert request.use_tools is False  # service 层强制置 true，schema 不做隐式改写
    assert request.max_steps == 8
    assert request.allowed_tools is None
    assert request.denied_tools == []


def test_agent_request_rejects_bad_kb_id() -> None:
    from pydantic import ValidationError

    from app.schemas.agent import AgentRunRequest

    with pytest.raises(ValidationError):
        AgentRunRequest(query="你好", kb_ids=["bad"])


def test_llm_tool_call_import_is_exported() -> None:
    """``LLMToolCall`` 在 ``app.llm.base`` 公开导出（供工具层与测试共用）。"""
    call: LLMToolCall = tool_call("calculator", {"expression": "1"})
    assert call.arguments == '{"expression": "1"}'
