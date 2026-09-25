# 运行与维护

| 字段 | 内容 |
|---|---|
| 状态 | 已实现（SQLite 默认运行时、Vault 重建、外部编辑同步、可恢复入库任务、Windows 独立桌面包、本地诊断、旧数据迁移与隔离测试） |
| 版本 | v1.1 |
| 日期 | 2026-09-25 |
| 上游 | `design/03-data-model.md` · `design/04-retrieval.md` · `design/05-agent-workflow.md` |
| 关联 | A1–A2、B1–B4、C1–C7 修复 · SQLite 桌面化、文件系统重建、外部编辑同步、本地窗口与诊断生命周期 |

本文区分代码已经保证的行为、部署边界与后续建议。性能缺陷能解释请求等待，不足以证明某次浏览器或桌面 GUI 卡死的原因；确认实际原因仍需要对应请求的日志与耗时。

## 1. 当前运行边界

### SQLite 本地运行

日常运行使用进程内 SQLite，不启动 Docker Desktop、WSL、Docker Engine、PostgreSQL 或 Alembic。前端源码使用 TypeScript，Node.js 22.12+ 只参与 Vite 构建，应用运行时由 FastAPI 统一托管静态产物。默认用户数据位置如下：

| 系统 | 数据目录 |
|---|---|
| Windows | `%LOCALAPPDATA%\KnowBase` |
| macOS | `~/Library/Application Support/KnowBase` |
| Linux | `$XDG_DATA_HOME/knowbase`，未设置时为 `~/.local/share/knowbase` |

目录中 `knowbase.db` 保存运行状态、切块和检索索引，`storage/` 保存原始 Markdown、图片与 `.knowbase-vault.json` 原子清单。清单只含重建需要的稳定元数据和摘要，不含模型密钥、问答历史、chunk 或向量。需要整体改位置时只设置 `KNOWBASE_DATA_DIR`；普通用户不配置底层 `DATABASE_URL` 和 `STORAGE_DIR`。

项目根由脚本自身位置解析，不依赖当前用户名、盘符或仓库名。代码可位于含空格、中文和常见特殊字符的本地目录，其他人从 GitHub 克隆到不同路径也不会继承开发者机器的绝对路径。日常数据与 clone 分离，所以移动或重新下载代码不会隐式产生另一份个人库；网络共享盘的锁和原子改名语义因服务端实现不同，不在当前支持范围内。

### 原生桌面窗口

面向普通 Windows 用户的发布物是 PyInstaller 单目录包。完整解压后双击
`KnowBase.exe`；首次使用模型前双击 `Configure KnowBase.cmd`，填写用户目录中的
`config.env`。程序资源可以整体移动或替换，配置、SQLite、原文和日志仍位于
`%LOCALAPPDATA%\KnowBase`。发布 ZIP 带独立 SHA-256 文件；当前没有付费代码签名，
SmartScreen 提示属于已知发布边界。

维护者使用下面的命令构建。`package` extra 只增加构建期 PyInstaller 与桌面依赖，
不改变服务端业务边界：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[package]"
npm ci --no-audit --no-fund
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-windows-app.ps1
```

脚本输出 `artifacts/windows/KnowBase-<version>-windows-x64.zip` 及 `.sha256`，并在
临时用户目录完成两级验证：先以 `--check` 启动 SQLite 与 `/ready`，再创建隐藏
WebView2、等待 `/ui/` 加载并正常销毁。验证进程看不到开发机的 Python/Node 路径，
发布目录也会拒绝 `.env` 或 `config.env`；只有空白 `.env.example` 被封装用于首次配置。

源码环境安装 `desktop` 可选依赖后，Windows 可以创建 **KnowBase** 快捷方式：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-desktop-shortcut.ps1
```

安装脚本先复用 `scripts/build-frontend.ps1` 准备 Vite 生产资源，再把快捷方式指向隐藏运行的 `start-knowbase-app.ps1`。该启动器用 `pythonw` 进入 `app.desktop`，因此窗口打开后不依赖一个可见终端。默认端口是 8000；需要改端口时，安装快捷方式时传 `-Port 9000`。桌面模式和浏览器模式不要同时使用同一端口。

