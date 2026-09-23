import asyncio
import json
import logging
import traceback
from datetime import datetime
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, END

from mcp_client.mcp_loader import get_mcp_tools
from agent.intent.slots import format_query_time

logger = logging.getLogger(__name__)


# ============================================================
# Python query_type 到 Java MCP 工具名的映射
# 与 Java 侧 FireMcpServerConfig / PlatformFireMcp 对齐：
#   fire_assets     -> fire:getFireAssets
#   fire_alarm_num  -> fire:getFireAlarmNum
#   fire_alarm_list -> fire:getFireAlarmList
#   month_repair    -> fire:getMonthRepair
# ============================================================
_JAVA_TOOL_MAP = {
    "fire_assets": "fire:getFireAssets",
    "fire_alarm_num": "fire:getFireAlarmNum",
    "fire_alarm_list": "fire:getFireAlarmList",
    "month_repair": "fire:getMonthRepair",
}

# 消防子类型反问的固定选项顺序（与 _JAVA_TOOL_MAP / 反问文案顺序一致），
# 存入 slots._type_options 供追问轮解析「第N个」序数指代
_TYPE_OPTIONS = ["fire_assets", "fire_alarm_num", "fire_alarm_list", "month_repair"]


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
        "fire_assets": "消防设备台账",
        "fire_alarm_num": "设备告警统计",
        "fire_alarm_list": "实时告警",
        "month_repair": "月度报修",
    }.get(query_type, "消防数据")


# ============================================================
# 消防台账 / 实时告警列表优化：紧凑化
# 背景：平台 getAssetsList / getAlarmList 一次返回整个 rows 列表，
#       每条还带多个字段，原始 JSON 很大，
#       直接喂 LLM 又慢又费 token。
# 这里在每次调用 MCP 后，把原始 rows 压成紧凑文本
# （只保留关键字段、限制条数），再喂给 LLM，大幅减小 token、加快回复。
# 解析失败时兜底返回原始文本，保证流程不断。
# ============================================================

# rows 列表最大保留条数
_MAX_ROWS = 20

# 台账单行保留字段（截断长字段）
# 注意：压力/液位/电量/倾角不在行内平铺，而在 dataMap 数组里
# （每项 {monitorName, monitorValue, unit}），由 _compact_fire_assets 单独展平
_ASSET_KEEP_FIELDS = [
    "assetsName", "offLineFlag", "faultFlag", "usageFlag",
    "hiddenDangerFlag", "deviceCode",
]


def _compact_fire_rows(raw_text: str, keep_fields: list) -> str:
    """
    把 rows 列表的原始 JSON 压成紧凑文本，再喂给 LLM

    原始数据结构（来自平台）：
      { msg; total; code; rows: [...] }

    这里只保留 keep_fields 中列出的关键字段（字段值过长时截断），
    并限制最多 _MAX_ROWS 条，超过的在末尾注明总数，
    大幅减小喂给 LLM 的 token，加快回复。

    字段名以 Time 结尾且值为纯数字时，按毫秒时间戳格式化为 MM-dd HH:mm，
    避免 LLM 自己换算出错。

    解析失败时兜底返回原始文本，保证流程不断。
    """
    # 兼容字符串 JSON 和已解析的 dict/list
    try:
        data = json.loads(raw_text)
    except Exception:
        return raw_text

    if not isinstance(data, dict):
        return raw_text

    rows = data.get("rows")
    if not isinstance(rows, list):
        return raw_text

    total = data.get("total", len(rows))
    lines = [f"共 {total} 条，以下展示前 {min(len(rows), _MAX_ROWS)} 条："]

    for idx, row in enumerate(rows[:_MAX_ROWS], start=1):
        if not isinstance(row, dict):
            lines.append(f"{idx}. {row}")
            continue
        parts = []
        for field in keep_fields:
            value = row.get(field)
            if value is None:
                continue
            value_str = str(value)
            # 毫秒时间戳转可读时间，例如 nowAlarmTime=1725926400000 -> 10-10 08:00
            if field.endswith("Time") and value_str.isdigit():
                try:
                    value_str = datetime.fromtimestamp(int(value_str) / 1000).strftime("%m-%d %H:%M")
                except Exception:
                    pass
            # 单字段值过长时截断，避免异常长文本撑爆 prompt
            if len(value_str) > 50:
                value_str = value_str[:50] + "..."
            parts.append(f"{field}={value_str}")
        if parts:
            lines.append(f"{idx}. " + "，".join(parts))
        else:
            lines.append(f"{idx}. {str(row)[:100]}")

    if len(rows) > _MAX_ROWS:
        lines.append(f"... 其余 {len(rows) - _MAX_ROWS} 条已省略")

    return "\n".join(lines)


