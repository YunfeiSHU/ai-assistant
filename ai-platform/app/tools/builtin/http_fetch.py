"""``http_fetch``：抓取网页正文（``docs/04`` §3，P2，**默认禁用**）。

这是整个工具层里**唯一会主动访问外部网络**的工具，所以 SSRF 防护是它的主体逻辑，
而不是附加项。攻击路径很具体：用户说「帮我看看 http://169.254.169.254/latest/meta-data/
里的内容」或「http://localhost:6379/」，模型就会照办 —— 那就是一次从内网发起的
云元数据读取 / 内网端口探测。

防护清单（``docs/04`` §3.1）：

1. 只允许 ``http`` / ``https``；
2. 解析域名后**逐个**检查 IP：私网 / 回环 / 链路本地 / 保留段一律拒绝；
3. **不自动跟随重定向**（``follow_redirects=False``）：跟随等于把「只检查第一次解析」
   变成「每次都检查」的复杂度，而返回 3xx 给模型更透明；
4. 响应体积上限（``max_bytes``），避免把几十 MB 的文件灌进上下文。
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, ClassVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, field_validator

from app.core.text import decode_bytes
from app.tools.base import BuiltinTool, ToolContext, ToolExecutionError, ToolOutcome

TOOL_NAME = "http_fetch"
DESCRIPTION = (
    "抓取一个公开网页并返回其文本内容（已剥离 HTML 标签）。"
    "当用户明确给出 URL 且需要该页面的内容时使用；"
    "不要用它访问内网地址、也不要用它做搜索（它不做检索，只按给定 URL 取内容）。"
)

#: 允许的协议（``docs/04`` §3.1）
ALLOWED_SCHEMES = frozenset({"http", "https"})
#: 响应体积上限默认值（``docs/04`` §3 表：``max_bytes=1048576``）
DEFAULT_MAX_BYTES = 1024 * 1024
#: 响应体积上限硬顶
MAX_BYTES_CEILING = 8 * 1024 * 1024
#: 抽取后正文的字符上限（防止把 1MB 正文全塞进上下文）
TEXT_MAX_CHARS = 8000


class HttpFetchArgs(BaseModel):
    """``http_fetch`` 参数。"""

    url: str = Field(max_length=2048, description="要抓取的完整 URL（http/https）")
    max_bytes: int = Field(
        default=DEFAULT_MAX_BYTES,
        ge=1024,
        le=MAX_BYTES_CEILING,
        description="响应体积上限（字节）",
    )

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        url = value.strip()
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            msg = "只支持 http / https 协议的 URL"
            raise ValueError(msg)
        if not parsed.hostname:
            msg = "URL 缺少主机名"
            raise ValueError(msg)
        # 域名解析 + 私网检查放在校验阶段：这样「内网地址」是**参数不合法**，
        # 模型能立刻换一个 URL 重试，而不是收到一个含糊的「执行失败」。
        _assert_public_host(parsed.hostname)
        return url


def _assert_public_host(hostname: str) -> None:
    """解析主机名并拒绝一切非公网地址。

    **逐个检查所有解析结果**：一个域名可以同时解析出公网与私网 IP
    （DNS rebinding 的常见手法），只看第一个等于没防。
    """
    literal = _literal_ip(hostname)
    candidates = [literal] if literal is not None else _resolve(hostname)
    if not candidates:
        msg = f"无法解析主机名：{hostname}"
        raise ValueError(msg)
    for address in candidates:
        if not _is_public(address):
            msg = f"目标地址不可访问（内网/回环/保留地址）：{address}"
            raise ValueError(msg)


def _literal_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """主机名本身就是字面 IP 时直接返回（此时**不做** DNS 解析）。"""
    try:
        return ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return None


def _resolve(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError):
        return []
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:  # pragma: no cover - getaddrinfo 不会给非法 IP
            continue
        if parsed not in found:
            found.append(parsed)
    return found


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """是否公网可访问地址。

    ``is_private`` 覆盖私网/回环/链路本地；``is_reserved`` 覆盖 0.0.0.0/8、
    240.0.0.0/4 等保留段；``is_multicast`` 与 ``is_unspecified`` 单列是因为
    它们不属于上面任一类但同样不该被访问。
    """
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


class HttpFetchTool(BuiltinTool):
    """网页抓取工具（默认禁用）。"""

    name = TOOL_NAME
    description = DESCRIPTION
    input_model = HttpFetchArgs
    side_effect = "read"
    timeout_seconds = 20.0
    example_arguments: ClassVar[dict[str, Any]] = {"url": "https://example.com/policy"}

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        """由 ``tool_http_fetch_enabled`` 决定（``docs/04`` §3.1：默认 ``false``）。"""
        return self._enabled

    async def run(self, arguments: BaseModel, ctx: ToolContext) -> ToolOutcome:
        args = HttpFetchArgs.model_validate(arguments)
        try:
            async with httpx.AsyncClient(
                # 不跟随重定向：跟随会把「只检查一次解析结果」变成「每次跳转都要检查」，
                # 而返回 3xx 让模型知道「这个地址会跳转」反而更透明
                follow_redirects=False,
                timeout=self.timeout_seconds or 20.0,
            ) as client:
                response = await client.get(
                    args.url,
                    headers={"User-Agent": "ai-platform/0.1 (+tool:http_fetch)"},
                )
        except httpx.HTTPError as exc:
            raise ToolExecutionError(f"请求失败：{type(exc).__name__}") from exc

        raw = response.content[: args.max_bytes]
        text, _ = decode_bytes(raw)
        payload: dict[str, Any] = {
            "url": args.url,
            "status": response.status_code,
            "truncated": len(response.content) > args.max_bytes,
            "text": _to_text(text, response.headers.get("content-type", "")),
        }
        return ToolOutcome(
            status="ok",
            payload=payload,
            summary=f"HTTP {response.status_code}，取回 {len(payload['text'])} 字符",
        )


def _to_text(body: str, content_type: str) -> str:
    """HTML → 纯文本；其它类型原样返回（截断）。

    只做「剥标签」这一件事：工具的目的是把页面内容给模型看，不是做正文抽取
    （那需要 readability 之类的算法，收益不稳定且难测）。
    """
    text = body
    if "html" in content_type.lower() or "<html" in body[:2000].lower():
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(body, "html.parser")
            for tag in soup.find_all(("script", "style", "noscript", "head", "nav", "footer")):
                tag.decompose()
            text = soup.get_text("\n", strip=True)
        except Exception:  # pragma: no cover - bs4 解析失败就用原文
            text = body
    return text[:TEXT_MAX_CHARS]


__all__ = [
    "ALLOWED_SCHEMES",
    "DEFAULT_MAX_BYTES",
    "DESCRIPTION",
    "MAX_BYTES_CEILING",
    "TEXT_MAX_CHARS",
    "TOOL_NAME",
    "HttpFetchArgs",
    "HttpFetchTool",
]
