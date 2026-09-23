import asyncio
import json
import logging
import traceback
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, END

from mcp_client.mcp_loader import get_mcp_tools

# syncSource 码到中文名的权威映射（只用于拼 LLM 提示语，下发给 Java 的仍是码本身）
from agent.intent.slots import DEVICE_TYPE_DICT, format_query_time

logger = logging.getLogger(__name__)


# ============================================================
# Python query_type 到 Java MCP 工具名的映射
# 与 Java 侧 PlatformDeviceQueryMcp 对齐（/mcp/devicequery，工具名前缀 device_query:）：
#   device_list   -> device_query:listDevice
#   device_detail -> device_query:getDeviceDetail
#
# 口径：**资产库**（平台自有的 VISUAL_DEVICE_INFO 纳管资产），
# 与同一个端点上的厂商透传工具（device_query:getVendorDeviceList，走 /calldevice/*）
# 不是一回事：资产库的 status 是启用/停用，厂商的才是实时在线状态。
#
# 入参（都取自 Java 侧 @McpToolParam，下发前按工具声明的 schema 过滤）：
#   listDevice(syncSource 0门禁 1道闸 2梯控 3监控 4入侵报警 5广播 6水表 7电表,
#              deviceType 设备子类型、一般不传, status 启用状态 0停用 1启用 2维修 3报废,
#              name 名称模糊匹配, code 编号精确匹配, spaceId 空间ID,
#              maintained 是否已维护 0否 1是, pageCurrent, pageSize)
#   getDeviceDetail(id)   id 取自列表返回的 id 字段，不是设备编号 code
# 列表接口的筛选参数全是可选的（不传就是全部设备），地区/楼层只有 spaceId 一个口径，
# 所以 Python 侧的 area（"A栋3楼"这类位置名）不下发，改为拉回来按 spaceName 本地过滤。
# ============================================================
_JAVA_TOOL_MAP = {
    "device_list": "device_query:listDevice",
    "device_detail": "device_query:getDeviceDetail",
}

# 详情查询用来把"设备名称/编号"换成"设备内部 id"的工具
# （getDeviceDetail 只认 id，而用户报的是名称或编号，平台侧负责匹配）
_RESOLVE_TOOL = "device_query:listDevice"

# 详情反查：同名设备可能不止一台，多要几条再挑
_SEARCH_PAGE_SIZE = 20

# 用户按位置筛选时，位置名（"A栋3楼"）没法下发（后端只认 spaceId），
# 就多拉一些回来按 spaceName 本地过滤
_AREA_PAGE_SIZE = 200


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