快捷方式显式调用 Windows PowerShell 5.1；它会把没有 BOM 的 UTF-8 脚本按系统代码页读取，因此 `start-knowbase-app.ps1` 必须保持纯 ASCII，面向用户的中文由应用层负责。Windows CI 会先检查该文件没有非 ASCII 字节，再通过 `powershell.exe` 执行 `-Check -Port 0`，完整覆盖前端准备、桌面依赖、本地 API 启停和脚本解析。

`app.desktop` 在用户数据库旁持有 `.knowbase-instance.lock` 的操作系统文件锁，同一用户数据目录只允许一个桌面实例。锁文件可以在崩溃后保留，实际所有权由操作系统锁决定，进程退出会自动释放。取得锁后，启动器准备 schema、Vault 与外部编辑同步，在后台线程预绑定回环端口并运行单 worker Uvicorn；只有真实 `/ready` 返回 200 后才把 `/ui/` 交给主线程中的 pywebview。关闭最后一个窗口会请求 API 正常退出并释放 HTTP 连接池、端口和单实例锁。WebView 的持久化 profile 位于用户数据目录的 `webview/`。

本地服务器只监听 `127.0.0.1`。对于带 `Origin` 的浏览器写请求，中间件要求请求目标是明确的回环主机，并只接受与 API 同源的窗口页面或固定的本地 Vite 开发源 `127.0.0.1:5173` / `localhost:5173`；其它网站和 DNS 重绑定域名不能借用户浏览器调用本地修改接口。没有 `Origin` 的本机脚本和命令行请求保持兼容。这个检查不能替代将服务绑定到回环地址，也不能把当前 API 变成可安全暴露到局域网的多用户服务。

无需创建快捷方式时可直接启动；`--check` 会完整准备运行时、启动 API、验证就绪再停止，但不会创建 GUI：

```powershell
.\.venv\Scripts\python.exe -m app.desktop
.\.venv\Scripts\python.exe -m app.desktop --check
```

独立包使用自动分配的回环端口，避免与浏览器开发服务冲突；源码快捷方式继续显式使用 8000。Windows 冻结包和 CI 构建门已经完成，尚未完成的发布工作是 GitHub Releases 自动上传、正式代码签名、自动更新，以及 macOS/Linux 原生包；Linux 源码运行仍需显式选择 GTK 或 Qt pywebview 后端。

### 浏览器与开发模式

