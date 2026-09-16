import json

from mcp.server.fastmcp import FastMCP

# 创建孪生巡检 MCP 服务（本地 mock，返回结构贴合 docs/项目结构与接口文档.md 的 /twins/inspection 约定）
mcp = FastMCP("inspection_status")


# 今日巡检 mock 数据（对应平台 GET /screenPatrol/todayOverview）
# chartData 为分时段图表：yData1=计划任务数，yData2=已完成任务数
_MOCK_TODAY_INSPECTION = {
    "msg": "success",
    "code": 0,
    "data": {
        "taskCount": 8,
        "pointCount": 46,
        "completionRate": 87.5,
        "chartData": {
            "xData": ["08:00", "10:00", "12:00", "14:00", "16:00"],
            "yData1": [2, 3, 1, 1, 1],
            "yData2": [2, 2, 1, 1, 1],
        },
    },
}

# 今日任务列表 mock 数据（对应平台 GET /screenPatrol/todayTaskList 的 rows）
_MOCK_TODAY_TASKS = {
    "msg": "success",
    "total": 4,
    "code": 0,
    "rows": [
        {
            "name": "张伟",
            "type": "日常巡检",
            "state": "已完成",
            "stateClass": "success",
            "team": "巡检一班",
            "time": "08:30",
        },
        {
            "name": "李娜",
            "type": "设备巡检",
            "state": "进行中",
            "stateClass": "primary",
            "team": "巡检二班",
            "time": "10:00",
        },
        {
            "name": "王强",
            "type": "安全巡检",
            "state": "未开始",
            "stateClass": "info",
            "team": "巡检一班",
            "time": "14:00",
        },
        {
            "name": "赵敏",
            "type": "日常巡检",
            "state": "已完成",
            "stateClass": "success",
            "team": "巡检三班",
            "time": "09:00",
        },
    ],
}

# 巡检统计 mock 数据（对应平台 GET /screenPatrol/statistics）
# chartData 为巡检类型分布：{value, name}
_MOCK_INSPECTION_STATISTICS = {
    "msg": "success",
    "code": 0,
    "data": {
        "avgTime": "25分钟",
        "pointCount": 320,
        "hazardCount": 6,
        "chartData": [
            {"value": 12, "name": "日常巡检"},
            {"value": 5, "name": "设备巡检"},
            {"value": 3, "name": "安全巡检"},
        ],
    },
}

# 巡检执行状态 mock 数据（对应平台 GET /screenPatrol/executionStatus）
# yData1=巡检正常次数，yData2=巡检异常次数（按人）
_MOCK_INSPECTION_EXECUTION_STATUS = {
    "msg": "success",
    "code": 0,
    "data": {
        "xData": ["张伟", "李娜", "王强", "赵敏"],
        "yData1": [12, 10, 8, 9],
        "yData2": [1, 0, 2, 0],
    },
}


@mcp.tool()
async def getTodayInspection() -> str:
    """
    查询今日巡检总览（今日任务数/点位/完成率 + 分时段图表）

    对应平台接口：GET /screenPatrol/todayOverview
    :return: JSON 字符串，结构为 { msg; code; data: { taskCount; pointCount; completionRate; chartData: { xData; yData1; yData2 } } }
    """
    return json.dumps(_MOCK_TODAY_INSPECTION, ensure_ascii=False)


@mcp.tool()
async def getTodayTasks() -> str:
    """
    查询今日巡检任务列表（人员/类型/状态/班组/时间）

    对应平台接口：GET /screenPatrol/todayTaskList
    :return: JSON 字符串，结构为 { msg; total; code; rows: { name; type; state; stateClass; team; time }[] }
    """
    return json.dumps(_MOCK_TODAY_TASKS, ensure_ascii=False)


@mcp.tool()
async def getInspectionStatistics() -> str:
    """
    查询巡检统计（平均时长/点位/隐患数 + 巡检类型分布，近1月/3月/1年）

    对应平台接口：GET /screenPatrol/statistics
    :return: JSON 字符串，结构为 { msg; code; data: { avgTime; pointCount; hazardCount; chartData: { value; name }[] } }
    """
    return json.dumps(_MOCK_INSPECTION_STATISTICS, ensure_ascii=False)


@mcp.tool()
async def getInspectionExecutionStatus() -> str:
    """
    查询巡检执行状态（按人巡检正常/异常情况）

    对应平台接口：GET /screenPatrol/executionStatus
    :return: JSON 字符串，结构为 { msg; code; data: { xData: string[]; yData1: number[]; yData2: number[] } }
    """
    return json.dumps(_MOCK_INSPECTION_EXECUTION_STATUS, ensure_ascii=False)


# 当直接运行该文件时，启动 stdio 模式的 MCP 服务
if __name__ == "__main__":
    mcp.run(transport="stdio")
