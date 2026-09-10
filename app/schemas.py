from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class DocStatus(str, Enum):
    """文档处理状态（与 03 §3 DDL 的 CHECK 约束同源）。

    API 层用枚举做查询参数校验（非法值 → 422）；
    ORM 层仍存 str（DM6：数据库以 varchar + CHECK 为唯一约束）。
    """

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, min_length=1, max_length=500)


class KnowledgeBaseOut(BaseModel):
    id: int
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    doc_count: int = Field(default=0)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentOut(BaseModel):
    """文档视图：列表/详情展示 + 状态机可见（不暴露 file_path/content_hash）。"""

    id: int
    kb_id: int
    title: str
    status: str                      # pending/processing/ready/failed
    char_count: int = 0
    chunk_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    processed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentContentOut(BaseModel):
    """原文响应：查看与溯源高亮用（US-M1-03、US-M3-03）。"""

    title: str
    content: str


class UploadResult(BaseModel):
    """上传/重传响应（US-M1-02、US-M1-04）。

    content_changed 表达「这次请求是否真正改变了库内容」：
    新建 = True；重传且内容变化 = True；重传但 sha256 未变 = False（幂等跳过）。
    """

    document: DocumentOut
    content_changed: bool


class TurnIn(BaseModel):
    """一轮历史问答（前端持久化后随请求回传，用于追问改写）。"""

    question: str = Field(max_length=500)
    answer: str = Field(max_length=4000)


class AskRequest(BaseModel):
    """问答请求（M3 契约 §3）。kb_id 为 None 表示全库检索。

    对话历史由前端持久化并回传（history）：服务端保持无状态——
    刷新页面或重启服务都不会丢上下文，会话体验无需落库。
    """

    question: str = Field(min_length=1, max_length=500)
    kb_id: int | None = None
    session_id: str | None = None
    history: list[TurnIn] | None = Field(default=None, max_length=10)


class CitationOut(BaseModel):
    """引用：回答中 [n] 对应的来源块（溯源展示用）。"""

    index: int
    chunk_id: int
    doc_id: int
    doc_title: str
    chunk_text: str
    char_start: int
    char_end: int


class AnswerOut(BaseModel):
    """问答响应：拒答也是 200 + refused=true 的正常业务结果。"""

    question: str
    content: str
    session_id: str | None = None
    search_query: str | None = None    # 实际检索用语（有会话追问时可能被改写）
    citations: list[CitationOut] = []
    refused: bool = False
    refusal_reason: str | None = None


class CitationDetailOut(BaseModel):
    """溯源端点响应：引用 → 原文定位信息。"""

    doc_id: int
    doc_title: str
    chunk_text: str
    char_start: int
    char_end: int


class WorkflowRequest(BaseModel):
    """多步任务请求（05 §4）。kb_id 缺省表示全库。"""

    task: str = Field(min_length=1, max_length=500)
    kb_id: int | None = None
    max_steps: int | None = Field(default=None, ge=1, le=10)


class WorkflowStepOut(BaseModel):
    """单个子步骤的执行结果（进度对用户可见，US-M4-01）。"""

    index: int
    goal: str
    query: str
    status: str                        # answered / insufficient / error
    conclusion: str | None = None      # answered 时的结论
    note: str | None = None            # 缺料或故障说明
    citations: list[CitationOut] = []


class WorkflowResultOut(BaseModel):
    """多步任务结果：分步结论 + 全局统一编号的引用（AW4）。"""

    task: str
    steps: list[WorkflowStepOut]
    answer: str
    citations: list[CitationOut] = []


class GraphNodeOut(BaseModel):
    """关联图节点（= 一篇文档）。"""

    doc_id: int
    title: str
    chunks: int
    chars: int


class GraphEdgeOut(BaseModel):
    """关联图边（文档间的语义关联）。"""

    source: int
    target: int
    weight: float        # 归一化强度 0–1（线宽/透明度）
    links: int           # 跨文档近邻对数
    similarity: float    # 平均相似度


class GraphOut(BaseModel):
    """关联图响应（Obsidian graph view 的对应物）。"""

    nodes: list[GraphNodeOut] = []
    edges: list[GraphEdgeOut] = []
    truncated: bool = False   # 块数超过计算上限时为 True（结果不完整，诚实标注）