def _compact_fire_assets(raw_text: str) -> str:
    """
    压缩消防设备台账 rows

    台账行内字段（assetsName/offLineFlag/faultFlag/usageFlag/hiddenDangerFlag/
    deviceCode 等）之外，压力/液位/电量/倾角在 dataMap 数组里
    （每项 {monitorName, monitorValue, unit}），这里展平成
    “monitorName=monitorValueunit” 的形式，例如 压力=0.45MPa。
    """
    # 兼容字符串 JSON 和已解析的 dict/list
    try:
        data = json.loads(raw_text)
    except Exception:
        return raw_text

    if not isinstance(data, dict):
        return raw_text

    rows = data.get("rows")
    if not isinstance(rows, list):
        return raw_text

    total = data.get("total", len(rows))
    lines = [f"共 {total} 条，以下展示前 {min(len(rows), _MAX_ROWS)} 条："]

    for idx, row in enumerate(rows[:_MAX_ROWS], start=1):
        if not isinstance(row, dict):
            lines.append(f"{idx}. {row}")
            continue
        parts = [
            f"{f}={row[f]}"
            for f in _ASSET_KEEP_FIELDS
            if row.get(f) not in (None, "")
        ]
        # 展平 dataMap：压力=0.45MPa，液位=1.2m，电量=85%，倾角=1.2°
        for m in row.get("dataMap") or []:
            if isinstance(m, dict) and m.get("monitorName"):
                parts.append(
                    f"{m['monitorName']}={m.get('monitorValue', '')}{m.get('unit') or ''}"
                )
        lines.append(f"{idx}. " + ("，".join(parts) if parts else str(row)[:100]))

    if len(rows) > _MAX_ROWS:
        lines.append(f"... 其余 {len(rows) - _MAX_ROWS} 条已省略")

    return "\n".join(lines)


def _compact_fire_alarm_list(raw_text: str) -> str:
    """压缩实时消防告警 rows（仿 canteen 的 _compact_week_menu 思路）"""
    return _compact_fire_rows(raw_text, [
        "title", "alarmTypeName", "assetsName", "areaName",
        "alarmLevel", "handleStatus", "nowAlarmTime", "alarmReason",
    ])


async def _llm_format_fire_result(
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
            "fire_assets": "列出消防设备的状态、压力液位、电量、倾角等关键信息；",
            "fire_alarm_num": "列出火警数、故障数、隐患数、误报数、离人数、设备状态告警数等各类告警统计；",
            "fire_alarm_list": "按时间列出最近的消防告警（类型/等级/位置/时间）；",
            "month_repair": "按月份列出报修数量趋势；",
        }.get(query_type, "把返回数据整理清楚；")

        # 省略式追问（如「昨天呢」）时 original_query 仍是上一轮原话，其中的时间词
        # 不代表本次查询；把 slots 里真实查询时间显式交给 LLM，避免回答被原话带偏
        _qt = format_query_time(state.get("slots", {}).get("date"))
        query_time_line = (
            f"本次查询的时间范围：{_qt}（回答中的时间表述以此为准，不要沿用原话里的时间词）\n"
            if _qt else ""
        )

        prompt = (
            "你是智慧园区消防态势助手。下面是一次 MCP 工具查询的原始返回，"
            "请根据用户的查询类型，只提取对应的内容，用中文自然、简洁地回答用户。\n\n"
            f"用户查询类型：{query_type}（{label}）\n"
            f"{query_time_line}"
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
        logger.warning(f"LLM 格式化消防态势结果失败，降级为原样返回: {e}")

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


class EmergencyFireGraphState(TypedDict):
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
def init_state(state: EmergencyFireGraphState) -> dict:
    """
    初始化节点：确保 slots 中日期结构存在
    """
    slots = state.get("slots", {})
    if "date" not in slots or not slots.get("date"):
        slots["date"] = {}
    return {"slots": slots}


def check_missing_params(state: EmergencyFireGraphState) -> dict:
    """
    检查缺失参数
    消防后端只支持 4 类查询，query_type 落到 count 兜底
    （正则/embedding 分类器都没识别出子类型）时视为缺失，走追问
    """
    slots = state["slots"]
    missing = []

    query_type = slots.get("query_type")
    if not query_type or query_type == "count":
        missing.append("query_type")

    return {"missing_params": missing}


def route_missing_params(state: EmergencyFireGraphState) -> str:
    """
    条件路由：根据是否缺失参数决定下一步
    """
    if state["missing_params"]:
        if state["ask_count"] >= 3:
            return "give_up"
        return "ask_param"
    return "check_date"


def ask_param(state: EmergencyFireGraphState) -> dict:
    """
    参数缺失时生成追问问题
    """
    missing = state["missing_params"]

    updates = {}

    if "query_type" in missing:
        # 选项措辞与 slots.py 正则关键词对齐，
        # 用户直接回复选项词即可被识别
        question = "请问您想查询哪类消防数据？设备台账、告警统计、实时告警，还是月度报修？"
        # 存下选项清单与等待标记，供追问轮解析「第N个」这类序数指代
        # （仿 vague_date 的 _pending_date_clarify / _date_options 模式）
        slots = state["slots"]
        slots["_pending_type_clarify"] = True
        slots["_type_options"] = list(_TYPE_OPTIONS)
        updates["slots"] = slots
    elif "date" in missing:
        question = "请问您想查询哪一天的消防数据？"
    else:
        question = "请问您还需要补充什么信息？"

    ask_count = state["ask_count"] + 1

    updates.update({
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
    })
    return updates


def give_up(state: EmergencyFireGraphState) -> dict:
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


def check_date(state: EmergencyFireGraphState) -> dict:
    """
    日期检查占位节点，实际路由由条件边处理
    """
    return {}


def route_date_type(state: EmergencyFireGraphState) -> str:
    """
    根据日期类型路由
    消防态势未来日期直接拒绝
    """
    date_slots = state["slots"].get("date", {})
    time_type = date_slots.get("time_type")

    if time_type == "future":
        return "future_date"
    if time_type == "vague":
        return "vague_date"
    return "call_tool"


def future_date(state: EmergencyFireGraphState) -> dict:
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
                    "content": "消防数据目前只能查询今天及历史的统计，未来的数据暂时无法预测哦。",
                },
            },
            {"event": "custom", "data": {"type": "done"}},
        ],
    }


