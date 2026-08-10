import time
from typing import Dict, Optional
from dataclasses import dataclass, field

# ============================================================
# 意图状态数据类
# 用于保存某一次用户会话中，当前意图的完整上下文
# ============================================================
@dataclass
class IntentState:
    # 当前识别的意图模块名称，例如 "person_status"
    module: str
    
    # 已经从用户输入中提取到的参数（slot），例如 person_name、date 等
    slots: dict = field(default_factory=dict)
    
    # 当前还缺少的参数列表，例如 ["person_name"]
    missing_params: list = field(default_factory=list)
    
    # 系统已经追问的次数，防止无限追问
    ask_count: int = 0
    
    # 用户连续回复不相关的次数，超过阈值则放弃
    unrelated_count: int = 0
    
    # 上次系统提出的问题，用于在不相关时再次提醒用户
    last_question: str = ""
    
    # 用户最初触发该意图的问题，用于判断上下文
    original_query: str = ""
    
    # 状态最后一次更新的时间戳，用于超时清理
    last_update_time: float = 0.0
    
    # 当前意图流程是否已经完成
    done: bool = False


# ============================================================
# 会话状态管理器
# 负责按 user_id + session_id 保存和读取多轮对话状态
# ============================================================
class SessionStateManager:
    def __init__(self, timeout: float = 300.0):
        """
        初始化状态管理器
        :param timeout: 状态过期时间，单位秒，默认 300 秒（5 分钟）
        """
        # key 为 user_id:session_id，value 为 IntentState
        self.states: Dict[str, IntentState] = {}
        self.timeout = timeout
    
    def _key(self, user_id: str, session_id: str) -> str:
        """
        生成状态存储的 key
        用 user_id 和 session_id 拼接，保证每个会话独立
        """
        return f"{user_id}:{session_id}"
    
    def get(self, user_id: str, session_id: str) -> Optional[IntentState]:
        """
        获取某个会话的当前状态
        如果状态已过期，则自动清理并返回 None
        """
        key = self._key(user_id, session_id)
        state = self.states.get(key)
        
        # 如果状态存在且已超时，则清空返回 None
        if state and (time.time() - state.last_update_time) > self.timeout:
            self.clear(user_id, session_id)
            return None
        
        return state
    
    def set(self, user_id: str, session_id: str, state: IntentState):
        """
        保存某个会话的状态
        同时更新最后操作时间
        """
        state.last_update_time = time.time()
        self.states[self._key(user_id, session_id)] = state
    
    def update(self, user_id: str, session_id: str, **kwargs):
        """
        增量更新某个会话的状态字段
        例如 update(user_id, session_id, ask_count=1)
        """
        state = self.get(user_id, session_id)
        if state:
            for k, v in kwargs.items():
                setattr(state, k, v)
            state.last_update_time = time.time()
            self.set(user_id, session_id, state)
    
    def clear(self, user_id: str, session_id: str):
        """
        清空某个会话的状态
        通常在任务完成、放弃或超时时调用
        """
        self.states.pop(self._key(user_id, session_id), None)


# 全局单例，供整个项目使用
session_state = SessionStateManager()