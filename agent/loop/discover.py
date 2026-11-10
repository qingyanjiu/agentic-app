"""MCP 工具的动态发现（试点）。

背景：loop 的 14 个工具全部手写，而 MCP 侧实际有 87 个工具。
手写的原因是**参数口径**——Java 接口要 startTime / spaceId / syncSource，
模型编不出来，所以要把"用户原话 → 接口参数"的解析下沉到工具内部。

但有些工具不需要这层：必填参数为空或只有简单枚举，返回体也不大。
这类没必要手写，直接暴露给模型，让它看到工具描述自己决定怎么调，
返回的 JSON 交给模型组织成自然语言。

策略（试点阶段刻意保守）：
1. 只在白名单 server 上发现（默认 canteen / overview / inspection，共 9 个工具）；
2. 跳过必填参数带时间 / id / 编码 / 空间类的工具——模型填不出来，
   暴露出去只会让它瞎编参数；
3. 跳过已经被显式域用到的 MCP 工具，避免同一能力出现两个入口（模型会选错）；
4. 生成的工具 passthrough=False：返回值是 MCP 原始 JSON，
   必须由模型转成自然语言，不能直接甩给用户；
5. MCP 不可达时静默降级——只用显式工具，行为与改造前完全一致。

为什么不用启动时同步发现 + 直接塞进 registry：
system prompt 里的工具概览是 LoopAgent 构造时固化的，晚到的工具进不去。
所以这里做成「启动预热 → 缓存 → build_registry() 同步读取」：
只要在第一次建 pipeline 之前预热完成，prompt 里就有新工具。
"""
from __future__ import annotations

import logging
import re

from .govern import govern
from .registry import DomainSpec
from .runtime import McpToolbox
from .types import ToolOutcome, ToolSpec

logger = logging.getLogger(__name__)

# 试点 server。
# 实测（2026-11-09）：87 个 MCP 工具里 64 个已被显式域用满——local/security/
# canteen/vehicle/information/energy/meeting/fire/perimeter/device/overview/
# inspection 的空缺要么是 0，要么参数复杂不可直通。
# 唯一有大片空缺的是 devicequery（24 个工具只用了 2 个），剩下 22 个里 20 个
# 是统计类接口（countByDeviceType / getCameraOnlinePercentage / getDistribution…），
# required 为空或枚举，正好补上「设备在线率」「设备分布」这类现在答不了的问法。
PILOT_SERVERS: tuple[str, ...] = ("devicequery",)


# 参数再简单也不能直通：与显式工具语义重叠，模型会在两个入口之间瞎选。
# listDeviceWithSpace 和 device_query__list（listDeviceOnly）都是"列设备"。
#
# 2026-11-10 追加停用：countMonitorBySubType 在 Java 侧"不限空间"分支上
# 返回字面量 null（不是空数组），模型看到 null 会答成"查不到"。先停掉，
# 接口修好后从这个集合里删掉即可恢复。
#
# 注：countByDeviceType 同日一起坏过（500 / 耗时 19~95s），已在 16:52 修复
# 并实测 0.2s / 579 台与台账一致，已从下面移出，正常暴露。
_EXCLUDE_TOOLS: frozenset[str] = frozenset({
    "listDeviceWithSpace",
    "countMonitorBySubType",
})
# 时间戳类：要求精确到秒（yyyy-MM-dd HH:mm:ss）。描述写得再细，
# 模型也算不准"今天"对应的秒级时刻；而且填错会返回一段看起来合法的错误时间段，
# 用户从结果里察觉不到。这类必须由代码翻译，不能让模型填。
_TIME_PARAM = re.compile(r"time|date|start|end", re.IGNORECASE)

# 内部标识：必填 id 一定是"从另一个接口的返回里取"（如 getDeviceDetail 要
# listDeviceOnly 的 id）。用户不会主动报 uuid，模型只能编，所以直接排除。
# 注意 code（设备编号）不属于此类——那是用户能直接说出来的，模型抄原话即可。
_INTERNAL_ID_PARAM = re.compile(r"(^id$|Id$|_id$)", re.IGNORECASE)

# 描述里带"编码-中文"对照（7-电表 / 0门禁 / jk(监控设备)）说明取值可枚举，
# 模型照着选就行。这类不能因为参数名看着危险就排掉。
_ENUM_IN_DESC = re.compile(
    r"[0-9A-Za-z]+\s*[-=:：]\s*[\u4e00-\u9fa5]"
    r"|[(（][\u4e00-\u9fa5]{2,}[)）]"
)

_ALLOWED_TYPES = ("string", "integer", "number", "boolean")

# 预热结果缓存。build_registry() 是同步的，只能从这里读。
_discovered: list[DomainSpec] = []


# ----------------------------------------------------------------------
# 判定与构造（纯函数，方便单测）
# ----------------------------------------------------------------------
def _param_is_fillable(name: str, spec: dict) -> bool:
    """这个参数模型能不能自己填出合理值。

    依据不是参数名，而是**描述里有没有可枚举的取值**：
    MCP 侧把枚举写得很细（"deviceType: 0门禁 1道闸 2梯控…"），
    模型照着选完全没问题；按名字一刀切会把本来能的也排掉。
    """
    desc = str(spec.get("description") or "")
    if spec.get("enum"):
        return True
    if _ENUM_IN_DESC.search(desc):
        return True
    if _TIME_PARAM.search(name):
        return False
    if _INTERNAL_ID_PARAM.search(name):
        return False
    return spec.get("type", "string") in _ALLOWED_TYPES


