"""食堂管理业务域（3 个子类型）。"""
from agent.intent.slots import extract_canteen_status_slots

from ._generic import QueryDomain, build_from, preview_from, route_from

CONFIG = QueryDomain(
    name="canteen_status",
    server="canteen",
    description="食堂管理：本周菜谱、菜品排行、就餐人数",
    tool_name="canteen_status__query",
    tool_description=(
        "查询食堂管理数据。适用于「本周菜谱」「哪个菜最受欢迎」「今天多少人吃饭」。"
        "工具会自己判断是菜谱、菜品排行还是就餐人数。"
    ),
    extract_slots=extract_canteen_status_slots,
    tool_map={
        "dish_rank": "canteen:getDishPopularity",
        "week_menu": "canteen:getWeekMenu",
        "dining_count": "canteen:getDiningCount",
    },
    default_type="week_menu",
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
