"""活动原文、图片资源与 chunk 溯源锚点的快照校验。"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app import package_storage

from .errors import IntegrityError
from .report import FileAuditSummary


@dataclass(frozen=True)
class StorageAudit:
    summary: FileAuditSummary
    document_texts: dict[int, str]


def audit_storage(
    documents: Iterable[Mapping[str, Any]],
    storage_root: Path,
) -> StorageAudit:
    """校验所有活动原文及图片资源，并返回供 chunk 锚点核对的文本。"""
    root = storage_root.expanduser().resolve()
    document_rows = list(documents)
    file_records: dict[str, tuple[int, str]] = {}
    texts: dict[int, str] = {}
    packages = 0
    assets = 0

    for doc in document_rows:
        doc_id = int(doc["id"])
        status = str(doc["status"])
        if status in {"pending", "processing"}:
            raise IntegrityError(
                f"文档 {doc_id} 仍处于 {status}，请等待入库结束后再迁移"
            )
        pending_fields = (
            doc.get("pending_file_path"),
            doc.get("pending_content_hash"),
            doc.get("pending_char_count"),
        )
        if any(value is not None for value in pending_fields):
            raise IntegrityError(
                f"文档 {doc_id} 仍保留候选原文状态，不能生成静态迁移快照"
            )

        rel_path = str(doc.get("file_path") or "")
        if not rel_path:
            if status == "ready" or int(doc["chunk_count"]) != 0:
                raise IntegrityError(f"文档 {doc_id} 缺少活动原文路径")
            continue

        source = _safe_storage_path(root, rel_path)
        if not source.is_file():
            raise IntegrityError(f"文档 {doc_id} 的活动原文不存在：{rel_path}")

        try:
            manifest = package_storage.load_package_manifest(
                rel_path,
                storage_root=root,
            )
            if manifest is None:
                source_bytes = source.read_bytes()
                if hashlib.sha256(source_bytes).hexdigest() != doc["content_hash"]:
                    raise IntegrityError(f"文档 {doc_id} 的原文摘要不匹配")
                text = source_bytes.decode("utf-8")
                _register_file(file_records, root, source, source_bytes)
            else:
                manifest, text = package_storage.verify_stored_package(
                    rel_path,
                    expected_package_hash=str(doc["content_hash"]),
                    storage_root=root,
                )
                if (
                    manifest["kb_id"] != int(doc["kb_id"])
                    or manifest["doc_id"] != doc_id
                    or manifest["version"] != int(doc["ingest_version"])
                ):
                    raise IntegrityError(
                        f"文档 {doc_id} 的图片包归属或版本与数据库不一致"
                    )
                packages += 1
                assets += len(manifest["assets"])
                package_files = [
                    source,
                    source.parent / package_storage.MANIFEST_NAME,
                    *(
                        _safe_storage_path(root, asset["stored_path"])
                        for asset in manifest["assets"]
                    ),
                ]
                for path in package_files:
                    _register_file(file_records, root, path, path.read_bytes())
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise IntegrityError(
                f"文档 {doc_id} 的原文完整性校验失败：{exc}"
            ) from exc

        if len(text) != int(doc["char_count"]):
            raise IntegrityError(
                f"文档 {doc_id} 字符数不一致："
                f"数据库={doc['char_count']}，文件={len(text)}"
            )
        texts[doc_id] = text

    digest = hashlib.sha256()
    total_bytes = 0
    for rel_path, (size, sha256) in sorted(file_records.items()):
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\n")
        total_bytes += size

    return StorageAudit(
        summary=FileAuditSummary(
            documents=len(document_rows),
            packages=packages,
            assets=assets,
            files=len(file_records),
            bytes=total_bytes,
            combined_sha256=digest.hexdigest(),
        ),
        document_texts=texts,
    )


class SnapshotValidator:
    """在复制流中验证外键冗余、切块锚点、序号和数量。"""

    def __init__(
        self,
        knowledge_bases: Iterable[Mapping[str, Any]],
        documents: Iterable[Mapping[str, Any]],
        document_texts: Mapping[int, str],
    ) -> None:
        self._kb_ids = {int(row["id"]) for row in knowledge_bases}
        self._documents = {int(row["id"]): row for row in documents}
        self._texts = document_texts
        self._counts: Counter[int] = Counter()
        self._indices: dict[int, set[int]] = defaultdict(set)

        for doc_id, doc in self._documents.items():
            if int(doc["kb_id"]) not in self._kb_ids:
                raise IntegrityError(f"文档 {doc_id} 引用了不存在的知识库")

    def check_chunk(self, chunk: Mapping[str, Any]) -> None:
        chunk_id = int(chunk["id"])
        doc_id = int(chunk["doc_id"])
        doc = self._documents.get(doc_id)
        if doc is None:
            raise IntegrityError(f"块 {chunk_id} 引用了不存在的文档")
        if int(chunk["kb_id"]) != int(doc["kb_id"]):
            raise IntegrityError(f"块 {chunk_id} 的冗余 kb_id 与文档不一致")

        index = int(chunk["chunk_index"])
        if index < 0 or index in self._indices[doc_id]:
            raise IntegrityError(f"文档 {doc_id} 的块序号无效或重复：{index}")
        self._indices[doc_id].add(index)
        self._counts[doc_id] += 1

        text = self._texts.get(doc_id)
        if text is None:
            raise IntegrityError(f"块 {chunk_id} 找不到可校验的活动原文")
        start = int(chunk["char_start"])
        end = int(chunk["char_end"])
        if start < 0 or end <= start or end > len(text):
            raise IntegrityError(f"块 {chunk_id} 的原文区间越界：[{start}, {end})")
        if text[start:end] != chunk["content"]:
            raise IntegrityError(
                f"块 {chunk_id} 的内容与原文区间不一致；"
                "请先重新索引该文档（旧版 CRLF 换行归一化也会触发此检查）"
            )

    def finish(self) -> None:
        for doc_id, doc in self._documents.items():
            expected = int(doc["chunk_count"])
            actual = self._counts[doc_id]
            if actual != expected:
                raise IntegrityError(
                    f"文档 {doc_id} 的块数量不一致：数据库={expected}，实际={actual}"
                )
            if self._indices[doc_id] != set(range(actual)):
                raise IntegrityError(f"文档 {doc_id} 的块序号不连续")
            if doc["status"] == "ready" and actual == 0:
                raise IntegrityError(f"ready 文档 {doc_id} 没有可检索块")


def _safe_storage_path(root: Path, rel_path: str) -> Path:
    candidate = (root / rel_path).resolve()
    if candidate == root or root not in candidate.parents:
        raise IntegrityError(f"存储路径越出根目录：{rel_path}")
    return candidate


def _register_file(
    records: dict[str, tuple[int, str]],
    root: Path,
    path: Path,
    data: bytes,
) -> None:
    resolved = path.resolve()
    if root not in resolved.parents or not resolved.is_file():
        raise IntegrityError(f"迁移文件不存在或越出存储根目录：{path}")
    rel_path = resolved.relative_to(root).as_posix()
    record = (len(data), hashlib.sha256(data).hexdigest())
    previous = records.setdefault(rel_path, record)
    if previous != record:
        raise IntegrityError(f"迁移期间文件发生变化：{rel_path}")
