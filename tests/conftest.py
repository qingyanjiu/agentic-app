# -*- coding: utf-8 -*-
"""
测试公共设施（全部测试文件统一放在 tests/ 目录下）。

依赖说明：完整运行环境在 docker 镜像内（langgraph / langchain_mcp_adapters /
sentence_transformers / jionlp 均已安装，意图模型经 INTENT_MODEL_PATH 挂载）。
测试直接导入项目真实模块，不注入任何 stub。

图执行统一用 asyncio.run() 包装，不依赖 pytest-asyncio。
"""
import asyncio
import importlib
import sys
from pathlib import Path

import pytest

# 把项目根目录加入 sys.path，保证 tests/ 内可直接 import 项目模块
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def run(coro):
    """同步测试里跑协程的统一入口"""
    return asyncio.run(coro)


# ============================================================
# 8 个态势域的图模块注册表
# 命名规则统一：graph.<module>_langgraph 里的 build_<module>_graph(tools, llm)
# ============================================================
GRAPH_MODULES = {
    "person_status": "graph.person_status_langgraph",
    "security_status": "graph.security_status_langgraph",
    "canteen_status": "graph.canteen_status_langgraph",
    "vehicle_status": "graph.vehicle_status_langgraph",
    "information_status": "graph.information_status_langgraph",
    "energy_status": "graph.energy_status_langgraph",
    "meeting_status": "graph.meeting_status_langgraph",
    "emergency_fire": "graph.emergency_fire_langgraph",
    "device_status": "graph.device_status_langgraph",
    "compositive_overview": "graph.compositive_overview_langgraph",
}


def get_graph_module(module_key: str):
    return importlib.import_module(GRAPH_MODULES[module_key])


# ============================================================
# FakeTool：模拟 LangChain MCP 工具
# 记录每次调用（工具名 -> calls），便于断言"调没调对工具"
# ============================================================
class FakeTool:
    def __init__(self, name: str, result: str = "", delay: float = 0.0):
        self.name = name
        self.result = result
        self.delay = delay
        self.calls = []

    async def ainvoke(self, args=None, **kwargs):
        self.calls.append(args)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


def make_tools(results_by_tool: dict, delay: float = 0.0) -> dict:
    """
    按时 {java工具名: 返回文本} 构造 {java工具名: FakeTool}
    """
    return {
        name: FakeTool(name, result, delay=delay)
        for name, result in results_by_tool.items()
    }


# ============================================================
# 各域 MCP 工具的模拟返回数据
# 结构参照 Java sidecar 真实返回（rows 列表 / 文本），只保证流程能走通
# ============================================================
DOMAIN_TOOL_RESULTS = {
    "person_status": {
        "person_status:getTodayPersonnelAffairs":
            "实时在园人数：12人；今日进入人数：30人；今日离开人数：25人。",
        "person_status:getTodayPersonnelFlow":
            '{"code":200,"rows":[{"time":"09:00","enter":5,"leave":3}]}',
        "person_status:getPersonnelStructure":
            '{"code":200,"rows":[{"dept":"中通服","count":80}]}',
    },
    "security_status": {
        "security:getSecurityIndex": '{"code":200,"rows":[{"index":92.5}]}',
        "security:getSecurityAlarmList": '{"code":200,"total":1,"rows":[{"alarmName":"周界入侵","areaName":"大门口"}]}',
        "security:getPatrolMission": '{"code":200,"total":2,"rows":[{"missionName":"上午巡查"}]}',
        # emergency_aialert 并入的新工具（开发计划 §2.4）
        "security:getAlarmView": '{"code":200,"todayAlarmCount":5,"totalAlarmCount":320,"algorithmTypeCount":8,"accuracy":95.2}',
        "security:getAlertSituation": '{"code":200,"rows":[{"date":"2026-09-14","count":6},{"date":"2026-09-15","count":3}]}',
        "security:getAlarmListWithType": '{"code":200,"rows":[{"type":"管理预警","count":4},{"type":"环境预警","count":2}]}',
    },
    "canteen_status": {
        "canteen:getDiningCount": '{"code":200,"total":1,"rows":[{"date":"2026-09-10","count":321}]}',
        "canteen:getDishPopularity": '{"code":200,"total":2,"rows":[{"dishName":"红烧肉","score":98}]}',
        "canteen:getWeekMenu": '{"code":200,"total":1,"rows":[{"day":"周一","meal":"午餐","dish":"番茄炒蛋"}]}',
    },
    "vehicle_status": {
        "vehicle_status:getParkingSpace": '{"code":200,"rows":[{"total":200,"used":120,"remain":80}]}',
        "vehicle_status:getTrafficVolume": '{"code":200,"rows":[{"time":"09:00","in":10,"out":8}]}',
    },
    "information_status": {
        "information:getInfoView": '{"code":200,"rows":[{"deviceName":"大堂信息屏","online":1}]}',
        "information:getTaskTrend": '{"code":200,"rows":[{"date":"2026-09-10","taskCount":15}]}',
    },
    "energy_status": {
        "energy:getOverallEnergyConsu": '{"code":200,"rows":[{"electricity":12000,"water":3000}]}',
        "energy:getElectricityUsageRanking": '{"code":200,"rows":[{"area":"A栋","value":500}]}',
    },
    "meeting_status": {
        "meeting:getMeetingStatistics": '{"code":200,"rows":[{"month":"2026-09","count":42,"avgDuration":55}]}',
        "meeting:getMeetingRoomStatus": '{"code":200,"rows":[{"roomName":"第一会议室","status":"占用"}]}',
        "meeting:getRoomMeetListByDay": '{"code":200,"total":1,"rows":[{"roomName":"第一会议室","meetName":"周会","startTime":"09:30"}]}',
        "meeting:getNumberOfMeetings": '{"code":200,"rows":[{"date":"2026-09-10","count":5}]}',
        "meeting:getMeetingRoomOverview": '{"code":200,"rows":[{"roomName":"第一会议室","capacity":12}]}',
        "meeting:getHighFrequencyMeetingRooms": '{"code":200,"rows":[{"roomName":"第一会议室","times":18}]}',
    },
    "emergency_fire": {
        "fire:getFireAlarmList":
            '{"code":200,"total":1,"rows":[{"title":"烟感火警","alarmTypeName":"火警",'
            '"assetsName":"3F-烟感-001","areaName":"A栋","alarmLevel":"高",'
            '"handleStatus":"未处理","nowAlarmTime":1725926400000,"alarmReason":"测试"}]}',
        "fire:getFireAlarmNum": '{"code":200,"rows":[{"fireNum":2,"faultNum":1}]}',
        "fire:getFireAssets": '{"code":200,"total":1,"rows":[{"assetsName":"干粉灭火器","deviceCode":"MH-001"}]}',
        "fire:getMonthRepair": '{"code":200,"rows":[{"month":"2026-09","repairNum":4}]}',
    },
    "device_status": {
        "device:getEquipClass":
            '{"code":200,"rows":[{"name":"消防设备","value":120},{"name":"空调","value":86},'
            '{"name":"能耗设备","value":64}]}',
        "device:getCategoryHealth":
            '{"code":200,"allDevices":[{"name":"消防设备","score":92,"onlineRate":0.98,'
            '"maintenanceRate":0.9,"lifeRate":0.85}]}',
        "device:getMonthMaintenance":
            '{"code":200,"xData":["2026-07","2026-08","2026-09"],"series":[{"name":"维修量","data":[5,8,3]}]}',
        "device:getMonthRepair":
            '{"code":200,"xData":["2026-07","2026-08","2026-09"],"series":[{"name":"报修量","data":[12,9,7]}]}',
        "device:getStatisRegion":
            '{"code":200,"MAX":[200,180],"VALUE":[150,120],"xAxisNameMap":["A栋","B栋"]}',
        "device:getAnfangDeviceOnlinePercentage":
            '{"code":200,"total":{"online":180,"total":200},"zhoujie":{"online":50,"total":52},'
            '"dz":{"online":60,"total":65},"mj":{"online":70,"total":72}}',
        "device:getGbOnlinePercentage":
            '{"code":200,"online":45,"total":50}',
        "device:getMjOnlinePercentage":
            '{"code":200,"online":70,"total":72}',
    },
    "compositive_overview": {
        "overview:getBasicInfo":
            '{"code":200,"centerArea":320,"iotDeviceCount":1200}',
        "overview:getDeviceHealth":
            '{"code":200,"rows":[{"name":"消防设备","score":92,'
            '"onlineRate":{"current":98,"total":100,"percent":98},'
            '"maintenanceRate":{"percent":90},'
            '"lifeRate":{"current":85,"total":100,"percent":85}}]}',
    },
}

