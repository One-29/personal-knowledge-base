from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from .models import KnowledgeBase, Document
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

def create_document(
    db: Session,
    kb_id: int,
    title: str,
    file_path: str,
    content_hash: str,
    char_count: int,
) -> Document:
    """登记新文档（status 由 default 落 pending；文件已由调用方写入 storage）"""
    db_doc = Document(kb_id=kb_id, title=title, file_path=file_path,
                      content_hash=content_hash, char_count=char_count)
    db.add(db_doc)
    db.commit()
    db.refresh(db_doc)
    return db_doc

def list_documents(
    db: Session,
    kb_id: int,
    status: str | None = None,
    title: str | None = None,
) -> list[Document]:
    """某库全部文档（可选状态/标题过滤），按创建时间倒序。"""
    stmt = select(Document).where(Document.kb_id == kb_id)
    if status is not None:
        stmt = stmt.where(Document.status == status)
    if title is not None:
        stmt = stmt.where(Document.title.ilike(f"%{title}%"))
    stmt = stmt.order_by(desc(Document.created_at))
    return list(db.scalars(stmt).all())

def get_document(db: Session, doc_id: int) -> Document | None:
    return db.get(Document, doc_id)


def get_document_by_title(db: Session, kb_id: int, title: str) -> Document | None:
    """同库同名查询：上传查重（409）与重传定位用（UNIQUE(kb_id,title) 语义）。"""
    stmt = select(Document).where(Document.kb_id == kb_id, Document.title == title)
    return db.scalars(stmt).first()


def delete_document(db: Session, doc_id: int) -> bool:
    doc = db.get(Document, doc_id)
    if doc is None:
        return False
    db.delete(doc)
    db.commit()
    return True
