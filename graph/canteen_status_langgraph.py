import asyncio
import json
import logging
import traceback
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, END

from mcp_client.mcp_loader import get_mcp_tools
from agent.intent.slots import format_query_time

logger = logging.getLogger(__name__)


# ============================================================
# Python event_type 到 Java MCP 工具名的映射
# 与 Java 侧核对（DiningHallMcpServerConfig / PlatformDiningHallMcp）：
#   week_menu    -> canteen:getWeekMenu      ✅ Java 已实现（无入参，直接返回本周食谱）
#   dish_rank    -> canteen:getDishPopularity ✅ Java 已实现（支持 startTime/endTime）
#   dining_count -> canteen:getDiningCount    ✅ Java 已实现（支持 startTime/endTime/meal）
# ============================================================
_JAVA_TOOL_MAP = {
    "dish_rank": "canteen:getDishPopularity",
    "week_menu": "canteen:getWeekMenu",
    "dining_count": "canteen:getDiningCount",
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

    # 先解开外层 dict 里的 content 列表
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
    """根据 event_type 返回中文名称，用于生成 LLM 提示语"""
    return {
        "dish_rank": "本月菜品热度排行",
        "week_menu": "本周菜谱",
        "dining_count": "就餐人数统计",
    }.get(event_type, "食堂数据")


async def _llm_format_canteen_result(
    llm: Any, event_type: str, state: dict, raw_text: str
) -> str:
    """
    让 LLM 把 MCP 工具返回的数据组织成自然语言回答。

    任何异常（LLM 未配置 / 调用失败 / 返回为空）都降级为原样返回，
    保证流程不断。
    """
    if llm is None:
        return raw_text

    try:
        label = _query_type_label(event_type)
        original_query = state.get("original_query") or event_type

        # 今天日期 + 星期，让 LLM 知道今天周几（用户问"今天吃什么"时能正确回应当天）
        from datetime import datetime
        _today = datetime.now().date()
        today_label = "周" + "一二三四五六日"[_today.weekday()]
        today_str = f"{_today}（{today_label}）"

        # 每种子类型给不同的整理要求
        requirement = {
            "dish_rank": "按热度从高到低列出菜品，给出排名和热度/销量信息；",
            "week_menu": "有日期/星期区分就按天列，有餐次区分就按餐次分组；只有一天的数据就直接列出当天的菜；",
            "dining_count": "直接告诉用户就餐人数，如果是多个时间段，按时间列出；",
        }.get(event_type, "把返回数据整理清楚；")

        # 省略式追问（如「昨天呢」）时 original_query 仍是上一轮原话，其中的时间词
        # 不代表本次查询；把 slots 里真实查询时间显式交给 LLM，避免回答被原话带偏
        _qt = format_query_time(state.get("slots", {}).get("date"))
        query_time_line = (
            f"本次查询的时间范围：{_qt}（回答中的时间表述以此为准，不要沿用原话里的时间词）\n"
            if _qt else ""
        )

        prompt = (
            "你是智慧园区食堂管理助手。下面是一次 MCP 工具查询的原始返回，"
            "请用中文自然、简洁地整理给用户。\n\n"
            f"今天日期：{today_str}\n"
            f"用户查询类型：{event_type}（{label}）\n"
            f"{query_time_line}"
            f"用户原话：{original_query}\n"
            "MCP 工具返回的原始数据：\n"
            f"{raw_text}\n\n"
            "要求：\n"
            f"1. {requirement}\n"
            "2. 回答必须基于返回数据，不要编造；\n"
            "3. 如果返回数据为空，只如实说明没有查到数据，不要建议查询其他时间段或其他内容；\n"
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
        logger.warning(f"LLM 格式化食堂结果失败，降级为原样返回: {e}")

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


class CanteenStatusGraphState(TypedDict):
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
def init_state(state: CanteenStatusGraphState) -> dict:
    """
    初始化节点：确保 slots 中日期结构存在
    """
    slots = state.get("slots", {})
    if "date" not in slots or not slots.get("date"):
        slots["date"] = {}
    return {"slots": slots}


def check_missing_params(state: CanteenStatusGraphState) -> dict:
    """
    检查缺失参数
    食堂查询必须有时间范围（MCP 传 {startTime, endTime}）
    """
    slots = state["slots"]
    missing = []

    if not slots.get("date"):
        missing.append("date")

    return {"missing_params": missing}


def route_missing_params(state: CanteenStatusGraphState) -> str:
    """
    条件路由：根据是否缺失参数决定下一步
    """
    if state["missing_params"]:
        if state["ask_count"] >= 3:
            return "give_up"
        return "ask_param"
    return "check_date"


def ask_param(state: CanteenStatusGraphState) -> dict:
    """
    参数缺失时生成追问问题
    """
    missing = state["missing_params"]

    if "date" in missing:
        question = "请问您想查询哪天的食堂信息？"
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


def give_up(state: CanteenStatusGraphState) -> dict:
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


def check_date(state: CanteenStatusGraphState) -> dict:
    """
    日期检查占位节点，实际路由由条件边处理
    """
    return {}


def route_date_type(state: CanteenStatusGraphState) -> str:
    """
    根据日期类型路由
    食堂支持任意时间区间（{startTime, endTime}），无需 confirm_today
    """
    date_slots = state["slots"].get("date", {})
    time_type = date_slots.get("time_type")

    if time_type == "future":
        # 本周菜谱的数据是整周的（平台一次返回周一~周日），明天/后天等未来日期可以直接查；
        # 其他类型（热度排行/就餐人数）没有未来数据，仍走 future_date 拒绝
        if state["slots"].get("event_type") == "week_menu":
            return "call_tool"
        return "future_date"
    if time_type == "vague":
        return "vague_date"
    return "call_tool"


def future_date(state: CanteenStatusGraphState) -> dict:
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
                    "content": "明天还没到呢，目前只能查询今天及历史的数据哦。",
                },
            },
            {"event": "custom", "data": {"type": "done"}},
        ],
    }