def screen_schema(schema: dict | None) -> tuple[bool, str]:
    """判断一个 MCP 工具的入参是否"模型能自己填"。

    只看 required：可选参数不填就是后端默认口径，不会出错。
    """
    schema = schema or {}
    props = schema.get("properties") or {}
    for name in schema.get("required") or []:
        spec = props.get(name) or {}
        if _param_is_fillable(name, spec):
            continue
        if _TIME_PARAM.search(name):
            why = "要秒级时间戳，模型算不准"
        elif _INTERNAL_ID_PARAM.search(name):
            why = "要从别的接口取内部 id"
        else:
            why = "参数类型无法自动映射"
        return False, f"必填参数 {name}：{why}"
    return True, ""


def tool_name_for(server: str, mcp_name: str) -> str:
    """MCP 工具名 → loop 工具名。

    `canteen:getWeekMenu` → `canteen__getWeekMenu`
    （OpenAI 的函数名不允许冒号，所以用双下划线分隔 server）
    """
    short = mcp_name.split(":")[-1]
    return f"{server}__{short}"


def build_spec(server: str, tool, toolbox: McpToolbox) -> ToolSpec:
    """把一个 MCP 工具包成 loop 工具：参数透传、返回值交给模型组织。"""
    schema = dict(getattr(tool, "inputSchema", None) or {})
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    mcp_name = getattr(tool, "name", "")
    label = f"{server}_{mcp_name.split(':')[-1]}"

    async def execute(raw_args: dict) -> ToolOutcome:
        try:
            raw = await toolbox.call(server, mcp_name, raw_args or {})
        except Exception as exc:
            logger.warning(f"[discover] {label} 调用失败：{exc}")
            return ToolOutcome(f"查询失败：{exc}", is_error=True)
        return ToolOutcome(govern(raw, label=label))

    return ToolSpec(
        name=tool_name_for(server, mcp_name),
        description=(getattr(tool, "description", "") or mcp_name),
        parameters=schema,
        execute=execute,
        # 返回值是 MCP 原始 JSON，交给模型组织，不能直出
        passthrough=False,
    )


# ----------------------------------------------------------------------
# 发现
# ----------------------------------------------------------------------
async def discover_domains(
    toolbox: McpToolbox,
    known: set[tuple[str, str]],
    servers: tuple[str, ...] = PILOT_SERVERS,
) -> list[DomainSpec]:
    """按 server 拉工具清单，筛出能直通的，每个 server 组成一个域。

    :param known: 已被显式域使用的 (server, mcp_tool)，这些跳过
    """
    domains: list[DomainSpec] = []

    for server in servers:
        try:
            tools = await toolbox.tool_schemas(server)
        except Exception as exc:
            logger.warning(f"[discover] {server} 工具清单拉取失败，跳过：{exc}")
            continue

        specs: list[ToolSpec] = []
        skipped: list[str] = []
        for tool in tools:
            mcp_name = getattr(tool, "name", "")
            if not mcp_name:
                continue
            if (server, mcp_name) in known:
                skipped.append(f"{mcp_name}(已被显式工具使用)")
                continue
            if mcp_name.split(":")[-1] in _EXCLUDE_TOOLS:
                skipped.append(f"{mcp_name}(与显式工具语义重叠)")
                continue
            ok, reason = screen_schema(getattr(tool, "inputSchema", None))
            if not ok:
                skipped.append(f"{mcp_name}({reason})")
                continue
            specs.append(build_spec(server, tool, toolbox))

        if skipped:
            logger.info(f"[discover] {server} 跳过 {len(skipped)} 个：{'；'.join(skipped)}")
        if not specs:
            continue

        domains.append(
            DomainSpec(
                name=f"{server}_tools",
                description=f"{server} 的 MCP 工具（参数简单，可直接调用）",
                tools=specs,
                mcp_tools=tuple((server, t.name.split("__")[-1]) for t in specs),
            )
        )
        logger.info(f"[discover] {server} 新增 {len(specs)} 个工具")

    return domains


async def warm_discovered(
    toolbox: McpToolbox | None = None,
    servers: tuple[str, ...] = PILOT_SERVERS,
) -> list[DomainSpec]:
    """启动时调一次：发现 + 缓存。失败只记日志，返回空列表。"""

    global _discovered
    if _discovered:
        return _discovered

    from .domains import build_default_domains

    own = toolbox is None
    box = toolbox or McpToolbox()
    try:
        explicit = build_default_domains(box)
        known = {pair for domain in explicit for pair in domain.mcp_tools}
        _discovered = await discover_domains(box, known, servers)
        return _discovered
    except Exception as exc:
        logger.warning(f"[discover] 工具发现失败，仅使用显式工具：{exc}")
        return []
    finally:
        if own:
            try:
                await box.close()
            except BaseException:  # noqa: BLE001
                # mcp SDK 关会话可能抛 BaseExceptionGroup，清理路径上忽略
                logger.debug("[discover] 关闭临时 MCP 会话时忽略异常", exc_info=True)


def get_discovered() -> list[DomainSpec]:
    """build_registry() 用：同步取预热结果，未被预热时返回空。"""
    return list(_discovered)


def reset_discovered() -> None:
    """测试用。"""
    global _discovered
    _discovered = []