需要浏览器、OpenAPI 联调或可见服务日志时，从项目根目录启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-knowbase.ps1
```

浏览器启动器先确认现有服务和端口状态，再调用 `scripts/build-frontend.ps1`：核对 Node.js 版本，以 `package-lock.json` 的 SHA-256 判断是否需要 `npm ci`，再以源码树指纹判断是否需要 Vite 构建。随后 `app.runtime` 初始化 schema、WAL、外键、FTS5、embedding 指纹与 Vault 一致性，并同步已登记普通原文的外部变化，最后以单 worker 启动 API；`/ready` 确认 API 与数据库均可用后才打开浏览器。重复启动时，如果 API 已经正常运行，脚本只打开界面；如果端口被其它程序占用，则明确报错。

浏览器模式按 `Ctrl+C` 停止 API；桌面模式关闭窗口即可。SQLite 没有需要单独关闭的服务。离线备份时先停止 API，再复制整个用户数据目录，确保数据库、可能尚未清理的 WAL 文件、Vault 清单和原文属于同一停止时刻。恢复时同样保持 API 关闭，并整体恢复数据库与 `storage/`，不要随意拼接不同时间的副本。

### 本地日志与复制诊断信息

日常 SQLite 运行会在用户数据目录创建 `logs/knowbase.log`。日志以 2 MiB 为单文件上限，最多保留 4 份轮转备份，加当前文件总上限约 10 MiB；单 worker/单实例边界保证同一时刻只有一个应用进程负责轮转。PostgreSQL 迁移、评估和测试进程不写这份日常日志。日志目录创建失败不会阻止应用启动，诊断报告会显示 `local_log: inactive`。

每行日志采用 UTC 时间，并包含级别、logger、`request_id` 与事件。HTTP 中间件不读取请求/响应正文，也不记录 query string，只记录方法、路径、状态和总耗时；响应头 `X-Request-ID` 与同一请求下的改写、查询 Embedding、双通道检索、回答生成、工作流步骤、文档切分和索引 Embedding 阶段日志一致。未处理异常的 500 响应只返回固定文案和 request ID，路径、SQL、供应商响应与堆栈不回显到界面。

文件 formatter 会替换当前配置中的 Embedding/LLM Key，并通用遮盖 Bearer、`api_key`/`token`/`secret`/`authorization` 字段和 URL userinfo。这个保护降低误分享风险，不承诺理解任意供应商错误文本；现有业务错误还可能包含本地路径、文档标题或供应商摘要，因此发送完整日志前仍应人工检查。

界面左下角 **复制诊断信息** 调用只读 `GET /api/v1/diagnostics`。复制内容仅含应用/Python/系统版本、数据库方言与状态计数、schema 版本、默认/自定义数据位置标记、embedding 模型指纹、LLM 模型、前端构建状态、相对日志文件名和请求 ID；不含 Key、服务地址、问题、回答、库名、文档名、原文或绝对日志路径。API 单独返回绝对 `log_path` 只用于本机按钮提示，不拼入可复制报告，并设置 `Cache-Control: no-store`。

### 从原文 Vault 重建 SQLite

正常用户无需手工删除数据库。需要验证恢复或数据库确实损坏时，先停止 KnowBase，把 `knowbase.db` 以及同名的 `-wal`、`-shm`、`-journal` 文件一起移动到用户数据目录外的备份文件夹，保留整个 `storage/`。再次运行启动器后会执行：

1. 严格校验 Vault 格式、payload SHA-256、主键关系和全部活动/候选原文；
2. 在目标数据库同目录创建随机候选文件，并保留知识库和文档 ID；
3. 使用当前 embedding 配置逐文档重新切分和向量化；崩溃前已登记的候选版本在完整成功后提升为活动版本；
4. 核对原文区间、块序号、外键、FTS5、逻辑摘要与 `quick_check`，收束 WAL；
5. 仅当目标仍不存在时发布，随后再复核发布文件。

重建会调用 embedding 服务，耗时随文档量增长，也要求 API key、网络和模型仍可用。任何文档失败时，候选数据库会被清理，Vault 与原文保留；并发启动时也不会覆盖或删除另一进程先发布的目标。Vault 存在时优先从 Vault 重建，不会误导入仓库 `data/` 中可能过期的迁移快照。已有 SQLite 与 Vault 的身份元数据无法安全解释时会停止启动，不会猜测应该保留哪一份。

### 外部编辑已登记原文

外部编辑同步在 API 启动前执行，目前不持续监听运行中的文件变化。推荐先停止 KnowBase，用其它编辑器保存已登记的普通 Markdown/TXT，再重新启动；运行期间完成的修改会在下次启动时生效。

启动扫描按以下规则执行：

1. 大小与 mtime 未变时跳过文件读取；任一观察值变化时读取稳定快照，并以 SHA-256 判断正文是否真的改变。
2. 只有时间戳变化时只刷新 Vault 观察值，不调用 embedding，也不改写 chunk。
3. 正文变化时使用与普通入库相同的切分和向量校验，只替换该文档的 chunk 与 FTS 行；其它文档不受影响。
4. 远程模型调用后再次读取原文；若同步期间又被保存，则本次启动失败，旧数据库与旧 Vault 保持一致。
5. 新版本先原子写入 Vault，再在单个 SQLite 事务内发布索引。若进程在两步之间退出，SQLite 会呈现可识别的“落后于 Vault”状态，下次启动按同一版本续接，不会再次递增版本。

自动同步只接受清单中已经登记的普通原文。新放进 `storage/` 的散文件不会被猜测为哪一个库或文档，应从界面正常导入。已登记文件缺失、改名、变为空内容或越出 Vault 时会停止启动；请恢复原路径和内容，再从 KnowBase 删除或重传。Markdown 图片包同时包含 `source.md`、资源和 manifest，任何直接修改都无法维持整包校验，因此必须重新上传 ZIP。内容变化和中断续接都会重新调用当前 embedding 服务，需要有效配置、网络与额度。

### PostgreSQL 日常数据迁移到 SQLite

迁移前先停止 API，避免迁移完成后 PostgreSQL 继续接收新写入而与 SQLite
分叉；数据库容器可以继续运行。然后从项目根执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\migrate-to-sqlite.ps1
```

