from abc import abstractmethod

class MemoryPersistor:
    """简单 JSON 持久化"""

    # 持久化历史对话记录
    @abstractmethod
    def save(self, user_id: str, session_id: str, data: dict):
        pass

    # 从持久化存储读取历史对话记录
    @abstractmethod
    def load(self, user_id: str, session_id):
        pass
        