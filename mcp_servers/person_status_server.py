from mcp.server.fastmcp import FastMCP

# 创建人员态势 MCP 服务
mcp = FastMCP("person_status")


@mcp.tool()
async def query_person_status(
    query_type: str,
    person_name: str = None,
    date_start: str = None,
    date_end: str = None,
    area: str = None
) -> str:
    """
    查询人员态势信息

    :param query_type: 查询类型
        - location: 人员当前位置
        - trace: 人员历史轨迹
        - count: 区域人数统计
        - abnormal: 异常人员
        - realtime: 实时人数
        - enter: 进入人数
        - leave: 离开人数
        - flow: 人员流动趋势
        - structure: 人员结构分布
    :param person_name: 人员姓名
    :param date_start: 开始时间（ISO 格式）
    :param date_end: 结束时间（ISO 格式）
    :param area: 区域名称
    :return: 查询结果字符串
    """
    # 当前是 mock 实现，数据与人员态势大屏保持一致
    if query_type == "location":
        return f"{person_name} 当前在 A栋 3 楼会议室"

    elif query_type == "trace":
        return f"{person_name} 轨迹：A栋 → 食堂 → 停车场"

    elif query_type == "count":
        # 园区总人数，与今日态势实时人数保持一致
        return "园区当前共有 226 人"

    elif query_type == "abnormal":
        return "今日异常人员 0 人"

    elif query_type == "realtime":
        # 今日态势：实时人数
        return "实时人数 226 人"

    elif query_type == "enter":
        # 今日态势：进入人数
        return "今日进入人数 226 人"

    elif query_type == "leave":
        # 今日态势：离开人数
        return "今日离开人数 226 人"

    elif query_type == "flow":
        # 人员流动折线图 mock 数据（与图中进入/离开两条线走势一致）
        return (
            "人员流动趋势（单位：人）：\\n"
            "08:00  进入 15 人，离开 7 人\\n"
            "09:00  进入 27 人，离开 14 人\\n"
            "10:00  进入 21 人，离开 16 人\\n"
            "11:00  进入 38 人，离开 25 人\\n"
            "12:00  进入 29 人，离开 13 人\\n"
            "13:00  进入 46 人，离开 18 人\\n"
            "14:00  进入 34 人，离开 29 人"
        )

    elif query_type == "structure":
        # 人员结构环形图 mock 数据（与图中四类数据一致）
        return (
            "人员结构分布：\\n"
            "中通服和信科技：192 人\\n"
            "省公司：192 人\\n"
            "规划设计院：38 人\\n"
            "其他：38 人"
        )

    return "未知查询类型"


# 当直接运行该文件时，启动 stdio 模式的 MCP 服务
if __name__ == "__main__":
    mcp.run(transport="stdio")