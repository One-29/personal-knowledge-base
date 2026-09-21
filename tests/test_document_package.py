"""一期 Markdown 图片包：位置、原件字节、ZIP 安全与 API 集成。"""

import hashlib
import io
import json
import stat
import zipfile

import pytest
from PIL import Image
from sqlalchemy import select

from app import ingest, package_storage, storage
from app.core.config import settings
from app.document_io import UploadValidationError, prepare_upload
from app.models import Chunk, Document


def _image_bytes(fmt: str, color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (7, 5), color).save(output, format=fmt)
    return output.getvalue()


def _animated_png() -> bytes:
    output = io.BytesIO()
    first = Image.new("RGB", (7, 5), (20, 80, 140))
    second = Image.new("RGB", (7, 5), (140, 80, 20))
    first.save(output, format="PNG", save_all=True, append_images=[second], duration=100)
    return output.getvalue()


PNG = _image_bytes("PNG", (20, 80, 140))
JPEG = _image_bytes("JPEG", (180, 90, 20))
WEBP = _image_bytes("WEBP", (40, 150, 80))
ANIMATED_PNG = _animated_png()


def _zip(entries: dict[str, bytes], *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return output.getvalue()


def _zip_with_symlink() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doc.md", b"![x](x.png)")
        link = zipfile.ZipInfo("x.png")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, b"target.png")
    return output.getvalue()


