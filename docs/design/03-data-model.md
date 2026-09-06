# 03-数据模型（v0.2 草案）

| 字段 | 内容 |
|---|---|
| 状态 | 草案（本层决策 DM1–DM6 已拍板） |
| 版本 | v0.2 |
| 日期 | 2026-09-06 |
| 上游 | `01-requirements.md`（PRD v0.4，决策 D1–D7）· `02-modules.md`（v0.3，模块边界） |
| 变更 | v0.2：DM1–DM6 拍板并入 §6 决策记录 |
| 关联 | M1/M2 子 Issue（建仓后建立） |

> 本文回答：需求落成哪几张表、字段与约束怎么定、存储与索引选型、如何映射到 SQLAlchemy。
> 本文不写：检索算法与切分策略（`04-retrieval.md`）、API 契约（模块开发时填模板）。

## 1. 设计依据

### 1.1 表 ↔ 模块 ↔ 决策映射

| 表 | 概念归属 | 支撑的决策/故事 |
|---|---|---|
| `knowledge_bases` | M1 | D2 多知识库；US-M1-01 |
| `documents` | M1（M2 写状态字段） | D2/D3/D6；US-M1-02~06 |
| `chunks` | M2 产物 | D3 全量重建；US-M3-03 溯源 |
| （无会话表） | M3 | D5：会话仅进程内存，V1.0 持久化时再增量建表，不预建 |

### 1.2 三条建模原则

1. **表服务于已拍板的语义，不为"将来可能用"建表**：会话持久化（V1.0）不预建表，演进时增量加。
2. **用户输入永不直接进文件路径**：原文文件名一律用 `doc_id`（防路径注入与重名），用户文件名只存 `title` 字段。
3. **导入绝不破坏可用性**：重传处理期间旧块保持可检索，成功才替换（细节见 §4 状态机）。

## 2. ER 图

> 图注：一张库含多篇文档，一篇文档产生多块；chunks 冗余持有 kb_id——
> 检索层按库过滤免 join documents（代价与理由见 DM3）。

```mermaid
erDiagram
    KNOWLEDGE_BASES ||--o{ DOCUMENTS : "1 库含 N 文档"
    DOCUMENTS ||--o{ CHUNKS : "1 文档产生 N 块"
    KNOWLEDGE_BASES ||--o{ CHUNKS : "冗余归属 检索免join"

    KNOWLEDGE_BASES {
        bigint id PK
        varchar name UK "库名唯一"
        varchar description
        timestamptz created_at
        timestamptz updated_at
    }
    DOCUMENTS {
        bigint id PK
        bigint kb_id FK "级联删除"
        varchar title "上传文件名 同库唯一"
        varchar file_path "相对存储根的原文路径 D6"
        char content_hash "sha256 重传幂等判定"
        varchar status "pending/processing/ready/failed"
        varchar last_error_code "失败分类码"
        text last_error_message
        integer char_count
        integer chunk_count "冗余 列表展示免聚合"
        timestamptz processed_at
        timestamptz created_at
        timestamptz updated_at
    }
    CHUNKS {
        bigint id PK
        bigint doc_id FK "级联删除"
        bigint kb_id FK "冗余归属 DM3"
        integer chunk_index "文档内顺序 同doc唯一"
        text content
        integer char_start "原文字符偏移 溯源高亮"
        integer char_end
        vector embedding "维度N 04定稿"
        timestamptz created_at
    }
```

## 3. 表设计（DDL 草案）

> 以下为可执行草稿：`embedding` 维度 1536 已定（04 §8 DR2，text-embedding-3-small）；
> HNSW 向量索引（cosine）随首个迁移版本与列一同落库。

```sql
-- 知识库：多库隔离的根实体（D2）
CREATE TABLE knowledge_bases (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        varchar(100) NOT NULL UNIQUE,     -- 库名唯一：防同名混淆
    description varchar(500),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- 文档：元数据 + 原文路径（D6 原文在文件系统，库内不存正文）
CREATE TABLE documents (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kb_id         bigint NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    title         varchar(255) NOT NULL,          -- 上传文件名（仅存元数据，不进路径）
    file_path     varchar(500) NOT NULL,          -- 相对存储根路径: {kb_id}/{doc_id}.md
    content_hash  char(64) NOT NULL,              -- sha256(原文)：重传内容未变则跳过重建
    status        varchar(16) NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','processing','ready','failed')),
    last_error_code    varchar(32),               -- UNSUPPORTED_FORMAT/EMPTY_CONTENT/TOO_LARGE/PARSE_FAILED/EMBED_FAILED
    last_error_message text,                      -- 人类可读原因（US-M1-06）
    char_count    integer NOT NULL DEFAULT 0,     -- 原文字符数（按字符，非字节）
    chunk_count   integer NOT NULL DEFAULT 0,     -- 冗余：列表展示免聚合；M2 维护（见 DM 说明）
    processed_at  timestamptz,                    -- 最近一次处理落定时间
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kb_id, title)                         -- 同库同文件名 = 重传对象（US-M1-04 语义落点）
);
CREATE INDEX idx_documents_kb ON documents (kb_id);

-- 切块：检索单元 + 溯源锚点（M2 的唯一产物表）
CREATE TABLE chunks (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    doc_id      bigint NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kb_id       bigint NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    chunk_index integer NOT NULL,                 -- 文档内顺序，跨文档重建时全删全插
    content     text NOT NULL,
    char_start  integer NOT NULL,                 -- 原文内字符区间 [start,end)：溯源跳转高亮用
    char_end    integer NOT NULL,
    embedding   vector(1536) NOT NULL,            -- 维度已定：1536（04 §8 DR2）
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (doc_id, chunk_index)
);
CREATE INDEX idx_chunks_doc ON chunks (doc_id);
-- 向量索引（HNSW / cosine）待 04 定稿 embedding 模型后随迁移创建
```

