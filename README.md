# KnowBase · 个人知识库问答系统

[![CI](https://github.com/One-29/personal-knowledge-base/actions/workflows/ci.yml/badge.svg)](https://github.com/One-29/personal-knowledge-base/actions/workflows/ci.yml)

个人知识库问答服务：把 Markdown 笔记喂给它，用自然语言提问，每个回答都标注原文出处；知识库覆盖不了的问题，它明说不知道，不编造。提供文档入库、混合检索、带引用回答与多步工作流所需的服务端与 Web 端边界，不绑定具体 embedding / LLM 供应商，也不支持 PDF、Word 等格式。

改代码前请阅读 docs/design/00-overview.md（项目总览与边界）、docs/design/02-modules.md（模块边界与依赖方向）和 docs/README.md（文档与写作规范）。

## 现状与结论

- M1–M5 与检索评估已完成，CI 绿灯。前端是零构建单页，由 API 挂在 `/ui`。
- 首次评估（3 篇语料 / 12 条样本）：recall@8 = **1.000**、MRR = **1.000**、库外拒答率 **100%**、库内误拒率 **0%**。
- 拒答阈值 τ 由评估数据校准：0.35 → **0.45**。
- 已知边界：当前只支持单 worker——会话与后台入库任务都在进程内存里，加 worker 拿不到可靠的跨进程会话与任务恢复。
- 上述是小样本结论，不能据此推断真实多库表现；完整口径见 docs/design/00-overview.md。

## 本地运行

需要 Python 3.12+、Windows 11 + WSL2（Ubuntu，启用 systemd）、PostgreSQL 16 + pgvector。不需要 Docker Desktop。问答必须配置模型 Key，任何 OpenAI 兼容的 embedding 与 chat 服务都可以。

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

启动器会启动 WSL 里的 Docker、按 `compose.yaml` 创建或复用 `knowbase-pg` 与卷 `knowbase_pgdata`、等健康检查、执行 `alembic upgrade head`，再以单 worker 启动 API，`/ready` 通过后打开 <http://127.0.0.1:8000/ui/>。首次拉取 `pgvector/pgvector:pg16` 镜像可能需要几分钟。可加 `-Port 9000` 换端口，`-NoBrowser` 不自动开浏览器。

界面五个视图：问答、工作流、关联图、文档、知识库。

单独起 API（前端是静态文件，不需要另外起进程）：

```powershell
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000
```

开发时加 `--reload`。`Ctrl+C` 只停 API，数据库和 WSL 保活进程继续运行；要一并释放：

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

## 测试

数据库集成测试需要 PostgreSQL 可用，且 `knowbase_test` 库已存在（conftest 只重建扩展与表，不建库）：

```powershell
wsl.exe -d Ubuntu -- docker exec knowbase-pg createdb -U postgres knowbase_test
pytest -q
```

测试统一使用假 embedding provider，不调真实模型，也不需要密钥。没有 PostgreSQL 时可以只跑纯单元测试：

```powershell
pytest -q tests/test_chunking.py tests/test_embedding.py
```

评估在独立的 `knowbase_eval` 库上运行，脚本自己建库、自己迁移，只在子进程内覆盖环境变量，不碰日常数据：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-eval.ps1 -Retrieval   # 只跑检索
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-eval.ps1              # 完整评估
```

CI 在 pgvector service container 上跑 pytest，另外在空库里执行 `alembic upgrade head` 与 `alembic check`，并校验 `compose.yaml`、`scripts/install-wsl-docker-engine.sh` 和 `frontend/app.js` 的语法。

日常库 `knowbase`、测试库 `knowbase_test`、评估库 `knowbase_eval` 与各自的原文目录互不可见，对照表见 docs/operations.md。

## 参与

从 main 开分支，按 docs/design/02-modules.md 的边界放置代码。本地测试通过后向 main 开 Pull Request，说明改了什么、怎么验证。一种改动一个 PR；较大的行为或边界变化请先开 Issue。

## License

[MIT](LICENSE)
