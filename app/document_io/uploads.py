"""普通文本与 Markdown 图片 ZIP 的限量读取和标准化。"""

import hashlib
import io
import stat
import unicodedata
import zipfile
from pathlib import PurePosixPath

from app.core.config import settings

from .archive_paths import normalize_archive_path
from .image_validation import IMAGE_EXTENSIONS, IMAGE_FORMATS, validate_image
from .markdown_images import resolve_image_reference, scan_image_occurrences
from .models import PreparedAsset, PreparedDocument, PreparedOccurrence, UploadValidationError
from .package_hash import calculate_package_hash

PLAIN_EXTENSIONS = {".md", ".txt"}
SUPPORTED_EXTENSIONS = PLAIN_EXTENSIONS | {".zip"}
_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


def read_upload(filename: str | None, fileobj) -> PreparedDocument:
    """按扩展名限量读取 UploadFile，并返回已校验文档。"""
    title = _upload_name(filename)
    suffix = PurePosixPath(title).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise UploadValidationError("仅支持 .md / .txt，含图片文档请上传 .zip 包")
    limit = settings.max_package_bytes if suffix == ".zip" else settings.max_upload_bytes
    payload = fileobj.read(limit + 1)
    if len(payload) > limit:
        label = "ZIP 图片包" if suffix == ".zip" else "文件"
        raise UploadValidationError(f"{label}超过大小上限", status_code=413)
    return prepare_upload(title, payload)


def prepare_upload(filename: str, payload: bytes) -> PreparedDocument:
    """校验普通文本或 ZIP 图片包；供路由与纯单元测试共同调用。"""
    title = _upload_name(filename)
    suffix = PurePosixPath(title).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise UploadValidationError("仅支持 .md / .txt，含图片文档请上传 .zip 包")
    if suffix == ".zip":
        if len(payload) > settings.max_package_bytes:
            raise UploadValidationError("ZIP 图片包超过大小上限", status_code=413)
        return _prepare_zip(payload)
    if len(payload) > settings.max_upload_bytes:
        raise UploadValidationError("文件超过大小上限", status_code=413)
    if not payload.strip():
        raise UploadValidationError("文件内容为空", status_code=400)
    text = _decode_markdown(payload)
    return PreparedDocument(
        title=title,
        source_bytes=payload,
        text=text,
        content_hash=hashlib.sha256(payload).hexdigest(),
    )