脚本先执行最新 Alembic 迁移，再调用 `app.migration`。迁移器会完成以下检查：

1. 拒绝 `pending` / `processing` 文档和仍挂着候选原文的记录；
2. 用 PostgreSQL `REPEATABLE READ` 与四张业务表的 `SHARE` 锁取得一致快照；
3. 逐个校验活动普通原文的 SHA-256，以及 Markdown 图片包的清单、全部资源、逻辑摘要和图片出现位置；
4. 核对每个 chunk 的 `kb_id`、连续序号、数量、向量维度及 `[char_start, char_end)` 对应的原文；
5. 在同目录临时文件复制显式主键和数据，随后运行外键检查、FTS5 external-content `integrity-check`、`quick_check` 和源/目标逐表摘要比对；
6. checkpoint 候选 WAL 并切回单文件模式，最终才原子改名为 `data/knowbase.db`。

默认不会覆盖已有目标。确认要用新快照替换时执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\migrate-to-sqlite.ps1 `
  -ReplaceExisting
```

旧文件会保留为 `knowbase.db.pre-migration-<UTC>.bak`。任何候选阶段失败都会清理
候选文件并保留现有目标；最终发布复核失败会撤回新文件并恢复备份。迁移报告
`data/knowbase-migration-report.json` 只记录计数、摘要、模型名、维度和指纹，
不会写入数据库口令、模型密钥或 embedding 服务地址。迁移不会移动或删除
`data/storage`。迁移完成后从旧 `.env` 删除 `DATABASE_URL` 与 `STORAGE_DIR`；
下一次普通启动会把迁移库和原文复制到操作系统用户数据目录。

首次复制使用 SQLite backup API，能读取已提交但仍在 WAL 中的页面；原文树在
复制前后分别计算路径和内容摘要，并拒绝符号链接、目录联接、缺失活动原文或
不匹配的 chunk 锚点。数据库与原文都先写候选位置，外键、FTS5、`quick_check`
和确定性摘要通过后才发布。失败会清理候选，已有用户库不会被覆盖，项目内的
迁移源也不会移动或删除。

若迁移报告“块内容与原文区间不一致”，必须先重新索引对应文档，不能跳过
校验。旧版本曾通过文本模式读取文件，Windows 上会把 CRLF 转成 LF 后再计算
引用位置；数据库块虽然能检索，但它的字符偏移不再对应原始文件。当前
`storage.read()` 按 UTF-8 原始字节解码，重建后会保留换行并恢复可验证引用。

### PostgreSQL 兼容与旧数据维护

`compose.yaml`、`KnowBase.WslDocker.psm1`、WSL Docker 安装/停止脚本、Alembic
以及 PostgreSQL 备份恢复脚本暂时保留，服务于旧日常库迁移、PostgreSQL 方言
集成测试和评估对照；`start-knowbase.ps1` 与桌面快捷方式不会调用它们。需要运行
这些维护工具时，再按脚本参数准备 Ubuntu WSL2 与独立 Docker Engine。旧的
`restore-knowbase-backup.ps1` 只恢复 PostgreSQL 快照，不应当用于当前 SQLite
用户数据目录。

### 单 worker 与后台任务

开发后端前先运行 `scripts/build-frontend.ps1` 和 `python -m app.runtime`，再使用 `uvicorn app.main:app --reload`。需要前端热更新时另起 `npm run frontend:dev`，访问 `http://127.0.0.1:5173/ui/`；Vite 只做开发代理，生产与演示仍使用 FastAPI 托管的 `frontend/dist`。演示使用 `uvicorn app.main:app --workers 1`，避免修改文件重启正在运行的服务。

