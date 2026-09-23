import asyncio
import logging
import traceback
from datetime import datetime, timedelta
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, END

from mcp_client.mcp_loader import get_mcp_tools
from agent.intent.slots import format_query_time

logger = logging.getLogger(__name__)


# ============================================================
# Python query_type 到 Java MCP 工具名的映射
# 与 Java 侧 EnergyMcpServerConfig / PlatformEnergyMcp 对齐：
#   overall_energy      -> energy:getOverallEnergyConsu
#   metering_equipment  -> energy:getMeteringEquipment
#   electricity_rank    -> energy:getElectricityUsageRanking
#   water_rank          -> energy:getWaterUsageRanking
#   device_status       -> energy:getDeviceStatus
#   realtime_electricity-> energy:getRealtimeElectricityUsage
#   realtime_water      -> energy:getRealtimeWaterUsage
# ============================================================
_JAVA_TOOL_MAP = {
    "overall_energy": "energy:getOverallEnergyConsu",
    "metering_equipment": "energy:getMeteringEquipment",
    "electricity_rank": "energy:getElectricityUsageRanking",
    "water_rank": "energy:getWaterUsageRanking",
    "device_status": "energy:getDeviceStatus",
    "realtime_electricity": "energy:getRealtimeElectricityUsage",
    "realtime_water": "energy:getRealtimeWaterUsage",
}


def _extract_text(result: Any) -> str:
    """
    从 MCP 工具返回结构中提取文本内容
    兼容：
      - 普通字符串
      - LangChain 的 [{type: 'text', text: '...'}] 列表
      - {'content': [{type: 'text', text: '...'}], 'id': '...'} 字典包装
    """
    if isinstance(result, str):
        return result

    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            result = content

    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            return first.get("text") or str(first)

    return str(result)


def _query_type_label(query_type: str) -> str:
    """根据 query_type 返回中文名称，用于生成 LLM 提示语"""
    return {
        "overall_energy": "总体能耗",
        "metering_equipment": "表具设备",
        "electricity_rank": "用电排名",
        "water_rank": "用水排名",
        "device_status": "能耗设备状态",
        "realtime_electricity": "用电周对比",
        "realtime_water": "用水周对比",
    }.get(query_type, "能源数据")


# ============================================================
# 能耗数值单位口径
# ------------------------------------------------------------
# Java 侧能源工具只回裸数字（如 {"electricity":12000,"water":3000}），返回结构里
# 没有任何单位字段，单位只能由本层在提示词里定死；不写死 LLM 会自由发挥成
# 「度」「MWh」「万度」，同一屏里单位打架（参照 get_energy_analysis_prompt 里
# 「不得出现单位混乱（如同时使用 MWh 和 度）」的同一条口径）。
#   电量类（electricity / 用电量） -> 千瓦时
#   水量类（water / 用水量）       -> 吨
# 注意计数不是能耗量：getMeteringEquipment 回的是电表/水表**数量**、
# getDeviceStatus 回的是设备**台数**，用「台」或「个」，不能跟着电量口径写成千瓦时。
# ============================================================
_ENERGY_UNIT_RULE = (
    "所有数值必须带单位：电量类数值（原始字段 electricity / 用电量）一律用「千瓦时」，"
    "水量类数值（原始字段 water / 用水量）一律用「吨」；"
    "表具数量、设备在线/离线台数等计数用「台」或「个」，不要给计数加千瓦时或吨。"
)


