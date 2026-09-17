import asyncio
import json
import logging
import re
import traceback
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, END

from mcp_client.mcp_loader import get_mcp_tools

logger = logging.getLogger(__name__)


# ============================================================
# Python query_type 到 Java MCP 工具名的映射
# 与 Java 侧 DeviceMcpServerConfig / PlatformDeviceMcp 对齐（/mcp/device）：
#   device_list   -> device:getDeviceList
#   device_detail -> device:getDeviceDetail
#
# 注意：设备查询是"台账口径"，与设备态势的 device:getEquipClass /
# device:getCategoryHealth 等统计口径工具不是一回事，不要混用。
#
# 入参名（deviceType / area / deviceCode）需与 Java 侧保持一致：
#   deviceType 必填，取值 jk 监控 / mj 门禁 / dz 道闸 / gb 广播 / xxfb 信息发布
#   area（区域/楼栋/楼层）为可选筛选；deviceCode 用于详情
# Java 侧如未实现这两个工具，调用时会走"工具未加载"分支提示用户。
# ============================================================
_JAVA_TOOL_MAP = {
    "device_list": "device:getDeviceList",
    "device_detail": "device:getDeviceDetail",
}

# 详情查询用来把"设备名称"匹配成"设备编码"的工具
# 用户约定：设备名称/编码的模糊搜索在本地做——
# 先全量拉列表，再用名称或编码匹配搜索串，不把关键字下发给 Java 工具
_RESOLVE_TOOL = "device:getDeviceList"

# 设备编码在返回行里可能出现的字段名
_CODE_FIELDS = (
    # 各厂商返回结构里的"设备/通道编码"字段（顺序即优先级，见 _find_device_code）
    #   jk   大华 channelCode / 海康 cameraIndexCode
    #   gb   ITC EndpointID
    #   mj   大华 deviceCode（道闸 dz 结构同 mj）
    #   xxfb 和信 code
    "channelCode", "cameraIndexCode",
    "EndpointID", "EndpointId", "endpointId", "endpointID",
    "deviceCode", "assetsCode", "equipmentCode", "deviceNo",
    "code", "id",
)


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
    从返回 JSON 里取出设备行列表（各厂商结构不同，这里做兼容）

      - jk   大华：data 直接是通道数组；海康：{total, list[]}
      - mj   大华：{totalRows, pageData[]}（道闸 dz 结构相同）
      - gb   ITC：{EndPointsArray:[...]}
      - xxfb 和信：data 直接是设备数组
      - 其它：{rows:[...]} / {records:[...]} / {data:{...}} 等常见包装
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]

    if isinstance(data, dict):
        for key in (
            "pageData", "EndPointsArray", "endPointsArray",
            "rows", "list", "records", "data", "content",
        ):
            if key in data:
                return _iter_rows(data[key])

    return []


def _normalize_text(text: str) -> str:
    """去掉分隔符并转小写，用于设备名称的模糊匹配（"A栋-枪机" ≈ "a栋枪机"）"""
    return re.sub(r"[\s\-_/]", "", text).lower()


def _looks_like_code(keyword: str) -> bool:
    """关键字是不是"设备编码"形态（如 MJ-003 / CAM102），是的话不用再反查列表"""
    return bool(re.fullmatch(r"[A-Za-z]{1,8}[-_]?\d{1,6}", keyword or ""))


def _find_device_code(raw_text: str, keyword: str) -> str:
    """
    在设备列表返回里按名称/编码模糊匹配，取出该设备的编码

    供详情查询使用：用户报的是设备名称时，先用列表接口反查设备编码，
    再拿编码调详情接口。

    :return: 匹配到的设备编码；匹配不到返回空字符串
    """
    if not keyword:
        return ""

    key = keyword.lower()
    loose_key = _normalize_text(keyword)

    # 1. 能当 JSON 解析：逐行匹配（先精确、再去分隔符模糊匹配），命中行里取编码字段
    try:
        data = json.loads(raw_text)
    except Exception:
        data = None

    if data is not None:
        rows = _iter_rows(data)
        for matcher in (
            lambda blob: key in blob,
            lambda blob: loose_key in _normalize_text(blob),
        ):
            for row in rows:
                blob = json.dumps(row, ensure_ascii=False).lower()
                if matcher(blob):
                    for field in _CODE_FIELDS:
                        value = row.get(field)
                        if value not in (None, ""):
                            return str(value)

    # 2. 文本兜底：直接找编码字段（只有唯一一个时才敢用）
    codes = re.findall(
        r'"(?:channelCode|cameraIndexCode|EndpointID|deviceCode|assetsCode|'
        r'equipmentCode|deviceNo|code)"\s*:\s*"([^"]+)"',
        raw_text,
    )
    if len(set(codes)) == 1:
        return codes[0]

    return ""


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


# deviceType 码到中文名（仅用于拼 LLM 提示语，下发给 Java 的仍是码本身）
_DEVICE_TYPE_LABELS = {
    "jk": "监控设备",
    "mj": "门禁设备",
    "dz": "道闸设备",
    "gb": "广播设备",
    "xxfb": "信息发布设备",
}


