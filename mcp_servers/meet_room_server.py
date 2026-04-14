from mcp.server.fastmcp import FastMCP
import time
from datetime import datetime
from typing import Optional, List

mcp = FastMCP()

# 模拟会议室数据
MEETING_ROOMS = ["A101", "A102", "B201", "B202"]
bookings = {}

def _now_ts():
    return int(time.time())

def _dt() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")

# ====================== 1. 查看空闲时段 ======================
@mcp.tool()
def query_meeting_room_free(
    room: Optional[str] = None,
    date: Optional[str] = None  # 格式 2026-04-08
) -> str:
    """
    查询会议室空闲时段
    - room: 会议室编号（如A101），不填则返回全部
    - date: 查询日期，默认今天
    """
    target_date = date or datetime.now().strftime("%Y-%m-%d")
    rooms = [room] if room else MEETING_ROOMS

    result = [f"📅 {target_date} 会议室空闲情况:"]
    for r in rooms:
        key = f"{r}_{target_date}"
        booked = bookings.get(key, [])
        if not booked:
            result.append(f"✅ {r}: 全天可用")
        else:
            result.append(f"🕒 {r}: 已预订 {len(booked)} 段")
    return "\n".join(result)

# ====================== 2. 预订会议室 ======================
@mcp.tool()
def book_meeting_room(
    room: str,
    start_time: str,  # 2026-04-08 14:00
    end_time: str,
    booker: str = "匿名"
) -> str:
    """
    预订会议室
    - room: 会议室编号
    - start_time: 开始时间
    - end_time: 结束时间
    - booker: 预订人
    """
    if room not in MEETING_ROOMS:
        return f"❌ 会议室 {room} 不存在"

    date = start_time.split(" ")[0] if " " in start_time else datetime.now().strftime("%Y-%m-%d")
    key = f"{room}_{date}"
    bookings.setdefault(key, []).append({
        "start": start_time,
        "end": end_time,
        "booker": booker,
        "ts": _now_ts()
    })
    return (
        f"✅ 预订成功\n"
        f"📍 会议室: {room}\n"
        f"⏰ 时间: {start_time} ~ {end_time}\n"
        f"👤 预订人: {booker}\n"
        f"📆 日期: {date}"
    )

# ====================== 3. 取消预订 ======================
@mcp.tool()
def cancel_meeting_room(
    room: str,
    start_time: str,
    booker: str
) -> str:
    """取消会议室预订"""
    date = start_time.split(" ")[0] if " " in start_time else datetime.now().strftime("%Y-%m-%d")
    key = f"{room}_{date}"
    if key not in bookings:
        return "❌ 该时段无预订"

    new_lst = []
    removed = False
    for item in bookings[key]:
        if item["start"] == start_time and item["booker"] == booker:
            removed = True
        else:
            new_lst.append(item)
    if removed:
        bookings[key] = new_lst
        return f"✅ 已取消 {room} {start_time} 的预订"
    return "❌ 未找到对应预订记录"

# ====================== 4. 我的预订/提醒 ======================
@mcp.tool()
def list_my_bookings(booker: str) -> str:
    """查询我的所有预订（用于提醒）"""
    my = []
    for key, items in bookings.items():
        room, date = key.split("_")
        for item in items:
            if item["booker"] == booker:
                my.append(f"【{room}】{item['start']}~{item['end']}")
    if not my:
        return "📭 暂无预订"
    return "📅 我的预订:\n" + "\n".join(my)

if __name__ == "__main__":
    mcp.run()