async def _llm_format_energy_result(
    llm: Any, query_type: str, state: dict, raw_text: str
) -> str:
    """
    根据 query_type 让 LLM 分析 MCP 工具返回，生成对应的中文回答。

    任何异常（LLM 未配置 / 调用失败 / 返回为空）都降级为原样返回，
    保证流程不断。
    """
    if llm is None:
        return raw_text

    try:
        label = _query_type_label(query_type)
        original_query = state.get("original_query") or query_type

        requirement = {
            "overall_energy": "列出电/水的年度累计和今日用量；",
            "metering_equipment": "列出电表和水表的数量；",
            "electricity_rank": "按用电量从高到低列出单位/区域排名；",
            "water_rank": "按用水量从高到低列出单位/区域排名；",
            "device_status": "列出能耗设备在线/离线数量；",
            "realtime_electricity": "按时间列出本周和上周的用电对比曲线；",
            "realtime_water": "按时间列出本周和上周的用水对比曲线；",
        }.get(query_type, "把返回数据整理清楚；")

        # 省略式追问（如「昨天呢」）时 original_query 仍是上一轮原话，其中的时间词
        # 不代表本次查询；把 slots 里真实查询时间显式交给 LLM，避免回答被原话带偏
        _qt = format_query_time(state.get("slots", {}).get("date"))
        query_time_line = (
            f"本次查询的时间范围：{_qt}（回答中的时间表述以此为准，不要沿用原话里的时间词）\n"
            if _qt else ""
        )

        prompt = (
            "你是智慧园区能源态势助手。下面是一次 MCP 工具查询的原始返回，"
            "请根据用户的查询类型，只提取对应的内容，用中文自然、简洁地回答用户。\n\n"
            f"用户查询类型：{query_type}（{label}）\n"
            f"{query_time_line}"
            f"用户原话：{original_query}\n"
            "MCP 工具返回的原始数据：\n"
            f"{raw_text}\n\n"
            "要求：\n"
            f"1. {requirement}\n"
            f"2. {_ENERGY_UNIT_RULE}\n"
            "3. 回答必须基于返回数据，不要编造数字；\n"
            "4. 如果返回数据里没有用户要查的信息，只如实说明即可，不要建议查询其他时间段或其他内容；\n"
            "5. 不要向用户提出任何追问、提议或反问，只回答本次查询的结果；\n"
            "6. 回答要简短、口语化。"
        )

        resp = await llm.ainvoke(prompt)
        content = getattr(resp, "content", None)
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in content
            )
        else:
            text = str(resp)
        text = text.strip().strip('"\n')
        if text:
            return text
    except Exception as e:
        logger.warning(f"LLM 格式化能源态势结果失败，降级为原样返回: {e}")

    return raw_text


# ============================================================
# LangGraph 状态定义
# ============================================================
def _merge_dicts(x: dict, y: dict) -> dict:
    """合并两个字典，用于 slots 的增量更新"""
    if x is None:
        return y or {}
    if y is None:
        return x
    return {**x, **y}


def _merge_lists(x: list, y: list) -> list:
    """合并两个列表，用于 events 的追加"""
    if x is None:
        x = []
    if y is None:
        y = []
    return list(x) + list(y)


class EnergyStatusGraphState(TypedDict):
    # 从当前意图状态中提取的参数
    slots: Annotated[dict, _merge_dicts]

    # 还缺少的参数列表
    missing_params: list

    # 追问次数计数
    ask_count: int

    # 用户连续回复不相关次数
    unrelated_count: int

    # 上次系统提出的问题
    last_question: str

    # 用户原始问题（供 LLM 生成回答时理解上下文）
    original_query: str

    # 最终返回给用户的答案
    answer: Optional[str]

    # 错误信息
    error: Optional[str]

    # 流程是否结束
    done: bool

    # 需要流式返回给前端的事件列表
    events: Annotated[list, _merge_lists]


# ============================================================
# 图节点函数
# ============================================================
def init_state(state: EnergyStatusGraphState) -> dict:
    """
    初始化节点：确保 slots 中日期结构存在
    """
    slots = state.get("slots", {})
    if "date" not in slots or not slots.get("date"):
        slots["date"] = {}
    return {"slots": slots}


def check_missing_params(state: EnergyStatusGraphState) -> dict:
    """
    检查缺失参数
    能源态势目前不强制需要额外参数
    """
    slots = state["slots"]
    missing = []
    return {"missing_params": missing}


def route_missing_params(state: EnergyStatusGraphState) -> str:
    """
    条件路由：根据是否缺失参数决定下一步
    """
    if state["missing_params"]:
        if state["ask_count"] >= 3:
            return "give_up"
        return "ask_param"
    return "check_date"


def ask_param(state: EnergyStatusGraphState) -> dict:
    """
    参数缺失时生成追问问题
    """
    missing = state["missing_params"]

    if "date" in missing:
        question = "请问您想查询哪一天的能源数据？"
    else:
        question = "请问您还需要补充什么信息？"

    ask_count = state["ask_count"] + 1

    return {
        "last_question": question,
        "ask_count": ask_count,
        "events": [{
            "event": "custom",
            "data": {
                "type": "ask",
                "question": question,
                "missing_params": missing,
                "ask_count": ask_count,
            },
        }],
    }


def give_up(state: EnergyStatusGraphState) -> dict:
    """
    追问次数超限，放弃当前任务
    """
    return {
        "done": True,
        "events": [
            {
                "event": "custom",
                "data": {
                    "type": "answer",
                    "content": "追问次数过多，我先不继续了。请问您还有其他问题吗？",
                },
            },
            {"event": "custom", "data": {"type": "done"}},
        ],
    }


def check_date(state: EnergyStatusGraphState) -> dict:
    """
    日期检查占位节点，实际路由由条件边处理
    """
    return {}