def _device_type_label(device_type: str) -> str:
    """deviceType 码转中文名，未知码原样返回"""
    if not device_type:
        return "不限"
    return f"{_DEVICE_TYPE_LABELS.get(device_type, device_type)}（{device_type}）"


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
            "device_list": "把查到的设备逐条列出来（设备名称、编码、类型、所在区域、状态）；",
            "device_detail": "只回答这一台设备的详情（名称、编码、类型、所在区域、状态、参数等）；",
        }.get(query_type, "把返回数据整理清楚；")

        # 各厂商返回里的枚举码含义（LLM 不认识这些数字，直接照抄会答错状态）
        code_rule = {
            "jk": "cameraType：1-枪机 2-球机 3-半球 5-本地采集；status：0-离线 1-在线",
            "mj": "设备在线情况看返回里的在线字段，取不到就只说设备名称/编码/IP",
            "dz": "同门禁：设备在线情况看返回里的在线字段",
            "gb": "只有终端编号/名称/IP，没有在线状态就不要编造状态",
            "xxfb": "online：0-离线 1-在线；status：0-未激活 1-占用 3-停用 4-异常",
        }.get(slots.get("device_type") or "", "")

        # 用户约定：名称/编码模糊搜索在本地做，返回列表后再按关键字过滤
        keyword_rule = ""
        if query_type == "device_list" and keyword:
            keyword_rule = (
                f"4. 只保留设备名称或设备编码里包含「{keyword}」的设备，"
                "其余全部丢弃；一台都没匹配上就如实说没找到；\n"
            )
        elif query_type == "device_detail" and keyword:
            keyword_rule = (
                f"4. 只回答与「{keyword}」对应的那台设备，不要列出其它设备；\n"
            )

        prompt = (
            "你是智慧园区设备台账助手。下面是一次 MCP 工具查询的原始返回，"
            "请根据用户的查询类型，只提取对应的内容，用中文自然、简洁地回答用户。\n\n"
            f"用户查询类型：{query_type}（{label}）\n"
            f"用户原话：{original_query}\n"
            f"设备筛选条件：设备类型={_device_type_label(slots.get('device_type'))}，"
            f"区域={slots.get('area') or '不限'}\n"
            "MCP 工具返回的原始数据：\n"
            f"{raw_text}\n\n"
            "要求：\n"
            f"1. {requirement}\n"
            "2. 回答必须基于返回数据，不要编造设备或字段；\n"
            "3. 如果返回数据里没有用户要查的信息，只如实说明即可，不要建议查询其他内容；\n"
            f"{keyword_rule}"
            "5. 不要向用户提出任何追问、提议或反问，只回答本次查询的结果；\n"
            "6. 回答要简短、口语化，设备多时可以用简短列表；\n"
            + (f"7. 字段含义（照此翻译枚举值，不要直接念数字）：{code_rule}。" if code_rule else "")
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
    deviceType 是后端必填参数，列表/详情都缺不得；
    详情查询另外还必须有设备名称或编码
    """
    slots = state["slots"]
    missing = []

    if not slots.get("device_type"):
        missing.append("device_type")

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

    # 按优先级生成问题：先定设备类型（后端必填），再定具体设备
    if "device_type" in missing:
        if state["slots"].get("query_type") == "device_detail":
            question = "请问您想查看哪类设备的详情？监控、门禁、道闸、广播，还是信息发布设备？"
        else:
            question = "请问您想查询哪类设备？监控、门禁、道闸、广播，还是信息发布设备？"
    elif "device_keyword" in missing:
        question = "请问您想查看哪台设备的详情？可以说设备名称或设备编码。"
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
        直接调 device:getDeviceList（带设备类型/区域筛选），
        设备名称/编码关键字由 LLM 在返回列表里本地匹配
    query_type == device_detail：
        先用 device:getDeviceList 把"设备名称"反查成"设备编码"，
        再调 device:getDeviceDetail；列表里匹配不到就如实说没找到
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
                raw_text = await _call(
                    java_tool_name,
                    {"deviceType": device_type, "area": area},
                )
                answer_text = await _llm_format_device_query_result(
                    llm, query_type, state, raw_text
                )
                return _answer(answer_text)

            # ---------------- 设备详情 ----------------
            # 1. 用户报的是名称时，先用列表接口反查出设备编码
            #    （报的已经是编码形态就跳过反查，省一次 MCP 调用）
            code = keyword if _looks_like_code(keyword) else ""
            if not code:
                try:
                    list_text = await _call(
                        _RESOLVE_TOOL,
                        {"deviceType": device_type, "area": area},
                    )
                    code = _find_device_code(list_text, keyword)
                except Exception as e:
                    # 列表反查失败不致命：退化成把关键字直接交给详情接口
                    logger.warning(f"设备列表反查设备编码失败，直接用关键字查详情: {e}")

            if not code:
                logger.info(f"设备列表中未匹配到「{keyword}」，改用关键字直接查详情")

            # 2. 拿设备编码调详情接口
            raw_text = await _call(
                java_tool_name,
                {"deviceCode": code or keyword, "deviceType": device_type, "area": area},
            )
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
                                       ├-> ask_param (缺设备类型/编码，追问后等用户回复) -> END
                                       └-> give_up (追问超过 3 次) -> END

    必填参数：deviceType（jk/mj/dz/gb/xxfb）；详情另需设备名称或编码

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
    （详情查询要先反查编码，所以列表工具也要一起加载）
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
