import json
import logging
import re
import traceback
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import StateGraph, END

from agent.intent.slots import _parse_chinese_number

logger = logging.getLogger(__name__)

'''
UE 场景控制 LangGraph 管线（MCP sidecar /mcp/scene）。

链路：
    用户指令 → 拉取场景状态(get_scene_state) → LLM 从 20 个工具里选一个并给参数
             → 注入 X-Session-Id 调 tools/call → 把 sidecar 返回整理成回答

设计要点（与 docs 里《UE 场景 MCP 服务说明》对齐）：
    1. X-Session-Id 由 app.py 从聊天入口透传进 state，**绝不由 LLM 生成**；
    2. sidecar 成功返回的文本里带"回复用户时不要说已完成"这类内部提示，
       发送给用户前必须清掉；
    3. dispatched ≠ 场景已改变（UE 不回执），回答只能表述为"已下发"；
    4. 缺必填参数时走 ask 事件追问，追问轮由 app.py 把上一轮原话拼进来重规划。
'''

# 工具名 -> 中文名（追问文案/日志用；未知工具回退工具名）
_TOOL_LABELS = {
    "scene_switch_floor": "切换楼层",
    "scene_reset_view": "重置视角",
    "scene_recover_view": "恢复视角",
    "scene_close_detail": "关闭详情",
    "scene_reset_all": "全部重置",
    "scene_show_visitor_track": "访客轨迹",
    "scene_show_camera": "监控画面",
    "scene_show_inspection": "巡检展示",
    "scene_show_meeting_rooms": "会议室展示",
    "scene_show_dining": "食堂展示",
    "scene_show_area_overview": "防区总览",
    "scene_show_device_category": "设备类目点位",
    "scene_show_fusion": "虚实融合",
    "scene_focus_actor": "聚焦对象",
    "scene_set_actor_visible": "对象显隐",
    "scene_set_poi_visible": "POI 显隐",
    "scene_set_env": "环境效果",
    "scene_set_area_state": "防区状态",
    "scene_set_inspection_state": "巡检任务控制",
    "get_scene_state": "场景状态",
}

# 必填参数中文名（追问文案用）
_ARG_LABELS = {
    "floor": "楼层",
    "tag": "点位/对象标识",
    "category": "设备类目",
    "actorType": "对象大类",
    "type": "对象子类",
    "visible": "显示还是隐藏",
    "state": "状态",
    "build": "楼栋",
    "floorNum": "楼层",
    "env_time": "时间",
    "env_weather": "天气",
}

# 破坏性动作：即使 sidecar 已放行，回答里也要提示"改变了现场状态"
_DESTRUCTIVE_TOOLS = {"scene_set_area_state", "scene_set_inspection_state"}

# scene_set_env.env_weather 的合法枚举（运行时优先取 sidecar tools/list 里的 enum，
# 这里只是兜底，避免 LLM/规则把"雨""雪""晴"这类口语值直接传给 sidecar 被拒）
_ENV_WEATHER_VALUES = (
    "晴天", "多云", "阴天", "小雨", "中雨", "大雨", "暴雨",
    "雷阵雨", "雾", "小雪", "中雪", "大雪", "暴雪", "雨夹雪",
)

# 口语说法 → 枚举值；复合词必须排在前面，否则"雨夹雪"会被"雨"截胡
_ENV_WEATHER_ALIASES = (
    ("雨夹雪", "雨夹雪"),
    ("雷阵雨", "雷阵雨"),
    ("雷雨", "雷阵雨"),
    ("打雷", "雷阵雨"),
    ("暴雨", "暴雨"),
    ("大雨", "大雨"),
    ("中雨", "中雨"),
    ("小雨", "小雨"),
    ("阵雨", "小雨"),
    ("雨", "小雨"),
    ("暴雪", "暴雪"),
    ("大雪", "大雪"),
    ("中雪", "中雪"),
    ("小雪", "小雪"),
    ("雪", "小雪"),
    ("多云", "多云"),
    ("阴天", "阴天"),
    ("阴", "阴天"),
    ("雾", "雾"),
    ("晴天", "晴天"),
    ("放晴", "晴天"),
    ("晴", "晴天"),
)

