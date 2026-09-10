# KnowBase · 个人知识库问答系统

[![CI](https://github.com/One-29/personal-knowledge-base/actions/workflows/ci.yml/badge.svg)](https://github.com/One-29/personal-knowledge-base/actions/workflows/ci.yml)

> 设计阶段（v0.1）· 首个可运行版本规划见 [路线图](#路线图)

**自托管的个人知识库：把 Markdown 笔记喂给它，用自然语言提问，每个回答都附原文出处；知识库覆盖不了的问题，它明说不知道，不编造。**

> 产品代号 KnowBase（仓库名建仓时定）。完整产品背景与决策见 [docs/design/01-requirements.md](docs/design/01-requirements.md)。

## ✨ 核心特性

- **多知识库组织**：笔记按主题分库（课程、项目、读书笔记…），导入与问答均可限定范围
- **语义问答 + 溯源**：回答逐句标注引用 `[1] [2]`，点击直达原文段落核对
- **显式拒答**：覆盖不足或相关度不足时明说「不知道」并给建议，杜绝编造
- **Agent 多步工作流**：跨文档归纳/对比等综合任务自动拆步执行，步骤进度与缺料可见
- **隐私自持**：文档与向量全部本地存储；云 LLM 仅接收问题与命中的候选片段
- **Markdown 优先**：保留标题/列表结构，入库即按语义切块（截图随首个可运行版本补充）

## 🚀 快速开始

> 以下命令为规划形态，随 v0.1 落地验证后更新（代码真实可运行为硬标准）。

**环境要求**

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | 3.13 | 后端运行时 |
| PostgreSQL | 16+ | 主数据库，含 pgvector 扩展（03 §6 DM2） |
| Node.js | 20+ | 前端构建（M5 启动前定稿） |

**计划启动方式**（开发环境）

```bash
# 1. 启动数据库
docker compose up -d postgres

# 2. 安装依赖并初始化
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                                 # 填入 LLM API 密钥

# 3. 运行
uvicorn app.main:app --reload                        # 后端: http://localhost:8000/docs
```

## 🛠 技术栈

| 层 | 选型 | 状态 |
|---|---|---|
| 后端框架 | FastAPI + Pydantic v2 | 已定 |
| 数据库 | PostgreSQL 16 + SQLAlchemy 2.x | 已定 |
| 向量检索 | pgvector（HNSW / cosine） | 已定（03 §6 DM2） |
| Embedding / LLM | OpenAI 兼容 API，供应商可配；默认 text-embedding-3-small（1536 维） | 已定（04 §8 DR2） |
| 前端 | 简洁可用（技术待定） | M5 启动前定 |
| 工程化 | Docker Compose · GitHub Actions · pytest | 收尾期落地 |

## 📐 项目结构

```text
.
├── app/                  # FastAPI 应用（按模块 M1-M4 分包，见 02 文档）
├── docs/                 # 设计文档唯一来源（见下）
├── tests/                # pytest 测试（单元 + API）
└── frontend/             # 前端（M5 里程碑落地）
```

## 📖 设计文档

设计先行是本项目的开发方式：每个模块先出设计（填子 Issue 模板），再写代码、再提 PR。

| 文档 | 内容 | 状态 |
|---|---|---|
| [docs/README.md](docs/README.md) | 文档体系与写作规范 | 已确认 |
| [01-requirements.md](docs/design/01-requirements.md) | 产品需求：定位/范围/NFR/用户故事/流程/决策 | v0.4 已拍板 |
| [02-modules.md](docs/design/02-modules.md) | 模块拆分与业务边界 | v0.3 草案 |
| [03-data-model.md](docs/design/03-data-model.md) | 数据模型：ER / DDL / 状态机 / 存储选型 | v0.2 已拍板 |
| [04-retrieval.md](docs/design/04-retrieval.md) | 检索链路：切分/embedding/混合检索/防幻/拒答 | v0.2 已拍板 |
| [module-design.md](docs/templates/module-design.md) | 模块设计模板（= 子 Issue 模板） | 已确认 |
| [workflow.md](docs/workflow.md) | 开发流程规范：步骤/DoD/内容归属/简洁原则 | v0.1 |

（05 Agent 工作流 · 06 评估方案随里程碑推进补充）

## 🗺 路线图

| 版本 | 内容 |
|---|---|
| v0.1（当前目标） | M1 库/文档管理 + M2 入库管线 + M3 问答溯源拒答 + M4 工作流 + 简洁前端 |
| V1.0 | PDF/Word 导入、会话持久化、增量文件同步、CI/CD 全量落地 |
| V2.0 | 语义图谱（Graph RAG）、更多导入源 |

## 📄 License

MIT（拟议，随仓库首次提交附带 LICENSE 文件）
