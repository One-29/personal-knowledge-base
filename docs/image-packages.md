# Markdown 本地图片包

KnowBase 的检索对象仍是一篇 Markdown 文本。笔记含本地图片时，用 ZIP 作为传输容器，把 Markdown 和它引用的原图一起上传。系统保存原始图片字节、图片在 Markdown 中的出现顺序和字符位置；图片不经过转码，也不做 OCR、视觉理解或向量化。

## 1. 最小目录示例

```text
高等数学-极限.zip
└── notes/
    ├── 极限.md
    └── images/
        ├── epsilon-delta.png
        └── squeeze.jpg
```

`notes/极限.md` 使用相对于自身位置的路径：

```markdown
# 极限

![ε-δ 定义示意](images/epsilon-delta.png)

夹逼准则如下图所示：

![夹逼准则](images/squeeze.jpg)
```

PowerShell 可以在内容目录的上一级创建包：

```powershell
Compress-Archive -Path .\notes -DestinationPath .\高等数学-极限.zip
```

ZIP 内必须恰好有一篇 `.md`。图片支持静态 `.png`、`.jpg`、`.jpeg` 和 `.webp`；每张图片必须被 Markdown 引用，包内也不能放其它文件。

## 2. 支持的 Markdown 写法

支持行内图片和引用式图片：

```markdown
![说明](images/a.png)
![结果][result]

[result]: images/result.webp
```

相对路径可从 Markdown 所在目录进入子目录或返回父目录，但解析后的目标必须仍位于 ZIP 根目录。路径按 UTF-8 和 Unicode NFC 校验，使用 `/` 分隔；大小写不同但折叠后相同的两个路径会被视为冲突，以保证 Windows、macOS 和 Linux 行为一致。

以下写法会被拒绝：

- HTTP(S)、`data:`、盘符路径或 `/` 开头的绝对路径；
- 越出 ZIP 根目录的 `../`；
- 带查询参数或片段的图片路径，包括百分号编码后的 `?` 和 `#`；
- HTML `<img>`、动态图片、符号链接、加密 ZIP 和非普通文件；
- 缺失的引用图片、未被引用的多余图片、扩展名与实际格式不一致的图片。

代码围栏和行内代码中的图片示例不会被当作真实图片引用。

## 3. 默认限制

| 配置 | 默认值 | 作用 |
|---|---:|---|
| `MAX_UPLOAD_BYTES` | 10 MiB | 普通文本，以及 ZIP 内 Markdown 的大小上限 |
| `MAX_PACKAGE_BYTES` | 50 MiB | ZIP 压缩文件上限 |
| `MAX_PACKAGE_UNCOMPRESSED_BYTES` | 100 MiB | ZIP 所有普通文件解压后总大小上限 |
| `MAX_PACKAGE_FILES` | 200 | ZIP 条目总数上限，显式目录也计数 |
| `MAX_IMAGE_BYTES` | 25 MiB | 单张图片字节上限 |
| `MAX_IMAGE_PIXELS` | 40,000,000 | 单张图片宽 × 高上限 |
| `MAX_ZIP_COMPRESSION_RATIO` | 100 | 单个 ZIP 条目的最大解压缩比 |

上传时先限量读取压缩包，再验证所有条目路径、类型、压缩方式、大小、压缩比和 CRC，最后解码验证图片。校验全部通过后才会写入候选版本。

## 4. 存储与版本语义

每个图片包写入独立的不可变目录：

```text
data/storage/{kb_id}/{doc_id}/v{version}-{package_hash16}/
├── source.md
├── manifest.json
└── assets/{image_sha256}.{ext}
```

`manifest.json` 记录文档归属、版本、原文摘要、每个资源的摘要/尺寸/MIME，以及每次图片出现的序号和 Markdown 字符区间。读取完整原文时会重新校验原文摘要和图片位置；读取原图时会再次校验原图 SHA-256 与大小。包在同级临时目录完整写好后才原子发布，数据库提交前进程退出时，相同重试只会复用内容完全一致且通过全量校验的目录。

成功重传后，旧图片包会保留到文档删除，使历史回答中已经保存的原图 URL 仍然有效。删除文档或知识库会清理其全部历史包。频繁替换大图会增加磁盘占用，这是维持历史引用可核对性的明确取舍。

## 5. 检索与展示边界

切块器把完整的 Markdown 图片语法当作不可切断区间，因此一个引用块不会只包含半截图片标记。检索、embedding 和 LLM 只看到 Markdown 文本、alt 文本与周围正文，不读取图片像素。

`GET /api/v1/documents/{doc_id}/content` 和 `GET /api/v1/citations/{chunk_id}` 会返回图片出现清单。原图由下面的不可变版本接口提供：

```text
GET /api/v1/documents/{doc_id}/versions/{version}/images/{ordinal}
```

前端按字符位置把图片插回原文或引用片段；同一文件被引用多次时，每次出现都有独立序号，显示顺序与 Markdown 一致。
