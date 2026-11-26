"""安防态势业务域（11 个子类型）。"""
import re

from agent.intent.slots import extract_security_status_slots

from ._generic import (
    QueryDomain,
    build_from,
    preview_from,
    route_from,
    time_args_for,
)

# 时间参数在这个域里是三种形态，一个域只能挂一个 args_builder，所以串起来用：
# 每个 builder 只认自己白名单里的子类型，不属于自己的返回 {}。
_YMD_ARGS = time_args_for(
    "ai_alert", "ai_inspection", "alarm_detail", "ai_alarm_list"
)
# getAlertSituation 把时间拼进 query string，但格式是 ISO，不是 yyyy-MM-dd
_ISO_ARGS = time_args_for("ai_trend", mode="iso")
# getInspectionTrend 的参数名是 date 且只支持单日；「本周」这种区间不传，
# 否则会把整周压成周一那一天
_DATE_ARGS = time_args_for("inspection_trend", mode="date")


def _security_args(slots: dict) -> dict:
    return _YMD_ARGS(slots) or _ISO_ARGS(slots) or _DATE_ARGS(slots)


# 安防接口（getSecurityAlarmList / getAiAlertSituation / getSecurityIndex ...）
# 只接受时间/园区范围，没有楼层参数。带楼层的问法必须先说清楚，
# 不能把园区范围的结果套上「7楼没有告警」这种错误结论。
_FLOOR_INTENT_RE = re.compile(
    r"(?:[0-9一二两三四五六七八九十]+\s*(?:楼|层|[Ff])|[Bb]\s*1|负一|地下一层|"
    r"这层|当前层|本层|该层|楼层|每层|各层|逐层)"
)


def _floor_capability_precheck(query: str) -> str | None:
    if _FLOOR_INTENT_RE.search(query or ""):
        return (
            "安防态势接口目前只支持按园区和时间查询，不能按楼层筛选。"
            "请把范围改成园区或指定时间段。"
        )
    return None


CONFIG = QueryDomain(
    name="security_status",
    server="security",
    description="安防态势：告警列表/详情、巡查任务、安防指数、AI 告警与趋势",
    tool_name="security_status__query",
    tool_description=(
        "查询安防态势数据。适用于「今天的安防告警」「巡查任务完成情况」"
        "「安防指数是多少」「AI 告警趋势」。"
        "工具会自己判断是告警列表、告警详情、巡查、指数还是 AI 告警，调用方不需要指定子类型。"
        "查某条告警详情时，用户需要提供告警编号。"
    ),
    extract_slots=extract_security_status_slots,
    tool_map={
        "alarm_list": "security:getSecurityAlarmList",
        "alarm_detail": "security:getAiInspectionEvents",
        "patrol": "security:getPatrolMission",
        "device": "security:getDeviceDetail",
        "security_index": "security:getSecurityIndex",
        "ai_alert": "security:getAiAlertSituation",
        "inspection_trend": "security:getInspectionTrend",
        "ai_inspection": "security:getAiInspectionEvents",
        "ai_overview": "security:getAlarmView",
        "ai_trend": "security:getAlertSituation",
        "ai_alarm_list": "security:getAlarmListWithType",
    },
    default_type="alarm_list",
    args_builder=_security_args,
    # 接口正常但没数据时别让模型说成"服务异常"（实测 getSecurityAlarmList 返回 "null"）
    empty_hint="这个时间段没有安防告警事件",
    required={"alarm_detail": ("alarm_id",)},
    missing_hint={"alarm_id": "缺少要查询的告警。请向用户确认告警编号"},
    precheck=_floor_capability_precheck,
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
