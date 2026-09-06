# [总功能] 个人知识库问答系统：产品功能边界与模块划分

> 本 Issue 是项目总功能文档：概述产品边界与模块划分。子 Issue 逐个挂在本文评论区/任务清单下。
> 设计文档链接请把 `One-29` 替换为你的 GitHub 用户名。

## 产品一句话

一个自托管的个人知识库：把 Markdown 笔记喂给它，用自然语言提问，每个回答都附原文出处可核对；知识库覆盖不了的问题，它明说不知道，不编造。（定位细节见 [01-requirements §1](https://github.com/One-29/personal-knowledge-base/blob/main/docs/design/01-requirements.md)）

## 功能边界

**本期包含**：多知识库管理、文档导入（Markdown/纯文本，异步处理 + 状态可见）、库内问答（带溯源引用、可跳原文）、显式拒答、会话追问、Agent 多步工作流、简洁前端。

**本期不做**：PDF/Word 解析、多用户与访问控制、在线编辑器、语义图谱（Graph RAG）、问答历史持久化——每项的演进版本见 [01 §2.3](https://github.com/One-29/personal-knowledge-base/blob/main/docs/design/01-requirements.md)。

## 模块划分（对应子 Issue 开发顺序）

- [ ] **M1 知识库与文档管理**（9 月）：库 CRUD、文档导入/列表/删除/重传、文档状态机、原文存储 → [设计](https://github.com/One-29/personal-knowledge-base/blob/main/docs/design/02-modules.md)
- [ ] **M2 入库管线**（10 月上）：Markdown 结构切分、embedding、索引写入与清理（异步任务）
- [ ] **M3 问答·溯源·拒答**（10 月）：混合检索（向量+关键词，RRF 合并）、带引用生成、引用校验、两级拒答、会话追问
- [ ] **M4 Agent 多步工作流**（11 月）：综合任务拆步执行、缺料可见（复用 M3，不造第二个检索）
- [ ] **M5 前端**（12 月）：库/文档管理、问答与溯源高亮、任务进度展示

## 关键设计决策（详见各文档决策记录）

- 多知识库组织（kb_id 隔离与级联删除）· 导入「登记-异步-回报」三段式 + 文档状态机
- 数据：PostgreSQL + pgvector（1536 维）+ pg_trgm；全文 3 张表
- 检索：双通道 RRF；拒答两级判定（τ + LLM 自检）；引用越界即幻觉（生成后强制校验）

## 开发流程（本项目遵守）

1. 每个模块开发前填写 `docs/templates/module-design.md`（= 子 Issue 内容）
2. 子 Issue 挂载到本 Issue，标注依赖
3. 每个功能一个 PR、关联对应 Issue，粒度小
4. 收尾：CI/CD、README 更新、端到端可运行

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/README.md](https://github.com/One-29/personal-knowledge-base/blob/main/docs/README.md) | 文档体系与写作规范 |
| [docs/workflow.md](https://github.com/One-29/personal-knowledge-base/blob/main/docs/workflow.md) | 开发流程规范（S0–S6） |
| [01-requirements](https://github.com/One-29/personal-knowledge-base/blob/main/docs/design/01-requirements.md) | 产品需求 PRD v0.4（D1–D7） |
| [02-modules](https://github.com/One-29/personal-knowledge-base/blob/main/docs/design/02-modules.md) | 模块拆分与边界 v0.3 |
| [03-data-model](https://github.com/One-29/personal-knowledge-base/blob/main/docs/design/03-data-model.md) | 数据模型 v0.2（DM1–DM6） |
| [04-retrieval](https://github.com/One-29/personal-knowledge-base/blob/main/docs/design/04-retrieval.md) | 检索链路 v0.2（DR1–DR6） |
| [module-design](https://github.com/One-29/personal-knowledge-base/blob/main/docs/templates/module-design.md) | 子 Issue 模板（含 M3 填表示例） |