def _iter_rows(data: Any) -> list:
    """
    从返回 JSON 里取出设备行列表

    资产库列表接口返回 {code, data:{page:{total,size,pages,current}, data:[设备数组]}}，
    这里顺着包装往下找，兼容 data/list/rows/records 等常见壳子
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]

    if isinstance(data, dict):
        for key in ("data", "list", "rows", "records"):
            if key in data:
                return _iter_rows(data[key])

    return []


def _rows_of(raw_text: str) -> list:
    """把工具返回的文本解析成设备行列表；解析不出来返回空列表"""
    try:
        data = json.loads(raw_text)
    except Exception:
        return []
    return _iter_rows(data)


def _pick_device(rows: list, keyword: str) -> tuple:
    """
    从列表返回里挑出用户要的那台设备

    编号/名称与关键字**完全相等**的那条最可信；没有完全相等的，只有一行时就用它；
    多行又都不相等时不敢猜，交回给用户指定。

    :return: (设备行, "") / (None, "not_found") / (None, "ambiguous")
    """
    if not rows:
        return None, "not_found"

    exact = [
        r for r in rows
        if str(r.get("code") or "").strip() == keyword
        or str(r.get("name") or "").strip() == keyword
    ]
    if len(exact) == 1:
        return exact[0], ""
    if len(rows) == 1:
        return rows[0], ""
    return None, "ambiguous"


def _format_candidates(rows: list, keyword: str) -> str:
    """匹配到多台设备时，把候选列出来让用户指定（不猜）"""
    lines = []
    for row in rows[:10]:
        name = row.get("name") or "未命名设备"
        code = row.get("code") or "无编号"
        where = row.get("spaceName") or ""
        tail = f"，{where}" if where else ""
        lines.append(f"- {name}（编号 {code}{tail}）")
    more = f"，另外还有 {len(rows) - 10} 台" if len(rows) > 10 else ""
    return (
        f"「{keyword}」匹配到 {len(rows)} 台设备{more}：\n"
        + "\n".join(lines)
        + "\n请用完整设备编号再指定一下要查哪一台。"
    )


def _tool_arg_names(tool: Any) -> set:
    """
    取工具声明的入参名集合（LangChain StructuredTool 的 .args）
    取不到时返回空集合，表示不做过滤
    """
    args = getattr(tool, "args", None)
    if isinstance(args, dict) and args:
        return set(args.keys())
    return set()


def _build_tool_args(tool: Any, candidates: dict) -> dict:
    """
    组装 MCP 工具入参：去掉空值；工具声明了入参 schema 时按 schema 过滤，
    避免把 Java 侧不认识的参数传过去导致调用失败
    """
    args = {k: v for k, v in candidates.items() if v not in (None, "")}

    declared = _tool_arg_names(tool)
    if declared:
        dropped = [k for k in args if k not in declared]
        if dropped:
            logger.info(f"工具 {getattr(tool, 'name', '?')} 不接受参数 {dropped}，已忽略")
        args = {k: v for k, v in args.items() if k in declared}

    return args


def _query_type_label(query_type: str) -> str:
    """根据 query_type 返回中文名称，用于生成 LLM 提示语"""
    return {
        "device_list": "设备列表查询",
        "device_detail": "设备详情查询",
    }.get(query_type, "设备查询")


def _device_type_label(device_type: str) -> str:
    """syncSource 码转中文名（别名词典的首个别名即标准名），未知码原样返回"""
    if not device_type:
        return "不限"
    return f"{DEVICE_TYPE_DICT.get(device_type, [device_type])[0]}（{device_type}）"


async def _llm_format_device_query_result(
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
        slots = state.get("slots", {})
        label = _query_type_label(query_type)
        original_query = state.get("original_query") or query_type
        keyword = slots.get("device_keyword") or ""

        requirement = {
            "device_list": "把查到的设备逐条列出来（设备名称、设备编号、类型、所在位置、启用状态）；",
            "device_detail": "只回答这一台设备的详情（名称、编号、类型、所在位置、启用状态、负责人等）；",
        }.get(query_type, "把返回数据整理清楚；")

        # 资产库返回里的枚举含义（LLM 不认识这些码，直接照抄会把"启用"说成"在线"）
        code_rule = (
            "syncSource：0门禁 1道闸 2梯控 3监控 4入侵报警 5广播 6水表 7电表；"
            "status 是**启用状态**（0停用 1启用 2维修 3报废），不是在线/离线，"
            "不要说成在线状态；maintained：0未维护 1已维护；"
            "spaceName 是设备所在位置的全路径名（如 园区/北门/门岗）"
        )

        # 位置筛选后端只认 spaceId，这里是按位置名在返回结果里本地过滤
        area_rule = ""
        if query_type == "device_list" and slots.get("area"):
            area_rule = (
                f"4. 只保留 spaceName 里包含「{slots['area']}」的设备，"
                "其余全部丢弃；一台都没匹配上就如实说这个位置没查到设备；\n"
            )

        # 用户报的是名称/编号时，列表口径下也再确认一遍关键字
        keyword_rule = ""
        if query_type == "device_list" and keyword:
            keyword_rule = (
                f"5. 只保留设备名称或设备编号里包含「{keyword}」的设备，"
                "其余全部丢弃；一台都没匹配上就如实说没找到；\n"
            )

        # 省略式追问（如「昨天呢」）时 original_query 仍是上一轮原话，其中的时间词
        # 不代表本次查询；把 slots 里真实查询时间显式交给 LLM，避免回答被原话带偏
        _qt = format_query_time(slots.get("date"))
        query_time_line = (
            f"本次查询的时间范围：{_qt}（回答中的时间表述以此为准，不要沿用原话里的时间词）\n"
            if _qt else ""
        )

        prompt = (
            "你是智慧园区设备台账助手。下面是一次 MCP 工具查询的原始返回，"
            "请根据用户的查询类型，只提取对应的内容，用中文自然、简洁地回答用户。\n\n"
            f"用户查询类型：{query_type}（{label}）\n"
            f"{query_time_line}"
            f"用户原话：{original_query}\n"
            f"设备筛选条件：设备类型={_device_type_label(slots.get('device_type'))}，"
            f"位置={slots.get('area') or '不限'}，名称/编号关键字={keyword or '不限'}\n"
            "MCP 工具返回的原始数据：\n"
            f"{raw_text}\n\n"
            "要求：\n"
            f"1. {requirement}\n"
            "2. 回答必须基于返回数据，不要编造设备或字段；\n"
            "3. 如果返回数据里没有用户要查的信息，只如实说明即可，不要建议查询其他内容；\n"
            f"{area_rule}"
            f"{keyword_rule}"
            "6. 不要向用户提出任何追问、提议或反问，只回答本次查询的结果；\n"
            "7. 回答要简短、口语化，设备多时可以用简短列表；\n"
            f"8. 字段含义（照此翻译枚举值，不要直接念数字、不要把编码当名称）：{code_rule}。"
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
        logger.warning(f"LLM 格式化设备查询结果失败，降级为原样返回: {e}")

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


class DeviceQueryGraphState(TypedDict):
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
def init_state(state: DeviceQueryGraphState) -> dict:
    """
    初始化节点：确保设备查询的 slots 结构存在
    """
    slots = state.get("slots", {})
    for key in ("device_type", "area", "device_keyword"):
        if key not in slots or slots.get(key) is None:
            slots[key] = ""
    if not slots.get("query_type"):
        slots["query_type"] = "device_list"
    return {"slots": slots}


def check_missing_params(state: DeviceQueryGraphState) -> dict:
    """
    检查缺失参数（与 DeviceQueryHandler._get_missing_params 保持一致）

    资产库列表接口的筛选参数全是可选的（不传 syncSource 就是全部类型），
    所以列表查询没有必填项；只有详情必须能定位到某一台设备——
    用户没报名称/编号时问一句，问不到就没法查。
    """
    slots = state["slots"]
    missing = []

    if slots.get("query_type") == "device_detail" and not slots.get("device_keyword"):
        missing.append("device_keyword")

    return {"missing_params": missing}


def route_missing_params(state: DeviceQueryGraphState) -> str:
    """
    条件路由：缺参数就追问，追问超过 3 次放弃，否则直接调用工具
    """
    if state["missing_params"]:
        if state["ask_count"] >= 3:
            return "give_up"
        return "ask_param"
    return "call_tool"


def ask_param(state: DeviceQueryGraphState) -> dict:
    """
    参数缺失时生成追问问题（与 DeviceQueryHandler.generate_question 保持一致）
    """
    missing = state["missing_params"]

    # 只有详情会缺参数：缺的是"查哪一台设备"
    if "device_keyword" in missing:
        question = "请问您想查看哪台设备的详情？可以说设备名称或设备编号。"
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


def give_up(state: DeviceQueryGraphState) -> dict:
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

    query_type == device_list：
        直接调 device_query:listDevice（带设备类型筛选）；
        用户报了名称/编号时交给后端按名称模糊搜（搜不到再按编号精确搜），
        位置筛选（area）后端只认 spaceId，改为多拉一些回来按 spaceName 本地过滤
    query_type == device_detail：
        先用 device_query:listDevice 按名称/编号把设备搜出来，取它的 id，
        再调 device_query:getDeviceDetail；搜不到就如实说没找到，
        匹配到多台又都不完全相等时不猜，把候选列给用户
    """
    async def _call(tool_name: str, args: dict) -> str:
        """调用一个 MCP 工具并返回文本结果"""
        tool = tools.get(tool_name)
        if not tool:
            raise RuntimeError(f"{tool_name} 工具未加载")

        tool_args = _build_tool_args(tool, args)
        print(f"[MCP CALL] tool={tool_name}, args={tool_args}")

        result = await tool.ainvoke(tool_args)
        print(f"[MCP RAW RESULT] tool={tool_name}, result={result}")
        return _extract_text(result)

    def _answer(content: str) -> dict:
        return {
            "answer": content,
            "events": [{
                "event": "custom",
                "data": {"type": "answer", "content": content},
            }],
        }

    async def _search(keyword: str, extra: dict) -> tuple:
        """
        按名称/编号搜设备（详情定位、列表按关键字筛都用它）

        平台侧 name 是模糊匹配、code 是精确匹配：先按名称搜，
        名称搜不到（用户报的是编号）再按编号搜一次。

        :return: (原始返回文本, 解析出的设备行列表)
        """
        raw_text = await _call(
            _RESOLVE_TOOL,
            {"name": keyword, "pageSize": _SEARCH_PAGE_SIZE, **extra},
        )
        rows = _rows_of(raw_text)
        if not rows:
            raw_text = await _call(
                _RESOLVE_TOOL,
                {"code": keyword, "pageSize": _SEARCH_PAGE_SIZE, **extra},
            )
            rows = _rows_of(raw_text)
        return raw_text, rows

    async def call_tool(state: DeviceQueryGraphState) -> dict:
        slots = state["slots"]
        query_type = slots.get("query_type") or "device_list"
        device_type = slots.get("device_type") or ""
        area = slots.get("area") or ""
        keyword = slots.get("device_keyword") or ""

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

        try:
            # ---------------- 设备列表 ----------------
            if query_type == "device_list":
                if keyword:
                    # 按名称/编号找设备：让平台侧匹配，比本地过滤准
                    raw_text, rows = await _search(keyword, {"syncSource": device_type})
                    if not rows:
                        return _answer(
                            f"没有找到名称或编号为「{keyword}」的设备，请确认一下名称或编号。"
                        )
                else:
                    args = {"syncSource": device_type}
                    if area:
                        # 位置名下发不了（只认 spaceId），多拉一些回来按 spaceName 本地过滤
                        args["pageSize"] = _AREA_PAGE_SIZE
                    raw_text = await _call(java_tool_name, args)

                answer_text = await _llm_format_device_query_result(
                    llm, query_type, state, raw_text
                )
                return _answer(answer_text)

            # ---------------- 设备详情 ----------------
            # 1. 先把设备搜出来（详情接口只认内部 id，用户报的是名称或编号）
            _, rows = await _search(keyword, {})
            row, reason = _pick_device(rows, keyword)

            if reason == "not_found":
                logger.info(f"设备列表中没搜到「{keyword}」")
                return _answer(
                    f"没有找到名称或编号为「{keyword}」的设备，请确认一下名称或编号。"
                )
            if reason == "ambiguous":
                logger.info(f"「{keyword}」匹配到 {len(rows)} 台设备，列出候选让用户指定")
                return _answer(_format_candidates(rows, keyword))

            device_id = row.get("id")
            if device_id in (None, ""):
                logger.warning(f"设备「{keyword}」的返回里没有 id 字段: {row}")
                return _answer("查到这台设备了，但返回数据里没有设备 id，暂时取不到详情。")

            # 2. 拿内部 id 调详情接口
            raw_text = await _call(java_tool_name, {"id": str(device_id)})
            answer_text = await _llm_format_device_query_result(
                llm, query_type, state, raw_text
            )
            return _answer(answer_text)

        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"调用设备查询工具失败: {e}\n{tb}")
            return {
                "error": f"查询失败: {str(e)}\n{tb}",
                "events": [{
                    "event": "custom",
                    "data": {"type": "error", "content": f"查询失败: {str(e)}\n{tb}"},
                }],
            }

    return call_tool


