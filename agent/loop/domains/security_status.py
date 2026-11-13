"""安防态势业务域（11 个子类型）。"""
from agent.intent.slots import extract_security_status_slots

from ._generic import QueryDomain, build_from, preview_from, route_from

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
    # 接口正常但没数据时别让模型说成"服务异常"（实测 getSecurityAlarmList 返回 "null"）
    empty_hint="这个时间段没有安防告警事件",
    required={"alarm_detail": ("alarm_id",)},
    missing_hint={"alarm_id": "缺少要查询的告警。请向用户确认告警编号"},
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
