# memory/store.py
from langchain_classic.memory.chat_memory import BaseChatMemory
from langchain_classic.memory import ConversationBufferMemory, ConversationBufferWindowMemory
from langchain_classic.schema import AIMessage, HumanMessage

from memory.memory_persistor import MemoryPersistor
from memory.memory_persistor_json import MemoryPersistorJSON
from memory.memory_persistor_sqlite import MemoryPersistorSqlite
from memory.memory_persistor_mysql import MemoryPersistorMysql
from utils.utils import get_config
import logging
from utils.utils import serialize_messages

logging.basicConfig(
    filename='app.log',
    # 追加模式 'a'，覆盖模式 'w' 
    filemode='w',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

'''
记忆持久化服务实例的工厂方法，根据类型生成对象
persistor_type 描述:
1.sqlite: 本地sqlite数据库存储
2.json: 本地json文件存储（毫无并发，测试用）
后期可添加其他实现
'''
def memory_persistor_factory(persistor_type='json') -> MemoryPersistor:
    if(persistor_type == 'json'):
        return MemoryPersistorJSON()
    elif(persistor_type == 'sqlite'):
        return MemoryPersistorSqlite()
    elif(persistor_type == 'mysql'):
        return MemoryPersistorMysql()

'''
加载时直接初始化，保证单例
'''
config = get_config()
memory_persistor_type = config['memory_persistor']['type']
memory_buffer_window = config['memory_persistor']['memory_buffer_window']
memory_persistor = memory_persistor_factory(memory_persistor_type)

class MemoryStore:
    """
    多用户 memory 管理 + 自动持久化 memory
    """
    def __init__(self):
        self.store: BaseChatMemory
        self.persistor = memory_persistor

    # 从持久化存储读取以前的聊天记录（恢复成 ConversationBufferWindowMemory）
    def get_memory(self, user_id: str, session_id: str):
        memory = ConversationBufferWindowMemory(
            k=memory_buffer_window,
            memory_key="chat_history", 
            return_messages=True
        )
        # 赋值新memory对象s
        self.store = memory
        # 从持久化数据读取，并填充数据到memory对象
        try:
            saved = self.persistor.load(user_id, session_id)
            if saved:
                # 恢复对话
                for msg in saved.get("messages", []):
                    if(msg):
                        role = msg.get("role")
                        content = msg.get("content")
                        if role == "user":
                            self.store.chat_memory.add_user_message(content)
                        elif role == "ai":
                            self.store.chat_memory.add_ai_message(content)
        except Exception as e:
            logging.error(f'获取对话历史失败，返回空对话历史:{e}')
        return self.store
    
    # 保存对话历史 (ConversationBufferWindowMemory)
    def persist_memory(self, user_id: str, session_id: str):
        """
        保存 memory 和 trace
        """
        messages = []
        for msg in self.store.chat_memory.messages:
            if msg.type == "human":
                messages.append({"role": "user", "content": msg.content})
            elif msg.type == "ai":
                messages.append({"role": "ai", "content": msg.content})
        data = {
            "messages": messages,
        }
        try:
            self.persistor.save(user_id, session_id, data)
        except Exception as e:
            logging.error(f'保存对话历史失败:{e}')
    
    # 恢复成 langgraph的checkpointer
    def graph_get_history_messages(self, user_id: str, session_id: str):
        messages = []
        try:
            saved = self.persistor.load(user_id, session_id)
            if saved:
                for msg in saved.get("messages", []):
                    if not msg:
                        continue
                    role = msg.get("role")
                    content = msg.get("content", "")
                    if role == "user":
                        messages.append(HumanMessage(content=content))
                    elif role == "ai":
                        messages.append(AIMessage(content=content))
        except Exception as e:
            logging.error(f"获取对话历史失败，返回空历史: {e}")
        return messages


    # 保存对话历史 (langgraph的checkpointer)
    def graph_persist_memory(self, user_id: str, session_id: str, last_turn_msg: list[dict]):
        messages = []
        for msg in last_turn_msg:
            if msg.get('type', '') == "human":
                messages.append({"role": "user", "content": msg.get('content','')})
            elif msg.get('type', '')== "ai":
                messages.append({"role": "ai", "content": msg.get('content','')})
        if len(messages) > 0:
            data = {
                "messages": messages,
            }
            try:
                self.persistor.save(user_id, session_id, data)
            except Exception as e:
                logging.error(f'保存对话历史失败:{e}')
    