"""MCP 配置地址归一化的测试。

背景：yaml 里 15 个 server 原来每条都写完整 url（`http://10.204.146.44:2222/mcp/xxx`），
换部署环境（宿主机 ↔ 容器网络）要改 15 处。现在顶层写一个 `mcp_base_url`，
各条只写路径（`/personStatus`），读取时自动拼接。
"""
from mcp_client.mcp_config import apply_base_url, load_servers, resolve_url


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
