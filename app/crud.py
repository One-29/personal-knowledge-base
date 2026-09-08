from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from .models import KnowledgeBase
from .schemas import KnowledgeBaseCreate


def create_kb(db: Session, data: KnowledgeBaseCreate) -> KnowledgeBase:
    db_kb = KnowledgeBase(
        name=data.name,
        description=data.description,
    )
    db.add(db_kb)
    db.commit()
    db.refresh(db_kb)
    return db_kb


def list_kbs(db: Session) -> list[KnowledgeBase]:
    stmt = select(KnowledgeBase).order_by(desc(KnowledgeBase.created_at))
    return list(db.scalars(stmt).all())


def get_kb(db: Session, kb_id: int) -> KnowledgeBase | None:
    return db.get(KnowledgeBase, kb_id)

def get_kb_by_name(db: Session, name: str) -> KnowledgeBase | None:
    stmt = select(KnowledgeBase).where(KnowledgeBase.name == name)
    return db.scalars(stmt).first()

def delete_kb(db: Session, kb_id: int) -> bool:
    db_kb = db.get(KnowledgeBase, kb_id)
    if db_kb is None:
        return False
    db.delete(db_kb)
    db.commit()
    return True