# ============================================================
# 通用时间槽位（与 parse_time_slot 返回结构一致）
# ============================================================
SPAN_DATE = {
    "time_type": "span",
    "start_time": "2026-09-01T00:00:00",
    "end_time": "2026-09-10T12:00:00",
    "raw": "近十天",
}


def graph_input(slots: dict, ask_count: int = 0, original_query: str = "测试问题") -> dict:
    """
    构造喂给各域图的输入状态，字段与 app.py run_xxx_graph 的 graph_input 一致
    """
    return {
        "slots": slots,
        "missing_params": [],
        "ask_count": ask_count,
        "unrelated_count": 0,
        "last_question": "",
        "original_query": original_query,
        "answer": None,
        "error": None,
        "done": False,
        "events": [],
    }


def build_graph(module_key: str, tools: dict):
    """构建指定域的编译图（tools 为 {java工具名: FakeTool}，llm 传 None 走原样返回）"""
    module = get_graph_module(module_key)
    build_fn = getattr(module, f"build_{module_key}_graph")
    return build_fn(tools, llm=None)


def default_tools(module_key: str) -> dict:
    """按域的 _JAVA_TOOL_MAP 构造全量 mock 工具（每个 java 工具一个 FakeTool）"""
    module = get_graph_module(module_key)
    results = DOMAIN_TOOL_RESULTS[module_key]
    tools = {}
    for java_name in set(module._JAVA_TOOL_MAP.values()):
        tools[java_name] = FakeTool(java_name, results.get(java_name, '{"code":200,"rows":[]}'))
    return tools


def events_of(final_state: dict, event_type: str) -> list:
    """从图输出里筛出 data.type == event_type 的事件"""
    return [
        e["data"]
        for e in final_state.get("events", [])
        if e.get("event") == "custom" and e.get("data", {}).get("type") == event_type
    ]


def answers_of(final_state: dict) -> list:
    return [e.get("content", "") for e in events_of(final_state, "answer")]


def asks_of(final_state: dict) -> list:
    return events_of(final_state, "ask")


# ============================================================
# session 级缓存：每个域的编译图只 build 一次（Happy path 默认工具集）
# ============================================================
@pytest.fixture(scope="session")
def domain_graphs():
    cache = {}

    def _get(module_key: str):
        if module_key not in cache:
            cache[module_key] = build_graph(module_key, default_tools(module_key))
        return cache[module_key]

    return _get


@pytest.fixture(scope="session")
def fire_handler():
    from agent.intent.handlers.emergency_fire_handler import EmergencyFireHandler
    return EmergencyFireHandler()