def _package(markdown: str | None = None, *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    source = markdown or (
        "# 多图示例\r\n"
        "第一张：![蓝图](images/one.png)\r\n"
        "中间文字😀\r\n"
        "第二张：![结果](images/two.jpg)\r\n"
        "再次引用：![蓝图复用](images/one.png)\r\n"
    )
    return _zip(
        {
            "notes/chapter.md": source.encode("utf-8"),
            "notes/images/one.png": PNG,
            "notes/images/two.jpg": JPEG,
        },
        compression=compression,
    )


def test_prepare_package_preserves_source_positions_order_and_original_bytes():
    prepared = prepare_upload("chapter.zip", _package())

    assert prepared.title == "chapter.md"
    assert prepared.source_bytes.decode("utf-8") == prepared.text
    assert [item.ordinal for item in prepared.occurrences] == [1, 2, 3]
    assert [item.asset_source_path for item in prepared.occurrences] == [
        "notes/images/one.png",
        "notes/images/two.jpg",
        "notes/images/one.png",
    ]
    assert [prepared.text[item.char_start:item.char_end] for item in prepared.occurrences] == [
        "![蓝图](images/one.png)",
        "![结果](images/two.jpg)",
        "![蓝图复用](images/one.png)",
    ]
    assets = {item.source_path: item for item in prepared.assets}
    assert assets["notes/images/one.png"].data == PNG
    assert assets["notes/images/one.png"].content_hash == hashlib.sha256(PNG).hexdigest()
    assert assets["notes/images/two.jpg"].data == JPEG
    assert all((item.width, item.height) == (7, 5) for item in prepared.assets)


def test_logical_hash_does_not_change_with_zip_compression_metadata():
    compressed = prepare_upload("a.zip", _package(compression=zipfile.ZIP_DEFLATED))
    stored = prepare_upload("b.zip", _package(compression=zipfile.ZIP_STORED))
    assert compressed.content_hash == stored.content_hash


def test_corrupted_zip_entry_fails_crc_before_storage():
    payload = bytearray(_package(compression=zipfile.ZIP_STORED))
    image_offset = payload.find(PNG)
    assert image_offset >= 0
    payload[image_offset + len(PNG) // 2] ^= 0x01
    with pytest.raises(UploadValidationError, match="CRC|完整性"):
        prepare_upload("corrupt.zip", bytes(payload))


def test_prepare_upload_enforces_limits_even_without_http_reader(monkeypatch):
    package = _package()
    monkeypatch.setattr(settings, "max_package_bytes", len(package) - 1)
    with pytest.raises(UploadValidationError, match="大小上限") as exc_info:
        prepare_upload("too-large.zip", package)
    assert exc_info.value.status_code == 413

    monkeypatch.setattr(settings, "max_upload_bytes", 3)
    with pytest.raises(UploadValidationError, match="大小上限") as exc_info:
        prepare_upload("too-large.md", b"1234")
    assert exc_info.value.status_code == 413


def test_directory_entries_count_toward_archive_limit(monkeypatch):
    payload = _zip({
        "notes/": b"",
        "notes/images/": b"",
        "notes/doc.md": b"![x](images/x.png)",
        "notes/images/x.png": PNG,
    })
    monkeypatch.setattr(settings, "max_package_files", 3)
    with pytest.raises(UploadValidationError, match="条目数量") as exc_info:
        prepare_upload("too-many-entries.zip", payload)
    assert exc_info.value.status_code == 413


def test_zip_symlink_is_rejected_before_content_is_read():
    with pytest.raises(UploadValidationError, match="普通文件"):
        prepare_upload("symlink.zip", _zip_with_symlink())


def test_package_requires_exactly_one_markdown():
    payload = _zip({
        "one.md": b"![x](x.png)",
        "two.md": b"![x](x.png)",
        "x.png": PNG,
    })
    with pytest.raises(UploadValidationError, match="必须且只能"):
        prepare_upload("two-docs.zip", payload)


def test_reference_style_images_are_resolved_and_code_examples_are_ignored():
    markdown = (
        "示例代码 `![不是图片](missing.png)`\n"
        "真实图片：![结果][result]\n"
        "再次出现：![结果][]\n\n"
        "[result]: assets/result.webp \"标题\"\n"
        "[结果]: assets/result.webp\n"
    )
    payload = _zip({"doc.md": markdown.encode(), "assets/result.webp": WEBP})
    prepared = prepare_upload("doc.zip", payload)
    assert len(prepared.occurrences) == 2
    assert all(item.asset_source_path == "assets/result.webp" for item in prepared.occurrences)


def test_nested_markdown_can_reference_an_image_from_a_parent_directory():
    markdown = "根目录图片：![示意](../images/root.png)\n"
    payload = _zip({"notes/doc.md": markdown.encode(), "images/root.png": PNG})
    prepared = prepare_upload("nested.zip", payload)
    assert prepared.occurrences[0].asset_source_path == "images/root.png"


@pytest.mark.parametrize(
    "entries, message",
    [
        ({"doc.md": b"![x](missing.png)"}, "不存在"),
        ({"doc.md": b"![x](https://example.com/x.png)"}, "相对图片路径"),
        ({"doc.md": b"![x](x.png)", "x.png": PNG, "unused.jpg": JPEG}, "未被"),
        ({"doc.md": b"![x](x.png)", "x.png": b"not an image"}, "损坏"),
        ({"doc.md": b"![x](x.jpg)", "x.jpg": PNG}, "扩展名"),
        ({"doc.md": b"![x](../x.png)", "x.png": PNG}, "越出"),
        ({"../doc.md": b"![x](x.png)", "x.png": PNG}, "不安全路径"),
        ({"C:doc.md": b"![x](x.png)", "x.png": PNG}, "不安全路径"),
        ({"../unsafe/": b"", "doc.md": b"![x](x.png)", "x.png": PNG}, "不安全路径"),
        ({"doc.md": b'<img src="x.png">', "x.png": PNG}, "HTML"),
        ({"doc.md": b"![x](x.png%3Fraw=1)", "x.png": PNG}, "查询参数"),
        ({"doc.md": b"![x](x.png%23preview)", "x.png": PNG}, "查询参数"),
        ({"doc.md": b"![x](x.png)", "x.png": ANIMATED_PNG}, "动态图片"),
    ],
)
def test_invalid_packages_are_rejected_before_storage(entries, message):
    with pytest.raises(UploadValidationError, match=message):
        prepare_upload("invalid.zip", _zip(entries))


def test_storage_roundtrip_and_tamper_detection():
    prepared = prepare_upload("chapter.zip", _package())
    source_path = package_storage.save_package(4, 9, 2, prepared)
    assert storage.read(source_path) == prepared.text
    manifest = package_storage.load_package_manifest(source_path)
    assert manifest is not None
    assert [item["ordinal"] for item in manifest["occurrences"]] == [1, 2, 3]
    package_storage.verify_package_source(manifest, prepared.text)
    verified_manifest, verified_text = package_storage.verify_stored_package(
        source_path,
        expected_package_hash=prepared.content_hash,
    )
    assert verified_manifest == manifest
    assert verified_text == prepared.text
    with pytest.raises(package_storage.StorageIntegrityError, match="原文"):
        package_storage.verify_package_source(manifest, prepared.text + "篡改")

    resolved = package_storage.resolve_version_image(4, 9, 2, 1)
    assert resolved is not None
    path, asset, _ = resolved
    assert path.read_bytes() == PNG
    assert asset["content_hash"] == hashlib.sha256(PNG).hexdigest()

    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(package_storage.StorageIntegrityError, match="完整性"):
        package_storage.resolve_version_image(4, 9, 2, 1)


def test_storage_reuses_a_complete_orphaned_candidate_after_commit_crash():
    prepared = prepare_upload("chapter.zip", _package())
    first = package_storage.save_package(4, 12, 2, prepared)
    second = package_storage.save_package(4, 12, 2, prepared)
    assert second == first

    resolved = package_storage.resolve_version_image(4, 12, 2, 1)
    assert resolved is not None
    resolved[0].write_bytes(b"tampered")
    with pytest.raises(package_storage.StorageIntegrityError, match="完整性|大小"):
        package_storage.save_package(4, 12, 2, prepared)


def test_manifest_cannot_redirect_an_asset_or_forge_occurrence_positions():
    prepared = prepare_upload("chapter.zip", _package())
    source_path = package_storage.save_package(4, 10, 3, prepared)
    manifest_path = (settings.storage_dir / source_path).parent / "manifest.json"
    original = json.loads(manifest_path.read_text(encoding="utf-8"))

    redirected = json.loads(json.dumps(original))
    redirected["assets"][0]["stored_path"] = redirected["assets"][1]["stored_path"]
    manifest_path.write_text(json.dumps(redirected), encoding="utf-8")
    with pytest.raises(package_storage.StorageIntegrityError, match="存储路径"):
        package_storage.load_package_manifest(source_path)

    forged = json.loads(json.dumps(original))
    forged["occurrences"][0]["char_start"] += 1
    manifest_path.write_text(json.dumps(forged), encoding="utf-8")
    manifest = package_storage.load_package_manifest(source_path)
    with pytest.raises(package_storage.StorageIntegrityError, match="原文不一致"):
        package_storage.verify_package_source(manifest, prepared.text)


def test_manifest_rejects_noncanonical_source_archive_path():
    prepared = prepare_upload("chapter.zip", _package())
    source_path = package_storage.save_package(4, 11, 1, prepared)
    manifest_path = (settings.storage_dir / source_path).parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_archive_path"] = "https:chapter.md"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(package_storage.StorageIntegrityError, match="Markdown 路径"):
        package_storage.load_package_manifest(source_path)


def test_manifest_reader_rejects_duplicate_keys_and_excessive_size(monkeypatch):
    prepared = prepare_upload("chapter.zip", _package())
    source_path = package_storage.save_package(4, 13, 1, prepared)
    manifest_path = (settings.storage_dir / source_path).parent / "manifest.json"

    manifest_path.write_text('{"format":1,"format":1}', encoding="utf-8")
    with pytest.raises(package_storage.StorageIntegrityError, match="清单损坏"):
        package_storage.load_package_manifest(source_path)

    manifest_path.write_bytes(b"x" * 17)
    monkeypatch.setattr(package_storage, "MAX_MANIFEST_BYTES", 16)
    with pytest.raises(package_storage.StorageIntegrityError, match="大小上限"):
        package_storage.load_package_manifest(source_path)


def test_package_upload_content_and_original_image_endpoint(client):
    kb_id = client.post("/api/v1/kbs", json={"name": "图片包测试"}).json()["id"]
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        files={"file": ("chapter.zip", _package(), "application/zip")},
    )
    assert response.status_code == 201, response.text
    document = response.json()["document"]
    assert document["title"] == "chapter.md"
    assert document["image_count"] == 3

    content = client.get(f"/api/v1/documents/{document['id']}/content").json()
    assert len(content["images"]) == 3
    assert [item["ordinal"] for item in content["images"]] == [1, 2, 3]
    assert [item["char_start"] for item in content["images"]] == sorted(
        item["char_start"] for item in content["images"]
    )
    first = client.get(content["images"][0]["content_url"])
    second = client.get(content["images"][1]["content_url"])
    assert first.status_code == 200 and first.content == PNG
    assert first.headers["content-type"] == "image/png"
    assert first.headers["etag"] == f'"{hashlib.sha256(PNG).hexdigest()}"'
    assert second.status_code == 200 and second.content == JPEG


def test_equivalent_reupload_is_idempotent_even_if_zip_compression_changes(client):
    kb_id = client.post("/api/v1/kbs", json={"name": "图片包幂等"}).json()["id"]
    created = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        files={
            "file": (
                "first.zip",
                _package(compression=zipfile.ZIP_DEFLATED),
                "application/zip",
            )
        },
    ).json()
    response = client.post(
        f"/api/v1/documents/{created['document']['id']}/reupload",
        files={
            "file": (
                "second.zip",
                _package(compression=zipfile.ZIP_STORED),
                "application/zip",
            )
        },
    )
    assert response.status_code == 200
    assert response.json()["content_changed"] is False


