import asyncio
import logging
import traceback
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, END

from mcp_client.mcp_loader import get_mcp_tools

# deviceType 码到中文名的权威映射（别名词典的首个别名即标准名），
# 只用于拼 LLM 提示语，下发给 Java 的仍是码本身
from agent.intent.slots import DEVICE_TYPE_DICT, format_query_time

logger = logging.getLogger(__name__)


def _device_type_label(device_type: str) -> str:
    """deviceType 码转中文名，未知码原样返回"""
    if not device_type:
        return "不限"
    return DEVICE_TYPE_DICT.get(device_type, [device_type])[0]


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
#
# 另外一条台账口径的分支（与 device_query 共用同一个工具）：
#   device_list      -> device_query:listDeviceOnly
#
#   "查下设备"这类笼统问法正则落 count 兜底、分类器也救不回来时，
#   不复用"暂不支持"守卫，而是反问"哪类设备"；用户选定后按台账口径
#   列出该类设备（设备名称/编号/所在位置/启用状态），用的就是 device_query
#   意图的资产库列表工具——设备域只有"设备列表/设备详情"两个台账工具。
#   注意这条分支走的是**资产库**口径（syncSource 0门禁…7电表），
#   和设备态势的在线率/统计工具不是一回事。
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
    "device_list": "device_query:listDeviceOnly",
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
        "device_list": "设备列表查询",
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

        # 台账口径要带上用户选定的设备类型（设备态势其余子类型是全域统计）
        type_rule = ""
        if query_type == "device_list":
            device_type = state.get("slots", {}).get("device_type") or ""
            if device_type:
                type_rule = (
                    f"用户选定的设备类型：{_device_type_label(device_type)}，"
                    "回答里点明查的是这类设备；\n"
                )

        requirement = {
            "equip_class": "列出各设备分类的数量和占比；",
            "category_health": "按类别列出健康度评分、在线率、维保率、寿命情况；",
            "month_maintenance": "按月份列出维修量趋势；",
            "month_repair": "按月份列出报修量趋势；",
            "statis_region": "按区域列出设备数量统计；",
            "anfang_online": "列出安防设备在线率（total/zhoujie/dz/mj 各组统计）；",
            "gb_online": "列出广播设备在线率；",
            "mj_online": "列出门禁设备在线率；",
            "device_list": (
                "把查到的设备逐条列出来（设备名称、设备编号、所在位置、启用状态；"
                "status 是启用状态 0停用 1启用 2维修 3报废，不要说成在线/离线）；"
            ),
        }.get(query_type, "把返回数据整理清楚；")

        # 省略式追问（如「昨天呢」）时 original_query 仍是上一轮原话，其中的时间词
        # 不代表本次查询；把 slots 里真实查询时间显式交给 LLM，避免回答被原话带偏
        _qt = format_query_time(state.get("slots", {}).get("date"))
        query_time_line = (
            f"本次查询的时间范围：{_qt}（回答中的时间表述以此为准，不要沿用原话里的时间词）\n"
            if _qt else ""
        )

        prompt = (
            "你是智慧园区设备态势助手。下面是一次 MCP 工具查询的原始返回，"
            "请根据用户的查询类型，只提取对应的内容，用中文自然、简洁地回答用户。\n\n"
            f"用户查询类型：{query_type}（{label}）\n"
            f"{query_time_line}"
            f"用户原话：{original_query}\n"
            "MCP 工具返回的原始数据：\n"
            f"{raw_text}\n\n"
            "要求：\n"
            f"1. {requirement}\n"
            f"{type_rule}"
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

    兜底值 count 只表示"正则没判出子类型"，本身不是合法工具类型：
    设备类型已明确时按台账口径归一到 device_list（与 handle_reply 的落地一致）
    """
    slots = state.get("slots", {})
    if "date" not in slots or not slots.get("date"):
        slots["date"] = {}
    if slots.get("query_type") in (None, "", "count") and slots.get("device_type"):
        slots["query_type"] = "device_list"
    return {"slots": slots}


def check_missing_params(state: DeviceStatusGraphState) -> dict:
    """
    检查缺失参数（与 DeviceStatusHandler._get_missing_params 保持一致）

    设备态势其余子类型不强制需要额外参数（月度趋势的 date 为可选），
    唯一会缺的是"笼统问法"：query_type 落到 count 兜底，
    分不清用户要哪类设备数据，此时视为缺 device_type，走反问。
    """
    slots = state["slots"]
    query_type = slots.get("query_type")
    missing = []

    if not query_type or query_type == "count":
        missing.append("device_type")

    return {"missing_params": missing}


def route_missing_params(state: DeviceStatusGraphState) -> str:
    """
    条件路由：缺设备类型就反问，反问超过 3 次放弃，否则直接调用工具
    """
    if state["missing_params"]:
        if state["ask_count"] >= 3:
            return "give_up"
        return "ask_param"
    return "call_tool"


def ask_param(state: DeviceStatusGraphState) -> dict:
    """
    参数缺失时生成追问问题（与 DeviceStatusHandler.generate_question 保持一致）
    """
    missing = state["missing_params"]

    if "device_type" in missing:
        question = "请问您想查询哪类设备？门禁、道闸、梯控、监控、入侵报警、广播、水表，还是电表？"
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


def give_up(state: DeviceStatusGraphState) -> dict:
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
        elif query_type == "device_list":
            # 台账口径：资产库列表按 syncSource（设备类型码）筛选
            device_type = state["slots"].get("device_type")
            if device_type:
                tool_args["syncSource"] = device_type

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
        init -> check_missing_params ->┬-> call_tool (调用 MCP，LLM 组织回答) -> finalize -> END
                                       ├-> ask_param (笼统问法缺设备类型，反问后等用户回复) -> END
                                       └-> give_up (追问超过 3 次) -> END

    参数：除"哪类设备"（deviceType）外无强制必填参数；
    月度趋势的 date 为可选，由 call_tool 透传

    :param tools: MCP 工具字典（工具名 -> 工具）
    :param llm: 可选的 LLM，用于根据 query_type 分析 MCP 返回生成回答；不传则原样返回
    """
    workflow = StateGraph(DeviceStatusGraphState)

    # 注册节点
    workflow.add_node("init", init_state)
    workflow.add_node("check_missing_params", check_missing_params)
    workflow.add_node("ask_param", ask_param)
    workflow.add_node("give_up", give_up)
    workflow.add_node("call_tool", make_call_tool_node(tools, llm))
    workflow.add_node("finalize", finalize)

    # 入口和顺序边
    workflow.set_entry_point("init")
    workflow.add_edge("init", "check_missing_params")

    workflow.add_conditional_edges(
        "check_missing_params",
        route_missing_params,
        {
            "ask_param": "ask_param",
            "give_up": "give_up",
            "call_tool": "call_tool",
        },
    )

    # 结束边
    workflow.add_edge("ask_param", END)
    workflow.add_edge("give_up", END)
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