def _prepare_zip(payload: bytes) -> PreparedDocument:
    if not payload:
        raise UploadValidationError("ZIP 图片包为空", status_code=400)
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, zipfile.LargeZipFile):
        raise UploadValidationError("ZIP 图片包已损坏或格式无效") from None

    with archive:
        all_infos = archive.infolist()
        if len(all_infos) > settings.max_package_files:
            raise UploadValidationError("ZIP 包内条目数量超过上限", status_code=413)
        infos: list[zipfile.ZipInfo] = []
        normalized: dict[str, zipfile.ZipInfo] = {}
        folded: set[str] = set()
        total_size = 0
        for info in all_infos:
            is_directory = info.is_dir()
            original_name = getattr(info, "orig_filename", info.filename)
            raw_path = original_name[:-1] if is_directory else original_name
            path = normalize_archive_path(raw_path)
            folded_path = path.casefold()
            if folded_path in folded:
                raise UploadValidationError(f"ZIP 包含重名路径：{path}")
            folded.add(folded_path)
            if info.flag_bits & 0x1:
                raise UploadValidationError("不支持加密 ZIP 图片包")
            if info.compress_type not in _ALLOWED_COMPRESSION:
                raise UploadValidationError("ZIP 仅支持 store/deflate 压缩方式")
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(unix_mode)
            accepted_types = {0, stat.S_IFDIR} if is_directory else {0, stat.S_IFREG}
            if file_type not in accepted_types:
                raise UploadValidationError("ZIP 包内只允许普通文件")
            if is_directory:
                if info.file_size != 0:
                    raise UploadValidationError("ZIP 目录条目大小无效")
                continue
            if info.file_size < 0 or info.compress_size < 0:
                raise UploadValidationError("ZIP 文件大小信息无效")
            total_size += info.file_size
            if total_size > settings.max_package_uncompressed_bytes:
                raise UploadValidationError("ZIP 解压后总大小超过上限", status_code=413)
            if info.file_size > 0:
                ratio = info.file_size / max(info.compress_size, 1)
                if ratio > settings.max_zip_compression_ratio:
                    raise UploadValidationError(f"ZIP 条目压缩比异常：{path}")
            normalized[path] = info
            infos.append(info)

        if not infos:
            raise UploadValidationError("ZIP 图片包为空", status_code=400)

        try:
            bad_entry = archive.testzip()
        except (EOFError, OSError, RuntimeError, zipfile.BadZipFile):
            raise UploadValidationError("ZIP 图片包完整性校验失败") from None
        if bad_entry is not None:
            raise UploadValidationError(f"ZIP 条目 CRC 校验失败：{bad_entry}")

        markdown_paths = [p for p in normalized if PurePosixPath(p).suffix.lower() == ".md"]
        if len(markdown_paths) != 1:
            raise UploadValidationError("ZIP 包内必须且只能包含一篇 .md 文档")
        unsupported = [
            p for p in normalized
            if p not in markdown_paths and PurePosixPath(p).suffix.lower() not in IMAGE_EXTENSIONS
        ]
        if unsupported:
            raise UploadValidationError(
                "ZIP 包内仅允许 Markdown 与 PNG/JPEG/WebP 图片：" + unsupported[0]
            )

        source_path = markdown_paths[0]
        if normalized[source_path].file_size > settings.max_upload_bytes:
            raise UploadValidationError("ZIP 内 Markdown 超过文本大小上限", status_code=413)
        try:
            source_bytes = archive.read(normalized[source_path])
        except (EOFError, OSError, RuntimeError, zipfile.BadZipFile):
            raise UploadValidationError("读取 ZIP 内 Markdown 失败") from None
        if not source_bytes.strip():
            raise UploadValidationError("Markdown 内容为空", status_code=400)
        text = _decode_markdown(source_bytes)
        raw_occurrences = scan_image_occurrences(text)
        if not raw_occurrences:
            raise UploadValidationError("ZIP 内 Markdown 没有可识别的本地图片引用")

        resolved_occurrences = []
        referenced: set[str] = set()
        for occurrence in raw_occurrences:
            asset_path = resolve_image_reference(source_path, occurrence.source_reference)
            if asset_path not in normalized:
                raise UploadValidationError(f"Markdown 引用的图片不存在：{occurrence.source_reference}")
            if PurePosixPath(asset_path).suffix.lower() not in IMAGE_EXTENSIONS:
                raise UploadValidationError(f"图片格式不受支持：{occurrence.source_reference}")
            resolved_occurrences.append((occurrence, asset_path))
            referenced.add(asset_path)

        packaged_images = {
            path for path in normalized if PurePosixPath(path).suffix.lower() in IMAGE_EXTENSIONS
        }
        unused = sorted(packaged_images - referenced)
        if unused:
            raise UploadValidationError(f"ZIP 包含未被 Markdown 引用的图片：{unused[0]}")

        assets: list[PreparedAsset] = []
        for asset_path in sorted(referenced):
            info = normalized[asset_path]
            if info.file_size > settings.max_image_bytes:
                raise UploadValidationError(f"单张图片超过大小上限：{asset_path}", status_code=413)
            try:
                data = archive.read(info)
            except (EOFError, OSError, RuntimeError, zipfile.BadZipFile):
                raise UploadValidationError(f"读取图片失败：{asset_path}") from None
            image_format, mime_type, width, height = validate_image(asset_path, data)
            expected_format = IMAGE_FORMATS[PurePosixPath(asset_path).suffix.lower()][0]
            if image_format != expected_format:
                raise UploadValidationError(f"图片扩展名与实际格式不一致：{asset_path}")
            assets.append(PreparedAsset(
                source_path=asset_path,
                data=data,
                content_hash=hashlib.sha256(data).hexdigest(),
                mime_type=mime_type,
                width=width,
                height=height,
            ))

        occurrences = tuple(
            PreparedOccurrence(
                ordinal=index,
                source_reference=raw.source_reference,
                asset_source_path=asset_path,
                alt_text=raw.alt_text,
                char_start=raw.char_start,
                char_end=raw.char_end,
            )
            for index, (raw, asset_path) in enumerate(resolved_occurrences, start=1)
        )
        return PreparedDocument(
            title=PurePosixPath(source_path).name,
            source_bytes=source_bytes,
            text=text,
            content_hash=calculate_package_hash(
                source_path,
                source_bytes,
                ((asset.source_path, asset.data) for asset in assets),
            ),
            source_archive_path=source_path,
            assets=tuple(assets),
            occurrences=occurrences,
        )


def _upload_name(filename: str | None) -> str:
    normalized = (filename or "").replace("\\", "/")
    title = PurePosixPath(normalized).name
    if not title or title in {".", ".."} or any(ord(char) < 32 for char in title):
        raise UploadValidationError("文件名无效")
    # 浏览器可能传 C:\\fakepath\\name.md；只保留 basename，再统一 Unicode。
    return unicodedata.normalize("NFC", title)


def _decode_markdown(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise UploadValidationError("文件编码需为 UTF-8") from None
