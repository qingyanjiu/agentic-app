import asyncio
import logging
import traceback
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, END

from mcp_client.mcp_loader import get_mcp_tools

logger = logging.getLogger(__name__)


# ============================================================
# Python event_type 到 Java MCP 工具名的映射
# 信息发布模块 MCP 后端尚未实现，当前全部占位
# 待 Java 侧实现后，把占位名替换为真实工具名即可
# ============================================================
_JAVA_TOOL_MAP = {
    "info_view": "information:getInfoView",
    "broadcast_view": "information:getBroadcastView",
    "task_trend": "information:getTaskTrend",
    "program_count": "information:getProgramCount",
    "info_equip": "information:getInfoPublishiEquip",
    "broadcast_equip": "information:getBroadcastEquip",
}


def _extract_text(result: Any) -> str:
    """
    从 MCP 工具返回结构中提取文本内容
    兼容普通字符串 / LangChain 列表 / dict 包装
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


def _query_type_label(event_type: str) -> str:
    """根据 event_type 返回中文名称"""
    return {
        "info_view": "信息发布一览",
        "broadcast_view": "广播一览",
        "task_trend": "任务执行趋势",
        "program_count": "节目数量趋势",
        "info_equip": "信息发布设备",
        "broadcast_equip": "广播设备",
    }.get(event_type, "信息发布数据")


async def _llm_format_information_result(
    llm: Any, event_type: str, state: dict, raw_text: str
) -> str:
    """
    让 LLM 把 MCP 工具返回的数据组织成自然语言回答。
    任何异常都降级为原样返回，保证流程不断。
    """
    if llm is None:
        return raw_text

    try:
        label = _query_type_label(event_type)
        original_query = state.get("original_query") or event_type

        requirement = {
            "info_view": "列出信息发布设备统计、类型占比和设备列表；",
            "broadcast_view": "列出广播设备在线/离线/占用统计；",
            "task_trend": "按时间列出任务执行量趋势；",
            "program_count": "按时间列出节目数量趋势；",
            "info_equip": "列出信息发布设备明细；",
            "broadcast_equip": "列出广播设备明细及当前任务；",
        }.get(event_type, "把返回数据整理清楚；")

        prompt = (
            "你是智慧园区信息发布助手。下面是一次 MCP 工具查询的原始返回，"
            "请用中文自然、简洁地整理给用户。\n\n"
            f"用户查询类型：{event_type}（{label}）\n"
            f"用户原话：{original_query}\n"
            "MCP 工具返回的原始数据：\n"
            f"{raw_text}\n\n"
            "要求：\n"
            f"1. {requirement}\n"
            "2. 回答必须基于返回数据，不要编造；\n"
            "3. 如果返回数据为空，如实说明没有查到数据；\n"
            "4. 回答要简短、口语化。"
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
        logger.warning(f"LLM 格式化信息发布结果失败，降级为原样返回: {e}")

    return raw_text


def _merge_dicts(x: dict, y: dict) -> dict:
    if x is None:
        return y or {}
    if y is None:
        return x
    return {**x, **y}


def _merge_lists(x: list, y: list) -> list:
    if x is None:
        x = []
    if y is None:
        y = []
    return list(x) + list(y)


class InformationStatusGraphState(TypedDict):
    slots: Annotated[dict, _merge_dicts]
    missing_params: list
    ask_count: int
    unrelated_count: int
    last_question: str
    original_query: str
    answer: Optional[str]
    error: Optional[str]
    done: bool
    events: Annotated[list, _merge_lists]


def init_state(state: InformationStatusGraphState) -> dict:
    """初始化节点：确保 slots 中日期结构存在"""
    slots = state.get("slots", {})
    if "date" not in slots or not slots.get("date"):
        slots["date"] = {}
    return {"slots": slots}


def check_missing_params(state: InformationStatusGraphState) -> dict:
    """检查缺失参数：信息发布查询必须有时间范围"""
    slots = state["slots"]
    missing = []
    if not slots.get("date"):
        missing.append("date")
    return {"missing_params": missing}


def route_missing_params(state: InformationStatusGraphState) -> str:
    """条件路由：根据是否缺失参数决定下一步"""
    if state["missing_params"]:
        if state["ask_count"] >= 3:
            return "give_up"
        return "ask_param"
    return "check_date"


def ask_param(state: InformationStatusGraphState) -> dict:
    """参数缺失时生成追问问题"""
    missing = state["missing_params"]

    if "date" in missing:
        question = "请问您想查询哪天的信息发布数据？"
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


def give_up(state: InformationStatusGraphState) -> dict:
    """追问次数超限，放弃当前任务"""
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


def check_date(state: InformationStatusGraphState) -> dict:
    """日期检查占位节点"""
    return {}


def route_date_type(state: InformationStatusGraphState) -> str:
    """根据日期类型路由"""
    date_slots = state["slots"].get("date", {})
    time_type = date_slots.get("time_type")

    if time_type == "future":
        return "future_date"
    if time_type == "vague":
        return "vague_date"
    return "call_tool"


def future_date(state: InformationStatusGraphState) -> dict:
    """未来时间直接提醒，不查询"""
    return {
        "done": True,
        "events": [
            {
                "event": "custom",
                "data": {
                    "type": "answer",
                    "content": "明天还没到呢，目前只能查询今天及历史的信息发布数据哦。",
                },
            },
            {"event": "custom", "data": {"type": "done"}},
        ],
    }


def vague_date(state: InformationStatusGraphState) -> dict:
    """模糊时间先让用户确认具体范围"""
    date_slots = state["slots"].get("date", {})
    options = date_slots.get("options", ["近三天", "近一周", "近一个月"])

    slots = state["slots"]
    slots["_pending_date_clarify"] = True
    slots["_date_options"] = options

    options_str = "、".join(options)
    question = (
        f"您想查询近几天的信息发布数据？可以直接回复具体天数，例如“近两天”、“近四天”，"
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
    """构造调用 MCP 工具的节点"""
    async def call_tool(state: InformationStatusGraphState) -> dict:
        event_type = state["slots"].get("event_type")
        java_tool_name = _JAVA_TOOL_MAP.get(event_type)

        if not java_tool_name:
            logger.warning(f"event_type={event_type} 暂不被 Java MCP 后端支持")
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

        date_slots = state["slots"].get("date", {})
        tool_args = {
            "startTime": date_slots.get("start_time"),
            "endTime": date_slots.get("end_time"),
        }

        print(f"[MCP CALL] tool={java_tool_name}, args={tool_args}")

        try:
            result = await tool.ainvoke(tool_args)
            print(f"[MCP RAW RESULT] event_type={event_type}, result={result}")
            raw_text = _extract_text(result)
            answer_text = await _llm_format_information_result(
                llm, event_type, state, raw_text
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
            logger.error(f"调用信息发布工具失败: {e}\n{tb}")
            return {
                "error": f"查询失败: {str(e)}\n{tb}",
                "events": [{
                    "event": "custom",
                    "data": {"type": "error", "content": f"查询失败: {str(e)}\n{tb}"},
                }],
            }

    return call_tool


def finalize(state: InformationStatusGraphState) -> dict:
    """标记流程完成"""
    return {
        "done": True,
        "events": [{"event": "custom", "data": {"type": "done"}}],
    }


def build_information_status_graph(tools: dict, llm=None) -> StateGraph:
    """
    构建信息发布 LangGraph 状态图

    流程：
        init -> check_missing_params
            -> ask_param (参数缺失，追问) -> END
            -> give_up (追问超限) -> END
            -> check_date
                -> future_date (未来时间) -> END
                -> vague_date (模糊时间) -> END
                -> call_tool (调用 MCP，LLM 组织回答) -> finalize -> END
    """
    workflow = StateGraph(InformationStatusGraphState)

    workflow.add_node("init", init_state)
    workflow.add_node("check_missing_params", check_missing_params)
    workflow.add_node("ask_param", ask_param)
    workflow.add_node("give_up", give_up)
    workflow.add_node("check_date", check_date)
    workflow.add_node("future_date", future_date)
    workflow.add_node("vague_date", vague_date)
    workflow.add_node("call_tool", make_call_tool_node(tools, llm))
    workflow.add_node("finalize", finalize)

    workflow.set_entry_point("init")
    workflow.add_edge("init", "check_missing_params")

    workflow.add_conditional_edges(
        "check_missing_params",
        route_missing_params,
        {
            "ask_param": "ask_param",
            "give_up": "give_up",
            "check_date": "check_date",
        },
    )

    workflow.add_conditional_edges(
        "check_date",
        route_date_type,
        {
            "future_date": "future_date",
            "vague_date": "vague_date",
            "call_tool": "call_tool",
        },
    )

    workflow.add_edge("ask_param", END)
    workflow.add_edge("give_up", END)
    workflow.add_edge("future_date", END)
    workflow.add_edge("vague_date", END)
    workflow.add_edge("call_tool", "finalize")
    workflow.add_edge("finalize", END)

    return workflow.compile()


async def load_information_status_tools() -> dict:
    """
    从 MCP 配置加载信息发布工具，并以工具名为 key 返回
    类级缓存由调用方维护
    """
    java_tool_names = set(_JAVA_TOOL_MAP.values())

    try:
        tools = await asyncio.wait_for(
            get_mcp_tools("mcp_client/mcp_server_config.yaml"),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.error("加载 MCP 信息发布工具超时")
        return {}
    except Exception as e:
        logger.error(f"加载 MCP 信息发布工具失败: {e}", exc_info=True)
        return {}

    filtered = [t for t in tools if t.name in java_tool_names]
    if not filtered:
        logger.warning(f"未找到任何信息发布 MCP 工具，期望 {java_tool_names}")

    return {t.name: t for t in filtered}
