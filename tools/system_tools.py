from langchain_core.tools import tool
from pydantic import BaseModel
from datetime import datetime
import logging

from tools.custom_tool import CustomTool

logging.basicConfig(
    filename='app.log',
    # 追加模式 'a'，覆盖模式 'w' 
    filemode='w',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)


# 工具名称和displayName的关联关系，方便前端展示工具调用情况
TOOL_NAME_MAPPING = {
    'get_weather': '获取天气',
    'get_time': '获取时间'
}

class WeatherParams(BaseModel):
    city: str
    date: str

@tool(args_schema=WeatherParams, description='''
    查询城市某一天的天气情况，参数：city-要查询天气的城市，date-要查询天气的日期,格式是 yyyy-mm-dd HH:mm:ss。
    ''')
def get_weather(city: str, date: str) -> str:
    return "大暴雨"

@tool(description='''
    查询当前日期及时间，精确到秒
    ''')
def get_time() -> str:
    now = datetime.now()
    return now.strftime("%Y年%m月%d日 %H时%M分%S秒")

def genTools():
    TOOLS_RAW = [get_weather, get_time]
    TOOLS: list[CustomTool] = []
    for t in TOOLS_RAW:
        t = CustomTool(**vars(t))
        t.displayName = TOOL_NAME_MAPPING.get(t.name)
        TOOLS.append(t)
    return TOOLS