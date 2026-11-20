"""MCP 配置地址归一化的测试。

背景：yaml 里 15 个 server 原来每条都写完整 url（`http://10.204.146.44:2222/mcp/xxx`），
换部署环境（宿主机 ↔ 容器网络）要改 15 处。现在顶层写一个 `mcp_base_url`，
各条只写路径（`/personStatus`），读取时自动拼接。
"""
from mcp_client.mcp_config import (
    apply_base_url,
    load_servers,
    resolve_base_url,
    resolve_url,
)

def test_absolute_url_is_kept():
    """已经是完整地址的原样返回——老配置不用改也能跑。"""
    assert resolve_url("http://base/mcp", "http://other/x") == "http://other/x"
    assert resolve_url(None, "https://other/y") == "https://other/y"


def test_relative_path_is_joined():
    assert (
        resolve_url("http://h:2222/mcp", "/personStatus")
        == "http://h:2222/mcp/personStatus"
    )
    assert (
        resolve_url("http://h:2222/mcp", "personStatus")
        == "http://h:2222/mcp/personStatus"
    )


def test_slashes_are_normalized():
    """base 带尾斜杠、url 带前斜杠时不能拼出 `//`。"""
    assert resolve_url("http://h:2222/mcp/", "/canteen") == "http://h:2222/mcp/canteen"


def test_missing_base_returns_url_as_is():
    assert resolve_url("", "/personStatus") == "/personStatus"
    assert resolve_url(None, "/personStatus") == "/personStatus"


def test_empty_url():
    assert resolve_url("http://h", None) == ""
    assert resolve_url("http://h", "") == ""


def test_apply_base_url_skips_entries_without_url():
    """stdio 那种没有 url 的条目、以及非 dict 的条目都不能被碰。"""
    servers = {
        "a": {"url": "/x"},
        "b": {"command": "python"},
        "c": "not-a-dict",
    }

    out = apply_base_url(servers, "http://h/mcp")

    assert out["a"]["url"] == "http://h/mcp/x"
    assert "url" not in out["b"]
    assert out["c"] == "not-a-dict"


def test_apply_base_url_without_base_does_nothing():
    servers = {"a": {"url": "http://h/x"}}
    assert apply_base_url(servers, None)["a"]["url"] == "http://h/x"
    assert apply_base_url(servers, "")["a"]["url"] == "http://h/x"


def test_real_config_file_resolves():
    """真实配置文件：15 个 server 全部补全成完整地址，direct_client 标记不能丢。"""
    conf = load_servers("mcp_client/mcp_server_config.yaml")

    assert len(conf) == 15
    for name, item in conf.items():
        url = item.get("url") or ""
        assert url.startswith("http"), f"{name} 没补全：{url}"

    assert conf["scene"]["direct_client"] is True
    assert conf["knowledge"]["direct_client"] is True


# ------------------------------------------------------------
# 部署环境用 MCP_BASE_URL 覆盖 yaml，不用改仓库里的文件
# ------------------------------------------------------------
def test_env_base_url_wins_over_yaml(monkeypatch):
    monkeypatch.setenv("MCP_BASE_URL", "http://env-host:2223/mcp")
    assert resolve_base_url("http://yaml-host:2222/mcp") == "http://env-host:2223/mcp"


def test_env_base_url_empty_falls_back_to_yaml(monkeypatch):
    """compose 的 ${VAR:-} 会传空串，空串必须让位给 yaml。"""
    monkeypatch.setenv("MCP_BASE_URL", "")
    assert resolve_base_url("http://yaml-host:2222/mcp") == "http://yaml-host:2222/mcp"

    monkeypatch.setenv("MCP_BASE_URL", "   ")
    assert resolve_base_url("http://yaml-host:2222/mcp") == "http://yaml-host:2222/mcp"


def test_env_base_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("MCP_BASE_URL", "http://env-host:2223/mcp/")
    base = resolve_base_url(None)
    assert base == "http://env-host:2223/mcp"
    # 拼出来不能出现双斜杠
    assert resolve_url(base, "/devicequery") == "http://env-host:2223/mcp/devicequery"


