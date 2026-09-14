import asyncio
import logging
import traceback
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, END

from mcp_client.mcp_loader import get_mcp_tools

logger = logging.getLogger(__name__)


# ============================================================
# Python query_type 到 Java MCP 工具名的映射
# 与 Java 侧 DeviceMcpServerConfig / PlatformDeviceMcp 对齐：
#   equip_class      -> device:getEquipClass
#   category_health  -> device:getCategoryHealth
#   month_maintenance-> device:getMonthMaintenance
#   month_repair     -> device:getMonthRepair
#   statis_region    -> device:getStatisRegion
#   anfang_online    -> device:getAnfangDeviceOnlinePercentage
#   gb_online        -> device:getGbOnlinePercentage
#   mj_online        -> device:getMjOnlinePercentage
# ============================================================
_JAVA_TOOL_MAP = {
    "equip_class": "device:getEquipClass",
    "category_health": "device:getCategoryHealth",
    "month_maintenance": "device:getMonthMaintenance",
    "month_repair": "device:getMonthRepair",
    "statis_region": "device:getStatisRegion",
    "anfang_online": "device:getAnfangDeviceOnlinePercentage",
    "gb_online": "device:getGbOnlinePercentage",
    "mj_online": "device:getMjOnlinePercentage",
}

# 支持可选 date 参数的工具（Java 侧接口带 ?date=，格式 yyyy-MM）
_DATE_TOOL_TYPES = {"month_maintenance", "month_repair"}


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
        "equip_class": "设备分类占比",
        "category_health": "设备类别健康度",
        "month_maintenance": "月度维修趋势",
        "month_repair": "月度报修趋势",
        "statis_region": "分区域设备统计",
        "anfang_online": "安防设备在线率",
        "gb_online": "广播设备在线率",
        "mj_online": "门禁设备在线率",
    }.get(query_type, "设备态势数据")


async def _llm_format_device_result(
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
            "equip_class": "列出各设备分类的数量和占比；",
            "category_health": "按类别列出健康度评分、在线率、维保率、寿命情况；",
            "month_maintenance": "按月份列出维修量趋势；",
            "month_repair": "按月份列出报修量趋势；",
            "statis_region": "按区域列出设备数量统计；",
            "anfang_online": "列出安防设备在线率（total/zhoujie/dz/mj 各组统计）；",
            "gb_online": "列出广播设备在线率；",
            "mj_online": "列出门禁设备在线率；",
        }.get(query_type, "把返回数据整理清楚；")

        prompt = (
            "你是智慧园区设备态势助手。下面是一次 MCP 工具查询的原始返回，"
            "请根据用户的查询类型，只提取对应的内容，用中文自然、简洁地回答用户。\n\n"
            f"用户查询类型：{query_type}（{label}）\n"
            f"用户原话：{original_query}\n"
            "MCP 工具返回的原始数据：\n"
            f"{raw_text}\n\n"
            "要求：\n"
            f"1. {requirement}\n"
            "2. 回答必须基于返回数据，不要编造数字；\n"
            "3. 如果返回数据里没有用户要查的信息，只如实说明即可，不要建议查询其他时间段或其他内容；\n"
            "4. 不要向用户提出任何追问、提议或反问，只回答本次查询的结果；\n"
            "5. 回答要简短、口语化。"
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
        logger.warning(f"LLM 格式化设备态势结果失败，降级为原样返回: {e}")

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


class DeviceStatusGraphState(TypedDict):
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
def init_state(state: DeviceStatusGraphState) -> dict:
    """
    初始化节点：确保 slots 中日期结构存在
    """
    slots = state.get("slots", {})
    if "date" not in slots or not slots.get("date"):
        slots["date"] = {}
    return {"slots": slots}


def check_missing_params(state: DeviceStatusGraphState) -> dict:
    """
    检查缺失参数
    设备态势目前不强制需要额外参数（月度趋势的 date 为可选）
    """
    slots = state["slots"]
    missing = []
    return {"missing_params": missing}


def route_missing_params(state: DeviceStatusGraphState) -> str:
    """
    条件路由：设备态势无强制必填参数，直接调用工具
    """
    return "call_tool"


def make_call_tool_node(tools: dict, llm=None):
    """
    构造调用 MCP 工具的节点
    """
    async def call_tool(state: DeviceStatusGraphState) -> dict:
        query_type = state["slots"].get("query_type") or "equip_class"
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

        # 月度维修/报修趋势支持可选 date 参数（yyyy-MM），其余工具空参调用
        tool_args = {}
        if query_type in _DATE_TOOL_TYPES:
            date_slots = state["slots"].get("date") or {}
            start_time = date_slots.get("start_time")
            if start_time and isinstance(start_time, str):
                # start_time 为 ISO 时间串，取年月部分 yyyy-MM
                tool_args["date"] = start_time[:7]

        print(f"[MCP CALL] tool={java_tool_name}, args={tool_args}")

        try:
            result = await tool.ainvoke(tool_args)
            print(f"[MCP RAW RESULT] query_type={query_type}, result={result}")

            raw_text = _extract_text(result)
            answer_text = await _llm_format_device_result(
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
            logger.error(f"调用设备态势工具失败: {e}\n{tb}")
            return {
                "error": f"查询失败: {str(e)}\n{tb}",
                "events": [{
                    "event": "custom",
                    "data": {"type": "error", "content": f"查询失败: {str(e)}\n{tb}"},
                }],
            }

    return call_tool


def finalize(state: DeviceStatusGraphState) -> dict:
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
def build_device_status_graph(tools: dict, llm=None) -> StateGraph:
    """
    构建设备态势 LangGraph 状态图

    流程：
        init -> check_missing_params -> call_tool (调用 MCP，LLM 组织回答) -> finalize -> END

    :param tools: MCP 工具字典（工具名 -> 工具）
    :param llm: 可选的 LLM，用于根据 query_type 分析 MCP 返回生成回答；不传则原样返回
    """
    workflow = StateGraph(DeviceStatusGraphState)

    # 注册节点
    workflow.add_node("init", init_state)
    workflow.add_node("check_missing_params", check_missing_params)
    workflow.add_node("call_tool", make_call_tool_node(tools, llm))
    workflow.add_node("finalize", finalize)

    # 入口和顺序边
    workflow.set_entry_point("init")
    workflow.add_edge("init", "check_missing_params")

    # 设备态势无强制必填参数，直接路由到工具调用
    workflow.add_conditional_edges(
        "check_missing_params",
        route_missing_params,
        {
            "call_tool": "call_tool",
        },
    )

    # 结束边
    workflow.add_edge("call_tool", "finalize")
    workflow.add_edge("finalize", END)

    return workflow.compile()


# ============================================================
# 辅助：加载 MCP 工具
# ============================================================
async def load_device_status_tools() -> dict:
    """
    从 MCP 配置加载设备态势工具，并以工具名为 key 返回
    类级缓存由调用方维护
    """
    java_tool_names = set(_JAVA_TOOL_MAP.values())

    try:
        tools = await asyncio.wait_for(
            get_mcp_tools("mcp_client/mcp_server_config.yaml"),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.error("加载 MCP 设备态势工具超时")
        return {}
    except Exception as e:
        logger.error(f"加载 MCP 设备态势工具失败: {e}", exc_info=True)
        return {}

    filtered = [t for t in tools if t.name in java_tool_names]
    if not filtered:
        logger.warning(f"未找到任何设备态势 MCP 工具，期望 {java_tool_names}")

    return {t.name: t for t in filtered}