def vague_date(state: CanteenStatusGraphState) -> dict:
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
        f"您想查询近几天的食堂信息？可以直接回复具体天数，例如“近两天”、“近四天”，"
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


# ============================================================
# 本周菜谱优化：紧凑化
# 背景：平台 getWeeklyRecipe 一次返回整周菜谱（N 天 × 3 餐次 × 菜品，
#       每菜还带 image/elementJson 等长字段），原始 JSON 很大，
#       直接喂 LLM 又慢又费 token。
# 这里在每次调用 MCP 后，把原始数据压成紧凑文本（只保留 日期/餐次/菜名/价格/单位），
# 并按用户问的日期和餐次过滤，再喂给 LLM，大幅减小 token、加快回复。
# 注意：Python 侧不再缓存整周菜谱，每次查询都会真正调用 MCP。
# ============================================================

# 餐次 key -> 中文标签（对应平台返回的 breakfast/lunch/dinner）
_MEAL_LABELS = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}


def _weekday_label(date_obj) -> str:
    """把日期转成平台的星期标签（'周一' ~ '周日'）"""
    return "周" + "一二三四五六日"[date_obj.weekday()]


async def _get_week_menu_raw(tool, tool_args) -> tuple:
    """
    获取整周菜谱原始数据：每次直接调用 MCP，不再在 Python 侧做周缓存
    返回 (result, False)
    """
    result = await tool.ainvoke(tool_args)
    print(f"[week_menu] 已调用 MCP 获取整周菜谱")
    return result, False


def _compact_week_menu(result, state: dict) -> str:
    """
    把整周菜谱的原始数据压成紧凑文本，再喂给 LLM

    原始数据结构（来自平台）：
      data: [
        {"day": "周一", "breakfast": [...], "lunch": [...], "dinner": [...]},
        ...
      ]
      每个菜品还带 image / elementJson / memberPrice 等长字段，整体很大。

    这里只保留：day、餐次、dishName、price、unit，
    并按用户问的日期（今天/昨天/前天）和餐次过滤，
    大幅减小喂给 LLM 的 token，加快回复。

    解析失败时兜底返回原始文本，保证流程不断。
    """
    from datetime import datetime, timedelta

    # 兼容字符串 JSON 和已解析的 dict/list
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except Exception:
            return result
    else:
        data = result

    # 数据可能在 {data: [...]} 包装里，也可能直接是列表
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return str(data)

    # 用户问的具体日期（来自 slot 的 raw 提示词）
    date_slots = state.get("slots", {}).get("date", {})
    raw_hint = str(date_slots.get("raw", ""))
    today = datetime.now().date()
    if "前天" in raw_hint:
        target_date = today - timedelta(days=2)
    elif "昨天" in raw_hint:
        target_date = today - timedelta(days=1)
    elif "今天" in raw_hint:
        target_date = today
    elif "大后天" in raw_hint:
        target_date = today + timedelta(days=3)
    elif "后天" in raw_hint:
        target_date = today + timedelta(days=2)
    elif "明天" in raw_hint:
        target_date = today + timedelta(days=1)
    else:
        target_date = None
    target_day = _weekday_label(target_date) if target_date else None

    # 食堂菜单只有周一~周五，问周末（周六/周日）直接提示没有数据
    if target_date and target_date.weekday() >= 5:
        return f"{target_day}是周末，食堂暂无菜单数据。"

    # 用户问的具体餐次（如"本周午餐" -> 午餐）
    target_meal = state.get("slots", {}).get("meal") or ""

    lines = []
    for day_item in items:
        if not isinstance(day_item, dict):
            continue
        day_label = str(day_item.get("day", ""))

        # 按日期过滤：问"今天"就只留对应那一天
        if target_day and not (target_day in day_label or day_label in target_day):
            continue

        meal_lines = []
        for meal_key, meal_label in _MEAL_LABELS.items():
            # 按餐次过滤
            if target_meal and meal_label != target_meal:
                continue
            dishes = day_item.get(meal_key)
            if not isinstance(dishes, list) or not dishes:
                continue
            names = []
            for d in dishes:
                if not isinstance(d, dict):
                    continue
                name = d.get("dishName")
                if not name:
                    continue
                price = d.get("price")
                unit = d.get("unit")
                if price:
                    names.append(f"{name}({price}元/{unit})" if unit else f"{name}({price}元)")
                else:
                    names.append(name)
            if names:
                meal_lines.append(f"{meal_label}: {'、'.join(names)}")

        if meal_lines:
            prefix = f"{day_label} " if day_label else ""
            lines.append(prefix + " | ".join(meal_lines))

    if lines:
        return "\n".join(lines)

    # 解析成功但按日期/餐次过滤后没有数据
    # （如明天是周末、明天不在本周数据范围内），返回空串让 LLM 如实说没查到，
    # 而不是把整周数据又喂进去
    return ""


