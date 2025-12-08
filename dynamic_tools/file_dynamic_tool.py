import json

import requests
from dynamic_tools.dynamic_tool_generator import DynamicToolGenerator
'''
实现类，继承通用动态工具生成接口
通过从json文件查询所需要的工具列表，并生成供大模型调用的动态工具
'''
class FileDynamicTool(DynamicToolGenerator):
    
    # 获取所需工具列表的实现，因为也可能要请求接口鉴权等，所以需要实现类自己实现
    def query_tool_info_list(self, url=None, params: dict={}):
        json_str = ''
        with open('dynamic_tools/dynamic-tools-data.json', 'r', encoding='utf-8') as f:
            json_str = f.read()
        tools_json = json.loads(json_str)
        return tools_json