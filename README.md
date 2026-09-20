# KnowBase · 个人知识库问答系统

[![CI](https://github.com/One-29/personal-knowledge-base/actions/workflows/ci.yml/badge.svg)](https://github.com/One-29/personal-knowledge-base/actions/workflows/ci.yml)

个人知识库问答服务：把 Markdown 笔记喂给它，用自然语言提问，每个回答都标注原文出处；知识库覆盖不了的问题，它明说不知道，不编造。支持纯文本笔记，也支持把 Markdown 与本地 PNG/JPEG/WebP 原图组成 ZIP 导入；不绑定具体 embedding / LLM 供应商，暂不支持 PDF、Word 或图片内容识别。

改代码前请阅读 docs/design/00-overview.md（项目总览与边界）、docs/design/02-modules.md（模块边界与依赖方向）和 docs/README.md（文档与写作规范）。

## 现状与结论

- M1–M5 与检索评估已完成，CI 绿灯。前端使用严格 TypeScript + Vite，生产构建由 API 挂在 `/ui`。
- 含图片 Markdown 会保留原始图片字节、出现顺序与字符位置；完整原文和引用侧栏均可查看原图，图片本身不参与 OCR 或向量化。
- 首次评估（3 篇语料 / 12 条样本）：recall@8 = **1.000**、MRR = **1.000**、库外拒答率 **100%**、库内误拒率 **0%**。
- 拒答阈值 τ 由评估数据校准：0.35 → **0.45**。
- 已知边界：当前只支持单 worker——会话与后台入库任务都在进程内存里，加 worker 拿不到可靠的跨进程会话与任务恢复。
- 上述是小样本结论，不能据此推断真实多库表现；完整口径见 docs/design/00-overview.md。

## 本地运行

需要 Python 3.12+、Node.js 22.12+、Windows 11 + WSL2（Ubuntu，启用 systemd）、PostgreSQL 16 + pgvector。不需要 Docker Desktop。Node.js 只负责构建前端，不作为应用运行时服务；问答必须配置模型 Key，任何 OpenAI 兼容的 embedding 与 chat 服务都可以。

数据库跑在 WSL2 内的独立 Docker Engine 里，第一次使用先装一次：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-wsl-docker-engine.ps1
```

准备虚拟环境与依赖。命令都在项目根执行；PowerShell 不支持 `&&`，分两行或改用 `;`。不需要激活 venv，直接用它的解释器：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

在 `.env` 里填 `DATABASE_URL`、`EMBEDDING_*`、`LLM_*`。配置只从项目根读取（`pydantic-settings` 相对当前目录），换个目录启动会读不到。

一条命令拉起数据库和 API：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-knowbase.ps1
```

启动器先核对 Node.js 版本，根据 `package-lock.json` 安装锁定的前端依赖，并仅在源码变化时重建 `frontend/dist`；随后启动 WSL 里的 Docker、按 `compose.yaml` 创建或复用 `knowbase-pg` 与卷 `knowbase_pgdata`、等健康检查、执行 `alembic upgrade head`，再以单 worker 启动 API。`/ready` 通过后打开 <http://127.0.0.1:8000/ui/>。首次安装 npm 依赖或拉取 `pgvector/pgvector:pg16` 镜像需要联网，之后未改变依赖与前端源码时会直接复用。可加 `-Port 9000` 换端口，`-NoBrowser` 不自动开浏览器。

界面五个视图：问答、工作流、关联图、文档、知识库。

单独起 API 前先生成前端生产资源；FastAPI 只托管构建产物，不需要额外运行前端进程：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-frontend.ps1
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000
```

开发前端时，先运行 API，再在另一个终端执行 `npm run frontend:dev`，访问 <http://127.0.0.1:5173/ui/>；Vite 会把 `/api`、`/health` 和 `/ready` 代理到 8000 端口。开发后端时可给 uvicorn 加 `--reload`。`Ctrl+C` 只停对应进程，数据库和 WSL 保活进程继续运行；要一并释放：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-knowbase-database.ps1
```

仓库自带 8 篇高等数学演示笔记，入库后可直接试问答与关联图：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\load-calculus-demo.ps1
```

演示问题与工作流任务清单见 docs/demo.md。

不打开界面时，也可以用命令行走通整条链路：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/kbs \
  -H "Content-Type: application/json" -d '{"name":"计算机网络"}'

curl -X POST http://127.0.0.1:8000/api/v1/kbs/1/documents -F "file=@tcp.md"

curl -X POST http://127.0.0.1:8000/api/v1/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"TCP 为什么需要三次握手？","kb_id":1}'
```

上传登记即返回，切分与向量化在后台执行；提问若覆盖不足会返回 `refused=true`。

含本地图片的笔记需打成 ZIP：包内必须恰有一篇 `.md`，图片使用相对路径引用，支持静态 PNG/JPEG/WebP。系统校验 ZIP 路径、CRC、压缩比、图片格式与尺寸，并原样保存图片；具体目录示例、限制和版本保留策略见 [Markdown 图片包说明](docs/image-packages.md)。

## 测试

前端检查包含严格类型检查、Vitest 单元测试和生产构建：

```powershell
npm ci --no-audit --no-fund
npm run frontend:check
```

数据库集成测试需要 PostgreSQL 可用，且 `knowbase_test` 库已存在（conftest 只重建扩展与表，不建库）：

```powershell
wsl.exe -d Ubuntu -- docker exec knowbase-pg createdb -U postgres knowbase_test
.\.venv\Scripts\python.exe -m pytest -q
```

测试统一使用假 embedding provider，不调真实模型，也不需要密钥。没有 PostgreSQL 时可以只跑纯单元测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_chunking.py tests/test_embedding.py
```

评估在独立的 `knowbase_eval` 库上运行，脚本自己建库、自己迁移，只在子进程内覆盖环境变量，不碰日常数据：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-eval.ps1 -Retrieval   # 只跑检索
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-eval.ps1              # 完整评估
```

CI 在 pgvector service container 上跑 pytest，另外执行 TypeScript 严格类型检查、Vitest、Vite 生产构建，并在空库里执行 `alembic upgrade head` 与 `alembic check`，同时校验 Compose、Shell 和 PowerShell 脚本。

日常库 `knowbase`、测试库 `knowbase_test`、评估库 `knowbase_eval` 与各自的原文目录互不可见，对照表见 docs/operations.md。

## 参与

从 main 开分支，按 docs/design/02-modules.md 的边界放置代码。本地测试通过后向 main 开 Pull Request，说明改了什么、怎么验证。一种改动一个 PR；较大的行为或边界变化请先开 Issue。

## License

[MIT](LICENSE)
