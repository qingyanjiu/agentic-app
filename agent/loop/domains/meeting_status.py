"""会议管理业务域（6 个子类型）。"""
from agent.intent.slots import extract_meeting_status_slots


from ._generic import (  # noqa: E402
    QueryDomain,
    build_from,
    preview_from,
    route_from,
    time_args_for,
)


CONFIG = QueryDomain(
    name="meeting_status",
    server="meeting",
    description="会议管理：会议统计、高频会议室、会议室概览/状态、按天会议列表",
    tool_name="meeting_status__query",
    tool_description=(
        "查询会议管理数据。适用于「今天开了多少会」「哪个会议室用得最多」"
        "「会议室现在有人吗」「今天的会议列表」。工具会自己判断子类型。"
        "重要口径：后端这几个接口**都不支持按会议室/楼层过滤**（"
        "getMeetingStatistics、getMeetingRoomOverview、getMeetingRoomStatus 无参数；"
        "getRoomMeetListByDay 只有 startTime/endTime）。所以统计类结果"
        "（预约场次、平均时长、会议室概览/状态）一律是**全园区口径**。"
        "用户点名某个会议室（如「1F 北开标室」）时，必须在回答里说明"
        "「接口不支持按会议室过滤，以下为全园区数据」，不能把它当成该会议室的结论；"
        "要某个会议室的具体安排，只能查当天会议列表后按 roomName 自行筛选。"
    ),
    extract_slots=extract_meeting_status_slots,
    tool_map={
        "meeting_statistics": "meeting:getMeetingStatistics",
        "high_freq_meeting_rooms": "meeting:getHighFrequencyMeetingRooms",
        "meeting_room_overview": "meeting:getMeetingRoomOverview",
        "number_of_meetings": "meeting:getNumberOfMeetings",
        "meeting_room_status": "meeting:getMeetingRoomStatus",
        "room_meet_list_by_day": "meeting:getRoomMeetListByDay",
    },
    default_type="meeting_statistics",
    # 时间必须显式传下去：不配 args_builder 时 _generic 一律用空参数，
    # 「本周的会议安排」会退化成平台的默认口径，用户看到的就不是本周。
    args_builder=time_args_for("room_meet_list_by_day"),
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
