"""文档上传解析产生的值对象。"""

from dataclasses import dataclass


class UploadValidationError(ValueError):
    """可以安全返回给上传者的校验错误。"""

    def __init__(self, detail: str, *, status_code: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class PreparedAsset:
    """ZIP 中一份图片文件；data 始终是解压后的原始字节。"""

    source_path: str
    data: bytes
    content_hash: str
    mime_type: str
    width: int
    height: int

    @property
    def file_size(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class PreparedOccurrence:
    """Markdown 中一次图片出现；同一文件重复引用会产生多个 occurrence。"""

    ordinal: int
    source_reference: str
    asset_source_path: str
    alt_text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class PreparedDocument:
    """通过校验、可以原子写入候选版本的文档。"""

    title: str
    source_bytes: bytes
    text: str
    content_hash: str
    source_archive_path: str | None = None
    assets: tuple[PreparedAsset, ...] = ()
    occurrences: tuple[PreparedOccurrence, ...] = ()

    @property
    def is_package(self) -> bool:
        return self.source_archive_path is not None


@dataclass(frozen=True)
class ImageOccurrenceMatch:
    """从 Markdown 原文解析出、尚未解析为 ZIP 内路径的一次图片出现。"""

    source_reference: str
    alt_text: str
    char_start: int
    char_end: int
