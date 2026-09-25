# KnowBase · 个人知识库问答系统

[![CI](https://github.com/One-29/personal-knowledge-base/actions/workflows/ci.yml/badge.svg)](https://github.com/One-29/personal-knowledge-base/actions/workflows/ci.yml)

个人知识库问答服务：把 Markdown 笔记喂给它，用自然语言提问，每个回答都标注原文出处；知识库覆盖不了的问题，它明说不知道，不编造。支持纯文本笔记，也支持把 Markdown 与本地 PNG/JPEG/WebP 原图组成 ZIP 导入；不绑定具体 embedding / LLM 供应商，暂不支持 PDF、Word 或图片内容识别。

改代码前请阅读 docs/design/00-overview.md（项目总览与边界）、docs/design/02-modules.md（模块边界与依赖方向）和 docs/README.md（文档与写作规范）。

## 现状与结论

- M1–M5 与检索评估已完成，CI 绿灯。前端使用严格 TypeScript + Vite，生产构建由 API 挂在 `/ui`。
- 含图片 Markdown 会保留原始图片字节、出现顺序与字符位置；完整原文和引用侧栏均可查看原图，图片本身不参与 OCR 或向量化。
- v2 检索基线覆盖 5 个库、20 篇文档、60 条分层样本：PostgreSQL recall@8 = **1.000（50/50）**、MRR = **0.987**；SQLite recall@8 = **1.000（50/50）**、MRR = **1.000**，已通过自动迁移质量门。
- L1 拒答阈值由 v2 相似度分布重新校准为 **τ=0.50**：10 条库外问题拒答率 100%，50 条库内问题误拒率 0%。
- 桌面化存储迁移已完成默认 SQLite 运行时、文件系统重建和已登记普通文本的外部编辑同步：日常数据库被删除后可从原文重新生成；Markdown/TXT 在应用外保存后，下次启动只重建受影响文档。一键启动不依赖 WSL、Docker、PostgreSQL 或 Alembic，PostgreSQL 只保留给旧数据迁移、双方言回归和评估对照。
- Windows 独立桌面包已经接通：PyInstaller 单目录包包含 Python、FastAPI、前端产物和 pywebview 运行时，在隔离目录中通过 API 与真实 WebView2 双重启动检查；使用包时不需要安装 Python、Node.js、Docker 或源码。源码快捷方式仍保留给开发与调试。
- 普通问答已使用 POST SSE 流式输出：检索与 L1 拒答先完成，模型文本随后逐段显示并明确标为待校验草稿；完整内容通过 L2 引用校验后才形成可点击引用并写入会话。原 `/api/v1/ask` 同步契约继续保留给脚本和兼容客户端。
- 本地诊断链路已经接通：SQLite 日常运行把脱敏日志写入用户数据目录并自动轮转，每个 HTTP 请求返回关联 ID，问答、工作流、检索和向量化记录分阶段耗时；左下角可一键复制不含密钥与知识内容的诊断摘要。
- 文档入库任务已经落库：文档页展示校验、读取、切分、索引生成和发布进度；应用退出后，下一次启动会先核对 Vault，再续跑遗留的 `pending` / `processing` 文档。任务按文档版本原子领取，重复调度和较早重传不能覆盖新版本。
- 数据库会持久化 embedding 服务地址、模型和维度的指纹；配置变化且仍有旧块时，问答会明确提示重建，入库会保留旧块并记录可诊断错误，避免不同语义空间静默混用。
- 已知边界：当前只支持单 worker。兼容用 `session_id` 会话仍在进程内；入库任务状态和重启恢复虽已持久化，执行承载仍是当前进程的 FastAPI `BackgroundTasks`，尚未设计多 worker 的租约、心跳与跨进程抢占。
- 评估已能比较多库与难度层级，但仍是固定的 60 条基线，不能替代真实用户语料上的持续评估；完整口径见 docs/design/06-evaluation.md。

## Windows 独立应用包

发布包解压后必须保留完整的 `KnowBase/` 目录，直接双击 `KnowBase.exe`。首次使用问答前，双击同目录的 `Configure KnowBase.cmd`，在打开的 `config.env` 中填写 `EMBEDDING_API_KEY` 与 `LLM_API_KEY`，保存后重新启动。配置、SQLite、原文、WebView profile 和日志都在 `%LOCALAPPDATA%\KnowBase`，替换程序目录不会覆盖个人数据。

仓库维护者在 64 位 Windows 上构建发布 ZIP：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[package]"
npm ci --no-audit --no-fund
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-windows-app.ps1
```

产物位于 `artifacts/windows/`，包含版本化 ZIP 和对应 `.sha256`。构建脚本先生成 Vite 资源和多尺寸应用图标，再冻结单目录包；随后清空 `PYTHONHOME/PYTHONPATH`、把 `PATH` 收缩为 Windows 系统目录，在全新临时用户目录中分别验证 SQLite/API 启动与隐藏 WebView2 页面加载。任何一步失败都不会生成可接受的发布结果。当前包未做商业代码签名，Windows SmartScreen 可能提示确认；正式下载应只使用本仓库 Releases 并核对 SHA-256。

## 从源码运行

源码运行需要 Python 3.12+ 和 Node.js 22.12+，不需要 Docker Desktop、WSL、Docker Engine 或独立数据库服务。Node.js 只负责构建前端，不作为应用运行时服务；问答必须配置模型 Key，任何 OpenAI 兼容的 embedding 与 chat 服务都可以。Windows 原生窗口和独立包使用 WebView2 Runtime；Linux 源码运行还需按 [pywebview 安装说明](https://pywebview.flowrl.com/guide/installation)准备 GTK 或 Qt GUI 后端。

准备虚拟环境与依赖。命令都在项目根执行；PowerShell 不支持 `&&`，分两行或改用 `;`。不需要激活 venv，直接用它的解释器：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"
Copy-Item .env.example .env
```

在 `.env` 里填写 `EMBEDDING_*` 和 `LLM_*`。通常不应设置 `DATABASE_URL` 或 `STORAGE_DIR`；数据库与原文默认放在 Windows `%LOCALAPPDATA%\KnowBase`、macOS `~/Library/Application Support/KnowBase`，或 Linux `$XDG_DATA_HOME/knowbase`。如需整体改位置，只设置 `KNOWBASE_DATA_DIR`。

### Windows 原生桌面窗口

安装桌面 **KnowBase** 快捷方式：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-desktop-shortcut.ps1
```

安装器会准备前端生产资源，并创建一个没有常驻终端窗口的启动入口。以后双击 **KnowBase** 即可打开原生窗口；关闭窗口会正常停止它拥有的本地 API。重复双击不会同时写同一份 SQLite，而会提示切换到已经打开的窗口。旧版快捷方式仍指向浏览器启动器时，重新执行安装命令即可更新。

也可以不安装快捷方式，直接运行或执行完整启动自检：

```powershell
.\.venv\Scripts\python.exe -m app.desktop
.\.venv\Scripts\python.exe -m app.desktop --check
```

桌面壳只负责窗口、单实例锁和本地 API 生命周期，业务仍经过同一组 FastAPI REST 接口。它绑定回环地址 `127.0.0.1`；独立包和直接运行默认自动选择空闲端口，源码快捷方式默认固定为 8000，可给安装器加 `-Port 9000` 更换。非受信网站发出的浏览器写请求会在进入业务路由前被拒绝，命令行中不带 `Origin` 的本地 API 调用保持兼容。

源码快捷方式会在前端变化后调用 Node.js 重建资源；独立包已经带齐冻结资源，运行时不会回到源码目录或调用构建工具。自动更新、正式代码签名以及 macOS/Linux 二进制仍属于后续发布阶段。

### 诊断与本地日志

界面左下角的 **复制诊断信息** 会复制版本、系统、数据库后端与状态计数、模型指纹、前端构建状态和本次请求 ID。摘要不包含 API Key、服务地址、问题、回答、知识库/文档标题或原文，适合直接粘贴到 Issue。

详细日志位于用户数据目录的 `logs/knowbase.log`。单个文件上限 2 MiB，保留 4 份历史文件；日志使用 UTC 时间，包含请求 ID、请求总耗时以及改写、Embedding、检索、生成、工作流步骤和入库索引的阶段耗时。已知模型密钥、Bearer、常见凭据字段与 URL userinfo 会脱敏；错误摘要仍可能含本地文件路径或供应商返回文本，分享日志文件前应自行检查。日志目录不可写时应用继续运行，并把诊断报告标为日志未启用。

### 浏览器与开发模式

一条命令准备前端、SQLite 并在浏览器中启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-knowbase.ps1
```

浏览器启动器先核对端口和 Node.js 版本，根据 `package-lock.json` 安装锁定的前端依赖，并仅在源码变化时重建 `frontend/dist`；随后初始化 SQLite schema、WAL、外键、FTS5、embedding 指纹与原文 Vault 清单，检查已登记原文的外部变化并恢复中断的入库任务，再以单 worker 启动 API。`/ready` 通过后打开 <http://127.0.0.1:8000/ui/>。首次安装 npm 依赖需要联网，之后未改变依赖与前端源码时会直接复用。可加 `-Port 9000` 换端口，`-NoBrowser` 不自动开浏览器；在这个模式下按 `Ctrl+C` 停止 API。

若仓库的 `data/knowbase.db` 是旧 PostgreSQL 数据的已验证迁移产物，首次启动会通过 SQLite backup API 把数据库和完整 `data/storage` 复制到用户数据目录。候选库通过原文、chunk 锚点、外键、FTS 与摘要复核后才发布；源文件不会移动或删除，已有用户数据库绝不会被覆盖。

日常 `storage/.knowbase-vault.json` 保存知识库、文档、活动/候选原文的稳定元数据与校验和，不保存密钥、回答、chunk 或向量。若 `knowbase.db` 不存在而清单仍在，启动器会先完整校验原文，在候选 SQLite 中重新切分和调用 embedding，全部文档进入 `ready` 并通过外键、FTS、溯源区间和 `quick_check` 后才发布。失败不会留下半成品数据库；恢复需要当前 embedding 配置和可用的模型服务。

需要用其它编辑器修改已入库的普通 Markdown/TXT 时，建议先停止 KnowBase，保存原文件，再重新启动。启动器先用大小和 mtime 筛选，再用 SHA-256 判断内容是否真的改变；内容改变时只重新切分、向量化并替换这一篇的索引，模型调用期间再次保存会中止本次同步。若 Vault 已记录新版本而 SQLite 提交被中断，下次启动会自动续接且不会重复增加版本。直接修改图片包、删除或重命名已登记文件都会停止启动并给出恢复指引；图片包请通过 ZIP 重传，新放入 `storage/` 的散文件请通过界面导入。应用运行期间的外部修改在下一次启动时生效。

启动脚本从自身位置解析项目根目录，代码和数据路径都没有写死当前电脑的盘符或用户名。因此其他人从 GitHub 下载到不同目录时不会因绝对路径报错，目录包含空格或中文也可以；每个用户的数据仍进入自己的系统用户目录，不会跟随仓库位置变化。

界面五个视图：问答、工作流、关联图、文档、知识库。

单独起 API 前先生成前端生产资源；FastAPI 只托管构建产物，不需要额外运行前端进程：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-frontend.ps1
.\.venv\Scripts\python.exe -m app.runtime
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000
```

已有 PostgreSQL 日常库先关闭 API，再用下面的维护命令迁移。默认目标是
`data/knowbase.db`，已有目标会被拒绝，不会静默覆盖：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\migrate-to-sqlite.ps1
```

迁移器会锁定一致的 PostgreSQL 快照，保留全部业务主键和向量，校验活动原文、
图片包、每个 chunk 的原文区间、embedding 指纹、外键、FTS5 与 SQLite
`quick_check`，再比较源/目标确定性摘要。所有检查完成前只写同目录候选文件；
成功后才原子发布 SQLite 和 `data/knowbase-migration-report.json`。确需重跑时加
`-ReplaceExisting`，旧 SQLite 会保留为带 UTC 时间戳的 `.bak` 文件。迁移完成后，
从旧 `.env` 删除 `DATABASE_URL` 与 `STORAGE_DIR`；下一次普通启动会把这份候选及
原文完整复制到用户数据目录。

开发前端时，先运行 API，再在另一个终端执行 `npm run frontend:dev`，访问 <http://127.0.0.1:5173/ui/>；Vite 会把 `/api`、`/health` 和 `/ready` 代理到 8000 端口。开发后端时可给 uvicorn 加 `--reload`。`Ctrl+C` 会停止对应 API 进程；SQLite 没有需要另行停止的服务。

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

上传登记即返回，切分与向量化在后台执行；文档页约每 2.2 秒刷新真实任务阶段，完成或失败后停止轮询。进程在处理中退出时，下一次启动会自动续跑；提问若覆盖不足会返回 `refused=true`。

界面使用 `POST /api/v1/ask/stream` 接收 `metadata`、`delta` 和最终 `result` 事件；最终结果仍是与同步 `/ask` 相同的结构。SSE 草稿中的引用在完成前不可点击，若最终发现越界或零引用，界面会用可信拒答替换草稿。

含本地图片的笔记需打成 ZIP：包内必须恰有一篇 `.md`，图片使用相对路径引用，支持静态 PNG/JPEG/WebP。系统校验 ZIP 路径、CRC、压缩比、图片格式与尺寸，并原样保存图片；具体目录示例、限制和版本保留策略见 [Markdown 图片包说明](docs/image-packages.md)。

## 测试

前端检查包含严格类型检查、Vitest 单元测试和生产构建：

```powershell
npm ci --no-audit --no-fund
npm run frontend:check
```

完整回归为了验证保留的 PostgreSQL 方言，需要 PostgreSQL 可用，且 `knowbase_test` 库已存在（conftest 只重建扩展与表，不建库）：

```powershell
wsl.exe -d Ubuntu -- docker exec knowbase-pg createdb -U postgres knowbase_test
.\.venv\Scripts\python.exe -m pytest -q
```

测试统一使用假 embedding provider，不调真实模型，也不需要密钥。没有 PostgreSQL 时可以只跑纯单元测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_chunking.py tests/test_embedding.py
```

SQLite 集成测试使用临时真文件，覆盖建库、v1→v2 原地升级、WAL/外键、入库、FTS5 触发器、
混合检索、关联图、级联删除、Vault 原子清单、删除数据库后全量重建、外部编辑
的单篇增量重建与中断续接、入库任务跨重启恢复、单篇失败隔离、失败保护和重启持久化；迁移集成测试还会从隔离的
PostgreSQL 生成最终 SQLite，验证失败不覆盖。桌面与诊断测试还覆盖跨进程单实例锁、
后台 API 就绪与退出、窗口编排、端口冲突、回环 Web UI 的异源写请求保护、
请求 ID、日志轮转/脱敏和隐私安全诊断摘要；问答测试还覆盖供应商 SSE、任意网络分片、
非流式供应商回退、L1 无草稿拒答与 L2 最终覆盖：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_vault.py tests/test_external_sync.py tests/test_sqlite_storage.py tests/test_sqlite_migration.py tests/test_desktop.py tests/test_http_security.py tests/test_diagnostics.py
```

评估支持隔离的 PostgreSQL `knowbase_eval` 或固定 SQLite 文件，脚本只在子进程内覆盖环境变量，不碰日常数据：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-eval.ps1 -Retrieval   # 只跑检索
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-eval.ps1              # 完整评估
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-eval.ps1 -Retrieval -TopK 8 -ReportPath eval\baselines\postgresql-bge-m3-v2.json
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-eval.ps1 -Backend SQLite -Retrieval -TopK 8 -ReportPath eval\baselines\sqlite-bge-m3-v2.json -ReferenceReportPath eval\baselines\postgresql-bge-m3-v2.json
```

归档的真实 BGE-M3 检索结果与阈值扫描见
`eval/baselines/postgresql-bge-m3-v2.json` 与
`eval/baselines/sqlite-bge-m3-v2.json`；评估脚本只会替换专用评估库/文件。

CI 在 pgvector service container 上跑双方言 pytest，另外执行 TypeScript 严格类型检查、Vitest、Vite 生产构建，并在空 PostgreSQL 库里执行 `alembic upgrade head` 与 `alembic check`，同时校验兼容用 Compose、Shell 和 PowerShell 脚本。独立的 Windows job 还会从源码冻结应用，并在无 Python/Node 路径的隔离环境中验证 API 和 WebView2 启动链。

用户目录中的日常 SQLite、PostgreSQL 测试库 `knowbase_test`、评估库 `knowbase_eval` 与各自的原文目录互不可见，对照表见 docs/operations.md。

## 参与

从 main 开分支，按 docs/design/02-modules.md 的边界放置代码。本地测试通过后向 main 开 Pull Request，说明改了什么、怎么验证。一种改动一个 PR；较大的行为或边界变化请先开 Issue。

## License

[MIT](LICENSE)
