# memory/store.py
from memory.memory_persistor import MemoryPersistor
import sqlite3
from contextlib import contextmanager
from utils.static import MEMORY_STORE_PATH_SQLITE
import uuid

"""
sqlite持久化
"""

'''
获取数据库连接，执行完sql后自动提交事务并关闭连接
'''
@contextmanager
def get_conn():
    conn = sqlite3.connect(MEMORY_STORE_PATH_SQLITE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

# 建表语句    
SQL_CREATE_TABLE = '''
-- t_chat_session 对话会话表
CREATE TABLE IF NOT EXISTS t_chat_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,       -- 自增主键
    title TEXT NOT NULL DEFAULT '新对话',                      -- 消息标题
    user_id VARCHAR(36) NOT NULL,                      -- 用户ID，区分用户
    session_id VARCHAR(36) NOT NULL,                   -- 会话ID，用于区分不同会话
    start_time TEXT DEFAULT (datetime('now','localtime')), -- 对话开始时间
    update_time TEXT DEFAULT (datetime('now','localtime')) -- 对话最近更新时间
);
-- 索引
CREATE INDEX IF NOT EXISTS idx_user_id_start_time
    ON t_chat_session(user_id, start_time);
CREATE INDEX IF NOT EXISTS idx_ts
    ON t_chat_session(start_time);

-- t_chat_content 对话内容表
CREATE TABLE IF NOT EXISTS t_chat_content (
    id INTEGER PRIMARY KEY AUTOINCREMENT,       -- 自增主键
    chat_session_id VARCHAR(36) NOT NULL,       -- 关联的会话表id
    role TEXT NOT NULL,                         -- user / assistant / system / tool
    content TEXT NOT NULL,                      -- 消息内容
    update_time TEXT DEFAULT (datetime('now','localtime')) -- 生成这条对话的时间
);
-- 索引
CREATE INDEX IF NOT EXISTS idx_chat_session_id
    ON t_chat_content(chat_session_id);
'''

# 查询某个用户的所有session
SQL_QUERY_USER_CHAT_SESSIONS = '''
SELECT * FROM t_chat_session where user_id=? order by start_time desc
'''

# 查询某个用户的session是否存在
SQL_QUERY_SESSION_EXISTS = '''
SELECT * FROM t_chat_session where user_id=? and session_id=?
'''

# 创建session
SQL_CREATE_CHAT_SESSION = '''
INSERT INTO t_chat_session (user_id, session_id) VALUES (?, ?)
'''

# 写入聊天内容
SQL_SAVE_CHAT_CONTENT = '''
INSERT INTO t_chat_content (chat_session_id, role, content) VALUES (?, ?, ?)
'''

# 更新session标题
SQL_UPDATE_CHAT_SESSION_TITLE = '''
UPDATE t_chat_session SET title=? where id=?
'''

# 查询某一条session的聊天数据列表
SQL_QUERY_CHAT_SESSION_CONTENT= '''
SELECT c.* FROM t_chat_session s
    LEFT JOIN t_chat_content c 
        ON s.id=c.chat_session_id
    WHERE s.user_id=? AND s.session_id=? 
    order by c.update_time asc
'''

class MemoryPersistorSqlite(MemoryPersistor):
    def __init__(self):
        self.init_db()

    # 持久化历史对话记录
    def save(self, user_id: str, session_id: str, data: dict):
        with get_conn() as conn:
            cursor = conn.cursor()
            # t_chat_session表的id，需要关联t_chat_content表数据
            cursor.execute(
                SQL_QUERY_SESSION_EXISTS,
                (user_id, session_id)
            )
            session_info = cursor.fetchall()
            t_session_id = ''
            # 如果查到已有session记录(应该只有一条)，直接保存content表
            if(len(session_info) == 1):
                # 从查到的session表记录中获取id
                t_session_id = session_info[0]['id']
            else:
                # 如果没有session，则创建记录
                cursor.execute(
                    SQL_CREATE_CHAT_SESSION,
                    (user_id, session_id)
                )
                # 获取刚插入的session表记录id
                t_session_id = cursor.lastrowid
            # 最后写入 content 表（消息只取最后两条，因为history是所有的对话，咱们只需要拿最后两条 一条 human 一条 ai，增量持久化）
            # 判断一下t_session_id不应该为空，防止出现脏数据
            if(t_session_id):
                for d in data['messages'][-2:]:
                    cursor.execute(
                        SQL_SAVE_CHAT_CONTENT,
                        (t_session_id, d['role'], d['content'])
                    )

    # 从持久化存储读取历史对话记录
    def load(self, user_id: str, session_id):
         with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                SQL_QUERY_CHAT_SESSION_CONTENT,
                (user_id, session_id)
            )
            t_chat_rows = cursor.fetchall()
            t_chat_dict = [dict(row) for row in t_chat_rows]
            return {"messages": t_chat_dict}
    
    # 初始化数据表
    def init_db(self):
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.executescript(SQL_CREATE_TABLE)