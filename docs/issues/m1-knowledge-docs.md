# [M1] 知识库与文档管理：多知识库 CRUD、文档导入管理与状态机

> 本文件 = M1 子 Issue 内容（按 `docs/templates/module-design.md` 填写）。
> 依据：01 §3.1（US-M1-01~06）· 02 §3（M1 边界）· 03 §3/§4（DDL 与状态机）。

---

## 1. 功能定位与上下游

让用户建立多个知识库（如按课程分库），把 Markdown/纯文本笔记导入指定库，随时查看文档与处理状态，支持删除与重传覆盖（US-M1-01~06）。

- 上游依赖：无（本模块为数据入口；`M2 入库管线` 将在 M1 之后接入：M1 登记后触发 `M2.process(doc_id)`，状态由 M2 写入）
- 下游被依赖：`M3 问答`（溯源取原文）、`M2 入库管线`（读写文档登记与状态）、`M5 前端`

## 2. 数据原型（伪代码）

```python
# M1 对外对象（复用 03 §3 DDL：knowledge_bases / documents 两表）
class DocStatus(str, Enum):
    PENDING = "pending"      # 已登记待处理
    PROCESSING = "processing"  # 处理中
    READY = "ready"          # 可检索
    FAILED = "failed"        # 处理失败且无可用内容

class KBOut:                 # 知识库视图
    id: int
    name: str                # UNIQUE：重名创建 → 409
    description: str | None
    doc_count: int           # 冗余展示（实时 count 聚合）
    created_at: datetime

class DocumentOut:           # 文档视图
    id: int
    kb_id: int
    title: str               # = 上传文件名（同库唯一）
    status: DocStatus
    char_count: int
    chunk_count: int         # M2 写入；M1 只读展示
    last_error_code: str | None   # UNSUPPORTED_FORMAT/EMPTY_CONTENT/TOO_LARGE/PARSE_FAILED/EMBED_FAILED
    last_error_message: str | None
    processed_at: datetime | None
    created_at: datetime
    updated_at: datetime

class UploadResult:
    document: DocumentOut
    content_changed: bool    # False = sha256 命中，幂等跳过（未重建）
```

## 3. 对外接口（伪代码）

```text
# 知识库
POST /api/v1/kbs
  请求体: {"name": str(1..100), "description"?: str}
  201: KBOut
  409: 库名已存在（UNIQUE）
  422: 参数校验失败

GET /api/v1/kbs
  200: [KBOut]           # 按创建时间倒序

GET /api/v1/kbs/{kb_id}
  200: KBOut
  404: 库不存在

DELETE /api/v1/kbs/{kb_id}
  204: 删除成功（编排三步：删元数据→删原文文件→调 M2 清理块与向量）
  404: 库不存在
```

```text
# 文档（归属指定库）
POST /api/v1/kbs/{kb_id}/documents        # multipart 上传，filename=title
  201: UploadResult（登记即返回，处理异步；文件扩展名校验 .md/.txt）
  404: 库不存在
  409: 同库同名已存在（指引客户端调 reupload 显式覆盖——覆盖是重操作，不隐式触发）
  400: EMPTY_CONTENT（空文件）
  413: TOO_LARGE（> 10MB）
  422: UNSUPPORTED_FORMAT（非 .md/.txt）

GET /api/v1/kbs/{kb_id}/documents?status=&title=
  200: [DocumentOut]      # 按更新时间倒序；支持状态/标题过滤

GET /api/v1/documents/{doc_id}
  200: DocumentOut（含最新状态与错误信息）
  404: 文档不存在

GET /api/v1/documents/{doc_id}/content
  200: {title, content: str}   # 原文（供前端查看与溯源高亮；经 file_path 读取）
  404: 文档不存在 / 原文文件缺失（错误语义明确，见 §4 故障域）

POST /api/v1/documents/{doc_id}/reupload  # multipart：重传覆盖（US-M1-04）
  200: UploadResult（sha256 未变 → content_changed=False，状态不动；变了 → 重建）
  404 / 400 / 413 / 422 同上

DELETE /api/v1/documents/{doc_id}
  204: 删除成功（事务删行 + 删原文文件；chunks 由 FK 级联，向量由 M2 清理）
  404: 文档不存在
```

## 4. 模块边界对照表（唯一权威）

| 属于本模块 | 不属于（归属模块） |
|---|---|
| 知识库 CRUD 与级联删除编排（三步，02 §3） | 切分、向量化、索引写入（M2） |
| 文档登记/列表/查看/删除/重传接口 | 处理任务执行与状态机推进（M2 写状态，M1 只读展示） |
| 上传现场校验：扩展名、空内容、大小 | 解析与 embedding 期失败（PARSE_FAILED/EMBED_FAILED 由 M2 写入 last_error） |
| 原文文件存储：路径管理、读取、删除（D6） | 检索、回答、拒答（M3） |
| 文档状态/统计对外暴露 | 前端渲染（M5） |

**故障域声明**：原文文件删除/读取失败属于 M1（文件是 M1 的资源）；处理期失败属于 M2 并落到 `documents.last_error_*`，M1 只负责展示。

## 5. 验收清单（对应 PR 合并条件）

骨架 PR（本子 Issue 第一阶段）验收：

- [ ] 库：创建（重名 409）/列表/详情/删除（204；删除后该库文档不可见，原文文件已删）
- [ ] 上传：`.md`/`.txt` 登记成功（status=pending 或直转 processing）；非法扩展名 → UNSUPPORTED_FORMAT；空文件 → EMPTY_CONTENT；>10MB → 413
- [ ] 文档：列表（状态/标题过滤）/详情/查看原文（内容与上传一致）/删除（204 后不可见）/重传（同内容幂等 content_changed=False；新内容触发重建流程）
- [ ] 状态机：pending→processing→ready/failed 流转可测（骨架阶段以服务层直调模拟 M2 写入，联调后由 M2 真实驱动）
- [ ] 测试：上述用例 pytest 覆盖；Swagger 手工走通
- [ ] 设计无漂移：伪代码 ↔ Pydantic 模型 ↔ 路由签名一致
- [ ] PR 关联本 Issue，提交粒度小

联调期补充验收（M2 落地后）：真实上传 → ready → 列表块数可见 → 重传替换 → 删除后检索不可见。

---

## 台账（M1）

| 日期 | 事项 | Issue | PR |
|---|---|---|---|
| — | 子 Issue 创建 | — | — |
