from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from .. import crud, storage
from ..db import get_db
from ..models import KnowledgeBase
from ..schemas import KnowledgeBaseCreate, KnowledgeBaseOut

# 模块只声明业务路径；版本前缀在 main.py 装配（你的方案，单点修改）
router = APIRouter(prefix="/kbs", tags=["Knowledge Bases"])


def _kb_out(kb: KnowledgeBase) -> KnowledgeBaseOut:
    """ORM → 响应模型：在会话存活时显式组装（防懒加载时序坑）。"""
    return KnowledgeBaseOut(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        doc_count=len(kb.documents),   # 技术债：N+1，规模上来后改 selectinload
        created_at=kb.created_at,
    )


@router.post("", response_model=KnowledgeBaseOut, status_code=201)
def create_kb(data: KnowledgeBaseCreate, db: Session = Depends(get_db)):
    if crud.get_kb_by_name(db, data.name):        # 重名检查：数据层先查
        raise HTTPException(status_code=409, detail="知识库名称已存在")
    return _kb_out(crud.create_kb(db, data))


@router.get("", response_model=list[KnowledgeBaseOut])
def list_kbs(db: Session = Depends(get_db)):
    return [_kb_out(kb) for kb in crud.list_kbs(db)]


@router.get("/{kb_id}", response_model=KnowledgeBaseOut)
def get_kb(kb_id: int, db: Session = Depends(get_db)):
    kb = crud.get_kb(db, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return _kb_out(kb)


@router.delete("/{kb_id}", status_code=204)
def delete_kb(kb_id: int, db: Session = Depends(get_db)):
    """删除知识库（02 §3 编排：删元数据 → 删原文目录 → 块随外键级联）。"""
    if not crud.delete_kb(db, kb_id):
        raise HTTPException(status_code=404, detail="知识库不存在")
    storage.delete_kb_dir(kb_id)
    return Response(status_code=204)
