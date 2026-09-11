import json

from mcp.server.fastmcp import FastMCP

# 创建消防态势 MCP 服务（本地 mock，返回结构贴合 docs/项目结构与接口文档.md 的 /emergency/fire 约定）
mcp = FastMCP("fire_status")


# 消防设备台账 mock 数据（对应平台 POST /calldevice/fire/getAssetsList 的 rows）
_MOCK_FIRE_ASSETS = {
    "msg": "success",
    "total": 4,
    "code": 0,
    "rows": [
        {
            "deviceName": "A栋1层灭火器01",
            "deviceType": "灭火器",
            "status": "正常",
            "pressure": "1.2MPa",
            "liquidLevel": None,
            "electricQuantity": 96,
            "tiltAngle": "0°",
            "location": "A栋1层东侧走廊",
            "areaName": "A栋",
        },
        {
            "deviceName": "B栋消防栓02",
            "deviceType": "消防栓",
            "status": "正常",
            "pressure": "0.85MPa",
            "liquidLevel": "3.2m",
            "electricQuantity": 88,
            "tiltAngle": "0°",
            "location": "B栋2层南侧",
            "areaName": "B栋",
        },
        {
            "deviceName": "C栋烟感03",
            "deviceType": "烟感",
            "status": "告警",
            "pressure": None,
            "liquidLevel": None,
            "electricQuantity": 45,
            "tiltAngle": "2°",
            "location": "C栋3层会议室",
            "areaName": "C栋",
        },
        {
            "deviceName": "D栋水位计04",
            "deviceType": "液位计",
            "status": "离线",
            "pressure": None,
            "liquidLevel": "1.1m",
            "electricQuantity": 12,
            "tiltAngle": "0°",
            "location": "D栋地下水泵房",
            "areaName": "D栋",
        },
    ],
}

# 设备告警统计 mock 数据（对应平台 POST /calldevice/fire/getAlarmNum 的 data）
_MOCK_FIRE_ALARM_NUM = {
    "msg": "success",
    "code": 0,
    "data": {
        "allAlarmNum": 36,
        "fireNum": 2,
        "fireMisreportNum": 1,
        "fireDangerNum": 5,
        "fireDangerMisreportNum": 1,
        "deviceFaultNum": 8,
        "deviceFaultMisreportNum": 2,
        "deviceStateNum": 15,
        "userLeaveNum": 2,
    },
}

# 实时告警列表 mock 数据（对应平台 POST /calldevice/fire/getAlarmList 的 rows）
_MOCK_FIRE_ALARM_LIST = {
    "msg": "success",
    "total": 3,
    "code": 0,
    "rows": [
        {
            "alarmType": "火警",
            "alarmLevel": "紧急",
            "status": "未处理",
            "deviceName": "C栋烟感03",
            "location": "C栋3层会议室",
            "alarmTime": "2026-08-31 10:23:15",
            "content": "烟雾浓度超过阈值",
        },
        {
            "alarmType": "设备故障",
            "alarmLevel": "重要",
            "status": "处理中",
            "deviceName": "D栋水位计04",
            "location": "D栋地下水泵房",
            "alarmTime": "2026-08-31 09:40:02",
            "content": "设备离线超过 30 分钟",
        },
        {
            "alarmType": "火灾隐患",
            "alarmLevel": "一般",
            "status": "已处理",
            "deviceName": "B栋消防栓02",
            "location": "B栋2层南侧",
            "alarmTime": "2026-08-31 08:15:47",
            "content": "水压低于正常范围",
        },
    ],
}

# 月度报修 mock 数据（对应平台 GET /fire/monthRepair）
_MOCK_MONTH_REPAIR = {
    "category": ["3月", "4月", "5月", "6月", "7月", "8月"],
    "oneData": [12, 18, 15, 22, 19, 25],
    "twoData": [9, 14, 11, 17, 13, 20],
}


@mcp.tool()
async def getFireAssets() -> str:
    """
    查询消防设备台账（状态/压力液位/电量/倾角）

    对应平台接口：POST /calldevice/fire/getAssetsList
    :return: JSON 字符串，结构为 { msg; total; code; rows: FireAssetItem[] }
    """
    return json.dumps(_MOCK_FIRE_ASSETS, ensure_ascii=False)


@mcp.tool()
async def getFireAlarmNum() -> str:
    """
    查询消防设备告警统计（火警/故障/隐患/漏报/离人）

    对应平台接口：POST /calldevice/fire/getAlarmNum
    :return: JSON 字符串，结构为 { msg; code; data: { allAlarmNum; fireNum; ... } }
    """
    return json.dumps(_MOCK_FIRE_ALARM_NUM, ensure_ascii=False)


@mcp.tool()
async def getAlarmList() -> str:
    """
    查询实时消防告警列表

    对应平台接口：POST /calldevice/fire/getAlarmList
    :return: JSON 字符串，结构为 { msg; total; code; rows: AlarmRowItem[] }
    """
    return json.dumps(_MOCK_FIRE_ALARM_LIST, ensure_ascii=False)


@mcp.tool()
async def getMonthRepair() -> str:
    """
    查询消防月度报修趋势

    对应平台接口：GET /fire/monthRepair
    :return: JSON 字符串，结构为 { category:string[]; oneData:number[]; twoData:number[] }
    """
    return json.dumps(_MOCK_MONTH_REPAIR, ensure_ascii=False)


# 当直接运行该文件时，启动 stdio 模式的 MCP 服务
if __name__ == "__main__":
    mcp.run(transport="stdio")
