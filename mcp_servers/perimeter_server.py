import json

from mcp.server.fastmcp import FastMCP

# 创建周界态势 MCP 服务（本地 mock，返回结构贴合 docs/项目结构与接口文档.md 的 /emergency/perimeter 约定）
mcp = FastMCP("perimeter_status")


# 关键指标 mock 数据（对应平台 GET /emergency/perimeter/getKeyMetrics）
_MOCK_KEY_METRICS = {
    "msg": "success",
    "code": 0,
    "data": {
        "defenseTotal": 24,
        "onlineNum": 22,
        "offlineNum": 2,
        "todayAlarmNum": 5,
    },
}

# 防区一览 mock 数据（对应平台 GET /emergency/perimeter/getAreaOverview 的 rows）
_MOCK_AREA_OVERVIEW = {
    "msg": "success",
    "total": 4,
    "code": 0,
    "rows": [
        {
            "areaName": "东侧围墙防区01",
            "status": "布防",
            "online": 1,
            "deviceNum": 6,
            "areaType": "围墙",
        },
        {
            "areaName": "西侧围墙防区02",
            "status": "布防",
            "online": 1,
            "deviceNum": 6,
            "areaType": "围墙",
        },
        {
            "areaName": "南门防区03",
            "status": "撤防",
            "online": 1,
            "deviceNum": 4,
            "areaType": "出入口",
        },
        {
            "areaName": "北侧围栏防区04",
            "status": "布防",
            "online": 0,
            "deviceNum": 8,
            "areaType": "围栏",
        },
    ],
}

# 周界告警统计 mock 数据（对应平台 GET /emergency/perimeter/getPerimeterAlarmStats）
_MOCK_PERIMETER_ALARM_STATS = {
    "msg": "success",
    "code": 0,
    "data": {
        "areaStats": [
            {"areaName": "东侧围墙防区01", "alarmNum": 3},
            {"areaName": "北侧围栏防区04", "alarmNum": 2},
        ],
        "hourCurve": [
            {"hour": "08", "alarmNum": 1},
            {"hour": "10", "alarmNum": 2},
            {"hour": "14", "alarmNum": 2},
        ],
    },
}

# 告警一览 mock 数据（对应平台 GET /emergency/perimeter/getAlarmOverview 的 rows）
_MOCK_ALARM_OVERVIEW = {
    "msg": "success",
    "total": 2,
    "code": 0,
    "rows": [
        {
            "alarmName": "周界入侵告警",
            "areaName": "东侧围墙防区01",
            "alarmLevel": "重要",
            "handleStatus": "未处理",
            "alarmTime": 1725926400000,
            "alarmReason": "检测到翻越行为",
        },
        {
            "alarmName": "围栏振动告警",
            "areaName": "北侧围栏防区04",
            "alarmLevel": "一般",
            "handleStatus": "已处理",
            "alarmTime": 1725922800000,
            "alarmReason": "围栏振动触发",
        },
    ],
}


@mcp.tool()
async def getKeyMetrics() -> str:
    """
    查询周界关键指标（在线/离线防区设备数、今日告警数）

    对应平台接口：GET /emergency/perimeter/getKeyMetrics
    :return: JSON 字符串，结构为 { msg; code; data: { defenseTotal; onlineNum; offlineNum; todayAlarmNum } }
    """
    return json.dumps(_MOCK_KEY_METRICS, ensure_ascii=False)


@mcp.tool()
async def getAreaOverview() -> str:
    """
    查询防区一览（防区列表与布防状态）

    对应平台接口：GET /emergency/perimeter/getAreaOverview
    :return: JSON 字符串，结构为 { msg; total; code; rows: AreaOverviewItem[] }
    """
    return json.dumps(_MOCK_AREA_OVERVIEW, ensure_ascii=False)


@mcp.tool()
async def getPerimeterAlarmStats() -> str:
    """
    查询周界告警统计（按区域/时段统计与小时曲线）

    对应平台接口：GET /emergency/perimeter/getPerimeterAlarmStats
    :return: JSON 字符串，结构为 { msg; code; data: { areaStats[]; hourCurve[] } }
    """
    return json.dumps(_MOCK_PERIMETER_ALARM_STATS, ensure_ascii=False)


@mcp.tool()
async def getAlarmOverview() -> str:
    """
    查询周界告警一览（告警列表，点击看抓拍照片）

    对应平台接口：GET /emergency/perimeter/getAlarmOverview
    :return: JSON 字符串，结构为 { msg; total; code; rows: AlarmOverviewItem[] }
    """
    return json.dumps(_MOCK_ALARM_OVERVIEW, ensure_ascii=False)


# 当直接运行该文件时，启动 stdio 模式的 MCP 服务
if __name__ == "__main__":
    mcp.run(transport="stdio")
