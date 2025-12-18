from abc import abstractmethod
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, create_model
import requests
import logging

from tools.custom_tool import CustomTool

logging.basicConfig(
    filename='app.log',
    # 追加模式 'a'，覆盖模式 'w' 
    filemode='w',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

'''
生成动态工具抽象类，通过查询数据库等方式
get_tool_token: 查询需要什么工具可能需要的token，可以为空
call_tool_token: 调用工具的时候需要的token，可以为空
如果是固定的api-key可以构造的时候传入，否则就自己调用登录接口去维护
'''
class DynamicToolGenerator:
    def __init__(self, get_tool_token:str = '', call_tool_token: str = ''):
        self.get_tool_token = get_tool_token
        self.call_tool_token = call_tool_token

    """
    返回工具对象列表
    """
    def generate_tools(self, tool_query_url: str = '', tool_query_param_json: dict = {}) -> list[CustomTool]:
        # 先查询所需要的工具列表
        tool_info_data = self.query_tool_info_list(tool_query_url, tool_query_param_json)
        tool_info_list = tool_info_data['data']
        tool_list = []
        if tool_info_list:
            for tool in tool_info_list:
                t = self._gen_single_tool(tool)
                tool_list.append(t)
        else:
            logging.info('未获取到任何工具')
        return tool_list

    '''
    返回一条工具对象
    tool_info:
    {
        "name": "search_user",
        "description": "根据用户ID从系统查询用户信息",
        "endpoint": "http://xxx/api/user",
        "schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"]
        }
    }
    '''
    def _gen_single_tool(self, tool_info) -> CustomTool:
        # 可能带占位符，需要format
        endpoint_template = tool_info['endpoint']
        
        logging.info(f'获取到接口工具:{tool_info}')

        def _call_api(**kwargs):
            # api请求方式 post或者get
            # @@@@ 后期也许也可以加入一些其他类型来使用function调用啥的 @@@@
            method = tool_info['method'].lower()
            endpoint = endpoint_template.format(**kwargs)
            resp = self.tool_request(method, endpoint, kwargs)
            return resp

        # JSON Schema → Pydantic Model
        fields = {}
        for field_name, field_schema in tool_info["parameters"]["properties"].items():
            field_type = field_schema.get("type", "string")
            py_type = str if field_type == "string" else int if field_type == "integer" else float
            fields[field_name] = (py_type, ...)

        DynamicSchema = create_model(
            f"{tool_info['name']}_schema",
            **fields
        )

        return CustomTool.from_function(
            name=tool_info["name"],
            displayName=tool_info["displayName"],
            description=tool_info["description"],
            endpoint=tool_info["endpoint"],
            method=tool_info["method"],
            func=_call_api,
            args_schema=DynamicSchema,
        )
    
    
    # 工具动态调用api的实现，可能需要带token等操作，所以需要实现类来具体实现该方法，例如
    # res = requests.post(endpoint, json=data, timeout=10)
    # return res
    def tool_request(self, method, endpoint, data):
        call_tool_headers = {
            "Authorization": f"Bearer {self.call_tool_token}",
            "Content-Type": "application/json"
        }
        resp = ''
        if (method == 'get'):
            logging.info(f'get请求调用接口:{endpoint}')
            resp = requests.get(endpoint, headers=call_tool_headers, params=data)
        elif (method == 'post'):
            logging.info(f'post请求调用接口:{endpoint}')
            resp = requests.post(endpoint, headers=call_tool_headers, json=data)
        resp.raise_for_status()
        json_data = resp.json()
        logging.info(f'接口返回数据:{json_data}')
        return json_data
    
    # 获取所需工具列表的实现，因为也可能要请求接口鉴权等，所以需要实现类自己实现
    @abstractmethod
    def query_tool_info_list(self, url=None, params: dict={}):
        pass
    
    # 根据工具的displayName获取工具名称和displayName的关联关系，方便前端展示工具调用情况
    @staticmethod
    def get_tool_name_mapping(tools: list[CustomTool]):
        tool_mapping = dict(map(lambda x: (x.name, x.displayName), tools))
        return tool_mapping
    
    # 根据tool列表获取tool的json描述
    @staticmethod
    def get_tool_json_desc(tools: list[CustomTool]):
        tool_desc = dict(map(lambda x: (x.name, {'desc': x.displayName, 'args': x.args}), tools))
        return tool_desc
    
    