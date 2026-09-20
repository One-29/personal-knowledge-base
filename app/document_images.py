"""把版本清单中的图片出现映射为稳定的 API 数据。"""

from __future__ import annotations

from dataclasses import dataclass

from . import package_storage


@dataclass(frozen=True)
class ImageReferenceData:
    """一张图片在 Markdown 中的一次出现，而不是一份去重后的文件。"""

    ordinal: int
    source_reference: str
    alt_text: str
    char_start: int
    char_end: int
    content_hash: str
    mime_type: str
    file_size: int
    width: int
    height: int
    content_url: str


def images_for_source(
    doc_id: int,
    source_path: str,
    *,
    char_start: int | None = None,
    char_end: int | None = None,
) -> list[ImageReferenceData]:
    """读取一次图片出现列表；传字符区间时仅返回与区间相交的图片。"""
    manifest = package_storage.load_package_manifest(source_path)
    return images_from_manifest(
        doc_id,
        manifest,
        char_start=char_start,
        char_end=char_end,
    )


def images_from_manifest(
    doc_id: int,
    manifest: dict | None,
    *,
    char_start: int | None = None,
    char_end: int | None = None,
) -> list[ImageReferenceData]:
    """把已校验清单转换为 API 数据，避免同一请求重复读取清单。"""
    if (char_start is None) != (char_end is None):
        raise ValueError("char_start and char_end must be provided together")
    if (
        char_start is not None
        and char_end is not None
        and (char_start < 0 or char_end <= char_start)
    ):
        raise ValueError("image source range is invalid")
    if manifest is None:
        return []
    assets = {item.get("source_path"): item for item in manifest["assets"]}
    version = int(manifest["version"])
    result: list[ImageReferenceData] = []
    for occurrence in sorted(manifest["occurrences"], key=lambda item: item.get("ordinal", 0)):
        start = int(occurrence["char_start"])
        end = int(occurrence["char_end"])
        if char_start is not None and char_end is not None:
            if end <= char_start or start >= char_end:
                continue
        asset = assets.get(occurrence.get("asset_source_path"))
        if asset is None:
            raise package_storage.StorageIntegrityError("图片出现记录找不到对应资源")
        ordinal = int(occurrence["ordinal"])
        result.append(
            ImageReferenceData(
                ordinal=ordinal,
                source_reference=str(occurrence["source_reference"]),
                alt_text=str(occurrence.get("alt_text", "")),
                char_start=start,
                char_end=end,
                content_hash=str(asset["content_hash"]),
                mime_type=str(asset["mime_type"]),
                file_size=int(asset["file_size"]),
                width=int(asset["width"]),
                height=int(asset["height"]),
                content_url=(
                    f"/api/v1/documents/{doc_id}/versions/{version}/images/{ordinal}"
                ),
            )
        )
    return result