def vague_date(state: EmergencyFireGraphState) -> dict:
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
        f"您想查询近几天的消防数据？可以直接回复具体天数，例如“近两天”、“近四天”，"
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
    async def call_tool(state: EmergencyFireGraphState) -> dict:
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

        # 当前 Java 侧消防态势工具均为无参接口，直接空参调用
        tool_args = {}

        print(f"[MCP CALL] tool={java_tool_name}, args={tool_args}")

        try:
            result = await tool.ainvoke(tool_args)
            print(f"[MCP RAW RESULT] query_type={query_type}, result={result}")

            raw_text = _extract_text(result)

            # 台账 / 实时告警的 rows 列表原始 JSON 很大，
            # 先压成紧凑文本（只保留关键字段、限制条数）再喂 LLM，
            # 大幅减少 token、加快回复
            if query_type == "fire_assets":
                raw_text = _compact_fire_assets(raw_text)
            elif query_type == "fire_alarm_list":
                raw_text = _compact_fire_alarm_list(raw_text)

            answer_text = await _llm_format_fire_result(
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
            logger.error(f"调用消防态势工具失败: {e}\n{tb}")
            return {
                "error": f"查询失败: {str(e)}\n{tb}",
                "events": [{
                    "event": "custom",
                    "data": {"type": "error", "content": f"查询失败: {str(e)}\n{tb}"},
                }],
            }

    return call_tool


def finalize(state: EmergencyFireGraphState) -> dict:
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
def build_emergency_fire_graph(tools: dict, llm=None) -> StateGraph:
    """
    构建消防态势 LangGraph 状态图

    流程：
        init -> check_missing_params
            -> ask_param (参数缺失，追问) -> END
            -> give_up (追问超限) -> END
            -> check_date
                -> future_date (未来时间) -> END
                -> vague_date (模糊时间) -> END
                -> call_tool (调用 MCP，LLM 组织回答) -> finalize -> END

    :param tools: MCP 工具字典（工具名 -> 工具）
    :param llm: 可选的 LLM，用于根据 query_type 分析 MCP 返回生成回答；不传则原样返回
    """
    workflow = StateGraph(EmergencyFireGraphState)

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

    # 日期类型分支
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
async def load_emergency_fire_tools() -> dict:
    """
    从 MCP 配置加载消防态势工具，并以工具名为 key 返回
    类级缓存由调用方维护
    """
    java_tool_names = set(_JAVA_TOOL_MAP.values())

    try:
        tools = await asyncio.wait_for(
            get_mcp_tools("mcp_client/mcp_server_config.yaml"),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.error("加载 MCP 消防态势工具超时")
        return {}
    except Exception as e:
        logger.error(f"加载 MCP 消防态势工具失败: {e}", exc_info=True)
        return {}

    filtered = [t for t in tools if t.name in java_tool_names]
    if not filtered:
        logger.warning(f"未找到任何消防态势 MCP 工具，期望 {java_tool_names}")

    return {t.name: t for t in filtered}
