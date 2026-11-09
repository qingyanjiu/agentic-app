"""会议管理业务域（6 个子类型）。"""
from agent.intent.slots import extract_meeting_status_slots

from ._generic import QueryDomain, build_from, preview_from, route_from

CONFIG = QueryDomain(
    name="meeting_status",
    server="meeting",
    description="会议管理：会议统计、高频会议室、会议室概览/状态、按天会议列表",
    tool_name="meeting_status__query",
    tool_description=(
        "查询会议管理数据。适用于「今天开了多少会」「哪个会议室用得最多」"
        "「会议室现在有人吗」「今天的会议列表」。工具会自己判断子类型。"
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
)


def build_domain(toolbox):
    return build_from(CONFIG, toolbox)


def route_plan(query: str):
    return route_from(CONFIG, query)


def preview_plan(query: str) -> dict:
    return preview_from(CONFIG, query)
