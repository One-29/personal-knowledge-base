# [M2] 入库管线：切分 / 向量化 / 索引写入与清理

> 本文件 = M2 子 Issue 内容（按 `docs/templates/module-design.md` 填写）。
> 依据：01 §3.1（US-M1-02/04/06 的处理侧）· 02 §3（M2 边界）· 03 §3/§4（chunks 表与状态机）· 04 §2/§3（DR1 切分、DR2 embedding）。

## 1. 功能定位与上下游

把 M1 登记好的文档变成**可被检索的内容**：读原文 → Markdown 结构感知切分 → 批量向量化 → 事务内全量替换 chunks → 维护文档状态机。触发方是 M1 的上传/重传端点（登记即返回，处理异步）。

- 上游依赖：`M1 知识库与文档管理`（提供文档登记、原文文件、状态字段）
- 下游被依赖：`M3 问答`（检索 chunks 的向量与关键词索引）、`M1`（读取 chunk_count 展示）

## 2. 数据原型（伪代码）

```python
# 切分产物（内存对象，非 ORM）
class ChunkData:
    index: int          # 文档内顺序
    text: str           # 块文本
    char_start: int     # 原文字符区间（溯源锚点，与切分策略解耦）
    char_end: int

# 落库实体（03 §3 chunks 表；冗余 kb_id 为 DM3 决策）
class Chunk(Base):
    id, doc_id, kb_id, chunk_index, content, char_start, char_end
    embedding: Vector(1024)     # 维度由 settings.embedding_dimension 配置驱动
    created_at

# 对外配置（.env，04 DR2 边界规则：全项目模型唯一）
EMBEDDING_BASE_URL / EMBEDDING_MODEL / EMBEDDING_DIMENSION / EMBEDDING_API_KEY
CHUNK_MAX_CHARS=800 / CHUNK_OVERLAP_CHARS=80
```

## 3. 对外接口（伪代码）

```text
# 无 HTTP 端点——内部服务接口（由 M1 触发）
process_document(doc_id: int, db: Session | None = None) -> None
  行为: 读原文 → 切分 → 批量 embedding → 事务内 DELETE 旧块 + INSERT 新块
       → 更新 documents.chunk_count / status / processed_at
  成功: status=ready，chunk_count=N
  失败(DM5): 有旧块 → 回滚 status=ready + last_error_*；无旧块 → status=failed

# storage 层清理接口
delete_kb_dir(kb_id: int) -> None       # 删库时整目录清理（02 §3 第三步）
delete(rel_path: str) -> bool           # 删单文件 + 清空父目录
```

```text
# 状态机（03 §4）
pending → processing → ready
                    ↘ failed（仅当无可用旧块）
```

## 4. 模块边界对照表（唯一权威）

| 属于本模块 | 不属于（归属模块） |
|---|---|
| 文本切分策略与块边界（DR1 参数） | 上传接口、原文文件存储（M1） |
| 逐块向量化与批量调用（DR2） | 检索与问答（M3） |
| chunks 写入/清理（重传全量重建、删库清理） | 文档元数据 CRUD（M1） |
| 文档状态机推进与失败落库（DM5） | HTTP 语义与状态码（M1 路由层） |

## 5. 验收清单

- [x] 切分器单元测试 6 例（标题分段/偏移还原/重叠/空文本）
- [x] embedding provider 单元测试 4 例（空输入/顺序还原/错误/缺 key）
- [x] 管线测试 4 例（成功落库/重传替换/缺文件失败/失败保旧）
- [x] chunks 表 + vector(1024) + HNSW/cosine 索引迁移（81e964106c94、16f47d8d4982）
- [x] 上传/重传端点接 BackgroundTasks（D4）
- [x] **真实端到端验证**：Markdown → bge-m3 真实向量 → pgvector（status=ready、chunk_count=2、1024 维）
- [x] 原文文件生命周期（删库删目录、删文件清空目录）
- [x] 全量测试 38 passed

## 6. 关联 PR

| PR | 内容 | 状态 |
|---|---|---|
| #7 | 切分器 + embedding provider | 已合并 |
| #8 | chunks 表 + 向量索引 | 已合并 |
| #9 | 入库管线编排 + 后台触发 | 已合并 |
| #10 | 原文文件生命周期补齐 | 待合并 |

---

## 台账（M2）

| 日期 | 事项 | Issue | PR |
|---|---|---|---|
| 2026-09-09 | M2 全部完成 | 本 Issue | #7 #8 #9 #10 |
