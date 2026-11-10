"""MCP 工具动态发现的单元测试。

试点的核心不是"能发现"，而是"能正确筛掉模型填不出参数的工具"：
Java 接口要 startTime / spaceId / syncSource，模型编不出来，
放出去只会让它瞎填，查询结果全错。
"""
import asyncio
from types import SimpleNamespace as NS

from agent.loop import ToolRegistry, build_registry
from agent.loop.discover import (
    build_spec,
    discover_domains,
    get_discovered,
    reset_discovered,
    screen_schema,
    tool_name_for,
)


# --------------------------- 参数筛子 ---------------------------
def test_no_required_params_is_allowed():
    ok, _ = screen_schema({"type": "object", "properties": {}})
    assert ok is True


def test_time_params_are_rejected():
    schema = {
        "type": "object",
        "properties": {
            "startTime": {"type": "string"},
            "endTime": {"type": "string"},
        },
        "required": ["startTime", "endTime"],
    }
    ok, reason = screen_schema(schema)
    assert ok is False
    assert "startTime" in reason


def test_enum_in_description_is_fillable():
    """枚举写在描述里就够了。

    MCP 侧确实这么写（syncSource: "7-电表 / 6-水表"），
    按参数名一刀切会把这类本来能用的误排掉。
    """
    schema = {
        "properties": {
            "syncSource": {
                "type": "string",
                "description": "设备类型,必传。7-电表 / 6-水表",
            }
        },
        "required": ["syncSource"],
    }
    assert screen_schema(schema)[0] is True


def test_user_supplied_code_is_fillable():
    """code 是用户能直接说出来的编号（CY-HIK-JK-001-0001），模型抄原话即可。"""
    schema = {
        "properties": {
            "code": {
                "type": "string",
                "description": "设备编号,必填,形如 CY-HIK-JK-001-0001",
            }
        },
        "required": ["code"],
    }
    assert screen_schema(schema)[0] is True


def test_internal_id_is_rejected():
    """必填 id 只能从别的接口的返回里取，用户不会主动报 uuid，模型只能编。"""
    schema = {"properties": {"id": {"type": "string"}}, "required": ["id"]}
    ok, why = screen_schema(schema)
    assert ok is False
    assert "id" in why


def test_enum_param_is_fillable():
    """枚举参数模型能照着选，属于可放行。"""
    schema = {
        "properties": {"type": {"type": "string", "enum": ["day", "week"]}},
        "required": ["type"],
    }
    assert screen_schema(schema)[0] is True


def test_optional_unsafe_params_do_not_block():
    """可选参数不填就是后端默认口径，不该因为它排除整个工具。"""
    schema = {
        "properties": {"startTime": {"type": "string"}},
        "required": [],
    }
    assert screen_schema(schema)[0] is True


# --------------------------- 工具名与包装 ---------------------------
def test_tool_name_strips_colon():
    assert tool_name_for("canteen", "canteen:getWeekMenu") == "canteen__getWeekMenu"


def test_build_spec_calls_mcp_and_governs():
    calls: list = []

    class _Box:
        async def call(self, server, tool, args):
            calls.append((server, tool, args))
            return '{"data": "周一至周五"}'

    tool = NS(
        name="canteen:getWeekMenu",
        description="本周菜单",
        inputSchema={"type": "object", "properties": {}},
    )
    spec = build_spec("canteen", tool, _Box())

    assert spec.name == "canteen__getWeekMenu"
    assert spec.parameters["type"] == "object"
    # 返回值是 MCP 原始 JSON，交给模型组织，不能直出给用户
    assert spec.passthrough is False

    outcome = asyncio.run(spec.execute({}))
    assert calls == [("canteen", "canteen:getWeekMenu", {})]
    assert "周一至周五" in outcome.text


def test_build_spec_reports_errors_as_is_error():
    class _Box:
        async def call(self, server, tool, args):
            raise RuntimeError("sidecar 500")

    tool = NS(name="canteen:getWeekMenu", description="", inputSchema={})
    outcome = asyncio.run(build_spec("canteen", tool, _Box()).execute({}))

    assert outcome.is_error is True
    assert "sidecar 500" in outcome.text


