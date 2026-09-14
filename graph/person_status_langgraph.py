import asyncio
import logging
import re
import traceback
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, END

from agent.intent.handlers.person_status_handler import PersonStatusHandler
from mcp_client.mcp_loader import get_mcp_tools

logger = logging.getLogger(__name__)


# ============================================================
# Python query_type 到 Java MCP 工具名的映射
# Java 侧车目前只实现了今日态势和人员流动两个工具
# ============================================================
_JAVA_TOOL_MAP = {
    "realtime": "person_status:getTodayPersonnelAffairs",
    "enter": "person_status:getTodayPersonnelAffairs",
    "leave": "person_status:getTodayPersonnelAffairs",
    "count": "person_status:getTodayPersonnelAffairs",
    "flow": "person_status:getTodayPersonnelFlow",
    "structure": "person_status:getPersonnelStructure",
}


def _is_today(date_slots: dict) -> bool:
    """
    判断用户查询的时间范围是否为今天
    人员态势后端目前只支持查询今日数据
    """
    start_time = date_slots.get("start_time")
    if not start_time:
        return True

    try:
        start_dt = datetime.fromisoformat(start_time)
        return start_dt.date() == date.today()
    except Exception:
        # 解析失败时默认按今天处理，避免误拦截
        return True


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


def _format_person_status_result(query_type: str, raw_text: str) -> str:
    """
    根据 query_type 从 Java 返回的今日态势文本中提取用户关心的字段

    Java 返回格式示例：
      "实时在园人数：0人；今日进入人数：0人；今日离开人数：0人。"
    """
    # 1. 人员流动趋势：直接返回原始数据
    if query_type == "flow":
        return raw_text

    # 2. 实时/总人数：提取"实时在园人数"
    if query_type in ("realtime", "count"):
        m = re.search(r"实时在园人数[：:]\s*(\d+)\s*人", raw_text)
        if m:
            return f"实时在园人数：{m.group(1)}人"
        return raw_text

    # 3. 进入人数
    if query_type == "enter":
        m = re.search(r"今日进入人数[：:]\s*(\d+)\s*人", raw_text)
        if m:
            return f"今日进入人数：{m.group(1)}人"
        return raw_text

    # 4. 离开人数
    if query_type == "leave":
        m = re.search(r"今日离开人数[：:]\s*(\d+)\s*人", raw_text)
        if m:
            return f"今日离开人数：{m.group(1)}人"
        return raw_text

    # 兜底：原样返回
    return raw_text


def _query_type_label(query_type: str) -> str:
    """根据 query_type 返回中文名称，用于生成确认问题"""
    return {
        "realtime": "实时人数",
        "enter": "进入人数",
        "leave": "离开人数",
        "count": "园区总人数",
        "flow": "人员流动趋势",
        "structure":"人员结构"
    }.get(query_type, "数据")


async def _llm_format_person_status_result(
    llm: Any, query_type: str, state: dict, raw_text: str
) -> str:
    """
    根据 query_type 让 LLM 分析 MCP 工具返回，生成对应的中文回答。

    对齐 reactive_pipeline 里 Answer Composer 的写法：
    把"用户原话 + 原始返回"交给 LLM，由它提取相关项并组织自然语言。
    任何异常（LLM 未配置 / 调用失败 / 返回为空）都降级为原有的正则提取，保证流程不断。
    """
    if llm is None:
        return _format_person_status_result(query_type, raw_text)

    try:
        label = _query_type_label(query_type)
        original_query = state.get("original_query") or query_type

        prompt = (
            "你是园区人员态势助手。下面是一次 MCP 工具查询的原始返回，"
            "请根据用户的查询类型，只提取对应的内容，用中文自然、简洁地回答用户。\n\n"
            f"用户查询类型：{query_type}（{label}）\n"
            f"用户原话：{original_query}\n"
            "MCP 工具返回的原始数据：\n"
            f"{raw_text}\n\n"
            "要求：\n"
            "1. 只回答用户问的那一项。例如用户问进入人数，就只给今日进入人数，"
            "不要把所有字段都列出来；\n"
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
        logger.warning(f"LLM 格式化人员态势结果失败，降级为正则提取: {e}")

    return _format_person_status_result(query_type, raw_text)


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


class PersonStatusGraphState(TypedDict):
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
def init_state(state: PersonStatusGraphState) -> dict:
    """
    初始化节点：确保 slots 中日期结构存在
    """
    slots = state.get("slots", {})
    if "date" not in slots or not slots.get("date"):
        slots["date"] = {}
    return {"slots": slots}


def check_missing_params(state: PersonStatusGraphState) -> dict:
    """
    检查缺失参数
    位置和轨迹查询必须有人名
    """
    slots = state["slots"]
    query_type = slots.get("query_type")
    missing = []

    if query_type in ["location", "trace"] and not slots.get("person_name"):
        missing.append("person_name")

    return {"missing_params": missing}


def route_missing_params(state: PersonStatusGraphState) -> str:
    """
    条件路由：根据是否缺失参数决定下一步
    """
    if state["missing_params"]:
        if state["ask_count"] >= 3:
            return "give_up"
        return "ask_param"
    return "check_date"


def ask_param(state: PersonStatusGraphState) -> dict:
    """
    参数缺失时生成追问问题
    """
    missing = state["missing_params"]

    if "person_name" in missing:
        question = "请问您要查询哪位人员？"
    elif "date" in missing:
        question = "请问您想查询哪一天？"
    elif "area" in missing:
        question = "请问您想查询哪个区域？"
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


def give_up(state: PersonStatusGraphState) -> dict:
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


def check_date(state: PersonStatusGraphState) -> dict:
    """
    日期检查占位节点，实际路由由条件边处理
    """
    return {}


def route_date_type(state: PersonStatusGraphState) -> str:
    """
    根据日期类型路由
    """
    date_slots = state["slots"].get("date", {})
    time_type = date_slots.get("time_type")

    if time_type == "future":
        return "future_date"
    if time_type == "vague":
        return "vague_date"
    return "check_confirm"


def future_date(state: PersonStatusGraphState) -> dict:
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
                    "content": "明天还没到呢，目前只能查询今天及历史的人员态势数据哦。",
                },
            },
            {"event": "custom", "data": {"type": "done"}},
        ],
    }