# 时间口语说法 → 枚举值
_ENV_TIME_ALIASES = (
    ("傍晚", "傍晚"),
    ("黄昏", "傍晚"),
    ("晚上", "夜晚"),
    ("夜里", "夜晚"),
    ("夜晚", "夜晚"),
    ("天黑", "夜晚"),
    ("深夜", "夜晚"),
    ("白天", "白天"),
    ("早上", "白天"),
    ("上午", "白天"),
    ("中午", "白天"),
)

# sidecar 枚举里没有目标值时，同义组内的候选顺序（夜晚→傍晚 是兼容旧口径）
_ENV_TIME_FALLBACK = {
    "白天": ("白天", "早上", "上午", "中午"),
    "傍晚": ("傍晚", "黄昏"),
    "夜晚": ("夜晚", "晚上", "夜里", "天黑", "深夜", "傍晚"),
}


# ============================================================
# 通用工具函数
# ============================================================
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


def _resp_text(resp: Any) -> str:
    """兼容 str / AIMessage.content（str 或 block 列表）"""
    content = getattr(resp, "content", resp)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return str(content)


def _parse_llm_json(text: str) -> Optional[dict]:
    """从 LLM 输出里抠出第一个 JSON 对象；解析失败返回 None"""
    if not text:
        return None
    text = text.strip()
    # 去掉 ```json ... ``` 代码围栏
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _clean_scene_text(raw: str) -> str:
    """清掉 sidecar 返回文本里给模型看的内部提示，避免直接透给用户"""
    if not raw:
        return ""
    text = str(raw)
    # 动作成功时附带的"回复用户时不要说已完成，只能说已下发"
    text = re.sub(r"回复用户时不要说[^\n。]*。(?:只能说[^\n。]*。)?", "", text)
    # 动作失败时附带的"这个动作没有下发成功，不要告诉用户已经执行了"
    text = re.sub(r"（这个动作没有下发成功[^）]*）", "", text)
    # 压缩多余空行
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines).strip()


def _format_catalog(tools: list) -> tuple:
    """把 MCP tools/list 结果整理成提示词里的工具清单；返回 (catalog_text, tool_map)"""
    tool_map = {}
    lines = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name:
            continue
        tool_map[name] = tool
        desc = (tool.get("description") or "").strip().replace("\n", " ")
        schema = tool.get("inputSchema") or {}
        if not isinstance(schema, dict):
            schema = {}
        props = schema.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        required = set(schema.get("required") or [])
        params = []
        for pname, pschema in props.items():
            ptype = pschema.get("type", "string") if isinstance(pschema, dict) else "string"
            req = "必填" if pname in required else "可选"
            pdesc = (pschema.get("description") or "").strip().replace("\n", " ") \
                if isinstance(pschema, dict) else ""
            enum = pschema.get("enum") if isinstance(pschema, dict) else None
            extra = f" 取值:{'/'.join(map(str, enum))}" if enum else ""
            params.append(f"{pname}({ptype},{req}){(' ' + pdesc) if pdesc else ''}{extra}")
        param_text = "；".join(params) if params else "无参数"
        lines.append(f"- {name}: {desc}\n  参数: {param_text}")
    return "\n".join(lines), tool_map


def _required_args(tool: dict) -> list:
    schema = tool.get("inputSchema") or {}
    if not isinstance(schema, dict):
        return []
    return [k for k in (schema.get("required") or []) if k]


def _coerce_args(tool: dict, arguments: dict) -> dict:
    """按 schema 做轻量类型转换（floor/floorNum→int，visible/focus→bool）"""
    schema = tool.get("inputSchema") or {}
    if not isinstance(schema, dict):
        schema = {}
    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    out = {}
    for key, value in (arguments or {}).items():
        if value is None:
            continue
        pschema = props.get(key) or {}
        ptype = pschema.get("type")
        if ptype == "integer":
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                # 兼容 LLM 给 "3楼"/"B1" 这类字符串
                text = str(value).strip()
                if text.upper().startswith("B"):
                    out[key] = -1
                else:
                    m = re.search(r"\d+", text)
                    out[key] = int(m.group(0)) if m else value
        elif ptype == "boolean":
            if isinstance(value, str):
                out[key] = value.strip().lower() in ("true", "1", "是", "显示", "打开")
            else:
                out[key] = bool(value)
        else:
            out[key] = value
    return out


