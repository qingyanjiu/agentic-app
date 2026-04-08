from typing import Type, TypeVar, Generic, Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import select, update as sqlalchemy_update, delete as sqlalchemy_delete

ModelType = TypeVar("ModelType")


class CRUDBase(Generic[ModelType]):
    def __init__(self, model: Type[ModelType]):
        self.model = model

    def create(self, db: Session, obj_in: Dict[str, Any]) -> ModelType:
        """
        新增一条记录
        """
        db_obj = self.model(**obj_in)
        db.add(db_obj)
        db.flush()       # 让主键立即可用
        db.refresh(db_obj)
        return db_obj

    def get_by_id(self, db: Session, id: Any) -> Optional[ModelType]:
        """
        根据主键查询
        """
        stmt = select(self.model).where(self.model.id == id)
        return db.execute(stmt).scalars().first()

    def get_one_by_filters(self, db: Session, **filters) -> Optional[ModelType]:
        """
        按条件查询单条
        例如：get_one_by_filters(db, email="a@test.com")
        """
        stmt = select(self.model)
        for key, value in filters.items():
            stmt = stmt.where(getattr(self.model, key) == value)
        return db.execute(stmt).scalars().first()

    def get_list(
        self,
        db: Session,
        filters: Optional[Dict[str, Any]] = None,
        skip: int = 0,
        limit: int = 100
    ) -> List[ModelType]:
        """
        查询列表
        """
        stmt = select(self.model)

        if filters:
            for key, value in filters.items():
                stmt = stmt.where(getattr(self.model, key) == value)

        stmt = stmt.offset(skip).limit(limit)

        return db.execute(stmt).scalars().all()

    def update_by_id(self, db: Session, id: Any, obj_in: Dict[str, Any]) -> Optional[ModelType]:
        """
        按 ID 更新
        """
        db_obj = self.get_by_id(db, id)
        if not db_obj:
            return None

        for key, value in obj_in.items():
            if hasattr(db_obj, key):
                setattr(db_obj, key, value)

        db.add(db_obj)
        db.flush()
        db.refresh(db_obj)
        return db_obj

    def delete_by_id(self, db: Session, id: Any) -> bool:
        """
        按 ID 删除
        """
        db_obj = self.get_by_id(db, id)
        if not db_obj:
            return False

        db.delete(db_obj)
        db.flush()
        return True

    def delete_by_filters(self, db: Session, **filters) -> int:
        """
        按条件删除，返回删除条数
        """
        stmt = sqlalchemy_delete(self.model)
        for key, value in filters.items():
            stmt = stmt.where(getattr(self.model, key) == value)

        result = db.execute(stmt)
        return result.rowcount or 0