def vague_date(state: PersonStatusGraphState) -> dict:
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
        f"您想查询近几天？可以直接回复具体天数，例如“近两天”、“近四天”，"
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


def check_confirm(state: PersonStatusGraphState) -> dict:
    """
    确认节点占位，实际路由由条件边处理
    """
    return {}


def route_confirm(state: PersonStatusGraphState) -> str:
    """
    非今天查询时，先询问用户是否查看今日数据
    """
    query_type = state["slots"].get("query_type")
    date_slots = state["slots"].get("date", {})

    if query_type in ("realtime", "enter", "leave", "count"):
        if not _is_today(date_slots) and not state["slots"].get("_confirm_proceed"):
            return "confirm_today"

    return "call_tool"


def confirm_today(state: PersonStatusGraphState) -> dict:
    """
    非今天查询时，询问用户是否改为查看今日数据
    """
    query_type = state["slots"].get("query_type")
    slots = state["slots"]
    slots["_pending_confirm"] = True

    question = (
        "当前平台后端仅支持查询今日数据，"
        f"是否为您展示今日{_query_type_label(query_type)}？"
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
    async def call_tool(state: PersonStatusGraphState) -> dict:
        query_type = state["slots"].get("query_type")
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

        # 按 Java 工具签名组装参数
        if query_type == "flow":
            tool_args = {"type": "day"}
        else:
            tool_args = {}

        print(f"[MCP CALL] tool={java_tool_name}, args={tool_args}")

        try:
            result = await tool.ainvoke(tool_args)
            print(f"[MCP RAW RESULT] query_type={query_type}, result={result}")

            raw_text = _extract_text(result)
            answer_text = await _llm_format_person_status_result(
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
            logger.error(f"调用人员态势工具失败: {e}\n{tb}")
            return {
                "error": f"查询失败: {str(e)}\n{tb}",
                "events": [{
                    "event": "custom",
                    "data": {"type": "error", "content": f"查询失败: {str(e)}\n{tb}"},
                }],
            }

    return call_tool


def finalize(state: PersonStatusGraphState) -> dict:
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
def build_person_status_graph(tools: dict, llm=None) -> StateGraph:
    """
    构建人员态势 LangGraph 状态图

    流程：
        init -> check_missing_params
            -> ask_param (参数缺失，追问) -> END
            -> give_up (追问超限) -> END
            -> check_date
                -> future_date (未来时间) -> END
                -> vague_date (模糊时间) -> END
                -> check_confirm
                    -> confirm_today (非今天查询确认) -> END
                    -> call_tool (调用 MCP，LLM 根据 query_type 组织回答) -> finalize -> END

    :param tools: MCP 工具字典（工具名 -> 工具）
    :param llm: 可选的 LLM，用于根据 query_type 分析 MCP 返回生成回答；不传则降级为正则提取
    """
    workflow = StateGraph(PersonStatusGraphState)

    # 注册节点
    workflow.add_node("init", init_state)
    workflow.add_node("check_missing_params", check_missing_params)
    workflow.add_node("ask_param", ask_param)
    workflow.add_node("give_up", give_up)
    workflow.add_node("check_date", check_date)
    workflow.add_node("future_date", future_date)
    workflow.add_node("vague_date", vague_date)
    workflow.add_node("check_confirm", check_confirm)
    workflow.add_node("confirm_today", confirm_today)
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
            "check_confirm": "check_confirm",
        },
    )

    # 非今天确认分支
    workflow.add_conditional_edges(
        "check_confirm",
        route_confirm,
        {
            "confirm_today": "confirm_today",
            "call_tool": "call_tool",
        },
    )

    # 结束边
    workflow.add_edge("ask_param", END)
    workflow.add_edge("give_up", END)
    workflow.add_edge("future_date", END)
    workflow.add_edge("vague_date", END)
    workflow.add_edge("confirm_today", END)
    workflow.add_edge("call_tool", "finalize")
    workflow.add_edge("finalize", END)

    return workflow.compile()


# ============================================================
# 辅助：加载 MCP 工具
# ============================================================
async def load_person_status_tools() -> dict:
    """
    从 MCP 配置加载人员态势工具，并以工具名为 key 返回
    类级缓存由调用方维护
    """
    java_tool_names = set(_JAVA_TOOL_MAP.values())

    try:
        tools = await asyncio.wait_for(
            get_mcp_tools("mcp_client/mcp_server_config.yaml"),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.error("加载 MCP 人员态势工具超时")
        return {}
    except Exception as e:
        logger.error(f"加载 MCP 人员态势工具失败: {e}", exc_info=True)
        return {}

    filtered = [t for t in tools if t.name in java_tool_names]
    if not filtered:
        logger.warning(f"未找到任何人员态势 MCP 工具，期望 {java_tool_names}")

    return {t.name: t for t in filtered}