def _allowed_enum(tool: dict, field: str) -> tuple:
    """取 schema 里某字段的 enum；sidecar 没声明时返回空元组"""
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    pschema = props.get(field) if isinstance(props, dict) else None
    enum = pschema.get("enum") if isinstance(pschema, dict) else None
    return tuple(str(v) for v in enum) if isinstance(enum, list) else ()


def _match_env_weather(text: str, allowed: Optional[tuple] = None) -> Optional[str]:
    """把话术/模型参数收敛到 env_weather 枚举；无法判断时返回 None"""
    q = str(text or "").strip()
    if not q:
        return None
    allowed = tuple(allowed or _ENV_WEATHER_VALUES)
    # 1) 本身就是枚举值
    if q in allowed:
        return q
    # 2) 话术里直接出现枚举词（雨夹雪/雷阵雨 等复合词优先）
    for value in allowed:
        if value in q:
            return value
    # 3) 口语回落：下雨→小雨、下雪→小雪、放晴→晴天
    for keyword, value in _ENV_WEATHER_ALIASES:
        if keyword in q and value in allowed:
            return value
    return None


def _match_env_time(text: str, allowed: tuple) -> Optional[str]:
    """把时间口语收敛到 env_time 枚举；allowed 为空时原样返回"""
    q = str(text or "").strip()
    if not q:
        return None
    allowed = tuple(allowed or ())
    if not allowed:
        return q
    if q in allowed:
        return q
    for keyword, target in _ENV_TIME_ALIASES:
        if keyword not in q:
            continue
        for item in _ENV_TIME_FALLBACK.get(target, (target,)):
            if item in allowed:
                return item
        return None
    return None


def _normalize_env_args(tool: dict, arguments: dict) -> dict:
    """scene_set_env 的两个参数是枚举，统一收敛；无法识别的值直接丢弃"""
    arguments = dict(arguments or {})
    allowed_weather = _allowed_enum(tool, "env_weather") or _ENV_WEATHER_VALUES
    if arguments.get("env_weather") not in (None, ""):
        normalized = _match_env_weather(arguments["env_weather"], allowed_weather)
        if normalized:
            arguments["env_weather"] = normalized
        else:
            logger.warning(
                f"env_weather 不在枚举内，已丢弃: {arguments.get('env_weather')!r}，"
                f"可选: {allowed_weather}"
            )
            arguments.pop("env_weather", None)

    allowed_time = _allowed_enum(tool, "env_time")
    if allowed_time and arguments.get("env_time") not in (None, ""):
        normalized = _match_env_time(arguments["env_time"], allowed_time)
        if normalized:
            arguments["env_time"] = normalized
        else:
            logger.warning(
                f"env_time 不在枚举内，已丢弃: {arguments.get('env_time')!r}，"
                f"可选: {allowed_time}"
            )
            arguments.pop("env_time", None)
    return arguments


def _missing_required_args(tool_name: str, tool: dict, arguments: dict) -> list:
    """检查 schema 必填参数；scene_set_poi_visible 隐藏全部时忽略其余参数"""
    if tool_name == "scene_set_poi_visible" and arguments.get("visible") is False:
        return []
    return [k for k in _required_args(tool) if arguments.get(k) in (None, "")]


# ============================================================
# 规则兜底（LLM 不可用/返回异常时使用）
# ============================================================
def _extract_floor(query: str) -> Optional[int]:
    q = query
    if re.search(r"整栋|全楼|整座|整幢", q):
        return 99
    if re.search(r"(?:B|负)\s*1|地下\s*(?:一|1)\s*层|地下一层", q, re.I):
        return -1
    m = re.search(r"(?:第)?([0-9]+|[一二两三四五六七八九十]+)\s*(?:楼|层|F|f)", q)
    if not m:
        return None
    raw = m.group(1)
    try:
        return int(raw)
    except ValueError:
        return _parse_chinese_number(raw)