def make_call_tool_node(tools: dict, llm=None):
    """
    构造调用 MCP 工具的节点
    使用闭包传入 tools 和 llm，避免把依赖硬编码在节点里
    """
    async def call_tool(state: CanteenStatusGraphState) -> dict:
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
        # 可选参数 meal（餐次）只有抽到时才传，避免多余参数
        date_slots = state["slots"].get("date", {})
        tool_args = {
            "startTime": date_slots.get("start_time"),
            "endTime": date_slots.get("end_time"),
        }
        if state["slots"].get("meal"):
            tool_args["meal"] = state["slots"]["meal"]

        print(f"[MCP CALL] tool={java_tool_name}, args={tool_args}")

        try:
            # 本周菜谱：每次直接调用 MCP，然后在 Python 侧做紧凑化再喂给 LLM
            if event_type == "week_menu":
                result, _ = await _get_week_menu_raw(tool, tool_args)
                print(f"[MCP RAW RESULT] event_type={event_type}, result={result}")
                # 整周菜谱原始数据很大，先压成紧凑文本（含按日期/餐次过滤）
                # 再喂 LLM，大幅减少 token、加快回复
                raw_text = _compact_week_menu(_extract_text(result), state)
                print(f"[week_menu] compact raw_text={raw_text[:200]}...")
            else:
                result = await tool.ainvoke(tool_args)
                print(f"[MCP RAW RESULT] event_type={event_type}, result={result}")
                raw_text = _extract_text(result)
            answer_text = await _llm_format_canteen_result(
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
            logger.error(f"调用食堂工具失败: {e}\n{tb}")
            return {
                "error": f"查询失败: {str(e)}\n{tb}",
                "events": [{
                    "event": "custom",
                    "data": {"type": "error", "content": f"查询失败: {str(e)}\n{tb}"},
                }],
            }

    return call_tool


def finalize(state: CanteenStatusGraphState) -> dict:
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
def build_canteen_status_graph(tools: dict, llm=None) -> StateGraph:
    """
    构建食堂管理 LangGraph 状态图

    流程：
        init -> check_missing_params
            -> ask_param (参数缺失，追问) -> END
            -> give_up (追问超限) -> END
            -> check_date
                -> future_date (未来时间) -> END
                -> vague_date (模糊时间) -> END
                -> call_tool (调用 MCP，LLM 组织回答) -> finalize -> END

    说明：食堂 MCP 传 {startTime, endTime}，支持任意时间区间，
          所以不需要人员态势里的 confirm_today 确认节点。

    :param tools: MCP 工具字典（工具名 -> 工具）
    :param llm: 可选的 LLM，用于组织 MCP 返回生成回答；不传则原样返回
    """
    workflow = StateGraph(CanteenStatusGraphState)

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
async def load_canteen_tools() -> dict:
    """
    从 MCP 配置加载食堂工具，并以工具名为 key 返回
    类级缓存由调用方维护
    """
    java_tool_names = set(_JAVA_TOOL_MAP.values())

    try:
        tools = await asyncio.wait_for(
            get_mcp_tools("mcp_client/mcp_server_config.yaml"),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.error("加载 MCP 食堂工具超时")
        return {}
    except Exception as e:
        logger.error(f"加载 MCP 食堂工具失败: {e}", exc_info=True)
        return {}

    filtered = [t for t in tools if t.name in java_tool_names]
    if not filtered:
        logger.warning(f"未找到任何食堂 MCP 工具，期望 {java_tool_names}")

    return {t.name: t for t in filtered}
