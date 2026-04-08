# memory/store.py
from sqlalchemy.orm import Session

from memory.memory_persistor import MemoryPersistor
from db.database import db
from sqlalchemy import text


"""
mysql持久化
"""

# 建表语句    
SQL_CREATE_TABLE = '''
-- aiagent_chat_session 对话会话表
CREATE TABLE IF NOT EXISTS aiagenaiagent_chat_session (
    id INTEGER PRIMARY KEY auto_increment comment '自增主键',
    title varchar(100) NOT NULL DEFAULT '新对话' comment '消息标题',
    user_id VARCHAR(36) NOT NULL comment '用户ID，区分用户',
    session_id VARCHAR(36) NOT NULL comment '会话ID,用于区分不同会话',
    start_time datetime default now() comment '对话开始时间',
    update_time datetime default now() comment '对话最近更新时间'
);
-- 索引
CREATE INDEX idx_user_id_start_time
    ON aiagenaiagent_chat_session(user_id, start_time);
CREATE INDEX idx_ts
    ON aiagenaiagent_chat_session(start_time);

-- aiagent_chat_content 对话内容表
CREATE TABLE IF NOT EXISTS aiagenaiagent_chat_content (
    id INTEGER PRIMARY KEY auto_increment comment '自增主键',
    chat_session_id VARCHAR(36) NOT NULL comment '关联的会话表id',
    role TEXT NOT NULL comment 'ai 角色 - user / assistant / system / tool',
    content TEXT NOT NULL comment '消息内容',
    update_time datetime default now() comment '生成这条对话的时间'
);
-- 索引
CREATE INDEX idx_chat_session_id
    ON aiagenaiagent_chat_content(chat_session_id);
'''

# 查询某个用户的所有session
SQL_QUERY_USER_CHAT_SESSIONS = text('''
SELECT * FROM aiagent_chat_session where user_id=:user_id order by start_time desc
''')

# 查询某个用户的session是否存在
SQL_QUERY_SESSION_EXISTS = text('''
SELECT * FROM aiagent_chat_session where user_id=:user_id and session_id=:session_id
''')

# 创建session
SQL_CREATE_CHAT_SESSION = text('''
INSERT INTO aiagent_chat_session (user_id, session_id) VALUES (:user_id, :session_id)
''')

# 写入聊天内容
SQL_SAVE_CHAT_CONTENT = text('''
INSERT INTO aiagent_chat_content (chat_session_id, role, content) VALUES (:chat_session_id, :role, :content)
''')

# 更新session标题
SQL_UPDATE_CHAT_SESSION_TITLE = text('''
UPDATE aiagent_chat_session SET title=:title where id=:id
''')

# 查询某一条session的聊天数据列表
SQL_QUERY_CHAT_SESSION_CONTENT= text('''
SELECT c.* FROM aiagent_chat_session s
    LEFT JOIN aiagent_chat_content c 
        ON s.id=c.chat_session_id
    WHERE s.user_id=:user_id AND s.session_id=:session_id
    order by c.update_time asc
''')

class MemoryPersistorMysql(MemoryPersistor):
    # def __init__(self):

    # 持久化历史对话记录
    def save(self, user_id: str, session_id: str, data: dict):
        with db.get_session() as _con:
            con: Session = _con
            # 1. 查询 session 是否存在
            result = con.execute(
                SQL_QUERY_SESSION_EXISTS,
                {
                    "user_id": user_id,
                    "session_id": session_id
                }
            )

            session_info = result.mappings().all()
            t_session_id = None

            # 2. 如果已有 session 记录
            if len(session_info) == 1:
                t_session_id = session_info[0]["id"]

            else:
                # 3. 如果没有 session，则创建记录
                insert_result = con.execute(
                    SQL_CREATE_CHAT_SESSION,
                    {
                        "user_id": user_id,
                        "session_id": session_id
                    }
                )

                # 获取刚插入的 id
                t_session_id = insert_result.lastrowid

            # 4. 写入 content 表（只保存最后两条）
            if t_session_id:
                for d in data.get("messages", [])[-2:]:
                    con.execute(
                        SQL_SAVE_CHAT_CONTENT,
                        {
                            "chat_session_id": t_session_id,
                            "role": d["role"],
                            "content": d["content"]
                        }
                    )


    # 从持久化存储读取历史对话记录
    def load(self, user_id: str, session_id):
        with db.get_session() as _con:
            con: Session = _con
            result = con.execute(
                SQL_QUERY_CHAT_SESSION_CONTENT,
                {
                    "user_id": user_id,
                    "session_id": session_id
                }
            )
            t_chat_dict = [dict(row) for row in result.mappings().all()]
            return {"messages": t_chat_dict}
