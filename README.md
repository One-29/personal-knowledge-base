# KnowBase · 可溯源的个人知识库

[![CI](https://github.com/One-29/personal-knowledge-base/actions/workflows/ci.yml/badge.svg)](https://github.com/One-29/personal-knowledge-base/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

KnowBase 是一款本地个人知识库工具。它可以根据 Markdown 笔记回答问题，为回答提供可点击、可核对的原文引用；证据不足时会拒绝回答，证据处于临界区间时会展示候选原文供人工判断。

项目支持多知识库、普通问答、多步骤工作流和文档关联图。日常数据保存在本机 SQLite 与原文 Vault 中，不需要 Docker、WSL 或独立数据库服务。

> 当前尚未发布可直接下载的 GitHub Release，请先按下文从源码运行。Windows 独立应用包的构建与启动链路已经完成。

## 核心功能

- **引用溯源**：回答附带来源文档、原文片段和字符区间，可以直接打开核对。
- **证据门控**：区分正常回答、灰区核对和证据不足，降低无依据生成。
- **混合检索**：组合全文检索与向量检索，兼顾关键词匹配和语义召回。
- **多知识库**：可以分别管理不同主题的资料，也可以进行全库检索。
- **工作流与关联图**：支持多步骤资料整理，并按语义关系展示文档连边。
- **流式回答**：回答内容逐步显示，引用校验完成后再形成正式结果。
- **本地数据管理**：数据库、原始文档和日志保存在用户数据目录。
- **可恢复索引**：索引数据库损坏或丢失时，可以根据 Vault 原文重新生成。

## 回答与拒答逻辑

KnowBase 会先检索候选原文，再根据最强证据分数决定后续行为：

| 证据分数 | 结果 |
| --- | --- |
| 低于 `0.45` | 明确拒绝回答 |
| `0.45` 至低于 `0.55` | 不调用回答模型，展示候选原文供人工核对 |
| 达到 `0.55` | 调用回答模型，并执行引用校验 |

生成内容只有在引用来源、原文区间和引用编号通过校验后，才会作为带有可点击引用的正式回答返回。灰区展示的是可能相关的候选原文，不属于正式引用。

阈值来自当前固定评估集，不建议只凭主观体验修改。详细设计见[检索与防幻觉设计](docs/design/04-retrieval.md)。

## Windows 快速开始

### 1. 准备环境

需要：

- Python 3.12 或更高版本
- Node.js 22.12 或更高版本
- 可用的 OpenAI 兼容 Embedding 和 LLM 服务

日常运行不需要 Docker Desktop、WSL 或 PostgreSQL。

### 2. 下载并安装

在 PowerShell 中执行：

```powershell
git clone https://github.com/One-29/personal-knowledge-base.git
Set-Location personal-knowledge-base

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"
Copy-Item .env.example .env
```

打开 `.env`，至少填写：

```dotenv
EMBEDDING_API_KEY=你的Embedding服务密钥
LLM_API_KEY=你的LLM服务密钥
```

默认配置使用 SiliconFlow 的 OpenAI 兼容接口，两个通道可以使用同一个 API Key；也可以在 `.env` 中更换服务地址和模型。

### 3. 启动 KnowBase

直接打开桌面窗口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-knowbase-app.ps1
```

也可以安装桌面快捷方式：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-desktop-shortcut.ps1
```

安装完成后，双击桌面的 **KnowBase** 图标即可打开；关闭最后一个 KnowBase 窗口会同时停止本地服务。

启动脚本会自动识别项目所在目录，不依赖固定盘符、用户名或仓库路径。项目可以存放在包含空格或中文的本地目录中。

如果启动失败，请查看[运行与故障排查](docs/operations.md)。

## 体验高等数学演示库

仓库自带“大一上高等数学演示库”，包含 8 篇 Markdown 文档，覆盖函数、极限、连续、导数、积分、积分应用和常微分方程。

配置好 `.env` 后，在项目根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\load-calculus-demo.ps1
```

启动 KnowBase，在问答页面选择“大一上高等数学演示库”，可以尝试：

1. 数列极限定义中的 ε 和 N 分别表示什么？
2. 为什么可导一定连续，而连续不一定可导？
3. 牛顿—莱布尼茨公式如何联系导数、原函数和定积分？
4. 一阶线性微分方程如何使用积分因子求解？
5. 量子纠缠的基本原理是什么？

前四个问题用于查看正常回答和引用溯源，第五个问题用于观察知识库证据不足时的拒答或灰区结果。

演示库还可以用于测试：

- 点击回答引用核对原文
- 同时运行普通问答和工作流
- 调整关联图阈值，观察文档连边变化
- 查看不同知识库之间的检索隔离

完整演示流程见[高等数学演示指南](docs/demo.md)。

## 支持的内容

| 内容 | 状态 |
| --- | --- |
| Markdown 文档 | 支持 |
| TXT 文档 | 支持 |
| Markdown 与本地图片组成的 ZIP | 支持 |
| PNG、JPEG、WebP 原图保存与展示 | 支持 |
| PDF、Word | 暂不支持 |
| 图片文字识别和图片向量化 | 暂不支持 |

图片会按照原始字节保存，并在文档和引用核对区域展示，但当前不会进行 OCR 或语义检索。

Markdown 图片包的结构和限制见[图片文档包说明](docs/image-packages.md)。

## 数据与隐私

KnowBase 的数据库、原始文档、WebView 配置和日志默认保存在：

```text
Windows：%LOCALAPPDATA%\KnowBase
macOS：~/Library/Application Support/KnowBase
Linux：$XDG_DATA_HOME/knowbase
```

如需更换整个数据目录，可以在 `.env` 中设置：

```dotenv
KNOWBASE_DATA_DIR=D:/KnowBaseData
```

原始文档和检索索引保存在本机。生成向量和回答时，相关文本会发送给用户配置的 Embedding 或 LLM 服务，因此当前版本不属于完全离线运行。

界面左下角可以复制经过脱敏的诊断信息。诊断摘要不包含 API Key、问题、回答、知识库标题或文档原文。

## 当前边界

- 当前主要支持 Windows 桌面源码运行。
- Windows 独立应用包已经可以构建，但尚未发布正式 GitHub Release。
- macOS 和 Linux 暂无正式二进制安装包。
- 当前按照桌面单实例、单进程、单 worker 模型运行。
- 暂不支持多用户服务和局域网公开部署。
- 问答和向量化需要用户自行配置模型服务。
- 当前评估集为固定样本，只用于版本回归，不能代表所有真实知识库。

## 当前质量基线

v2 固定评估集包含 5 个知识库、20 篇文档和 60 条分层样本。

在当前 SQLite 基线上：

- 库内检索 `recall@8 = 1.000`
- 库内检索 `MRR = 1.000`
- 10 条库外问题的直接回答率为 `0`
- 50 条库内问题中，98% 正常回答，2% 进入灰区，0% 被明确拒答

完整评估口径、样本边界和阈值校准方法见[评估设计](docs/design/06-evaluation.md)。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| 前端 | TypeScript、Vite、原生 DOM 与 CSS |
| 后端 | Python、FastAPI、Pydantic |
| 数据访问 | SQLAlchemy |
| 日常存储 | SQLite、FTS5、WAL |
| 原文存储 | 文件系统 Vault |
| 桌面窗口 | pywebview、WebView2 |
| Windows 打包 | PyInstaller |
| 模型接口 | HTTPX、OpenAI 兼容 API |
| 测试 | pytest、Vitest |
| 持续集成 | GitHub Actions |

PostgreSQL 目前只保留用于旧数据迁移、双方言兼容测试和评估对照，不参与普通桌面运行。

## 项目文档

- [文档索引](docs/README.md)
- [运行、诊断与故障排查](docs/operations.md)
- [高等数学演示指南](docs/demo.md)
- [Markdown 图片包说明](docs/image-packages.md)
- [项目架构总览](docs/design/00-overview.md)
- [模块边界](docs/design/02-modules.md)
- [检索、引用与拒答设计](docs/design/04-retrieval.md)
- [工作流设计](docs/design/05-agent-workflow.md)
- [评估方案](docs/design/06-evaluation.md)

## 开发与测试

前端完整检查：

```powershell
npm ci --no-audit --no-fund
npm run frontend:check
```

无需 PostgreSQL 的基础后端测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_chunking.py tests/test_embedding.py
```

完整回归还会验证 PostgreSQL 兼容层、SQLite、桌面生命周期、诊断、迁移、工作流和引用校验。具体环境与命令见[运行与维护文档](docs/operations.md)。

## 参与贡献

修改代码前，请先阅读[项目架构总览](docs/design/00-overview.md)和[模块边界](docs/design/02-modules.md)。

建议一种改动对应一个 Pull Request，并说明：

- 修改了什么
- 为什么修改
- 如何验证
- 是否改变现有接口或数据格式

Pull Request 在检查通过后使用 **Squash merge** 合并到 `main`。

## License

[MIT](LICENSE)
