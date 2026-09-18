import asyncio
import logging
import traceback
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, END

from mcp_client.mcp_loader import get_mcp_tools
from agent.intent.slots import format_query_time

logger = logging.getLogger(__name__)


# ============================================================
# Python query_type 到 Java MCP 工具名的映射
# 与 Java 侧 OverviewMcpServerConfig 对齐（接口文档 §2.1 /compositive/overview）：
#   basic_info    -> overview:getBasicInfo
#   device_health -> overview:getDeviceHealth
# ============================================================
_JAVA_TOOL_MAP = {
    "basic_info": "overview:getBasicInfo",
    "device_health": "overview:getDeviceHealth",
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
        "basic_info": "园区基本信息（面积 / IoT设备总数）",
        "device_health": "设备健康度总览（健康分/在线率/维修率/寿命率）",
    }.get(query_type, "综合态势总览数据")


async def _llm_format_overview_result(
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
            "basic_info": "按用户原话只答对应的部分：问面积只答面积、问设备总数只答设备总数，"
                          "两者都问到或问得宽泛（如「基本信息/总览/概况」）才一并列出；",
            "device_health": "按设备类别列出健康分、在线率、维修率、寿命率；",
        }.get(query_type, "把返回数据整理清楚；")

        # 省略式追问（如「昨天呢」）时 original_query 仍是上一轮原话，其中的时间词
        # 不代表本次查询；把 slots 里真实查询时间显式交给 LLM，避免回答被原话带偏
        _qt = format_query_time(state.get("slots", {}).get("date"))
        query_time_line = (
            f"本次查询的时间范围：{_qt}（回答中的时间表述以此为准，不要沿用原话里的时间词）\n"
            if _qt else ""
        )

        prompt = (
            "你是智慧园区综合态势助手。下面是一次 MCP 工具查询的原始返回，"
            "请根据用户的查询类型，只提取对应的内容，用中文自然、简洁地回答用户。\n\n"
            f"用户查询类型：{query_type}（{label}）\n"
            f"{query_time_line}"
            f"用户原话：{original_query}\n"
            "MCP 工具返回的原始数据：\n"
            f"{raw_text}\n\n"
            "要求：\n"
            f"1. {requirement}\n"
            "2. 回答必须基于返回数据，不要编造数字；只回答用户原话问到的内容，"
            "返回数据里原话没问到的其他字段/指标不要罗列"
            "（原话问得宽泛如「总览/概况/统计」才完整列出）；\n"
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
        logger.warning(f"LLM 格式化综合态势总览结果失败，降级为原样返回: {e}")

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


class CompositiveOverviewGraphState(TypedDict):
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
def init_state(state: CompositiveOverviewGraphState) -> dict:
    """
    初始化节点：确保 slots 结构存在
    """
    slots = state.get("slots", {})
    if "date" not in slots or not slots.get("date"):
        slots["date"] = {}
    return {"slots": slots}


def check_missing_params(state: CompositiveOverviewGraphState) -> dict:
    """
    检查缺失参数
    综合态势总览两个子类型均无必填参数
    """
    slots = state["slots"]
    missing = []
    return {"missing_params": missing}


def route_missing_params(state: CompositiveOverviewGraphState) -> str:
    """
    条件路由：综合态势总览无强制必填参数，直接调用工具
    """
    return "call_tool"


def make_call_tool_node(tools: dict, llm=None):
    """
    构造调用 MCP 工具的节点
    """
    async def call_tool(state: CompositiveOverviewGraphState) -> dict:
        query_type = state["slots"].get("query_type") or "basic_info"
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

        # 总览两个子类型均无入参，空参调用
        tool_args = {}

        print(f"[MCP CALL] tool={java_tool_name}, args={tool_args}")

        try:
            result = await tool.ainvoke(tool_args)
            print(f"[MCP RAW RESULT] query_type={query_type}, result={result}")

            raw_text = _extract_text(result)
            answer_text = await _llm_format_overview_result(
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
            logger.error(f"调用综合态势总览工具失败: {e}\n{tb}")
            return {
                "error": f"查询失败: {str(e)}\n{tb}",
                "events": [{
                    "event": "custom",
                    "data": {"type": "error", "content": f"查询失败: {str(e)}\n{tb}"},
                }],
            }

    return call_tool


def finalize(state: CompositiveOverviewGraphState) -> dict:
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
def build_compositive_overview_graph(tools: dict, llm=None) -> StateGraph:
    """
    构建综合态势总览 LangGraph 状态图

    流程：
        init -> check_missing_params -> call_tool (调用 MCP，LLM 组织回答) -> finalize -> END

    :param tools: MCP 工具字典（工具名 -> 工具）
    :param llm: 可选的 LLM，用于根据 query_type 分析 MCP 返回生成回答；不传则原样返回
    """
    workflow = StateGraph(CompositiveOverviewGraphState)

    # 注册节点
    workflow.add_node("init", init_state)
    workflow.add_node("check_missing_params", check_missing_params)
    workflow.add_node("call_tool", make_call_tool_node(tools, llm))
    workflow.add_node("finalize", finalize)

    # 入口和顺序边
    workflow.set_entry_point("init")
    workflow.add_edge("init", "check_missing_params")

    # 综合态势总览无强制必填参数，直接路由到工具调用
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
async def load_compositive_overview_tools() -> dict:
    """
    从 MCP 配置加载综合态势总览工具，并以工具名为 key 返回
    类级缓存由调用方维护
    """
    java_tool_names = set(_JAVA_TOOL_MAP.values())

    try:
        tools = await asyncio.wait_for(
            get_mcp_tools("mcp_client/mcp_server_config.yaml"),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.error("加载 MCP 综合态势总览工具超时")
        return {}
    except Exception as e:
        logger.error(f"加载 MCP 综合态势总览工具失败: {e}", exc_info=True)
        return {}

    filtered = [t for t in tools if t.name in java_tool_names]
    if not filtered:
        logger.warning(f"未找到任何综合态势总览 MCP 工具，期望 {java_tool_names}")

    return {t.name: t for t in filtered}