def _is_supported_energy_date(date_slots: dict) -> bool:
    """
    能源后台只有「今天」和「今年」两个统计口径（今日用量/年度累计），
    其他时间范围（昨天/近N天/上周/上月等）一律不支持

    判定按语义、双保险（排查记录案例 16）：
      1. 原话里直接提到今天/今年类词汇就直接放行——jionlp 分支的 raw
         存的是整句原话，其解析出的时间跨度形状多变（同一会话里
         「今天园区能耗情况怎么样？」与「今天的」解析形状不一致，
         纯日期计算会把前者误判成不支持），原话明确说了今天就不再
         依赖解析形状
      2. 纯日期计算兜底：起止落在今天（终点容忍次日零点的区间上界
         写法）；或当年 1 月 1 日起、终点不越过次年 1 月 1 日
    """
    raw = str(date_slots.get("raw") or "")
    if any(k in raw for k in ("今年", "本年", "年度", "全年")):
        return True
    if any(k in raw for k in ("今天", "今日", "当天")):
        return True

    start = date_slots.get("start_time")
    end = date_slots.get("end_time")
    try:
        s = datetime.fromisoformat(str(start))
        e = datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return False

    today = datetime.now().date()
    year_start = today.replace(month=1, day=1)
    next_year_start = year_start.replace(year=today.year + 1)

    # 今天：起点在今天，终点在今天或次日零点（区间可能写成左闭右开）
    if s.date() == today and e.date() in (today, today + timedelta(days=1)):
        return True
    # 今年：起点为当年 1 月 1 日，终点不越过次年 1 月 1 日
    if s.date() == year_start and e.date() <= next_year_start:
        return True
    return False


def route_date_type(state: EnergyStatusGraphState) -> str:
    """
    根据日期类型路由
    能源后台只有今天/今年两个统计口径：
      - 未来时间直接拒绝
      - 本周/上周对比（realtime_*）由后台固定返回，不做时间校验
      - 其余类型非今天/今年一律兜底回答，不查询
    """
    date_slots = state["slots"].get("date", {})
    time_type = date_slots.get("time_type")

    if time_type == "future":
        return "future_date"
    if time_type == "vague":
        return "vague_date"

    # 本周/上周对比数据的后台接口固定返回本周 vs 上周，与查询时间无关
    query_type = state["slots"].get("query_type")
    if query_type in ("realtime_electricity", "realtime_water"):
        return "call_tool"

    if not _is_supported_energy_date(date_slots):
        return "unsupported_date"
    return "call_tool"


def unsupported_date(state: EnergyStatusGraphState) -> dict:
    """
    非今天/今年的查询直接兜底回答，不调用 MCP
    （后台只有今日用量、年度累计两类数据，其他范围查不到）
    """
    return {
        "done": True,
        "events": [
            {
                "event": "custom",
                "data": {
                    "type": "answer",
                    "content": "当前后台仅支持查询今天或今年的能耗数据，其他时间范围暂时查不到。",
                },
            },
            {"event": "custom", "data": {"type": "done"}},
        ],
    }


def future_date(state: EnergyStatusGraphState) -> dict:
    """
    未来时间直接提醒，不查询
    """
    return {
        "done": True,
        "events": [
            {
                "event": "custom",
                "data": {
                    "type": "answer",
                    "content": "能源数据目前只能查询今天及历史的统计，未来的数据暂时无法预测哦。",
                },
            },
            {"event": "custom", "data": {"type": "done"}},
        ],
    }


def vague_date(state: EnergyStatusGraphState) -> dict:
    """
    模糊时间先让用户确认具体范围
    """
    date_slots = state["slots"].get("date", {})
    options = date_slots.get("options", ["近三天", "近一周", "近一个月"])

    slots = state["slots"]
    slots["_pending_date_clarify"] = True
    slots["_date_options"] = options

    options_str = "、".join(options)
    question = (
        f"您想查询近几天的能源数据？可以直接回复具体天数，例如“近两天”、“近四天”，"
        f"或选择：{options_str}"
    )

    return {
        "slots": slots,
        "last_question": question,
        "events": [{
            "event": "custom",
            "data": {
                "type": "ask",
                "question": question,
                "missing_params": [],
                "ask_count": state["ask_count"],
            },
        }],
    }