def _extract_tag(query: str) -> Optional[str]:
    """从话术里粗略抠对象标识：优先英文设备编码，其次「防区/编号/tag」后的词"""
    m = re.search(r"[A-Za-z][A-Za-z0-9_\-]{2,}", query)
    if m:
        return m.group(0)
    m = re.search(r"(?:tag|标识|编号)[:：]?\s*([^\s，,。；;]+)", query, re.I)
    if m:
        return m.group(1)
    m = re.search(r"(防区[^\s，,。；;]{0,20})", query)
    if m:
        return m.group(1)
    return None


def _rule_based_plan(query: str, tool_map: dict) -> Optional[dict]:
    """LLM 不可用时的规则兜底；返回 None 表示没匹配上"""
    q = str(query or "")
    floor = _extract_floor(q)

    def _args(**kwargs):
        args = {k: v for k, v in kwargs.items() if v is not None}
        if floor is not None and "floor" not in args and "floorNum" not in args:
            args["floor"] = floor
        return args

    if re.search(r"第几层|当前.{0,3}楼层|现在.{0,4}(在哪|几层)|场景状态|在哪栋", q):
        return {"tool": "get_scene_state", "arguments": {}}
    if re.search(r"重置视角|复位|默认视角|初始视角|回到默认全景", q):
        return {"tool": "scene_reset_view", "arguments": {}}
    if re.search(r"恢复视角|退回|返回上一|上一个视角", q):
        return {"tool": "scene_recover_view", "arguments": {}}
    if re.search(r"关闭弹窗|关掉弹窗|退出详情|关闭详情", q):
        return {"tool": "scene_close_detail", "arguments": {}}
    if re.search(r"清场|全部关掉|都关掉|回到初始状态", q):
        return {"tool": "scene_reset_all", "arguments": {}}

    # 巡检任务控制（先于"看巡检"，否则「开始巡检」会被当成展示）
    if re.search(r"(开始|暂停|结束|停止|恢复).{0,2}巡检", q):
        tag = _extract_tag(q)
        if not tag:
            return {"need_clarify": True,
                    "question": "请告诉我巡检任务的标识（tag），例如“开始巡检 A-01”。"}
        state = "开始"
        if "暂停" in q:
            state = "暂停"
        elif "结束" in q or "停止" in q:
            state = "结束"
        return {"tool": "scene_set_inspection_state",
                "arguments": {"tag": tag, "state": state}}

    # 防区状态（破坏性）
    if re.search(r"(布防|撤防|设防)", q):
        tag = _extract_tag(q)
        if not tag:
            return {"need_clarify": True,
                    "question": "请告诉我防区标识（tag），例如“把 A-01 防区撤防”。"}
        args = {"tag": tag, "state": "撤防" if "撤防" in q else "布防"}
        return {"tool": "scene_set_area_state", "arguments": args}

    if re.search(r"虚实融合|融合", q):
        tag = _extract_tag(q)
        if not tag:
            return {"need_clarify": True,
                    "question": "请告诉我融合目标的 tag，例如“把 CY-HIK-JK-001 融合到场景里”。"}
        return {"tool": "scene_show_fusion", "arguments": {"tag": tag}}

    if re.search(r"(隐藏|关掉|关闭|清掉).{0,5}(点位|标记|POI)", q, re.I):
        return {"tool": "scene_set_poi_visible",
                "arguments": {"visible": False, "type": "", "build": "", "floorNum": floor or 1}}

    if re.search(r"(显示|打开).{0,8}(点位|摄像头|烟感|门禁)", q):
        category = "JIANKONG"
        if "烟感" in q:
            category = "YANGAN"
        elif "门禁" in q:
            category = "MENJIN"
        return {"tool": "scene_show_device_category",
                "arguments": _args(category=category)}

    if "访客" in q:
        return {"tool": "scene_show_visitor_track", "arguments": _args()}
    if re.search(r"监控|摄像头", q):
        tag = _extract_tag(q)
        if not tag:
            return {"need_clarify": True,
                    "question": "请告诉我摄像头点位的 tag，例如“看 CY-HIK-JK-001 的监控”。"}
        return {"tool": "scene_show_camera", "arguments": _args(tag=tag)}
    if "巡检" in q:
        return {"tool": "scene_show_inspection", "arguments": _args()}
    if "会议室" in q:
        return {"tool": "scene_show_meeting_rooms", "arguments": _args()}
    if "食堂" in q:
        return {"tool": "scene_show_dining", "arguments": _args()}
    if "防区" in q:
        return {"tool": "scene_show_area_overview", "arguments": _args()}
    if re.search(r"聚焦|对准|拉近|放大到", q):
        tag = _extract_tag(q)
        if not tag:
            return {"need_clarify": True,
                    "question": "请告诉我聚焦对象的标识（tag）。"}
        return {"tool": "scene_focus_actor",
                "arguments": {"actorType": "POI", "type": "JIANKONG", "tag": tag}}
    if re.search(
        r"天气|下雨|下雪|雨夹雪|雷阵雨|暴雨|大雨|中雨|阵雨|小雪|中雪|大雪|暴雪"
        r"|雾|晴|多云|阴天|傍晚|黄昏|晚上|夜里|夜晚|天黑|深夜|白天|环境",
        q,
    ):
        env_time = None
        # 只产出 sidecar 认可的枚举值，不能直接传"雨""雪""晴"
        env_weather = _match_env_weather(q)
        if re.search(r"傍晚|黄昏", q):
            env_time = "傍晚"
        elif re.search(r"晚上|夜里|夜晚|天黑|深夜", q):
            env_time = "夜晚"
        elif re.search(r"白天|早上|上午|中午", q):
            env_time = "白天"
        args = {k: v for k, v in (("env_time", env_time), ("env_weather", env_weather)) if v}
        if not args:
            return {"need_clarify": True,
                    "question": "请告诉我想要的环境效果，例如“改成傍晚”或“下雨”。"}
        return {"tool": "scene_set_env", "arguments": args}

    if floor is not None:
        return {"tool": "scene_switch_floor",
                "arguments": {"floor": floor, "focus": True}}

    return None