def test_ingest_keeps_image_syntax_atomic_and_citation_returns_images(
    client, db, monkeypatch
):
    monkeypatch.setattr(settings, "chunk_max_chars", 42)
    monkeypatch.setattr(settings, "chunk_overlap_chars", 6)
    kb_id = client.post("/api/v1/kbs", json={"name": "图片引用切块"}).json()["id"]
    created = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        files={"file": ("chapter.zip", _package(), "application/zip")},
    ).json()["document"]
    doc = db.get(Document, created["id"])
    ingest.process_document(
        doc.id,
        db,
        expected_version=doc.ingest_version,
        candidate_path=doc.pending_file_path,
    )
    db.refresh(doc)
    assert doc.status == "ready"
    manifest = package_storage.load_package_manifest(doc.file_path)
    chunks = db.scalars(
        select(Chunk).where(Chunk.doc_id == doc.id).order_by(Chunk.chunk_index)
    ).all()
    for occurrence in manifest["occurrences"]:
        containing = next(
            chunk for chunk in chunks
            if chunk.char_start <= occurrence["char_start"]
            and chunk.char_end >= occurrence["char_end"]
        )
        detail = client.get(f"/api/v1/citations/{containing.id}")
        assert detail.status_code == 200
        assert occurrence["ordinal"] in [image["ordinal"] for image in detail.json()["images"]]


