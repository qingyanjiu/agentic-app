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
    def extract_slots(self, query: str, is_followup: bool = False) -> dict:
        """
        从用户输入中提取当前模块需要的参数
        每个子类必须实现
        is_followup：追问轮置 True，只抽填空字段（人名/时间/ID 等），
        不重判子类型，避免短回复的默认值/误判污染原查询
        """
        pass
    
    @abstractmethod
    def is_related(self, state, query: str) -> bool:
        """
        判断用户回复是否与当前追问任务相关
        每个子类必须实现
        """
        pass