# ============================================================
# LangGraph 状态定义
# ============================================================
class SceneControlGraphState(TypedDict):
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
    # 场景页面会话 id（前端 WS hello 声明），逐请求注入 X-Session-Id
    scene_session_id: Optional[str]
    # get_scene_state 返回的快照文本，作为规划上下文
    scene_state_text: Optional[str]
    # LLM/规则产出的执行计划 {"tool":..., "arguments":...} 或 {"need_clarify":...}
    plan: Optional[dict]
    # 工具原始返回（调试用）
    tool_result: Optional[str]


# ============================================================
# 图构建
# ============================================================
def build_scene_control_graph(client: Any, llm: Any = None) -> StateGraph:
    """
    构建 UE 场景控制图。

    :param client: SceneMcpClient（需实现 list_tools / call_tool / get_scene_state）
    :param llm: ChatOpenAI 兼容模型；为 None 时走规则兜底
    """
    # tools/list 结果缓存（sidecar 启动后工具集稳定）
    catalog_cache = {"tools": None, "text": "", "map": {}}

    async def _load_catalog() -> tuple:
        if catalog_cache["tools"] is None:
            tools = await client.list_tools()
            text, tool_map = _format_catalog(tools)
            catalog_cache.update({"tools": tools, "text": text, "map": tool_map})
        return catalog_cache["text"], catalog_cache["map"]

    def init_state(state: SceneControlGraphState) -> dict:
        slots = state.get("slots") or {}
        return {
            "slots": slots,
            "ask_count": state.get("ask_count") or 0,
            "unrelated_count": state.get("unrelated_count") or 0,
            "last_question": state.get("last_question") or "",
            "original_query": state.get("original_query") or "",
            "done": bool(state.get("done")),
        }

    async def fetch_scene_state(state: SceneControlGraphState) -> dict:
        """读场景快照作为规划上下文；页面未连接/失败都不阻断主流程"""
        sid = state.get("scene_session_id")
        try:
            text = await client.get_scene_state(sid)
        except Exception as e:
            logger.warning(f"读取场景状态失败(忽略，继续规划): {e}")
            text = ""
        return {"scene_state_text": text}

    async def plan_action(state: SceneControlGraphState) -> dict:
        query = state.get("original_query") or ""
        scene_state_text = state.get("scene_state_text") or ""

        try:
            catalog_text, tool_map = await _load_catalog()
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"加载场景 MCP 工具失败: {e}\n{tb}")
            return {
                "error": str(e),
                "done": True,
                "events": [{
                    "event": "custom",
                    "data": {"type": "error",
                             "content": "场景服务暂时不可用，请确认 sidecar 已启动后重试。"},
                }],
            }

        plan = None
        if llm is not None and catalog_text:
            prompt = (
                "你是智慧园区 3D 场景控制助手。根据用户指令，从下面的工具清单里"
                "选择最合适的一个工具，并给出调用参数。\n\n"
                f"当前场景状态（可能为空）：\n{scene_state_text or '（未知）'}\n\n"
                f"用户指令：{query}\n\n"
                f"可用工具：\n{catalog_text}\n\n"
                "规则：\n"
                "1. 只能从工具清单里选，工具名必须逐字一致；\n"
                "2. 参数严格按 schema 的类型、必填要求和枚举取值；楼层语义：-1=B1，"
                "1~N=具体楼层，11/99=整栋楼；带“取值:”的字段必须原样使用其中的值，"
                "例如 env_weather 只能填“晴天/小雨/小雪”等枚举值，不能填“雨”“雪”“晴”；\n"
                "3. 不要编造 tag、设备编码；用户没给且无法从场景状态推断时，"
                "返回 need_clarify 追问；\n"
                "4. 需要连续执行多个动作时，只选用户本次最主要的一个；\n"
                "5. 只输出 JSON，不要解释、不要代码围栏。\n\n"
                "输出格式（二选一）：\n"
                '{"tool": "scene_switch_floor", "arguments": {"floor": 3, "focus": true}}\n'
                '{"need_clarify": true, "question": "请告诉我摄像头点位的 tag"}'
            )
            try:
                resp = await llm.ainvoke(prompt)
                plan = _parse_llm_json(_resp_text(resp))
                if plan is None:
                    logger.warning(f"场景规划 LLM 返回无法解析，降级规则: {_resp_text(resp)[:200]}")
            except Exception as e:
                logger.warning(f"场景规划 LLM 调用失败，降级规则: {e}")

        if not plan:
            plan = _rule_based_plan(query, tool_map)

        if not plan:
            return {
                "plan": {"need_clarify": True,
                         "question": "我没理解要执行哪个场景操作，可以说得具体些吗？"
                                     "例如“切到 3 楼”“看 A 栋的监控”“重置视角”。"},
            }

        if plan.get("need_clarify"):
            return {"plan": plan}

        tool_name = plan.get("tool")
        tool = tool_map.get(tool_name)
        if not tool:
            return {
                "plan": {"need_clarify": True,
                         "question": f"没有找到“{tool_name}”这个场景操作，请换个说法。"},
            }

        arguments = _coerce_args(tool, plan.get("arguments") or {})
        if tool_name == "scene_set_env":
            # 模型/规则可能给“雨”“晴”这类口语值，先收敛到枚举再下发
            arguments = _normalize_env_args(tool, arguments)
            if not arguments:
                allowed = _allowed_enum(tool, "env_weather") or _ENV_WEATHER_VALUES
                return {
                    "plan": {"need_clarify": True,
                             "question": "想要什么环境效果？天气可选："
                                         + " / ".join(allowed)},
                }
        missing = _missing_required_args(tool_name, tool, arguments)
        if missing:
            labels = "、".join(_ARG_LABELS.get(k, k) for k in missing)
            return {
                "plan": {"need_clarify": True,
                         "question": f"执行“{_TOOL_LABELS.get(tool_name, tool_name)}”"
                                     f"还缺少：{labels}。请补充一下。"},
            }

        return {"plan": {"tool": tool_name, "arguments": arguments}}

    def route_plan(state: SceneControlGraphState) -> str:
        if state.get("error"):
            return "finalize"
        plan = state.get("plan") or {}
        if plan.get("need_clarify"):
            if (state.get("ask_count") or 0) >= 3:
                return "give_up"
            return "ask_param"
        return "call_tool"

    def ask_param(state: SceneControlGraphState) -> dict:
        plan = state.get("plan") or {}
        question = plan.get("question") or "请补充一下场景操作需要的信息。"
        ask_count = (state.get("ask_count") or 0) + 1
        return {
            "last_question": question,
            "ask_count": ask_count,
            "done": False,
            "events": [{
                "event": "custom",
                "data": {"type": "ask", "question": question, "ask_count": ask_count},
            }],
        }

    def give_up(state: SceneControlGraphState) -> dict:
        return {
            "done": True,
            "events": [
                {"event": "custom",
                 "data": {"type": "answer",
                          "content": "追问次数过多，我先不继续了。请问您还有其他问题吗？"}},
                {"event": "custom", "data": {"type": "done"}},
            ],
        }

    async def call_tool(state: SceneControlGraphState) -> dict:
        plan = state.get("plan") or {}
        tool_name = plan.get("tool")
        arguments = plan.get("arguments") or {}
        sid = state.get("scene_session_id")

        print(f"[SCENE MCP CALL] tool={tool_name}, args={arguments}, x_session_id={sid}")
        try:
            raw_text = await client.call_tool(tool_name, arguments, sid)
            print(f"[SCENE MCP RAW RESULT] tool={tool_name}, result={raw_text}")

            answer_text = _clean_scene_text(raw_text)
            if not answer_text:
                # 协议层成功但没有文本：不能替服务端宣称"已下发"
                answer_text = f"动作 {tool_name} 已提交，场景服务未返回明细，请以页面实际显示为准。"

            # 破坏性动作额外提示（sidecar 放行时也提醒用户留意现场状态）
            if tool_name in _DESTRUCTIVE_TOOLS:
                answer_text += "\n该动作会改变现场状态，请以页面实际显示为准。"

            return {
                "tool_result": raw_text,
                "answer": answer_text,
                "events": [{
                    "event": "custom",
                    "data": {"type": "answer", "content": answer_text},
                }],
            }
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"调用场景工具失败: {e}\n{tb}")
            # sidecar 的失败文案里带"不要告诉用户已经执行了"这类内部提示，透传前清掉
            detail = _clean_scene_text(str(e)) or "场景服务返回异常"
            return {
                "error": str(e),
                "done": True,
                "events": [{
                    "event": "custom",
                    "data": {"type": "error",
                             "content": f"场景指令下发失败：{detail}"},
                }],
            }

    def finalize(state: SceneControlGraphState) -> dict:
        return {
            "done": True,
            "events": [{"event": "custom", "data": {"type": "done"}}],
        }

    workflow = StateGraph(SceneControlGraphState)
    workflow.add_node("init", init_state)
    workflow.add_node("fetch_scene_state", fetch_scene_state)
    workflow.add_node("plan_action", plan_action)
    workflow.add_node("ask_param", ask_param)
    workflow.add_node("give_up", give_up)
    workflow.add_node("call_tool", call_tool)
    workflow.add_node("finalize", finalize)

    workflow.set_entry_point("init")
    workflow.add_edge("init", "fetch_scene_state")
    workflow.add_edge("fetch_scene_state", "plan_action")
    workflow.add_conditional_edges(
        "plan_action",
        route_plan,
        {
            "ask_param": "ask_param",
            "give_up": "give_up",
            "call_tool": "call_tool",
            "finalize": "finalize",
        },
    )
    workflow.add_edge("ask_param", END)
    workflow.add_edge("give_up", END)
    workflow.add_edge("call_tool", "finalize")
    workflow.add_edge("finalize", END)

    return workflow.compile()
