"""MCP 配置解析单测（``REQ-MCP-001``，契约见 ``docs/05`` §2.2）。

**为什么这里值得写满**：MCP 配置是一份「启动期就决定生死」的输入，而且写错的
表现极其难查 —— 把 ``command`` 写成 ``comand``，``extra="ignore"`` 的实现会
安静地用一个空 command 去建连，日志里只有一句「连接失败」，没有任何线索指向
那个拼错的键。所以核心断言是：**未知字段必须报错，且错误信息里带上 Server 名与字段名**
（``AC-MCP-04``）。

另一半是密钥引用：``${VAR}`` 展开失败 MUST 报错，而不是把字面量当令牌发出去 ——
后者的表现是「401，但看配置一切正常」。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.mcp.config import (
    TOOL_TIMEOUT_MAX_SECONDS,
    McpConfigError,
    McpServerConfig,
    expand_references,
    namespaced_name,
    parse_server_config,
    parse_servers,
)


def test_stdio_config_defaults() -> None:
    """stdio 只有 ``command`` 是必填，其余走默认值。"""
    config = parse_server_config("fs", {"command": "npx", "args": ["-y", "server-filesystem"]})

    assert config.transport == "stdio"
    assert config.command == "npx"
    assert config.args == ["-y", "server-filesystem"]
    assert config.enabled is True
    assert config.required is False
    assert config.timeout_seconds == 30.0
    assert config.tools_allowlist is None
    assert config.tools_denylist == []
    assert config.write_tools == []


def test_unknown_field_is_rejected_with_server_and_field_name() -> None:
    """未知字段（如 ``comand``）必须报错，且信息里能定位到 Server 与字段（AC-MCP-04）。"""
    with pytest.raises(McpConfigError) as excinfo:
        parse_server_config("fs", {"comand": "npx"})

    error = excinfo.value
    assert error.server == "fs"
    assert "fs" in str(error)
    assert "comand" in str(error)
    assert "未知字段" in str(error)


def test_stdio_requires_command() -> None:
    """``transport=stdio`` 而没有 ``command`` → 启动期报错。"""
    with pytest.raises(McpConfigError, match="command"):
        parse_server_config("fs", {"args": ["-y", "x"]})


def test_streamable_http_requires_url() -> None:
    """``transport=streamable_http`` 而没有 ``url`` → 启动期报错。"""
    with pytest.raises(McpConfigError, match="url"):
        parse_server_config("remote", {"transport": "streamable_http"})


def test_transport_must_be_known_value() -> None:
    """未知 transport 名报错（而不是静默按 stdio 处理）。"""
    with pytest.raises(McpConfigError, match="transport"):
        parse_server_config("x", {"transport": "websocket", "command": "x"})


def test_timeout_above_the_documented_ceiling_is_rejected() -> None:
    """``timeout_seconds`` 超过 60s 会被拒 —— 那等于超出 Agent 的整体预算。"""
    with pytest.raises(McpConfigError, match="timeout_seconds"):
        parse_server_config(
            "fs", {"command": "npx", "timeout_seconds": TOOL_TIMEOUT_MAX_SECONDS + 1}
        )


def test_timeout_at_the_ceiling_is_accepted() -> None:
    """边界值（正好 60s）是允许的。"""
    config = parse_server_config(
        "fs", {"command": "npx", "timeout_seconds": TOOL_TIMEOUT_MAX_SECONDS}
    )

    assert config.timeout_seconds == TOOL_TIMEOUT_MAX_SECONDS


@pytest.mark.parametrize("field", ["timeout_seconds", "connect_timeout_seconds"])
def test_non_positive_timeouts_are_rejected(field: str) -> None:
    """超时必须为正：0 或负值会让 ``wait_for`` 立刻超时或抛错，表现为「永远连不上」。"""
    with pytest.raises(McpConfigError, match=field):
        parse_server_config("fs", {"command": "npx", field: 0})


@pytest.mark.parametrize("name", ["FS", "my server", "-bad", "", "x" * 33, "工具", "a.b"])
def test_invalid_server_names_are_rejected(name: str) -> None:
    """Server 名会拼进工具名，必须匹配 ``^[a-z0-9][a-z0-9_-]{0,31}$``。"""
    with pytest.raises(McpConfigError, match="Server 名不合法"):
        parse_server_config(name, {"command": "npx"})


@pytest.mark.parametrize("name", ["fs", "my-server", "a_b", "s1", "1st", "x" * 32])
def test_valid_server_names_are_accepted(name: str) -> None:
    """合法名（含长度边界 32、以及数字开头）都能通过。"""
    assert parse_server_config(name, {"command": "npx"}).command == "npx"


def test_extra_fields_are_forbidden_on_typed_model() -> None:
    """直接构造模型时同样禁止未知字段（``extra="forbid"``）。

    这里断言 pydantic 的 :class:`ValidationError` 而不是裸 ``Exception``：
    后者会把「拼错字段名导致的 TypeError」也算成通过。
    """
    with pytest.raises(ValidationError):
        McpServerConfig(command="npx", unknown=1)  # type: ignore[call-arg]


# ----------------------------------------------------------------------
# allowlist / denylist / write_tools
# ----------------------------------------------------------------------
def test_allowlist_none_means_everything() -> None:
    """``tools_allowlist=None`` 时全部工具可见。"""
    config = McpServerConfig(command="npx")

    assert config.allows_tool("anything")
    assert config.allows_tool("readFile")


def test_allowlist_restricts_tools() -> None:
    """配了 allowlist 之后只放行列出的工具。"""
    config = McpServerConfig(command="npx", tools_allowlist=["read_file", "list_dir"])

    assert config.allows_tool("read_file")
    assert not config.allows_tool("write_file")


def test_denylist_wins_over_allowlist() -> None:
    """``tools_denylist`` 优先级高于 ``tools_allowlist``（docs/05 §2.2）。"""
    config = McpServerConfig(
        command="npx",
        tools_allowlist=["read_file", "delete_file"],
        tools_denylist=["delete_file"],
    )

    assert config.allows_tool("read_file")
    assert not config.allows_tool("delete_file")


def test_denylist_alone_blocks_while_allowing_the_rest() -> None:
    """未配 allowlist 时，denylist 只封掉列出的那些。"""
    config = McpServerConfig(command="npx", tools_denylist=["exec_shell"])

    assert not config.allows_tool("exec_shell")
    assert config.allows_tool("read_file")


def test_side_effect_defaults_to_read_and_honours_write_tools() -> None:
    """只有显式声明在 ``write_tools`` 里的工具才算写操作。"""
    config = McpServerConfig(command="npx", write_tools=["write_file"])

    assert config.side_effect_of("write_file") == "write"
    assert config.side_effect_of("read_file") == "read"


# ----------------------------------------------------------------------
# 密钥引用
# ----------------------------------------------------------------------
def test_expand_references_reads_environment_mapping() -> None:
    """``${VAR}`` 从给定的映射里取值（测试不依赖真实环境变量）。"""
    expanded = expand_references(
        {"Authorization": "Bearer ${TOKEN}", "X-Plain": "v"}, {"TOKEN": "s3cr3t"}
    )

    assert expanded == {"Authorization": "Bearer s3cr3t", "X-Plain": "v"}


def test_expand_references_supports_multiple_references() -> None:
    """一个值里可以有多个引用。"""
    expanded = expand_references({"url": "${HOST}:${PORT}"}, {"HOST": "h", "PORT": "1"})

    assert expanded["url"] == "h:1"


def test_missing_reference_is_an_error() -> None:
    """引用了未设置的变量 → 报错（**不留下字面量** ``${...}``）。"""
    with pytest.raises(McpConfigError, match="MISSING_VAR"):
        expand_references({"token": "${MISSING_VAR}"}, {})


def test_empty_value_is_not_a_reference() -> None:
    """没有 ``${}`` 时原样返回。"""
    assert expand_references({"a": "b"}, {}) == {"a": "b"}


def test_stdio_expands_env_and_http_expands_headers() -> None:
    """stdio 展开 ``env``、http 展开 ``headers`` —— 密钥不会出现在配置文件里。"""
    stdio = parse_server_config(
        "fs",
        {"command": "npx", "env": {"API_KEY": "${FAKE_KEY}"}},
        environ={"FAKE_KEY": "k1"},
    )
    http = parse_server_config(
        "remote",
        {"transport": "streamable_http", "url": "http://x/mcp", "headers": {"X": "${FAKE_KEY}"}},
        environ={"FAKE_KEY": "k1"},
    )

    assert stdio.env == {"API_KEY": "k1"}
    assert http.headers == {"X": "k1"}


def test_missing_reference_in_config_raises() -> None:
    """配置里的引用缺失同样报错，并带上字段名。"""
    with pytest.raises(McpConfigError) as excinfo:
        parse_server_config(
            "remote",
            {"transport": "streamable_http", "url": "http://x/mcp", "headers": {"A": "${NOPE}"}},
            environ={},
        )

    assert excinfo.value.field == "A"


# ----------------------------------------------------------------------
# 整张表
# ----------------------------------------------------------------------
def test_parse_servers_handles_empty_and_none() -> None:
    """没配 MCP 是合法状态（返回空表，不报错）。"""
    assert parse_servers(None) == {}
    assert parse_servers({}) == {}


def test_parse_servers_reports_the_offending_server() -> None:
    """一项坏了就整体失败，且错误信息指向出问题的那一个。"""
    with pytest.raises(McpConfigError) as excinfo:
        parse_servers({"good": {"command": "npx"}, "bad": {"command": "npx", "typo": 1}})

    assert excinfo.value.server == "bad"


def test_parse_servers_keeps_configuration_order() -> None:
    """顺序即 ``GET /mcp/servers`` 的稳定排序基准。"""
    servers = parse_servers({"b": {"command": "x"}, "a": {"command": "y"}})

    assert list(servers) == ["b", "a"]


# ----------------------------------------------------------------------
# 工具名命名空间化
# ----------------------------------------------------------------------
def test_namespaced_name_shape() -> None:
    """正常名字是 ``mcp__{server}__{tool}``，全部小写。"""
    assert namespaced_name("fs", "read_file") == "mcp__fs__read_file"


def test_namespaced_name_sanitizes_illegal_characters() -> None:
    """``readFile`` / ``foo.bar`` 之类的第三方名字必须被改造成注册表合法名。"""
    name = namespaced_name("fs", "readFile")

    assert name.startswith("mcp__fs__")
    assert name.islower()
    assert all(c.isalnum() or c == "_" for c in name)


def test_namespaced_name_disambiguates_collisions() -> None:
    """``a.b`` 与 ``a_b`` 清洗后同为 ``a_b``，必须靠哈希后缀区分 —— 否则启动失败。"""
    first = namespaced_name("fs", "a.b")
    second = namespaced_name("fs", "a_b")

    assert first != second


def test_namespaced_name_is_stable() -> None:
    """同一个输入必须得到同一个名字：重载后工具名变化会让会话里的调用失效。"""
    assert namespaced_name("fs", "readFile") == namespaced_name("fs", "readFile")


def test_namespaced_name_never_exceeds_upstream_limit() -> None:
    """总长不超过 64 —— 上游 function name 超限会被拒绝，那是最难查的失败。"""
    name = namespaced_name("s" * 32, "t" * 60)

    assert len(name) <= 64
    assert name.islower()


def test_namespaced_name_handles_empty_tool_name() -> None:
    """空工具名（Server 给了脏数据）也不能产出非法名。"""
    name = namespaced_name("fs", "")

    assert name.startswith("mcp__fs__")
    assert name.islower()


def test_namespaced_name_starts_with_letter_after_prefix() -> None:
    """工具名以数字开头时要补前缀 —— 注册表要求首字符为字母。"""
    name = namespaced_name("fs", "123abc")

    assert "mcp__fs__t123abc" in name