def test_successful_reupload_retains_old_package_for_historical_image_urls(client, db):
    kb_id = client.post("/api/v1/kbs", json={"name": "历史原图版本"}).json()["id"]
    created = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        files={"file": ("chapter.zip", _package(), "application/zip")},
    ).json()["document"]
    doc = db.get(Document, created["id"])
    ingest.process_document(
        doc.id, db, expected_version=doc.ingest_version, candidate_path=doc.pending_file_path
    )
    db.refresh(doc)
    old_source = doc.file_path
    old_url = client.get(f"/api/v1/documents/{doc.id}/content").json()["images"][0][
        "content_url"
    ]

    markdown = "# 新版本\n新图：![绿色](images/new.png)\n"
    replacement = _zip({
        "notes/chapter.md": markdown.encode(),
        "notes/images/new.png": _image_bytes("PNG", (10, 220, 90)),
    })
    response = client.post(
        f"/api/v1/documents/{doc.id}/reupload",
        files={"file": ("replacement.zip", replacement, "application/zip")},
    )
    assert response.status_code == 200
    db.refresh(doc)
    ingest.process_document(
        doc.id, db, expected_version=doc.ingest_version, candidate_path=doc.pending_file_path
    )
    db.refresh(doc)

    assert doc.status == "ready"
    assert doc.file_path != old_source
    assert (settings.storage_dir / old_source).is_file()
    historical = client.get(old_url)
    assert historical.status_code == 200
    assert historical.content == PNG

    response = client.delete(f"/api/v1/documents/{doc.id}")
    assert response.status_code == 204
    assert not (settings.storage_dir / str(kb_id) / str(doc.id)).exists()
    assert client.get(old_url).status_code == 404


def test_failed_package_reupload_removes_whole_candidate_directory(client, db):
    kb_id = client.post("/api/v1/kbs", json={"name": "失败图片候选清理"}).json()["id"]
    created = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        files={"file": ("base.md", b"# old\nkeep", "text/markdown")},
    ).json()["document"]
    doc = db.get(Document, created["id"])
    ingest.process_document(
        doc.id, db, expected_version=doc.ingest_version, candidate_path=doc.pending_file_path
    )
    db.refresh(doc)
    active_path = doc.file_path

    response = client.post(
        f"/api/v1/documents/{doc.id}/reupload",
        files={"file": ("chapter.zip", _package(), "application/zip")},
    )
    assert response.status_code == 200
    db.refresh(doc)
    candidate_path = doc.pending_file_path
    candidate_dir = (settings.storage_dir / candidate_path).parent
    (candidate_dir / "manifest.json").write_text("{}", encoding="utf-8")

    ingest.process_document(
        doc.id, db, expected_version=doc.ingest_version, candidate_path=candidate_path
    )
    db.refresh(doc)
    assert doc.status == "ready"
    assert doc.file_path == active_path
    assert doc.pending_file_path is None
    assert not candidate_dir.exists()