def make_call_tool_node(tools: dict, llm=None):
    """
    构造调用 MCP 工具的节点
    """
    async def call_tool(state: EnergyStatusGraphState) -> dict:
        query_type = state["slots"].get("query_type") or "count"
        java_tool_name = _JAVA_TOOL_MAP.get(query_type)

        if not java_tool_name:
            logger.warning(f"query_type={query_type} 暂不被 Java MCP 后端支持")
            return {
                "done": True,
                "events": [{
                    "event": "custom",
                    "data": {
                        "type": "answer",
                        "content": "该查询类型当前 MCP 后端暂不支持，请稍后再试。",
                    },
                }],
            }

        tool = tools.get(java_tool_name)
        if not tool:
            return {
                "error": f"{java_tool_name} 工具未加载",
                "events": [{
                    "event": "custom",
                    "data": {"type": "error", "content": f"{java_tool_name} 工具未加载"},
                }],
            }

        # 当前 Java 侧能源态势工具均为无参 GET 接口，直接空参调用
        tool_args = {}

        print(f"[MCP CALL] tool={java_tool_name}, args={tool_args}")

        try:
            result = await tool.ainvoke(tool_args)
            print(f"[MCP RAW RESULT] query_type={query_type}, result={result}")

            raw_text = _extract_text(result)
            answer_text = await _llm_format_energy_result(
                llm, query_type, state, raw_text
            )

            return {
                "answer": answer_text,
                "events": [{
                    "event": "custom",
                    "data": {"type": "answer", "content": answer_text},
                }],
            }
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"调用能源态势工具失败: {e}\n{tb}")
            return {
                "error": f"查询失败: {str(e)}\n{tb}",
                "events": [{
                    "event": "custom",
                    "data": {"type": "error", "content": f"查询失败: {str(e)}\n{tb}"},
                }],
            }

    return call_tool


def finalize(state: EnergyStatusGraphState) -> dict:
    """
    标记流程完成
    """
    return {
        "done": True,
        "events": [{"event": "custom", "data": {"type": "done"}}],
    }


# ============================================================
# 图构建
# ============================================================
def build_energy_status_graph(tools: dict, llm=None) -> StateGraph:
    """
    构建能源态势 LangGraph 状态图

    流程：
        init -> check_missing_params
            -> ask_param (参数缺失，追问) -> END
            -> give_up (追问超限) -> END
            -> check_date
                -> future_date (未来时间) -> END
                -> vague_date (模糊时间) -> END
                -> unsupported_date (非今天/今年) -> END
                -> call_tool (调用 MCP，LLM 组织回答) -> finalize -> END

    :param tools: MCP 工具字典（工具名 -> 工具）
    :param llm: 可选的 LLM，用于根据 query_type 分析 MCP 返回生成回答；不传则原样返回
    """
    workflow = StateGraph(EnergyStatusGraphState)

    # 注册节点
    workflow.add_node("init", init_state)
    workflow.add_node("check_missing_params", check_missing_params)
    workflow.add_node("ask_param", ask_param)
    workflow.add_node("give_up", give_up)
    workflow.add_node("check_date", check_date)
    workflow.add_node("future_date", future_date)
    workflow.add_node("vague_date", vague_date)
    workflow.add_node("unsupported_date", unsupported_date)
    workflow.add_node("call_tool", make_call_tool_node(tools, llm))
    workflow.add_node("finalize", finalize)

    # 入口和顺序边
    workflow.set_entry_point("init")
    workflow.add_edge("init", "check_missing_params")

    # 缺失参数分支
    workflow.add_conditional_edges(
        "check_missing_params",
        route_missing_params,
        {
            "ask_param": "ask_param",
            "give_up": "give_up",
            "check_date": "check_date",
        },
    )

    # 日期类型分支
    workflow.add_conditional_edges(
        "check_date",
        route_date_type,
        {
            "future_date": "future_date",
            "vague_date": "vague_date",
            "unsupported_date": "unsupported_date",
            "call_tool": "call_tool",
        },
    )

    # 结束边
    workflow.add_edge("ask_param", END)
    workflow.add_edge("give_up", END)
    workflow.add_edge("future_date", END)
    workflow.add_edge("vague_date", END)
    workflow.add_edge("unsupported_date", END)
    workflow.add_edge("call_tool", "finalize")
    workflow.add_edge("finalize", END)

    return workflow.compile()


# ============================================================
# 辅助：加载 MCP 工具
# ============================================================
async def load_energy_status_tools() -> dict:
    """
    从 MCP 配置加载能源态势工具，并以工具名为 key 返回
    类级缓存由调用方维护
    """
    java_tool_names = set(_JAVA_TOOL_MAP.values())

    try:
        tools = await asyncio.wait_for(
            get_mcp_tools("mcp_client/mcp_server_config.yaml"),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.error("加载 MCP 能源态势工具超时")
        return {}
    except Exception as e:
        logger.error(f"加载 MCP 能源态势工具失败: {e}", exc_info=True)
        return {}

    filtered = [t for t in tools if t.name in java_tool_names]
    if not filtered:
        logger.warning(f"未找到任何能源态势 MCP 工具，期望 {java_tool_names}")

    return {t.name: t for t in filtered}
