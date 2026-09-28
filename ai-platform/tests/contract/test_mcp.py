"""MCP 接口契约测试（``AC-MCP-01`` ~ ``AC-MCP-07``，``docs/05`` §5）。

契约层只验证「经路由之后行为没走样」：管理器的并发/超时细节在
``tests/unit/test_mcp_manager.py`` 里逐条测过，这里重复一遍只会让失败定位变难。

本文件用 ``make_mcp_client`` 夹具：MCP 连接全部走 :class:`FakeMcpServer`，
但管理器、客户端、适配器、注册表、路由**都是真的**。注入点选在
``app.mcp.client.open_session`` —— 那是生产装配用的同一个名字，替换它等于
「只换 transport，其余一行不动」。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.support.fake_mcp import FakeMcpServer

from app.config import Settings
from app.mcp.session import McpToolDef

SERVERS = "/api/v1/mcp/servers"
TOOLS = "/api/v1/tools"
READY = "/api/v1/health/ready"


def _status_by_name(body: dict) -> dict[str, dict]:
    """把 ``/mcp/servers`` 的响应整理成 ``{name: item}``。"""
    return {item["name"]: item for item in body["items"]}


# ---------------------------------------------------------------------------
# GET /mcp/servers
# ---------------------------------------------------------------------------
def test_lists_configured_servers_with_status(
    mcp_app: tuple[FastAPI, Settings, TestClient],
) -> None:
    """``GET /mcp/servers`` 返回每个配置的 Server 及其实时状态。"""
    with mcp_app[2] as client:
        response = client.get(SERVERS)

    assert response.status_code == 200
    body = response.json()
    assert body["has_more"] is False
    statuses = _status_by_name(body)
    assert set(statuses) == {"fs", "git"}
    assert statuses["fs"]["status"] == "connected"
    assert statuses["fs"]["transport"] == "stdio"
    assert statuses["fs"]["tools_count"] == 2
    assert statuses["fs"]["required"] is False
    assert statuses["fs"]["latency_ms"] is not None
    assert statuses["fs"]["last_connected_at"]


def test_servers_endpoint_requires_auth(mcp_app: tuple[FastAPI, Settings, TestClient]) -> None:
    """MCP 状态暴露「本服务能碰到哪些外部系统」，必须鉴权。"""
    with mcp_app[2] as client:
        response = client.get(SERVERS, headers={"Authorization": ""})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_servers_pagination_cursor(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> None:
    """``/mcp/servers`` 走统一的游标分页（对内存列表做偏移）。"""
    _app, _, client = make_mcp_client()
    with client:
        first = client.get(SERVERS, params={"limit": 1}).json()
        second = client.get(SERVERS, params={"limit": 1, "cursor": first["next_cursor"]}).json()

    assert first["has_more"] is True
    assert first["next_cursor"] == "1"
    assert len(first["items"]) == 1
    assert second["has_more"] is False
    assert second["next_cursor"] is None
    assert second["items"][0]["name"] != first["items"][0]["name"]


def test_unavailable_server_is_reported_not_hidden(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> None:
    """``AC-MCP-01``：非必需 Server 连不上时服务照常启动，状态为 ``unavailable``。"""
    broken = FakeMcpServer(fail_connect=True, connect_error="spawn failed")
    _app, _, client = make_mcp_client(server=broken)

    with client:
        body = client.get(SERVERS).json()

    statuses = _status_by_name(body)
    assert statuses["fs"]["status"] == "unavailable"
    assert statuses["fs"]["last_error"] == "spawn failed"
    assert statuses["fs"]["tools_count"] == 0


def test_disabled_server_reports_disabled(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> None:
    """``enabled=false`` 的 Server 状态是 ``disabled``（不是 ``unavailable``）。"""
    _app, _, client = make_mcp_client(servers={"fs": {"command": "fake", "enabled": False}})

    with client:
        statuses = _status_by_name(client.get(SERVERS).json())

    assert statuses["fs"]["status"] == "disabled"


def test_allowlist_is_reflected_in_tools_count(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> None:
    """allowlist 过滤后的数量要体现在 ``tools_count`` 上。"""
    _app, _, client = make_mcp_client(
        servers={"fs": {"command": "fake", "tools_allowlist": ["read_file"]}}
    )

    with client:
        statuses = _status_by_name(client.get(SERVERS).json())

    assert statuses["fs"]["tools_count"] == 1


def test_no_servers_configured_returns_empty_page(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> None:
    """没配 MCP 时返回空列表而不是报错（MCP 是可选能力）。"""
    _app, _, client = make_mcp_client(servers={})

    with client:
        response = client.get(SERVERS)

    assert response.status_code == 200
    assert response.json()["items"] == []


# ---------------------------------------------------------------------------
# GET /mcp/servers/{name}/tools
# ---------------------------------------------------------------------------
def test_lists_tools_of_one_server(
    mcp_app: tuple[FastAPI, Settings, TestClient],
) -> None:
    """单 Server 工具列表的数据源是**注册表**（模型实际能调到的东西）。"""
    with mcp_app[2] as client:
        response = client.get(f"{SERVERS}/fs/tools")

    assert response.status_code == 200
    items = response.json()["items"]
    assert {item["name"] for item in items} == {"mcp__fs__read_file", "mcp__fs__write_file"}
    assert all(item["source"] == "mcp" for item in items)
    assert all(item["mcp_server"] == "fs" for item in items)


def test_server_tools_respect_allowlist(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> None:
    """``GET /mcp/servers/{name}/tools`` 能确认过滤有没有按预期生效。"""
    _app, _, client = make_mcp_client(
        servers={"fs": {"command": "fake", "tools_allowlist": ["read_file"]}}
    )

    with client:
        items = client.get(f"{SERVERS}/fs/tools").json()["items"]

    assert [item["name"] for item in items] == ["mcp__fs__read_file"]


def test_unknown_server_tools_returns_404(
    mcp_app: tuple[FastAPI, Settings, TestClient],
) -> None:
    """未配置的 Server → ``404 MCP_SERVER_NOT_FOUND``（不是 500）。"""
    with mcp_app[2] as client:
        response = client.get(f"{SERVERS}/nope/tools")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "MCP_SERVER_NOT_FOUND"
    assert "fs" in body["error"]["details"]["configured"]


def test_server_tools_pagination(mcp_app: tuple[FastAPI, Settings, TestClient]) -> None:
    """工具列表同样支持游标分页。"""
    with mcp_app[2] as client:
        first = client.get(f"{SERVERS}/fs/tools", params={"limit": 1}).json()
        second = client.get(
            f"{SERVERS}/fs/tools", params={"limit": 1, "cursor": first["next_cursor"]}
        ).json()

    assert first["has_more"] is True
    assert second["has_more"] is False
    assert first["items"][0]["name"] != second["items"][0]["name"]


# ---------------------------------------------------------------------------
# POST /mcp/servers/{name}/reload
# ---------------------------------------------------------------------------
def test_reload_reconnects_the_server(
    mcp_app: tuple[FastAPI, Settings, TestClient], fake_mcp_server: FakeMcpServer
) -> None:
    """``AC-MCP-05``：重载单个 Server 成功并刷新注册表。"""
    with mcp_app[2] as client:
        response = client.post(f"{SERVERS}/fs/reload", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "connected"
    assert fake_mcp_server.connects == 3  # 启动时 2 个 Server 各连一次 + 重载 1 次


def test_reload_refreshes_registry_after_tool_removal(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> None:
    """重载后工具下线，注册表必须跟着刷新 —— 否则模型调的是已失效的工具。"""
    server = FakeMcpServer(
        tools=[
            McpToolDef(name="read_file", description="读文件"),
            McpToolDef(name="write_file", description="写文件"),
        ]
    )
    _app, _, client = make_mcp_client(server=server, servers={"fs": {"command": "fake"}})
    with client:
        before = client.get(f"{SERVERS}/fs/tools").json()["items"]
        # Server 升级后下线了一个工具
        server.tools = server.tools[:1]
        reloaded = client.post(f"{SERVERS}/fs/reload", json={})
        remaining = client.get(f"{SERVERS}/fs/tools").json()["items"]
        count = client.get(SERVERS).json()["items"][0]["tools_count"]

    assert len(before) == 2
    assert reloaded.status_code == 200
    assert [item["name"] for item in remaining] == ["mcp__fs__read_file"]
    assert count == 1


def test_reload_unknown_server_returns_404(
    mcp_app: tuple[FastAPI, Settings, TestClient],
) -> None:
    """重载未配置的 Server → 404。"""
    with mcp_app[2] as client:
        response = client.post(f"{SERVERS}/nope/reload", json={})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MCP_SERVER_NOT_FOUND"


def test_reload_disabled_server_returns_503(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> None:
    """重载被禁用的 Server → 503（它存在，只是被关掉了）。"""
    _app, _, client = make_mcp_client(servers={"fs": {"command": "fake", "enabled": False}})

    with client:
        response = client.post(f"{SERVERS}/fs/reload", json={})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MCP_SERVER_UNAVAILABLE"


def test_reload_failure_returns_503_with_reason(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> None:
    """重载时连不上 → 503，并带上失败原因（排障要看这个）。"""
    server = FakeMcpServer()
    _app, _, client = make_mcp_client(server=server, servers={"fs": {"command": "fake"}})
    with client:
        server.fail_connect = True
        response = client.post(f"{SERVERS}/fs/reload", json={})

    assert response.status_code == 503
    assert response.json()["error"]["details"]["reason"]


def test_reload_requires_auth(mcp_app: tuple[FastAPI, Settings, TestClient]) -> None:
    """重载会打断线上调用，必须鉴权。"""
    with mcp_app[2] as client:
        response = client.post(f"{SERVERS}/fs/reload", json={}, headers={"Authorization": ""})

    assert response.status_code == 401


def test_reload_ignores_unknown_body_fields(
    mcp_app: tuple[FastAPI, Settings, TestClient],
) -> None:
    """请求体多传字段被忽略而不是 422。

    这是全局约定（``StrictModel`` 用 ``extra="ignore"``）：客户端多传一个字段
    （拼错的名字、旧版本遗留的字段）不该让调用整体失败 —— 那类失败对调用方
    极难定位。真正重要的「配置写错要报错」在 ``MCP_SERVERS`` 那一层用
    ``extra="forbid"`` 把关（``AC-MCP-04``）。
    """
    with mcp_app[2] as client:
        response = client.post(f"{SERVERS}/fs/reload", json={"unknown": 1})

    assert response.status_code == 200


def test_reload_force_flag_accepted(mcp_app: tuple[FastAPI, Settings, TestClient]) -> None:
    """``force=true`` 是合法请求体。"""
    with mcp_app[2] as client:
        response = client.post(f"{SERVERS}/fs/reload", json={"force": True})

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /tools?source=mcp
# ---------------------------------------------------------------------------
def test_mcp_tools_appear_in_the_shared_registry(
    mcp_app: tuple[FastAPI, Settings, TestClient],
) -> None:
    """``AC-MCP-03``：MCP 工具与内置工具在同一张注册表里，按 ``source`` 可筛。"""
    with mcp_app[2] as client:
        response = client.get(TOOLS, params={"source": "mcp"})

    assert response.status_code == 200
    items = response.json()["items"]
    assert {item["name"] for item in items} == {
        "mcp__fs__read_file",
        "mcp__fs__write_file",
        "mcp__git__read_file",
        "mcp__git__write_file",
    }
    assert all(item["source"] == "mcp" for item in items)


def test_builtin_tools_are_unaffected(mcp_app: tuple[FastAPI, Settings, TestClient]) -> None:
    """内置工具仍在（MCP 的接入不改动内置工具集合）。"""
    with mcp_app[2] as client:
        items = client.get(TOOLS, params={"source": "builtin"}).json()["items"]

    names = {item["name"] for item in items}
    assert {"calculator", "current_time", "kb_retrieve"} <= names
    assert not any(name.startswith("mcp__") for name in names)


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
def test_ready_includes_mcp_detail(mcp_app: tuple[FastAPI, Settings, TestClient]) -> None:
    """``AC-MCP-06``：``/health/ready`` 的 ``mcp`` 明细里能看到每个 Server 的状态。

    ``CheckResult.as_dict`` 会把 ``detail`` 的键**平铺**到该检查项上，
    所以直接读 ``checks["mcp"]["servers"]``。
    """
    with mcp_app[2] as client:
        response = client.get(READY)

    assert response.status_code == 200
    check = response.json()["checks"]["mcp"]
    assert check["ok"] is True
    assert check["available"] == 2
    assert check["connected"] == 2
    assert check["required_unavailable"] == []
    assert check["servers"]["fs"]["last_error"] is None
    assert check["servers"]["fs"]["tools_count"] == 2


def test_ready_stays_ok_when_optional_server_is_down(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> None:
    """``AC-MCP-01``：非必需 Server 掉线**不影响**就绪 —— 那正是「降级启动」的定义。"""
    _app, _, client = make_mcp_client(server=FakeMcpServer(fail_connect=True))

    with client:
        response = client.get(READY)

    assert response.status_code == 200
    assert response.json()["checks"]["mcp"]["ok"] is True


def test_startup_fails_when_required_server_is_down(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> None:
    """``AC-MCP-02``：``required=true`` 连不上 → 应用拒绝启动，错误里点名。"""
    server = FakeMcpServer(fail_connect=True, connect_error="spawn failed")
    _app, _, client = make_mcp_client(
        server=server, servers={"fs": {"command": "fake", "required": True}}
    )

    with pytest.raises(Exception) as excinfo, client:
        pass

    assert "fs" in str(excinfo.value)


def test_mcp_config_error_does_not_break_import(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> None:
    """``AC-MCP-04``：配置写错（未知字段）→ 启动期报错，且信息里有 Server 与字段名。"""
    _app, _, client = make_mcp_client(servers={"fs": {"comand": "npx"}})

    with pytest.raises(Exception) as excinfo, client:
        pass

    message = str(excinfo.value)
    assert "fs" in message
    assert "comand" in message or "未知字段" in message


# ---------------------------------------------------------------------------
# 写操作闸门（AC-MCP-07）
# ---------------------------------------------------------------------------
def test_write_tool_is_reachable_from_the_debug_endpoint(
    make_mcp_client: Callable[..., tuple[FastAPI, Settings, TestClient]],
) -> None:
    """声明为 ``write_tools`` 的工具也能从调试接口看到并校验参数。"""
    _app, _, client = make_mcp_client(
        servers={"fs": {"command": "fake", "write_tools": ["write_file"]}}
    )

    with client:
        response = client.post(
            "/api/v1/tools/mcp__fs__write_file/invoke",
            json={"arguments": {"path": "/tmp/a"}, "dry_run": True},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["error"] is None


def test_dry_run_does_not_call_upstream(
    mcp_app: tuple[FastAPI, Settings, TestClient], fake_mcp_server: FakeMcpServer
) -> None:
    """``dry_run=true`` 只校验参数，不真的打上游。"""
    with mcp_app[2] as client:
        response = client.post(
            "/api/v1/tools/mcp__fs__read_file/invoke",
            json={"arguments": {"path": "/tmp/a"}, "dry_run": True},
        )

    assert response.status_code == 200
    assert fake_mcp_server.calls == []


def test_missing_required_argument_returns_400(
    mcp_app: tuple[FastAPI, Settings, TestClient],
) -> None:
    """漏传必需参数 → ``400 INVALID_ARGUMENT``（与内置工具同一套错误信封）。"""
    with mcp_app[2] as client:
        response = client.post("/api/v1/tools/mcp__fs__read_file/invoke", json={"arguments": {}})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