- `session.store` 是进程内对象，不跨 worker 共享。仅携带 `session_id` 的兼容客户端在多 worker 下可能无法取得前一请求的历史。
- `BackgroundTasks` 仍在处理请求的进程内执行，但任务身份、阶段、尝试次数和终态已持久化。重复调度只有一个执行者能原子领取；进程退出后，下一次单 worker 启动会续跑遗留任务。
- 启动恢复先完成 Vault/SQLite 对账，再扫描任务和 `pending` / `processing` 文档。发布已完成时只补任务终态；普通单篇失败不阻断后续文档。它不是多 worker 队列：当前没有租约、心跳、执行者失联判定或运行中的跨进程抢占。
- 多知识库通过 `kb_id` 区分，与单进程部署没有冲突。增加 worker 不会使现有任务与会话自动具备跨进程一致性；当前支持范围仍是单 worker。

### 会话的两个存活范围

| 来源 | 生命周期 | 对用户的影响 |
|---|---|---|
| 浏览器 `localStorage` + 请求 `history` | 浏览器本地保存；清理站点数据或换设备后不共享 | 当前界面刷新后仍可继续追问；后端重启不清空浏览器保存的历史 |
| 兼容接口 `session_id` + `session.store` | 默认 TTL **30 分钟**，最近一次追加对话后计时；进程重启即清空 | TTL 到期或重启后，单凭旧 `session_id` 不能恢复上文；线程锁只解决同进程并发安全 |

TTL 由 `SESSION_TTL_SECONDS` 配置；到期记录在访问存储时清理，服务端没有独立的持久化会话表。

### 问答与工作流可以同时运行

同一浏览器页面为普通问答和工作流分别维护请求控制器与忙碌状态，因此允许一个普通问答和一个工作流同时在途；同类入口各自限制为一个在途请求。普通问答先在线程池完成检索快照，再以 SSE 迭代模型增量；工作流仍同步执行。每个请求由 `get_db()` 创建并关闭独立 SQLAlchemy Session，问答在模型生成前已经回滚只读事务，工作流内部仍按规划顺序逐步执行，不并行共享 Session。

请求发起时会固定浏览器会话 ID。任务完成后结果写回发起时的会话，即使用户期间切换了会话也不会串记录；切回原会话即可查看。若原会话已删除，结果不会写入本地记录，界面会提示本次结果未保存，也不会复活被删除的会话。并发回归测试同时验证两个 HTTP 请求发生执行重叠、获得不同请求级 Session，并在结束后分别关闭。

这个能力面向本机单用户的一问答加一工作流。多个标签页仍能产生更多服务端请求，当前没有全局模型排队、优先级或供应商限流退避；模型端的并发额度仍可能成为瓶颈。

### 日常、测试、评估与演示数据

| 用途 | 数据库/文件 | 原文目录 | 日常界面 |
|---|---|---|---:|
| 个人资料和高等数学演示库 | 用户数据目录 `knowbase.db` | 用户数据目录 `storage/` | 可见 |
| 旧日常库迁移源 | PostgreSQL `knowbase`；候选为仓库 `data/knowbase.db` | 仓库 `data/storage` | 普通启动不读取；首次导入后保留 |
| pytest 数据库集成测试 | PostgreSQL `knowbase_test` | `data/test-storage-*` | 不可见 |
| PostgreSQL 检索与拒答评估 | `knowbase_eval` | `data/eval-storage` | 不可见 |
| SQLite 迁移对照评估 | `data/eval/knowbase-eval.db` | `data/eval-storage` | 不可见 |

