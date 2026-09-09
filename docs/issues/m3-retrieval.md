# [M3] 问答·溯源·拒答：混合检索 / 带引用生成 / 拒答判定

> 本文件 = M3 子 Issue 内容（按 `docs/templates/module-design.md` 填写）。
> 依据：01 §3.2（US-M3-01~06）· 02 §3（M3 边界）· 03 §3（chunks 与偏移锚点）· 04 §4~§7（检索链路全链路设计）。

## 1. 功能定位与上下游

让用户在指定知识库（或全库）范围内提问，得到逐句标注 `[n]` 引用、可溯源核对、覆盖不足时明确拒答的回答；支持同会话追问（指代句改写后重检索）。

- 上游依赖：`M2 入库管线`（chunks + 向量索引 + pg_trgm 文本）、`M1 知识库与文档管理`（原文与库元数据）
- 下游被依赖：`M4 Agent 工作流`（复用本模块的检索问答能力做单步执行）

## 2. 数据原型（伪代码）

```python
class Answer:
    question: str
    content: str                  # 生成回答（Markdown，含 [n] 标注）
    session_id: str               # 会话 id：追问携带，用于查询改写
    citations: list[Citation]     # 与 content 中 [n] 一一对应
    refused: bool                 # 拒答是正常业务结果，非错误
    refusal_reason: str | None    # 低相关度 / 置信自检不过 / 库为空

class Citation:
    doc_id: int
    chunk_id: int
    chunk_text: str               # 溯源展示：命中块原文
    score: float                  # 相关度分数（供阈值判定与前端展示）
    index: int                    # 在 content 中的 [n] 序号
```

## 3. 对外接口（伪代码）

```text
POST /api/v1/ask
  请求体: {"question": str, "kb_id": int | "all", "session_id": str?}
  200: Answer                     # 含 refused=true 的拒答场景（正常结果）
  400: 空问题 / 知识库为空
  404: kb_id 指定的库不存在
  422: 参数校验失败
```

```text
GET /api/v1/citations/{citation_id}
  200: {doc_id, doc_title, chunk_text, char_start, char_end}
  404: 引用不存在（块已被重传替换）
```

## 4. 模块边界对照表（唯一权威）

| 属于本模块 | 不属于（归属模块） |
|---|---|
| 库范围校验、双通道检索触发（向量 + pg_trgm） | 切分与向量化（M2，入库时已完成） |
| 候选排序、RRF 合并、阈值 τ 判定 | 库/文档 CRUD 与原文存储（M1） |
| LLM 生成、引用标注与**引用越界校验**（防幻觉） | 任务拆解与跨步调度（M4） |
| 拒答文案、会话与查询改写 | 前端渲染与跳转交互（M5） |

## 5. 验收清单（PR 合并条件）

- [ ] 向量检索：cosine top-20，按 kb_id 过滤（US-M3-01）
- [ ] 关键词通道：pg_trgm top-10（DR4）
- [ ] RRF 合并排序（DR3）
- [ ] 拒答两级：τ 阈值（DR6）+ 生成后 LLM 自检；`refused=true` 走 200
- [ ] 引用校验：越界 `[n]` 被拦截（04 §5 纯规则，必执行）
- [ ] 溯源端点：citation → 原文段落（char_start/char_end 高亮）
- [ ] 会话追问：指代句改写后重检索（DR5），失败退化上轮问题
- [ ] 测试：正常问答 / 多文档合并引用 / 低相关度拒答 / 库外问题拒答 / 追问衔接 / 伪造越界引用被拦截
- [ ] Swagger 手工验证 ask / citations 全流程
- [ ] PR 关联本 Issue，粒度小

---

## 台账（M3）

| 日期 | 事项 | Issue | PR |
|---|---|---|---|
| 2026-09-09 | 子 Issue 创建（开发前） | 本 Issue | — |
