from abc import ABC, abstractmethod

# ============================================================
# 意图处理器基类
# 所有具体模块的 handler 都需要继承这个类
# 统一规范：参数抽取、相关性判断
# ============================================================
class IntentHandler(ABC):
    # handler 对应的意图模块名称
    name = ""
    
    @abstractmethod
    def extract_slots(self, query: str) -> dict:
        """
        从用户输入中提取当前模块需要的参数
        每个子类必须实现
        """
        pass
    
    @abstractmethod
    def is_related(self, state, query: str) -> bool:
        """
        判断用户回复是否与当前追问任务相关
        每个子类必须实现
        """
        pass