### 原文文件目录约定（D6）

```text
storage/                      # 根路径可配置（如 ./data/storage）
└── {kb_id}/
    └── {doc_id}.md           # 文件名 = doc_id，不信任用户文件名（§1.2 原则 2）
```

删除语义配合：删文档 = 事务删行（chunks 随 FK 级联）+ 删单个原文文件；删库 = 事务删 `documents`/`chunks` + 删整个 `{kb_id}/` 目录。**数据库级联与文件删除不同事务**——文件删除失败只记日志（成为无引用孤儿文件，无数据一致性影响），编排细节归 M1（02 §3）。

## 4. 文档状态机

> 图注：pending=已登记待处理；failed 的语义是「当前无任何可用内容」——
> 重传失败时若旧块仍有效则回滚为 ready 并记录 last_error（导入绝不破坏可用性，§1.2 原则 3）。

```mermaid
stateDiagram-v2
    [*] --> pending: 上传登记
    pending --> processing: 任务开始
    processing --> ready: 全部块成功
    processing --> failed: 失败 且无可用旧块
    failed --> processing: 用户重传
    ready --> processing: 重传 内容变更
    ready --> ready: 重传 内容未变(sha256 命中 幂等跳过)
    note right of processing
        重传期间旧块保持可检索
        成功事务: 删旧块+插新块
        失败: 旧块保留 回滚 ready
    end note
```

## 5. SQLAlchemy 映射骨架（2.x 声明式）

> 与 §3 DDL 一一对应，是 `app/models.py` 的底稿（M1 骨架阶段落成完整版）。
> 教学注：`mapped_column` 是 2.x 的声明写法，`server_default` 让默认值由数据库生成而非应用。

```python
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Identity, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    documents: Mapped[list["Document"]] = relationship(back_populates="kb")

class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("kb_id", "title"),
        CheckConstraint("status IN ('pending','processing','ready','failed')",
                        name="ck_documents_status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending",
        server_default="pending")
    last_error_code: Mapped[str | None] = mapped_column(String(32))
    char_count: Mapped[int] = mapped_column(default=0, server_default="0")
    kb: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="doc", cascade="all, delete-orphan")

class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    chunk_index: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int]
    char_end: Mapped[int]
    doc: Mapped[Document] = relationship(back_populates="chunks")
```

## 6. 决策记录（2026-09-06 拍板）

| 编号 | 决策（已拍板） | 备选与代价 | 理由 |
|---|---|---|---|
| DM1 | 主键 `bigint IDENTITY` | UUID：分布式/防枚举，URL 冗长 | 单用户自托管无分布式需求；自增可读、索引紧凑 |
| DM2 | 向量存储 pgvector（HNSW/cosine） | 独立向量库（Qdrant/Chroma）：多一套部署与同步；FAISS 文件：无事务 | 单库单事务：向量与元数据一致备份；部署只多一个扩展 |
| DM3 | chunks 冗余 `kb_id` | 不冗余：检索每次 join documents | 高频的按库过滤免 join；删除由 FK 级联兜底。代价：V1.0 支持跨库移动文档时需级联更新冗余列（已记录演进条件） |
| DM4 | 重传语义 = `title` 文件名 + `UNIQUE(kb_id,title)` + sha256 | 独立版本表：正确但 MVP 过重 | 同库同名即重传对象（US-M1-04）；哈希命中内容未变则幂等跳过 |
| DM5 | 导入失败保旧：无旧块→failed；有旧块→回滚 ready + `last_error` | 先删后建：失败即丢内容 | 导入绝不破坏可用性（§1.2 原则 3） |
| DM6 | 状态承载 `varchar` + CHECK | PG 原生 enum：演进枚举值需迁移 | 改枚举值免数据库迁移；SQLAlchemy 侧同字段字符串语义简单 |

## 7. 移交 04 的待定清单（不影响本层表结构的主体）

| 待定项 | 影响 |
|---|---|
| ~~Embedding 模型与维度 N~~ | **已定**：1536 / text-embedding-3-small（04 §8 DR2）；HNSW 索引语句随首个迁移落库 |
| 关键词检索承载 | 已定 pg_trgm（04 DR4）→ **无需新增 tsvector 列**；若未来升级 zhparser 再做迁移 |
| 检索阈值 / top-k | 纯参数，无新列 |
| 切分策略粒度 | 决定 chunk 实际大小分布，无新列 |

## 8. 下一步衔接

- 本层决策已全部拍板；根 README 技术栈同步：pgvector 由"评估中"转"已定"。
- M1 骨架开发前填写 `templates/module-design.md`：数据原型节直接引用本文 DDL 的 `knowledge_bases`/`documents`；本文件 DDL 草案是首个 Alembic 迁移的底稿（`embedding` 维度经 04 确认后落库）。