def finalize(state: DeviceQueryGraphState) -> dict:
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
def build_device_query_graph(tools: dict, llm=None) -> StateGraph:
    """
    构建设备查询 LangGraph 状态图

    流程：
        init -> check_missing_params ->┬-> call_tool (调用 MCP，LLM 组织回答) -> finalize -> END
                                       ├-> ask_param (详情缺设备名称/编号，追问后等用户回复) -> END
                                       └-> give_up (追问超过 3 次) -> END

    必填参数：详情需要设备名称或编号（列表接口的筛选参数全是可选的）

    :param tools: MCP 工具字典（工具名 -> 工具）
    :param llm: 可选的 LLM，用于按 query_type 分析 MCP 返回生成回答；不传则原样返回
    """
    workflow = StateGraph(DeviceQueryGraphState)

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
async def load_device_query_tools() -> dict:
    """
    从 MCP 配置加载设备查询工具，并以工具名为 key 返回
    （详情查询要先拿列表把名称/编号换算成内部 id，所以列表工具也要一起加载）
    类级缓存由调用方维护
    """
    java_tool_names = set(_JAVA_TOOL_MAP.values()) | {_RESOLVE_TOOL}

    try:
        tools = await asyncio.wait_for(
            get_mcp_tools("mcp_client/mcp_server_config.yaml"),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.error("加载 MCP 设备查询工具超时")
        return {}
    except Exception as e:
        logger.error(f"加载 MCP 设备查询工具失败: {e}", exc_info=True)
        return {}

    filtered = [t for t in tools if t.name in java_tool_names]
    if not filtered:
        logger.warning(f"未找到任何设备查询 MCP 工具，期望 {java_tool_names}")

    return {t.name: t for t in filtered}