# --------------------------- 发现流程 ---------------------------
def _box_with(tools_by_server: dict, fail: set | None = None):
    fail = fail or set()

    class _Box:
        async def tool_schemas(self, server):
            if server in fail:
                raise RuntimeError("连接失败")
            return tools_by_server.get(server, [])

        async def call(self, server, tool, args):
            return "{}"

    return _Box()


def test_discover_skips_known_and_unsafe_tools():
    box = _box_with(
        {
            "canteen": [
                NS(name="canteen:getWeekMenu", description="", inputSchema={}),
                # 已被显式域使用 → 跳过，避免两个入口
                NS(name="canteen:getTodayMenu", description="", inputSchema={}),
                # 必填时间参数 → 跳过
                NS(
                    name="canteen:getDishPopularity",
                    description="",
                    inputSchema={
                        "properties": {"startTime": {"type": "string"}},
                        "required": ["startTime"],
                    },
                ),
            ]
        }
    )
    known = {("canteen", "canteen:getTodayMenu")}

    domains = asyncio.run(discover_domains(box, known, ("canteen",)))

    assert len(domains) == 1
    names = [tool.name for tool in domains[0].tools]
    assert names == ["canteen__getWeekMenu"]
    # 域上记录依赖，供启动自检复用
    assert domains[0].mcp_tools == (("canteen", "getWeekMenu"),)


def test_discover_survives_unreachable_server():
    box = _box_with({}, fail={"canteen"})
    domains = asyncio.run(discover_domains(box, set(), ("canteen",)))
    assert domains == []


def test_discover_skips_server_with_no_candidates():
    box = _box_with({"overview": []})
    assert asyncio.run(discover_domains(box, set(), ("overview",))) == []


# --------------------------- 装配 ---------------------------
def test_build_registry_merges_discovered_domains(monkeypatch):
    from agent.loop import discover as discover_mod
    from agent.loop.registry import DomainSpec
    from agent.loop.types import ToolOutcome, ToolSpec, tool_schema

    async def execute(args):
        return ToolOutcome("ok")

    reset_discovered()
    baseline = len(build_registry().tool_names())

    monkeypatch.setattr(
        discover_mod,
        "_discovered",
        [
            DomainSpec(
                name="canteen_tools",
                description="动态发现",
                tools=[
                    ToolSpec(
                        name="canteen__getWeekMenu",
                        description="本周菜单",
                        parameters=tool_schema({}, []),
                        execute=execute,
                    )
                ],
            )
        ],
    )

    registry = build_registry()
    assert len(registry.tool_names()) == baseline + 1
    assert registry.get("canteen__getWeekMenu") is not None
    reset_discovered()


def test_get_discovered_is_empty_before_warm():
    reset_discovered()
    assert get_discovered() == []
    # 没有预热时，registry 只装显式域，行为与改造前一致
    assert isinstance(build_registry(), ToolRegistry)


def test_excluded_overlapping_tool_is_skipped():
    """"列设备"这一类有重复入口的要排掉——模型会在两个入口间瞎选。"""
    box = _box_with(
        {
            "devicequery": [
                NS(name="devicequery:listDeviceWithSpace", description="", inputSchema={}),
                NS(name="devicequery:countByStatus", description="", inputSchema={}),
            ]
        }
    )

    domains = asyncio.run(discover_domains(box, set(), ("devicequery",)))

    names = [tool.name for tool in domains[0].tools]
    assert names == ["devicequery__countByStatus"]


def test_broken_tools_are_blacklisted():
    """2026-11-10：countMonitorBySubType 仍返回 null，先停掉止血。

    countByDeviceType 同日一起坏过（500 / 耗时 19~95s），已在 16:52 修复并放开，
    所以这里它应该正常出现。接口修好后从 _EXCLUDE_TOOLS 删掉对应名字即可。
    """
    box = _box_with(
        {
            "devicequery": [
                NS(name="devicequery:countByDeviceType", description="", inputSchema={}),
                NS(name="devicequery:countMonitorBySubType", description="", inputSchema={}),
                NS(name="devicequery:getEquipClass", description="", inputSchema={}),
            ]
        }
    )

    domains = asyncio.run(discover_domains(box, set(), ("devicequery",)))

    names = [tool.name for tool in domains[0].tools]
    assert names == ["devicequery__countByDeviceType", "devicequery__getEquipClass"]
