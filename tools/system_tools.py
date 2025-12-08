from langchain_core.tools import tool
from pydantic import BaseModel
from datetime import datetime

class WeatherParams(BaseModel):
    city: str
    date: str

@tool(args_schema=WeatherParams, description='''
      查询城市某一天的天气情况，参数：city-要查询天气的城市，date-要查询天气的日期,格式必须是yyyy-mm-dd HH:mm:ss，两个参数必须要明确提供，否则请要求用户提供需要的参数。
      ''')
def get_weather(city: str, date: str) -> str:
    return "大暴雨"

TOOLS = [get_weather]
TOOL_NAMES = ['get_weather']


@tool(description='''
      查询当前日期及时间，精确到秒
      ''')
def get_time() -> str:
    now = datetime.now()
    return now.strftime("%Y年%m月%d日 %H时%M分%S秒")

TOOLS = [get_weather, get_time]
TOOL_NAMES = ['get_weather', 'get_time']