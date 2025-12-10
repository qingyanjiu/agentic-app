from langchain_core.tools import StructuredTool
from pydantic import Field

class CustomTool(StructuredTool):
    displayName: str = Field('', description="工具显示名称")
    endPoint: str = Field('', description="工具调用接口")
    method: str = Field('', description="接口方法 get/post")
    
    def __init__(self, *args, **kwargs):
        displayName = kwargs.pop('displayName', '')
        endpoint = kwargs.pop('endpoint', '')
        method = kwargs.pop('method', '')
        super().__init__(*args, displayName=displayName, endPoint=endpoint, method=method, **kwargs)