def test_env_base_url_overrides_real_config(monkeypatch):
    """部署时只改环境变量，不用动仓库里的 yaml。"""
    monkeypatch.setenv("MCP_BASE_URL", "http://env-host:2223/mcp")
    conf = load_servers("mcp_client/mcp_server_config.yaml")

    assert len(conf) == 15
    for name, item in conf.items():
        url = item.get("url") or ""
        assert url.startswith("http://env-host:2223/mcp/"), f"{name} 没跟着环境变量走：{url}"
    # 不靠 url 的标记不能因为换 base 丢掉
    assert conf["scene"]["direct_client"] is True


def test_real_config_uses_yaml_base_without_env(monkeypatch):
    monkeypatch.delenv("MCP_BASE_URL", raising=False)
    conf = load_servers("mcp_client/mcp_server_config.yaml")
    assert conf["local"]["url"].endswith("/personStatus")
    assert conf["local"]["url"].startswith("http")


def test_real_config_has_all_domains():
    """真实 yaml 必须包含全部业务域的 server 条目（少一个就是有域接不上）。

    原来这条在 tests/test_mcp_loader.py 里，随 langchain 版 loader 一起删掉后搬到这里，
    改成直接读 mcp_config.load_servers，覆盖不变。
    """
    conf = load_servers("mcp_client/mcp_server_config.yaml")
    for name in ["local", "security", "canteen", "vehicle", "information",
                 "energy", "meeting", "fire", "device", "devicequery",
                 "knowledge", "scene"]:
        assert name in conf, f"配置缺少 server [{name}]"
        assert conf[name].get("url"), f"server [{name}] 缺少 url"


def test_config_missing_file_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        load_servers(str(tmp_path / "not_exist.yaml"))


# ------------------------------------------------------------ API Key 注入（SEC-08）
# 这段在"彻底去 langchain"时被误删过一次：函数和调用一起没了，于是**所有**
# MCP 请求都不带 key，sidecar 一律 401（missing or invalid MCP API key），
# 场景指令直接报「场景服务暂时不可用」。这里锁住行为，别再被顺手动掉。
def test_api_key_is_injected_into_headers(monkeypatch):
    from mcp_client.mcp_config import apply_api_key

    monkeypatch.setenv("MCP_API_KEY", "demo-key")
    servers = {
        "scene": {"url": "http://h/mcp/scene", "direct_client": True},
        "meeting": {"url": "http://h/mcp/meetingStatus"},
    }

    out = apply_api_key(servers)

    assert out["scene"]["headers"]["Authorization"] == "Bearer demo-key"
    assert out["meeting"]["headers"]["Authorization"] == "Bearer demo-key"


def test_empty_api_key_injects_nothing(monkeypatch):
    """空串按"未设置"处理：不注入，让 sidecar 自己决定（返回 401）。"""
    from mcp_client.mcp_config import apply_api_key

    monkeypatch.setenv("MCP_API_KEY", "")
    servers = {"scene": {"url": "http://h/mcp/scene"}}

    assert apply_api_key(servers)["scene"].get("headers") is None


def test_existing_authorization_is_kept(monkeypatch):
    """单条自己写了 Authorization 的，不能被全局 key 覆盖。"""
    from mcp_client.mcp_config import apply_api_key

    monkeypatch.setenv("MCP_API_KEY", "demo-key")
    servers = {"special": {"url": "http://h/x", "headers": {"Authorization": "Bearer own"}}}

    assert apply_api_key(servers)["special"]["headers"]["Authorization"] == "Bearer own"


def test_load_servers_injects_api_key(monkeypatch):
    """load_servers 必须真的调用 apply_api_key —— 函数在、调用没了同样白搭。"""
    from mcp_client.mcp_config import load_servers

    monkeypatch.setenv("MCP_API_KEY", "demo-key")
    conf = load_servers("mcp_client/mcp_server_config.yaml")

    scene = conf["scene"]
    assert scene["headers"]["Authorization"] == "Bearer demo-key"


def test_scene_client_sends_authorization(monkeypatch):
    """scene 是 direct_client，不走 McpToolbox，鉴权头得自己带。"""
    from mcp_client.scene_mcp_client import SceneMcpClient

    monkeypatch.setenv("MCP_API_KEY", "demo-key")
    headers = SceneMcpClient("http://h/mcp/scene")._headers("sess-1")

    assert headers["Authorization"] == "Bearer demo-key"
    assert headers["X-Session-Id"] == "sess-1"