运行评估统一使用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-eval.ps1 -Retrieval
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-eval.ps1 -Retrieval -TopK 8 -ReportPath eval\baselines\postgresql-bge-m3-v2.json
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-eval.ps1 -Backend SQLite -Retrieval -TopK 8 -ReportPath eval\baselines\sqlite-bge-m3-v2.json -ReferenceReportPath eval\baselines\postgresql-bge-m3-v2.json
```

PostgreSQL 模式会创建或复用 `knowbase_eval` 并执行 Alembic；SQLite 模式只允许固定文件 `data/eval/knowbase-eval.db`，由应用 schema 初始化器准备 WAL、外键和 FTS5。`eval.run_eval` 会先校验配置，再核对 Session 的真实方言与数据库/文件；原文目录必须精确为项目内的 `data/eval-storage`。任何条件不满足都会在业务查询和删除前拒绝执行。五个评估语料库先分别写入候选库；只有全部文档进入 `ready` 后，才在一个事务中统一替换正式评估库。中途失败会清理候选并保留上一版完整基线。`-ReferenceReportPath` 还会强制语料、模型和 TopK 可比，要求 recall 不下降、MRR 下降不超过 0.01。

高等数学演示库通过以下命令幂等加载：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\load-calculus-demo.ps1
```

加载器先准备日常 SQLite，只替换同名“大一上高等数学演示库”，并采用临时库完整处理后再切换正式名称。它会拒绝 PostgreSQL/SQLite 的测试、评估和内存目标。8 篇文档与阈值演示步骤见[高等数学演示指南](demo.md)。

### 「停止」仅停止客户端等待

点击「停止」会通过 `AbortController` 断开前端读取，被停止的问题不写入前端会话记录。普通问答的流连接会关闭，但同步 `httpx.Client` 读取没有贯穿浏览器、FastAPI 与供应商的可靠取消令牌；正在等待的上游请求仍可能继续并消耗时间与额度。工作流仍是完成后一次响应，同样不会因此可靠取消。

普通首问通常包含 1 次 Embedding 和 1 次回答生成；带历史的追问还会增加 1 次改写生成。默认五步工作流的完整成功路径包含 1 次规划生成、5 次 Embedding、5 次回答生成，共 11 次远程模型调用；汇总本身按规则拼接，不再次调用 LLM。提前拒答或规划退化会减少调用次数。

复用 HTTP 连接、释放数据库事务与 SSE 增量均不会把「停止」变成可靠的服务端取消。真正取消需要另行设计任务取消状态、供应商取消能力与执行阶段检查。

## 2. 事务与连接池

问答在进行改写、Embedding 与回答生成前结束已经完成的只读事务；检索候选和引用元数据先整理为内存对象，避免等待模型时仍占着数据库连接。工作流某一步出现数据库错误后执行 `rollback()`，恢复会话后再执行下一步。

`Session` 对象的存活与连接被占用是两个概念：结束事务可归还该事务占用的连接，之后同一会话仍可以再查询。仅设置 `expire_on_commit=False` 不会自行结束事务或归还连接。参见 [SQLAlchemy Session Basics](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)。

当前连接池配置由环境变量显式设置：

| 环境变量 | 默认值 | 含义 |
|---|---:|---|
| `DB_POOL_SIZE` | 5 | 常驻连接数 |
| `DB_MAX_OVERFLOW` | 10 | 突发时允许额外创建的连接数 |
| `DB_POOL_TIMEOUT` | 5 | 等待空闲连接的秒数，超过后返回连接池超时错误 |
| `DB_POOL_RECYCLE` | 1800 | 连接在后续取用时按此秒数回收过旧连接 |

这些值是个人项目的明确起点，不是已经完成压力测试的容量承诺。缩短 `pool_timeout` 只能让资源不足更早报错；防止模型等待占满连接池依靠事务边界修复。

## 3. 切块修复与已有知识库

B1 修复保证长段落按实际切块结束位置继续，换行回缩不能跳过原文区间；每块文本仍对应原文 `[char_start, char_end)`。该修复在**下一次解析原文时**生效，不会自动修改已有 `chunks` 或向量。

受影响的旧文档需要重新解析并重新生成向量，否则之前漏掉的内容仍无法检索。注意：`/documents/{doc_id}/reupload` 会按内容 hash 跳过同内容重传，原文件再上传一次通常不会触发重建。

目前可维护调用 `app.ingest.process_document(doc_id)` 重新处理指定文档。先用文档详情接口核对目标 id、知识库、原文与配置，在没有同文档并行重传的维护窗口运行以下**单文档**命令；输入框只接受已经核对过的目标 id，不枚举其它库或文档：

