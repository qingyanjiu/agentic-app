import asyncio
import logging
import traceback
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, END

from mcp_client.mcp_loader import get_mcp_tools

logger = logging.getLogger(__name__)


# ============================================================
# Python event_type 到 Java MCP 工具名的映射
# 目前只实现告警列表一个工具
# 工具名与 Java 侧实际注册名一致：security:getSecurityAlarmList
# ============================================================
_JAVA_TOOL_MAP = {
    "alarm_list": "security:getSecurityAlarmList",
}


def _extract_text(result: Any) -> str:
    """
    从 MCP 工具返回结构中提取文本内容
    兼容：
      - 普通字符串
      - LangChain 的 [{type: 'text', text: '...'}] 列表
    """
    if isinstance(result, str):
        return result

    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            return first.get("text") or str(first)

    return str(result)


def _query_type_label(event_type: str) -> str:
    """根据 event_type 返回中文名称，用于生成确认问题"""
    return {
        "alarm_list": "告警列表",
    }.get(event_type, "数据")


async def _llm_format_security_result(
    llm: Any, event_type: str, state: dict, raw_text: str
) -> str:
    """
    让 LLM 把 MCP 工具返回的告警列表组织成自然语言回答。

    任何异常（LLM 未配置 / 调用失败 / 返回为空）都降级为原样返回，
    保证流程不断。
    """
    if llm is None:
        return raw_text

    try:
        label = _query_type_label(event_type)
        original_query = state.get("original_query") or event_type

        prompt = (
            "你是园区安防态势助手。下面是一次 MCP 工具查询的原始返回，"
            "请用中文自然、简洁地把告警列表整理给用户。\n\n"
            f"用户查询类型：{event_type}（{label}）\n"
            f"用户原话：{original_query}\n"
            "MCP 工具返回的原始数据：\n"
            f"{raw_text}\n\n"
            "要求：\n"
            "1. 如果有多条告警，逐条列出来，包含告警名称、发生位置、发生时间、状态；\n"
            "2. 回答必须基于返回数据，不要编造告警；\n"
            "3. 如果返回数据为空，如实说明今天没有告警；\n"
            "4. 回答要简短、口语化。"
        )

        resp = await llm.ainvoke(prompt)
        content = getattr(resp, "content", None)
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # 兼容 [{type: 'text', text: '...'}] 块列表格式
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
        logger.warning(f"LLM 格式化安防结果失败，降级为原样返回: {e}")

    return raw_text


# ============================================================
# LangGraph 状态定义
# 所有节点共享这个状态，节点通过返回更新字典来修改状态
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


class SecurityStatusGraphState(TypedDict):
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
# 每个节点接收当前状态，返回需要更新的字段
# ============================================================
def init_state(state: SecurityStatusGraphState) -> dict:
    """
    初始化节点：确保 slots 中日期结构存在
    """
    slots = state.get("slots", {})
    if "date" not in slots or not slots.get("date"):
        slots["date"] = {}
    return {"slots": slots}


def check_missing_params(state: SecurityStatusGraphState) -> dict:
    """
    检查缺失参数
    告警列表查询必须有时间范围（MCP 传 {startTime, endTime}）
    """
    slots = state["slots"]
    missing = []

    if not slots.get("date"):
        missing.append("date")

    return {"missing_params": missing}


def route_missing_params(state: SecurityStatusGraphState) -> str:
    """
    条件路由：根据是否缺失参数决定下一步
    """
    if state["missing_params"]:
        if state["ask_count"] >= 3:
            return "give_up"
        return "ask_param"
    return "check_date"


def ask_param(state: SecurityStatusGraphState) -> dict:
    """
    参数缺失时生成追问问题
    """
    missing = state["missing_params"]

    if "date" in missing:
        question = "请问您想查询哪个时间段的告警？"
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


def give_up(state: SecurityStatusGraphState) -> dict:
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


def check_date(state: SecurityStatusGraphState) -> dict:
    """
    日期检查占位节点，实际路由由条件边处理
    """
    return {}


