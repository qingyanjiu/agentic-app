# -*- coding: utf-8 -*-
"""
MCP 工具加载测试。

docs/问题排查记录.md 案例1 回归：
  旧实现把全部 server 塞进一个 MultiServerMCPClient，
  单个后端 404 会导致 TaskGroup 整组异常 -> 所有域「工具未加载」。
  修复后 get_mcp_tools 逐 server 独立加载：单个失败只 WARNING 跳过，其余照常返回。
"""
import asyncio
import types

import pytest

import mcp_client.mcp_loader as mcp_loader
from conftest import FakeTool, get_graph_module


def run(coro):
    return asyncio.run(coro)


# ============================================================
# 模拟 Java sidecar 的 MultiServerMCPClient
# 通过类属性注册：哪些 server 连接失败（如端点未部署 404）、哪些慢
# ============================================================
class FakeMultiServerMCPClient:
    fail_servers: set = set()          # 连接即抛异常（模拟 TaskGroup 里的 404）
    slow_servers: dict = {}            # {server名: 延迟秒}
    tools_by_server: dict = {}         # {server名: [FakeTool, ...]}

    def __init__(self, config: dict):
        # 修复后的 mcp_loader 每次只传一个 server：{name: conf}
        (name, _conf), = config.items()
        self.name = name

    async def get_tools(self):
        if self.name in self.fail_servers:
            # 与真实报错一致的描述：TaskGroup 包装单点失败
            raise RuntimeError(
                f"unhandled errors in a TaskGroup (1 sub-exception: "
                f"POST {self.name} HTTP/1.1 404)"
            )
        delay = self.slow_servers.get(self.name, 0.0)
        if delay:
            await asyncio.sleep(delay)
        return self.tools_by_server.get(self.name, [])


@pytest.fixture
def fake_client(monkeypatch):
    """重置类属性并 patch 进 mcp_loader"""
    FakeMultiServerMCPClient.fail_servers = set()
    FakeMultiServerMCPClient.slow_servers = {}
    FakeMultiServerMCPClient.tools_by_server = {}
    monkeypatch.setattr(mcp_loader, "MultiServerMCPClient", FakeMultiServerMCPClient)
    return FakeMultiServerMCPClient


def _person_tools():
    return [FakeTool("person_status:getTodayPersonnelAffairs", "实时在园人数：12人")]


def _canteen_tools():
    return [FakeTool("canteen:getWeekMenu", '{"rows":[]}')]


# ============================================================
# 1. 配置解析
# ============================================================
class TestConfigLoad:
    def test_real_config_has_all_domains(self):
        """真实 mcp_server_config.yaml 应包含全部 8 个域的 server 条目"""
        conf = mcp_loader.load_mcp_config("mcp_client/mcp_server_config.yaml")
        for name in ["local", "security", "canteen", "vehicle",
                     "information", "energy", "meeting", "fire", "device"]:
            assert name in conf, f"配置缺少 server [{name}]"
            assert conf[name].get("url"), f"server [{name}] 缺少 url"

    def test_config_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            mcp_loader.load_mcp_config(str(tmp_path / "not_exist.yaml"))


# ============================================================
# 2. 案例1 主回归：单 server 失败不传染其他域
# ============================================================
class TestCase1ServerIsolation:
    def test_single_server_404_others_still_load(self, fake_client):
        """复现案例1：fire 端点 404，person/canteen 应照常加载"""
        fake_client.tools_by_server = {
            "local": _person_tools(),
            "canteen": _canteen_tools(),
            # fire 未注册 -> 视为失败
        }
        fake_client.fail_servers = {"fire"}

        conf_path = "mcp_client/mcp_server_config.yaml"
        tools = run(mcp_loader.get_mcp_tools(conf_path))

        tool_names = {t.name for t in tools}
        assert "person_status:getTodayPersonnelAffairs" in tool_names
        assert "canteen:getWeekMenu" in tool_names

    def test_failed_server_logs_warning_and_skipped(self, fake_client, caplog):
        """失败的 server 打 WARNING 且被跳过；成功的打 INFO 带工具数量（启动日志可定位哪个后端没就绪）"""
        fake_client.tools_by_server = {"local": _person_tools()}
        fake_client.fail_servers = {"fire"}

        with caplog.at_level("INFO", logger="mcp_client.mcp_loader"):
            run(mcp_loader.get_mcp_tools("mcp_client/mcp_server_config.yaml"))

        warning_texts = [r.message for r in caplog.records if r.levelno == 30]
        info_texts = [r.message for r in caplog.records if r.levelno == 20]
        assert any("[fire]" in t and "工具加载失败" in t for t in warning_texts)
        assert any("[local]" in t and "加载到" in t for t in info_texts)

    def test_all_servers_fail_returns_empty_without_raise(self, fake_client):
        """极端情况：全部后端不可用 -> 返回空列表而不是抛异常"""
        fake_client.fail_servers = {"local", "fire"}

        tools = run(mcp_loader.get_mcp_tools("mcp_client/mcp_server_config.yaml"))
        assert tools == []


# ============================================================
# 3. 各域 load_xxx_tools 的过滤与容错（以消防为代表）
# ============================================================
class TestDomainToolLoading:
    def test_fire_tools_filtered_by_java_tool_map(self, monkeypatch):
        """load_emergency_fire_tools 只保留 fire:* 工具，其他域工具被过滤"""
        fire_mod = get_graph_module("emergency_fire")

        async def fake_get_mcp_tools(yaml_path):
            return _person_tools() + [
                FakeTool("fire:getFireAssets", "{}"),
                FakeTool("fire:getFireAlarmList", "{}"),
                FakeTool("fire:getFireAlarmNum", "{}"),
                FakeTool("fire:getMonthRepair", "{}"),
                FakeTool("canteen:getWeekMenu", "{}"),
            ]

        monkeypatch.setattr(fire_mod, "get_mcp_tools", fake_get_mcp_tools)
        tools = run(fire_mod.load_emergency_fire_tools())

        assert set(tools.keys()) == {
            "fire:getFireAssets", "fire:getFireAlarmList",
            "fire:getFireAlarmNum", "fire:getMonthRepair",
        }

    def test_load_error_returns_empty_dict(self, monkeypatch):
        """加载失败（如全部后端不可达）-> 返回空 dict，不向调用方抛异常"""
        fire_mod = get_graph_module("emergency_fire")

        async def fake_get_mcp_tools(yaml_path):
            raise RuntimeError("连接失败")

        monkeypatch.setattr(fire_mod, "get_mcp_tools", fake_get_mcp_tools)
        assert run(fire_mod.load_emergency_fire_tools()) == {}

    # 说明：15s 加载超时由 asyncio.wait_for（标准库）保证，返回 {} 分支
    # 与 test_load_error_returns_empty_dict 相同，不重复用例（避免用例本身等 15s）。