```bash
python -c "from app.ingest import process_document; process_document(int(input('Selected doc_id: ')))"
```

命令会**真实调用配置的 Embedding 服务，可能产生费用**，并替换该文档的块和向量。处理后核对文档状态、错误信息、原文区间覆盖以及遗漏段落能否召回；函数在业务失败时可能只记录错误，命令退出本身不能替代状态检查。重建会替换 chunk id，历史答案的旧引用链接可能失效。

本轮代码修复没有自动执行已有知识库的批量重建，也没有为绕过 hash 检查而修改用户原文。建议后续提供显式「重新索引」入口，展示范围、状态与错误，复用现有入库流程。

重传会先把新内容写入不可变候选文件，活动 `file_path` 和旧块保持不变。向量化完成后，入库事务同时替换块并把活动原文切换到该候选；失败则删除候选并继续使用匹配的旧原文和旧块。每次登记递增 `ingest_version`，任务提交前再次核对版本与候选路径，因此较早任务不能覆盖较新的重传结果。

### Markdown 图片包的维护边界

含本地图片的 Markdown 以 ZIP 上传，详细格式和限额见 [Markdown 本地图片包](image-packages.md)。图片包使用不可变版本目录和清单；原文读取会核对 Markdown 摘要及图片出现位置，原图读取会核对 SHA-256 和字节大小。重传失败时整份候选目录删除，不会留下只切换一半的资源。

历史图片包在成功重传后继续保留，以保证旧回答中保存的原图 URL 可用；删除文档或知识库时才统一清理。若频繁上传大图，应同时监控配置的用户 `storage/` 目录磁盘占用。当前没有独立的历史版本清理按钮，也不能在不破坏历史图片 URL 的前提下自动按时间淘汰。

## 4. 关键词与关联图的含义

日常 SQLite 关键词召回使用 FTS5 trigram 与 BM25；短于三个字符时使用已转义的 `LIKE` 回退。FTS 表以 `chunks` 为 external content，并由 insert/update/delete 触发器同步。

PostgreSQL 兼容后端仍先用 `content % :query` 预过滤，再按 `similarity()` 排序。阈值通过事务局部 `set_config('pg_trgm.similarity_threshold', ..., true)` 设置，事务结束后不污染复用连接；`%` 具备使用 `gin_trgm_ops` 的条件，实际计划仍由 PostgreSQL 按数据量和选择性决定。

关联图最多用 400 个源块发起近邻查询，按文档轮流抽取以减少早期导入的大文档垄断；每个源块仍在该库其它文档的全部块中寻找近邻。超过源块计算上限时即使没有边也返回 `truncated=true`；图是导航近似结果，不代表完整的文档关系，也不影响问答检索。

## 5. 如何区分「卡住」

| 观察到的现象 | 可核对的证据 | 判断边界 |
|---|---|---|
| 工作流前一步出错，后续步骤全部数据库报错 | 日志出现首个 SQL 错误，随后出现 `current transaction is aborted` | 对应事务未恢复；验证点是下一步真实 SQL 能再次成功 |
| 多个页面请求一起等待后超时 | 连接池等待超时、连接占用数、数据库 `idle in transaction` 与模型请求重叠 | 属于资源占用问题；单用户低并发不一定出现 |
| 问答长时间没有首段，但列表与健康接口仍能响应 | 等待区阶段、Embedding/改写/检索/流式生成耗时和远程超时日志 | `metadata` 前多为改写、Embedding 或检索；之后无 `delta` 多为供应商首段延迟 |
| 导入文档长期停在 `pending` / `processing` | 文档页阶段与百分比、诊断中的任务状态计数、对应请求/阶段日志 | 重启会自动恢复；若仍停留，按错误码区分模型、原文或数据库故障 |
| 页面滚动、点击或浏览器窗口本身失去响应 | 浏览器 Performance 的主线程长任务，图节点规模与重绘次数 | 需要前端证据，不能直接归因于数据库连接池 |

## 6. 修复后的优化顺序

以下是基于当前代码与产品范围的建议，不表示这些改动已经完成，也不是现网压测结论。

