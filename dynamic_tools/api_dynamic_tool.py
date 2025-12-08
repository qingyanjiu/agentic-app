from dynamic_tool_generator import DynamicToolGenerator
'''
实现类，继承通用动态工具生成接口
通过从API工具查询所需要的工具列表，并生成供大模型调用的动态工具
如果是固定的api-key可以构造的时候传入，否则就自己调用登录接口去维护
'''
class ApiDynamicTool(DynamicToolGenerator):
    
    # 获取所需工具列表的实现，因为也可能要请求接口鉴权等，所以需要实现类自己实现
    def query_tool_info_list(self, url=None, params: dict={}):
        pass