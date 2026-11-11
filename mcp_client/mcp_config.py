"""MCP 配置里地址的归一化。

单独一个小模块的原因：`agent/loop/runtime.py` 明确不想依赖
`mcp_client/mcp_loader.py`（后者顶部 import 了 langchain_mcp_adapters），
但两边都要做同一件"相对路径补全成完整地址"的事，所以抽到这里。

只依赖标准库。
"""
from __future__ import annotations

import os
import re

_ABSOLUTE = re.compile(r"^https?://", re.IGNORECASE)

# 部署环境切换用：覆盖 yaml 里的 mcp_base_url
ENV_BASE_URL = "MCP_BASE_URL"


def resolve_url(base: str | None, url: str | None) -> str:
    """把一个 server 的 url 补成完整地址。

    三种写法都支持，老配置不用改：

        base = "http://10.204.146.44:2222/mcp"
          url: /personStatus        → http://10.204.146.44:2222/mcp/personStatus
          url: personStatus         → http://10.204.146.44:2222/mcp/personStatus
          url: http://10.0.0.9/x    → 原样返回（已经是绝对地址，忽略 base）

    base 为空时相对路径原样返回，交给调用方判断（比如报"缺少地址"）。
    """
    if not url:
        return url or ""
    if _ABSOLUTE.match(url):
        return url
    if not base:
        return url
    return f"{base.rstrip('/')}/{url.lstrip('/')}"


def apply_base_url(servers: dict, base: str | None) -> dict:
    """就地补全 mcp_servers 下所有条目的 url，返回同一个 dict。

    没有 base 或条目没有 url 时不动它——这样 `direct_client` 之类
    不靠 url 的条目也不受影响。
    """
    if not base or not isinstance(servers, dict):
        return servers
    for conf in servers.values():
        if isinstance(conf, dict) and conf.get("url"):
            conf["url"] = resolve_url(base, conf["url"])
    return servers


def resolve_base_url(yaml_base: str | None) -> str | None:
    """决定最终的 base 地址：环境变量 `MCP_BASE_URL` 优先于 yaml 里的 `mcp_base_url`。

    各部署环境的 sidecar 地址不同，改环境变量就够了，不用动仓库里的 yaml。
    空串按"未设置"处理——compose 的 `${VAR:-}` 会传空串，不能让它把 yaml 冲掉。
    """
    env = (os.getenv(ENV_BASE_URL) or "").strip()
    if env:
        return env.rstrip("/")
    return (yaml_base or "").strip().rstrip("/") or None


def load_servers(yaml_path: str) -> dict:
    """读 yaml 并返回补全过 url 的 mcp_servers 段。

    base 地址来源：环境变量 `MCP_BASE_URL` > yaml 顶层的 `mcp_base_url`。
    url 已经是完整 http(s) 地址的条目不参与，老配置不受影响。
    """
    import yaml

    with open(yaml_path, encoding="utf-8") as handle:
        conf = yaml.safe_load(handle) or {}
    servers = conf.get("mcp_servers", {}) or {}
    return apply_base_url(servers, resolve_base_url(conf.get("mcp_base_url")))