def route_date_type(state: SecurityStatusGraphState) -> str:
    """
    根据日期类型路由
    安防支持任意时间区间（{startTime, endTime}），无需 confirm_today
    """
    date_slots = state["slots"].get("date", {})
    time_type = date_slots.get("time_type")

    if time_type == "future":
        return "future_date"
    if time_type == "vague":
        return "vague_date"
    return "call_tool"


def future_date(state: SecurityStatusGraphState) -> dict:
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
                    "content": "明天还没到呢，目前只能查询今天及历史的告警数据哦。",
                },
            },
            {"event": "custom", "data": {"type": "done"}},
        ],
    }


def vague_date(state: SecurityStatusGraphState) -> dict:
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
        f"您想查询近几天的告警？可以直接回复具体天数，例如“近两天”、“近四天”，"
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
    使用闭包传入 tools 和 llm，避免把依赖硬编码在节点里
    """
    async def call_tool(state: SecurityStatusGraphState) -> dict:
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

        # 按 Java 工具签名组装参数 {startTime, endTime}
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
            answer_text = await _llm_format_security_result(
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
            logger.error(f"调用安防工具失败: {e}\n{tb}")
            return {
                "error": f"查询失败: {str(e)}\n{tb}",
                "events": [{
                    "event": "custom",
                    "data": {"type": "error", "content": f"查询失败: {str(e)}\n{tb}"},
                }],
            }

    return call_tool


def finalize(state: SecurityStatusGraphState) -> dict:
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
def build_security_status_graph(tools: dict, llm=None) -> StateGraph:
    """
    构建安防态势 LangGraph 状态图

    流程：
        init -> check_missing_params
            -> ask_param (参数缺失，追问) -> END
            -> give_up (追问超限) -> END
            -> check_date
                -> future_date (未来时间) -> END
                -> vague_date (模糊时间) -> END
                -> call_tool (调用 MCP，LLM 组织告警列表回答) -> finalize -> END

    说明：安防 MCP 传 {startTime, endTime}，支持任意时间区间，
          所以不需要人员态势里的 confirm_today 确认节点。

    :param tools: MCP 工具字典（工具名 -> 工具）
    :param llm: 可选的 LLM，用于组织 MCP 返回生成回答；不传则原样返回
    """
    workflow = StateGraph(SecurityStatusGraphState)

    # 注册节点
    workflow.add_node("init", init_state)
    workflow.add_node("check_missing_params", check_missing_params)
    workflow.add_node("ask_param", ask_param)
    workflow.add_node("give_up", give_up)
    workflow.add_node("check_date", check_date)
    workflow.add_node("future_date", future_date)
    workflow.add_node("vague_date", vague_date)
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

    # 日期类型分支（非未来/模糊直接 call_tool）
    workflow.add_conditional_edges(
        "check_date",
        route_date_type,
        {
            "future_date": "future_date",
            "vague_date": "vague_date",
            "call_tool": "call_tool",
        },
    )

    # 结束边
    workflow.add_edge("ask_param", END)
    workflow.add_edge("give_up", END)
    workflow.add_edge("future_date", END)
    workflow.add_edge("vague_date", END)
    workflow.add_edge("call_tool", "finalize")
    workflow.add_edge("finalize", END)

    return workflow.compile()


# ============================================================
# 辅助：加载 MCP 工具
# ============================================================
async def load_security_tools() -> dict:
    """
    从 MCP 配置加载安防工具，并以工具名为 key 返回
    类级缓存由调用方维护
    """
    java_tool_names = set(_JAVA_TOOL_MAP.values())

    try:
        tools = await asyncio.wait_for(
            get_mcp_tools("mcp_client/mcp_server_config.yaml"),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.error("加载 MCP 安防工具超时")
        return {}
    except Exception as e:
        logger.error(f"加载 MCP 安防工具失败: {e}", exc_info=True)
        return {}

    filtered = [t for t in tools if t.name in java_tool_names]
    if not filtered:
        logger.warning(f"未找到任何安防 MCP 工具，期望 {java_tool_names}")

    return {t.name: t for t in filtered}
