# 运行与维护

| 字段 | 内容 |
|---|---|
| 状态 | 已确认（关键修复、并发回归与数据环境隔离已实施） |
| 版本 | v0.3 |
| 日期 | 2026-09-14 |
| 上游 | `design/03-data-model.md` · `design/04-retrieval.md` · `design/05-agent-workflow.md` |
| 关联 | A1–A2、B1–B4、C1–C7 修复 · WSL2 命令行运行环境 |

本文区分代码已经保证的行为、部署边界与后续建议。性能缺陷能解释请求等待，不足以证明某次浏览器或桌面 GUI 卡死的原因；确认实际原因仍需要对应请求的日志与耗时。

## 1. 当前运行边界

### WSL2 独立 Docker Engine 与命令行启动

KnowBase 不依赖 Docker Desktop。PostgreSQL 与 pgvector 运行在 Ubuntu WSL2 内的独立 Docker Engine 中，Windows PowerShell 通过 `wsl.exe` 调用它；API 继续使用项目的 Windows `.venv`，通过 WSL localhost 转发连接 `127.0.0.1:5432`。

首次配置运行下面的安装器。它要求 Ubuntu WSL2 已启用 [`systemd`](https://learn.microsoft.com/windows/wsl/systemd)，按 [Docker Engine Ubuntu 安装说明](https://docs.docker.com/engine/install/ubuntu/)从官方 apt 仓库安装 Engine、containerd、Buildx 和 Compose 插件，启用 `docker.service`，并把 WSL 默认用户加入 `docker` 用户组。Docker 官方说明该用户组拥有接近 root 的控制权限，详见[安装后配置](https://docs.docker.com/engine/install/linux-postinstall/)。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-wsl-docker-engine.ps1
```

日常从项目根目录启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-knowbase.ps1
```

启动器读取 `compose.yaml`，创建或复用 `knowbase-pg` 与 WSL 命名卷 `knowbase_pgdata`，等待数据库健康检查，执行 Alembic 迁移，再以单 worker 启动 API；`/ready` 确认 API 与数据库均可用后才打开浏览器。`/health` 只检查 API 进程存活，不访问数据库。首次拉取 `pgvector/pgvector:pg16` 镜像可能需要几分钟。重复启动时，如果 API 已经正常运行，脚本只打开界面；如果 8000 端口被其它程序占用，则明确报错。

WSL 路径转换只让 `wslpath` 返回 ASCII 的盘符挂载点，再由 PowerShell 拼接未经转码的目录部分，避免 Windows PowerShell 5.1 按本机代码页误解 WSL 的 UTF-8 输出。因此项目目录可以包含空格、中文和常见特殊字符，例如 `E:\ds Harness\实践项目`，也可以位于任意已挂载到 WSL 的本地 Windows 盘符。启动器不支持 `\\server\share` 形式的 UNC 或网络共享路径；从 GitHub 下载或克隆后，请把仓库放在 `C:`、`D:`、`E:` 等本地磁盘上。

WSL2 的 [`vmIdleTimeout`](https://learn.microsoft.com/windows/wsl/wsl-config) 默认会在虚拟机空闲后停止它。仅有 `systemd`、Docker 与容器服务时，这台机器仍可能被判定为空闲，因此启动模块用 `flock` 建立一个无窗口、单实例的 `sleep infinity` 保活进程。它只负责维持 Ubuntu 运行，不处理请求，也不持有数据库连接。按 `Ctrl+C` 会停止 Windows API，数据库和保活进程继续运行；如需一并释放资源，停止 API 后执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-knowbase-database.ps1
```

也可以安装桌面 **KnowBase** 快捷方式。快捷方式只是 PowerShell 启动命令的入口，不会打开或调用 Docker Desktop：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-desktop-shortcut.ps1
```

常用只读维护命令如下。`docker logs -f` 持续占用当前终端，按 `Ctrl+C` 退出日志查看，不会停止数据库。

```powershell
wsl.exe -d Ubuntu -- docker ps
wsl.exe -d Ubuntu -- docker logs --tail 100 knowbase-pg
wsl.exe -d Ubuntu -- docker logs -f knowbase-pg
wsl.exe -d Ubuntu -- docker volume inspect knowbase_pgdata
```

数据库使用密码认证，默认本机开发账号为 `postgres` / `postgres`；Compose 在 WSL 虚拟机接口发布 5432，再通过 [WSL localhost 转发](https://learn.microsoft.com/windows/wsl/networking)提供给 Windows。`DATABASE_URL` 必须与之匹配。若以后改密码，需要同时重建数据库卷或在 PostgreSQL 中修改角色密码，并同步更新 `.env`。

从 `pg_dump -Fc` 生成的快照恢复日常数据库和原文目录时，先停止 API，再运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\restore-knowbase-backup.ps1 `
  -BackupDirectory .\data\backups\<备份目录>
```

恢复器验证 `PGDMP` 文件头，先把快照完整恢复到临时数据库，成功后才短暂切换为 `knowbase`；损坏快照不会先清空当前数据库。原文恢复前，现有 `data/storage` 会复制到带时间戳的 `data/backups/before-restore-*`，随后按快照重建，避免残留旧文件。删除前脚本会核对绝对路径必须精确位于项目 `data` 目录下。这是有意覆盖日常数据的操作，PowerShell 会显示确认提示；测试和评估数据库不从这份快照恢复。

### 单 worker 与后台任务

开发使用 `uvicorn app.main:app --reload`；演示使用 `uvicorn app.main:app --workers 1`，避免修改文件重启正在运行的服务。

- `session.store` 是进程内对象，不跨 worker 共享。仅携带 `session_id` 的兼容客户端在多 worker 下可能无法取得前一请求的历史。
- `BackgroundTasks` 在处理请求的进程内执行，没有持久化队列、任务认领或跨进程恢复。进程退出后任务不会由另一 worker 自动接续。
- 当前没有启动时扫描并恢复遗留 `pending` / `processing` 的流程；本轮补修的异常处理会在数据库可写时记录 `PROCESS_FAILED`，但不覆盖进程被终止或数据库持续不可用的场景。
- 多知识库通过 `kb_id` 区分，与单进程部署没有冲突。增加 worker 不会使现有任务与会话自动具备跨进程一致性；当前支持范围仍是单 worker。

### 会话的两个存活范围

| 来源 | 生命周期 | 对用户的影响 |
|---|---|---|
| 浏览器 `localStorage` + 请求 `history` | 浏览器本地保存；清理站点数据或换设备后不共享 | 当前界面刷新后仍可继续追问；后端重启不清空浏览器保存的历史 |
| 兼容接口 `session_id` + `session.store` | 默认 TTL **30 分钟**，最近一次追加对话后计时；进程重启即清空 | TTL 到期或重启后，单凭旧 `session_id` 不能恢复上文；线程锁只解决同进程并发安全 |

TTL 由 `SESSION_TTL_SECONDS` 配置；到期记录在访问存储时清理，服务端没有独立的持久化会话表。

### 问答与工作流可以同时运行

同一浏览器页面为普通问答和工作流分别维护请求控制器与忙碌状态，因此允许一个普通问答和一个工作流同时在途；同类入口各自限制为一个在途请求。两个同步 FastAPI 路由在线程池中执行，每个请求由 `get_db()` 创建并关闭独立 SQLAlchemy Session。工作流内部仍按规划顺序逐步执行，不并行共享 Session。

请求发起时会固定浏览器会话 ID。任务完成后结果写回发起时的会话，即使用户期间切换了会话也不会串记录；切回原会话即可查看。若原会话已删除，结果不会写入本地记录，界面会提示本次结果未保存，也不会复活被删除的会话。并发回归测试同时验证两个 HTTP 请求发生执行重叠、获得不同请求级 Session，并在结束后分别关闭。

这个能力面向本机单用户的一问答加一工作流。多个标签页仍能产生更多服务端请求，当前没有全局模型排队、优先级或供应商限流退避；模型端的并发额度仍可能成为瓶颈。

### 日常、测试、评估与演示数据

| 用途 | PostgreSQL 数据库 | 原文目录 | 日常界面 |
|---|---|---|---:|
| 个人资料和高等数学演示库 | `knowbase` | `data/storage` | 可见 |
| pytest 数据库集成测试 | `knowbase_test` | `data/test-storage-*` | 不可见 |
| 检索与拒答评估 | `knowbase_eval` | `data/eval-storage` | 不可见 |

运行评估统一使用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-eval.ps1 -Retrieval
```

脚本会创建或复用 `knowbase_eval`，在该数据库执行迁移，并只为评估子进程设置数据库和存储目录。`eval.run_eval` 会先校验配置，再用只读查询核对 Session 实际连接的数据库：两者都必须精确指向 `knowbase_eval`，原文目录必须精确为项目内的 `data/eval-storage`；任何条件不满足都会在业务查询和删除前拒绝执行。评估语料先写入临时库，全部文档进入 `ready` 后才在一个事务中替换正式评估库；中途失败会保留上一次完整评估库。直接在日常配置下运行 `python -m eval.run_eval` 会拒绝执行。

高等数学演示库通过以下命令幂等加载：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\load-calculus-demo.ps1
```

加载器只替换同名“大一上高等数学演示库”，并采用临时库完整处理后再切换正式名称。8 篇文档与阈值演示步骤见[高等数学演示指南](demo.md)。

### 「停止」仅停止客户端等待

点击「停止」会通过 `AbortController` 断开前端等待，被停止的问题不写入前端会话记录。当前同步 `httpx.Client` 调用没有接入请求断连或取消令牌；服务端仍继续当前问答或工作流，已经发出的远程调用仍会消耗时间与额度。

普通首问通常包含 1 次 Embedding 和 1 次回答生成；带历史的追问还会增加 1 次改写生成。默认五步工作流的完整成功路径包含 1 次规划生成、5 次 Embedding、5 次回答生成，共 11 次远程模型调用；汇总本身按规则拼接，不再次调用 LLM。提前拒答或规划退化会减少调用次数。

复用 HTTP 连接与释放数据库事务均不会把「停止」变成服务端取消。真正取消需要另行设计任务取消状态与执行阶段检查；仅增加 SSE 输出也不会自动终止同步调用。

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

## 4. 关键词与关联图的含义

关键词召回先用 `content % :query` 预过滤，再按 `similarity()` 排序。阈值通过事务局部 `set_config('pg_trgm.similarity_threshold', ..., true)` 设置，事务结束后不污染复用连接；初始 `KEYWORD_SIMILARITY_THRESHOLD=0.1`，应随真实中文笔记评估校准。

`%` 是 `gin_trgm_ops` 支持的相似度运算符。查询具备使用该索引的条件，实际执行计划仍由 PostgreSQL 按数据量和选择性选择；小表选择顺序扫描不能单独证明修复无效。可在代表性数据上检查 `EXPLAIN (ANALYZE, BUFFERS)`，同时记录召回质量。参见 [PostgreSQL 16 pg_trgm 文档](https://www.postgresql.org/docs/16/pgtrgm.html)。

关联图最多用 400 个源块发起近邻查询，按文档轮流抽取以减少早期导入的大文档垄断；每个源块仍在该库其它文档的全部块中寻找近邻。超过源块计算上限时即使没有边也返回 `truncated=true`；图是导航近似结果，不代表完整的文档关系，也不影响问答检索。

## 5. 如何区分「卡住」

| 观察到的现象 | 可核对的证据 | 判断边界 |
|---|---|---|
| 工作流前一步出错，后续步骤全部数据库报错 | 日志出现首个 SQL 错误，随后出现 `current transaction is aborted` | 对应事务未恢复；验证点是下一步真实 SQL 能再次成功 |
| 多个页面请求一起等待后超时 | 连接池等待超时、连接占用数、数据库 `idle in transaction` 与模型请求重叠 | 属于资源占用问题；单用户低并发不一定出现 |
| 问答长时间等待但列表与健康接口仍能响应 | Embedding、改写、检索、生成各阶段耗时和远程超时日志 | 可能主要等待模型；当前接口在工作结束后统一响应 |
| 导入文档长期停在 `pending` / `processing` | 对应文档任务日志、服务是否重启、`last_error` | 需区分任务丢失与处理异常，不能只靠刷新页面判断 |
| 页面滚动、点击或浏览器窗口本身失去响应 | 浏览器 Performance 的主线程长任务，图节点规模与重绘次数 | 需要前端证据，不能直接归因于数据库连接池 |

## 6. 修复后的优化顺序

以下是基于当前代码与产品范围的建议，不表示这些改动已经完成，也不是现网压测结论。

| 优先级 | 建议 | 原因与验收方式 |
|---|---|---|
| 1 | 记录请求 id、模型/检索分阶段耗时、文档 id 与工作流步骤；给模型链路设置总耗时预算 | 让「卡住」可定位到模型、数据库或前端；验证超时错误能说明阶段，并观测多个页面同时操作时列表仍能响应 |
| 完成 | 重传采用不可变候选原文与单调版本，处理成功后统一切换 | Embedding 失败继续使用匹配的旧原文和旧块；处理中再次重传时，较早任务的结果与错误均不会覆盖新版本 |
| 1 | 增加显式重新索引入口与索引版本记录 | B1 等切分算法修复不能自动修好旧块；记录切块版本、Embedding 模型/维度与重建时间，以便只处理受影响文档 |
| 2 | 扩大真实中文多库评估集 | 本轮已让 MRR 把未命中计为 0，并同时校验来源和关键词；下一步用短术语、长行、同名文档跨库与库外问题校准阈值，早期 3 篇/12 条样本不足以代表真实多库 |
| 完成 | 给大文档 Embedding 分批并校验供应商响应 | 默认每批 32 块；限流、5xx 与传输故障只重试当前批次。HTTP 200 的 JSON 结构、数量、index、数值有效性和维度均在 provider 边界校验；测试覆盖中途批次失败与退避重试 |
| 2 | 给入库任务增加可恢复的任务记录 | 同文档版本检查已经阻止旧任务覆盖新内容；当前单进程后台任务仍不能跨重启续接，验收还需包含处理过程中重启与自动恢复 |
| 3 | 验证干净安装与构建产物 | CI 已在独立空库执行 `alembic upgrade head` 与 `alembic check`，并让默认 embedding 配置与当前 `vector(1024)` 迁移一致；后续仍需构建 wheel 并在无源码目录的干净环境验证前端、迁移与启动脚本是否齐全 |
| 3 | 按规模优化列表与图 | `selectinload` 解决库列表 N+1 后，可用聚合计数避免加载全部文档；前端力模拟现已在收敛、页面隐藏或离开关联图时停止 RAF，下一步仍应针对大节点数用浏览器 Performance 验证 O(n²) 斥力，并按索引版本缓存图请求 |

先保持 PostgreSQL + pgvector + FastAPI 的现有技术栈完成数据正确性、可观测性与恢复能力。只有确实需要多进程吞吐、任务恢复或更大文档规模时，再选择共享会话与任务调度方案。
