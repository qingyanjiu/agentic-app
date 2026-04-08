from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from utils.static import MEMORY_STORE_URL_MYSQL

# 你的 MySQL 配置

DATABASE_URL = (
    MEMORY_STORE_URL_MYSQL
)


class Database:
    _instance = None
    _engine = None
    _SessionLocal = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Database, cls).__new__(cls)

            # 创建全局唯一 engine（单例）
            cls._engine = create_engine(
                DATABASE_URL,
                pool_size=10,          # 连接池大小
                max_overflow=20,       # 最大溢出连接数
                pool_pre_ping=True,    # 自动检测断连
                pool_recycle=3600,     # 连接回收，避免 MySQL 断开
                echo=False             # 调试时可改 True
            )

            # 创建全局唯一 Session 工厂
            cls._SessionLocal = sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=cls._engine
            )

        return cls._instance

    @property
    def engine(self):
        return self._engine

    @property
    def SessionLocal(self):
        return self._SessionLocal

    @contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        """
        上下文方式获取 session
        自动提交/回滚/关闭
        """
        session = self._SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


# 全局单例实例
db = Database()