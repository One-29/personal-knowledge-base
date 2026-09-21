"""Markdown 图片包的版本化存储、清单校验与原图解析。"""

import hashlib
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

from app import storage
from app.core.config import settings
from app.document_io import (
    PreparedDocument,
    UploadValidationError,
    calculate_package_hash,
)
from app.document_io.archive_paths import is_canonical_archive_path
from app.document_io.image_validation import MIME_EXTENSIONS
from app.document_io.markdown_images import resolve_image_reference, scan_image_occurrences

MANIFEST_NAME = "manifest.json"
SOURCE_NAME = "source.md"
MANIFEST_FORMAT = 1
MAX_MANIFEST_BYTES = 2 * 1024 * 1024


class StorageIntegrityError(OSError):
    """已保存的图片包清单、原文或图片与登记信息不一致。"""


def save_package(
    kb_id: int,
    doc_id: int,
    version: int,
    prepared: PreparedDocument,
) -> str:
    """先完整写入同级临时目录，再原子公开一份不可变图片包版本。"""
    if not prepared.is_package or prepared.source_archive_path is None:
        raise ValueError("prepared document is not an image package")
    rel_dir = f"{kb_id}/{doc_id}/v{version}-{prepared.content_hash[:16]}"
    target = storage.resolve_relative(rel_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    source_rel = f"{rel_dir}/{SOURCE_NAME}"
    try:
        temporary.mkdir()
        source_path = temporary / SOURCE_NAME
        source_path.write_bytes(prepared.source_bytes)
        _verify_hash(source_path, hashlib.sha256(prepared.source_bytes).hexdigest())

        asset_records: list[dict] = []
        stored_by_hash: dict[tuple[str, str], str] = {}
        for asset in prepared.assets:
            key = (asset.content_hash, asset.mime_type)
            stored_rel = stored_by_hash.get(key)
            if stored_rel is None:
                suffix = MIME_EXTENSIONS[asset.mime_type]
                stored_rel = f"{rel_dir}/assets/{asset.content_hash}{suffix}"
                physical = temporary / "assets" / f"{asset.content_hash}{suffix}"
                physical.parent.mkdir(parents=True, exist_ok=True)
                physical.write_bytes(asset.data)
                _verify_hash(physical, asset.content_hash)
                stored_by_hash[key] = stored_rel
            asset_records.append({
                "source_path": asset.source_path,
                "stored_path": stored_rel,
                "content_hash": asset.content_hash,
                "mime_type": asset.mime_type,
                "file_size": asset.file_size,
                "width": asset.width,
                "height": asset.height,
            })

        manifest = {
            "format": MANIFEST_FORMAT,
            "kb_id": kb_id,
            "doc_id": doc_id,
            "version": version,
            "package_hash": prepared.content_hash,
            "source_path": source_rel,
            "source_archive_path": prepared.source_archive_path,
            "source_sha256": hashlib.sha256(prepared.source_bytes).hexdigest(),
            "source_char_count": len(prepared.text),
            "assets": asset_records,
            "occurrences": [
                {
                    "ordinal": occurrence.ordinal,
                    "source_reference": occurrence.source_reference,
                    "asset_source_path": occurrence.asset_source_path,
                    "alt_text": occurrence.alt_text,
                    "char_start": occurrence.char_start,
                    "char_end": occurrence.char_end,
                }
                for occurrence in prepared.occurrences
            ],
        }
        (temporary / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        if target.exists():
            # 进程可能在原子发布目录后、数据库提交前退出。重试相同版本时，
            # 只复用逐字段相同且所有字节仍通过校验的完整目录。
            existing = _load_manifest_path(
                target / MANIFEST_NAME,
                expected_source=source_rel,
            )
            if existing != manifest:
                raise StorageIntegrityError("同版本图片包与待写入内容不一致")
            _verify_saved_package(existing)
            return source_rel
        os.replace(temporary, target)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return source_rel


def load_package_manifest(
    rel_path: str,
    *,
    storage_root: Path | None = None,
) -> dict | None:
    """读取并校验图片包清单；普通文本版本返回 None。"""
    source = storage.resolve_relative(rel_path, storage_root=storage_root)
    manifest_path = source.parent / MANIFEST_NAME
    if source.name != SOURCE_NAME or not manifest_path.is_file():
        return None
    return _load_manifest_path(
        manifest_path,
        expected_source=rel_path,
        storage_root=storage_root,
    )


def _load_manifest_path(
    manifest_path: Path,
    *,
    expected_source: str | None = None,
    storage_root: Path | None = None,
) -> dict:
    root = (storage_root or settings.storage_dir).resolve()
    resolved_manifest = manifest_path.resolve()
    if resolved_manifest != root and root not in resolved_manifest.parents:
        raise StorageIntegrityError("图片包版本清单越出存储目录")
    if resolved_manifest.name != MANIFEST_NAME:
        raise StorageIntegrityError("图片包版本清单路径无效")
    try:
        with resolved_manifest.open("rb") as handle:
            raw_manifest = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw_manifest) > MAX_MANIFEST_BYTES:
            raise StorageIntegrityError("图片包版本清单超过大小上限")
        manifest = json.loads(
            raw_manifest.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except StorageIntegrityError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise StorageIntegrityError("图片包版本清单损坏") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT:
        raise StorageIntegrityError("图片包版本清单格式不受支持")

    required = {
        "kb_id", "doc_id", "version", "package_hash", "source_path",
        "source_archive_path", "source_sha256", "source_char_count", "assets", "occurrences",
    }
    if not required.issubset(manifest):
        raise StorageIntegrityError("图片包版本清单字段缺失")
    if any(
        not isinstance(manifest.get(field), int) or isinstance(manifest.get(field), bool)
        for field in ("kb_id", "doc_id", "version")
    ):
        raise StorageIntegrityError("图片包版本号或归属无效")
    if manifest["kb_id"] < 1 or manifest["doc_id"] < 1 or manifest["version"] < 1:
        raise StorageIntegrityError("图片包版本号或归属无效")
    if not _is_sha256(manifest["package_hash"]) or not _is_sha256(manifest["source_sha256"]):
        raise StorageIntegrityError("图片包摘要无效")
    if (
        not isinstance(manifest["source_char_count"], int)
        or isinstance(manifest["source_char_count"], bool)
        or manifest["source_char_count"] < 1
    ):
        raise StorageIntegrityError("图片包原文字数无效")
    if not isinstance(manifest["assets"], list) or not manifest["assets"]:
        raise StorageIntegrityError("图片包没有资源记录")
    if not isinstance(manifest["occurrences"], list) or not manifest["occurrences"]:
        raise StorageIntegrityError("图片包没有图片出现记录")

    package_dir = resolved_manifest.parent
    try:
        package_parts = package_dir.relative_to(root).parts
    except ValueError as exc:
        raise StorageIntegrityError("图片包版本目录越出存储目录") from exc
    if len(package_parts) != 3:
        raise StorageIntegrityError("图片包版本目录结构无效")
    expected_dir_name = f"v{manifest['version']}-{manifest['package_hash'][:16]}"
    if (
        package_parts[0] != str(manifest["kb_id"])
        or package_parts[1] != str(manifest["doc_id"])
        or package_parts[2] != expected_dir_name
    ):
        raise StorageIntegrityError("图片包清单与版本目录不匹配")
    package_rel = package_dir.relative_to(root).as_posix()
    canonical_source = f"{package_rel}/{SOURCE_NAME}"
    if manifest.get("source_path") != canonical_source:
        raise StorageIntegrityError("图片包清单与原文路径不匹配")
    if expected_source is not None and canonical_source != expected_source.replace("\\", "/"):
        raise StorageIntegrityError("图片包清单与请求原文不匹配")
    if not _is_safe_archive_path(manifest.get("source_archive_path")):
        raise StorageIntegrityError("图片包原始 Markdown 路径无效")

    asset_sources: set[str] = set()
    for asset in manifest["assets"]:
        if not isinstance(asset, dict):
            raise StorageIntegrityError("图片资源记录无效")
        source_path = asset.get("source_path")
        if not _is_safe_archive_path(source_path) or source_path in asset_sources:
            raise StorageIntegrityError("图片资源来源路径无效或重复")
        content_hash = asset.get("content_hash")
        mime_type = asset.get("mime_type")
        if not _is_sha256(content_hash) or mime_type not in MIME_EXTENSIONS:
            raise StorageIntegrityError("图片资源摘要或 MIME 类型无效")
        expected_stored = f"{package_rel}/assets/{content_hash}{MIME_EXTENSIONS[mime_type]}"
        if asset.get("stored_path") != expected_stored:
            raise StorageIntegrityError("图片资源存储路径与摘要不匹配")
        if any(
            not isinstance(asset.get(field), int)
            or isinstance(asset.get(field), bool)
            or asset[field] <= 0
            for field in ("file_size", "width", "height")
        ):
            raise StorageIntegrityError("图片资源大小或尺寸无效")
        asset_sources.add(source_path)

    previous_end = -1
    for expected_ordinal, occurrence in enumerate(manifest["occurrences"], start=1):
        if not isinstance(occurrence, dict) or occurrence.get("ordinal") != expected_ordinal:
            raise StorageIntegrityError("图片出现序号无效")
        if occurrence.get("asset_source_path") not in asset_sources:
            raise StorageIntegrityError("图片出现记录找不到对应资源")
        if not isinstance(occurrence.get("source_reference"), str) or not isinstance(
            occurrence.get("alt_text"), str
        ):
            raise StorageIntegrityError("图片出现说明无效")
        start = occurrence.get("char_start")
        end = occurrence.get("char_end")
        if (
            not isinstance(start, int) or isinstance(start, bool)
            or not isinstance(end, int) or isinstance(end, bool)
            or start < 0 or end <= start or end > manifest["source_char_count"]
            or start < previous_end
        ):
            raise StorageIntegrityError("图片出现字符位置无效")
        previous_end = end
    return manifest


def verify_stored_package(
    rel_path: str,
    *,
    expected_package_hash: str | None = None,
    storage_root: Path | None = None,
) -> tuple[dict, str]:
    """完整校验图片包清单、原文、资源与逻辑摘要。

    日常读取可以只校验实际访问的资源；备份和数据库迁移需要一次读完包内
    所有字节，因此提供这个显式的维护入口。
    """
    manifest = load_package_manifest(rel_path, storage_root=storage_root)
    if manifest is None:
        raise StorageIntegrityError("路径不是有效的 Markdown 图片包")
    if (
        expected_package_hash is not None
        and manifest["package_hash"] != expected_package_hash
    ):
        raise StorageIntegrityError("图片包摘要与文档记录不一致")

    source = storage.resolve_relative(rel_path, storage_root=storage_root)
    if not source.is_file():
        raise StorageIntegrityError("图片包原文缺失")
    source_bytes = source.read_bytes()
    try:
        text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StorageIntegrityError("图片包原文不是 UTF-8") from exc

    assets: list[tuple[str, bytes]] = []
    for asset in manifest["assets"]:
        path = storage.resolve_relative(
            asset["stored_path"],
            storage_root=storage_root,
        )
        if not path.is_file():
            raise StorageIntegrityError("图片包资源缺失")
        data = path.read_bytes()
        if len(data) != asset["file_size"]:
            raise StorageIntegrityError("图片文件大小与清单不一致")
        if hashlib.sha256(data).hexdigest() != asset["content_hash"]:
            raise StorageIntegrityError("图片包资源完整性校验失败")
        assets.append((asset["source_path"], data))

    calculated = calculate_package_hash(
        manifest["source_archive_path"],
        source_bytes,
        assets,
    )
    if calculated != manifest["package_hash"]:
        raise StorageIntegrityError("图片包逻辑摘要校验失败")
    verify_package_source(manifest, text)
    return manifest, text


def is_package_source(
    rel_path: str | None,
    *,
    storage_root: Path | None = None,
) -> bool:
    """快速判断路径是否指向一个图片包版本，不在清理路径中解析整个清单。"""
    if not rel_path:
        return False
    try:
        source = storage.resolve_relative(rel_path, storage_root=storage_root)
    except ValueError:
        return False
    return source.name == SOURCE_NAME and (source.parent / MANIFEST_NAME).is_file()


def verify_package_source(manifest: dict | None, text: str) -> None:
    """核对原文摘要，并重新解析图片位置以防清单与 Markdown 漂移。"""
    if manifest is None:
        return
    if len(text) != manifest["source_char_count"]:
        raise StorageIntegrityError("图片包原文字数与版本清单不一致")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != manifest["source_sha256"]:
        raise StorageIntegrityError("图片包原文完整性校验失败")
    try:
        parsed = scan_image_occurrences(text)
        expected = manifest["occurrences"]
        if len(parsed) != len(expected):
            raise StorageIntegrityError("图片出现记录与原文不一致")
        for occurrence, saved in zip(parsed, expected, strict=True):
            resolved = resolve_image_reference(
                manifest["source_archive_path"], occurrence.source_reference
            )
            if (
                occurrence.source_reference != saved["source_reference"]
                or occurrence.alt_text != saved["alt_text"]
                or occurrence.char_start != saved["char_start"]
                or occurrence.char_end != saved["char_end"]
                or resolved != saved["asset_source_path"]
            ):
                raise StorageIntegrityError("图片出现记录与原文不一致")
    except UploadValidationError as exc:
        raise StorageIntegrityError("图片包原文无法按清单重新解析") from exc


def find_version_manifest(kb_id: int, doc_id: int, version: int) -> dict | None:
    doc_dir = storage.resolve_relative(f"{kb_id}/{doc_id}")
    if not doc_dir.is_dir():
        return None
    matches = sorted(doc_dir.glob(f"v{version}-*/{MANIFEST_NAME}"))
    if not matches:
        return None
    if len(matches) != 1:
        raise StorageIntegrityError("同一文档版本存在多个图片包")
    return _load_manifest_path(matches[0])


def resolve_version_image(
    kb_id: int,
    doc_id: int,
    version: int,
    ordinal: int,
) -> tuple[Path, dict, dict] | None:
    """返回绝对图片路径、资源记录和出现记录，并校验原件摘要。"""
    manifest = find_version_manifest(kb_id, doc_id, version)
    if manifest is None:
        return None
    occurrence = next(
        (item for item in manifest["occurrences"] if item["ordinal"] == ordinal), None
    )
    if occurrence is None:
        return None
    asset = next(
        (
            item for item in manifest["assets"]
            if item["source_path"] == occurrence["asset_source_path"]
        ),
        None,
    )
    if asset is None:
        raise StorageIntegrityError("图片包清单的资源映射缺失")
    path = storage.resolve_relative(asset["stored_path"])
    if not path.is_file():
        raise FileNotFoundError(path)
    _verify_hash(path, asset["content_hash"])
    if path.stat().st_size != asset["file_size"]:
        raise StorageIntegrityError("图片文件大小与清单不一致")
    return path, asset, occurrence


def _verify_saved_package(
    manifest: dict,
    *,
    storage_root: Path | None = None,
) -> None:
    """读取并校验一整份已发布包；仅用于碰撞恢复，避免日常列表全量读图。"""
    source = storage.resolve_relative(
        manifest["source_path"],
        storage_root=storage_root,
    )
    if not source.is_file():
        raise StorageIntegrityError("图片包原文缺失")
    _verify_hash(source, manifest["source_sha256"])
    for asset in manifest["assets"]:
        path = storage.resolve_relative(
            asset["stored_path"],
            storage_root=storage_root,
        )
        if not path.is_file():
            raise StorageIntegrityError("图片包资源缺失")
        if path.stat().st_size != asset["file_size"]:
            raise StorageIntegrityError("图片文件大小与清单不一致")
        _verify_hash(path, asset["content_hash"])


def _verify_hash(path: Path, expected_hash: str) -> None:
    try:
        storage.verify_hash(path, expected_hash)
    except OSError as exc:
        raise StorageIntegrityError(str(exc)) from exc


def _is_sha256(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def _is_safe_archive_path(value) -> bool:
    return is_canonical_archive_path(value)