| 优先级 | 建议 | 原因与验收方式 |
|---|---|---|
| 完成 | 启动时同步已登记普通原文的外部编辑 | mtime/size 廉价筛选、SHA-256 最终判定；只重建变化文档，并覆盖模型等待期间二次保存、模型失败、Vault 已推进后 SQLite 失败与下次续接 |
| 完成 | pywebview 桌面壳原型与单实例生命周期 | 原生窗口只在真实 `/ready` 后打开；窗口关闭后 API、端口、HTTP 连接池和文件锁均释放；跨进程锁、端口冲突、GUI 编排和异源写保护有自动化测试 |
| 完成 | Windows 单目录发布包与隔离启动门 | 冻结 Python/前端/WebView 资源；不封装密钥和用户数据；缩减 PATH 后验证 SQLite、API、真实 WebView 加载及正常退出，生成 ZIP 与 SHA-256 |
| 完成 | 本地滚动日志、请求 ID、分阶段耗时与可复制诊断摘要 | 请求总耗时和模型/检索/索引阶段可关联；日志自动轮转并脱敏，500 不泄漏异常，复制报告排除密钥与知识内容 |
| 完成 | 普通问答 SSE 流式输出 | L1 在正文前完成；增量标为待校验草稿，L2 最终结果才绑定引用和写会话；同步接口、非流式供应商回退与中途故障均有回归测试 |
| 1 | 给模型链路增加跨阶段总耗时预算 | 现有 provider 各自有超时，工作流仍可能把多次合法等待串成很长总时长；验收需区分单次超时与任务总预算，并保持已完成步骤可诊断 |
| 完成 | 重传采用不可变候选原文与单调版本，处理成功后统一切换 | Embedding 失败继续使用匹配的旧原文和旧块；处理中再次重传时，较早任务的结果与错误均不会覆盖新版本 |
| 1 | 增加显式重新索引入口与索引版本记录 | B1 等切分算法修复不能自动修好旧块；记录切块版本、Embedding 模型/维度与重建时间，以便只处理受影响文档 |
| 2 | 继续扩大真实中文多库评估集 | v2 已扩到 5 库/20 文档/60 条分层样本，并据此把 τ 校准为 0.50；下一步扩到至少 10 库/100 条，增加同义改写、短术语、同名概念与更强对抗问题 |
| 完成 | 给大文档 Embedding 分批并校验供应商响应 | 默认每批 32 块；限流、5xx 与传输故障只重试当前批次。HTTP 200 的 JSON 结构、数量、index、数值有效性和维度均在 provider 边界校验；测试覆盖中途批次失败与退避重试 |
| 完成 | 给入库任务增加可恢复的任务记录和可见进度 | 当前版本任务与文档登记同事务；原子领取及版本守卫覆盖重复调度/重传竞态，启动可处理运行中退出与发布后退出，单篇失败继续，前端显示五阶段进度 |
| 3 | 建立正式 Releases 与跨平台安装验证 | Windows CI 已构建并隔离运行冻结包；下一步自动上传版本化 ZIP/校验和，并在 Windows Sandbox 或独立 VM 验证首次安装和旧 schema 升级，再补 macOS/Linux 原生包 |
| 3 | 按规模优化列表与图 | `selectinload` 解决库列表 N+1 后，可用聚合计数避免加载全部文档；前端力模拟现已在收敛、页面隐藏或离开关联图时停止 RAF，下一步仍应针对大节点数用浏览器 Performance 验证 O(n²) 斥力，并按索引版本缓存图请求 |

当前日常技术栈是 SQLite + FTS5 + FastAPI/StreamingResponse + TypeScript/Vite Fetch Streams + pywebview/PyInstaller，并由版本化 JSON Vault 保证原文可重建和外部编辑可续接，由 SQLAlchemy 任务表保证入库进度与重启恢复；PostgreSQL + pgvector 保留为迁移和质量对照。Windows 正式壳已经定为 pywebview 单目录冻结包；下一步建立 Releases、独立虚拟机安装门和跨平台产物。只有真实规模与测量证明精确向量扫描不足时，再引入可选向量扩展。
