"""设备态势业务域（8 个统计子类型）。

注意与 device_query 的区别：
- device_status 是**统计口径**（某类设备多少台、在线率、维保维修数）；
- device_query 是**台账口径**（具体某台设备是谁、在哪、什么状态）。
两者都走 /mcp/device 与 /mcp/devicequery 两个不同端点，不要混用。
"""
import json
import logging

from agent.intent.slots import extract_device_status_slots

from ._generic import QueryDomain, build_from, preview_from, route_from

logger = logging.getLogger(__name__)


def _format_equip_class(raw: str, query_type: str) -> str:
    """getEquipClass 返回的是 ECharts 饼图数据，先转成人话列表。

    原样丢给模型时，同一个问题两次回答格式会不一样：有一次它组织成
    「监控 268 台」列表，有一次把 `[{"itemStyle":…,"name":"监控","value":268}]`
    整串贴给用户。工具层统一成人话，两条链路（L1 composer / L2 模型）就一致了。

    其他子类型、或结构对不上时原样返回——宁可让模型自己看，也不猜。
    """
    if query_type != "equip_class":
        return raw
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(rows, list) or not rows:
        return raw

    items: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict) or "name" not in row or "value" not in row:
            return raw
        try:
            items.append((str(row["name"]), float(row["value"])))
        except (TypeError, ValueError):
            return raw

    total = int(sum(value for _, value in items))
    lines = [f"设备类型统计（各类合计 {total} 台）："]
    for name, value in sorted(items, key=lambda kv: kv[1], reverse=True):
        share = f"（{value / total * 100:.0f}%）" if total else ""
        shown = int(value) if value == int(value) else value
        lines.append(f"- {name} {shown} 台{share}")
    return "\n".join(lines)

CONFIG = QueryDomain(
    name="device_status",
    server="device",
    description="设备态势（统计口径）：设备分类、健康度、维保维修、区域分布、各网在线率",
    tool_name="device_status__query",
    tool_description=(
        "查询设备态势统计。适用于「园区有多少台设备」「设备健康度怎么样」"
        "「这个月维修了几台」「各区域设备分布」「安防设备在线率」。"
        "注意：这是统计口径；如果要查某一台具体设备，应该用 device_query 系列工具。"
    ),
    extract_slots=extract_device_status_slots,
    tool_map={
        "equip_class": "device:getEquipClass",
        "category_health": "device:getCategoryHealth",
        "month_maintenance": "device:getMonthMaintenance",
        "month_repair": "device:getMonthRepair",
        "statis_region": "device:getStatisRegion",
        "anfang_online": "device:getAnfangDeviceOnlinePercentage",
        "gb_online": "device:getGbOnlinePercentage",
        "mj_online": "device:getMjOnlinePercentage",
    },
    default_type="equip_class",
    # getEquipClass 返回 ECharts 饼图 JSON，先转成人话，别让模型贴 JSON 原文
    formatter=_format_equip_class,
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
