# memory/store.py
import os, json, time
from memory.memory_persistor import MemoryPersistor
from utils.static import MEMORY_STORE_PATH_JSON

"""
简单 JSON 持久化
* 仅用于dev测试
"""

class MemoryPersistorJSON(MemoryPersistor):

    # 持久化历史对话记录
    def save(self, user_id: str, session_id: str, data: dict):
        all_data = {}
        if os.path.exists(MEMORY_STORE_PATH_JSON):
            with open(MEMORY_STORE_PATH_JSON, "r", encoding="utf-8") as f:
                try:
                    all_data = json.load(f)
                except json.JSONDecodeError:
                    all_data = {}
        if (user_id not in all_data):
            all_data[user_id] = {}
        all_data[user_id][session_id] = {
            "timestamp": time.time(),
            "data": data
        }
        with open(MEMORY_STORE_PATH_JSON, "w", encoding="utf-8") as f:
            json.dump(all_data, f, ensure_ascii=False, indent=2)

    # 从持久化存储读取历史对话记录
    def load(self, user_id: str, session_id):
        if not os.path.exists(MEMORY_STORE_PATH_JSON):
            return None
        with open(MEMORY_STORE_PATH_JSON, "r", encoding="utf-8") as f:
            try:
                all_data = json.load(f)
            except json.JSONDecodeError:
                return None
        return all_data.get(user_id, {}).get(session_id, {}).get("data")