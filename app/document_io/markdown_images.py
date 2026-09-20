"""扫描 Markdown 本地图片语法并保留精确字符区间。"""

import posixpath
import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import unquote_to_bytes

from .models import ImageOccurrenceMatch, UploadValidationError

_REFERENCE_DEF_RE = re.compile(
    r"^[ \t]{0,3}\[([^\]\n]+)\]:[ \t]*(?:<((?:\\.|[^>])*)>|(\S+))",
    re.MULTILINE,
)
_HTML_IMAGE_RE = re.compile(r"<\s*img\b", re.IGNORECASE)
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def scan_image_occurrences(text: str) -> list[ImageOccurrenceMatch]:
    """识别行内、完整/折叠/快捷引用图片，并忽略代码区间。"""
    code_ranges = _code_ranges(text)
    definitions: dict[str, str] = {}
    for match in _REFERENCE_DEF_RE.finditer(text):
        if _in_ranges(match.start(), code_ranges):
            continue
        label = _normalize_label(match.group(1))
        destination = match.group(2) if match.group(2) is not None else match.group(3)
        definitions.setdefault(label, _unescape_markdown(destination))

    for match in _HTML_IMAGE_RE.finditer(text):
        if not _in_ranges(match.start(), code_ranges):
            raise UploadValidationError("一期暂不支持 HTML <img>；请使用 Markdown 图片语法")

    occurrences: list[ImageOccurrenceMatch] = []
    cursor = 0
    while True:
        start = text.find("![", cursor)
        if start < 0:
            break
        cursor = start + 2
        if _is_escaped(text, start) or _in_ranges(start, code_ranges):
            continue
        alt_end = _matching_delimiter(text, start + 1, "[", "]")
        if alt_end is None:
            raise UploadValidationError("Markdown 图片语法不完整")
        alt = _unescape_markdown(text[start + 2:alt_end])
        following = alt_end + 1
        destination: str | None = None
        end: int | None = None
        if following < len(text) and text[following] == "(":
            close = _matching_delimiter(text, following, "(", ")")
            if close is not None:
                destination = _inline_destination(text[following + 1:close])
                end = close + 1
        elif following < len(text) and text[following] == "[":
            close = _matching_delimiter(text, following, "[", "]")
            if close is not None:
                label_text = text[following + 1:close]
                label = _normalize_label(label_text or alt)
                destination = definitions.get(label)
                end = close + 1
        else:
            destination = definitions.get(_normalize_label(alt))
            end = following
        if destination is None or end is None:
            raise UploadValidationError(
                "无法解析 Markdown 图片；请使用 ![说明](相对路径) 或有效的引用式语法"
            )
        occurrences.append(ImageOccurrenceMatch(
            source_reference=destination,
            alt_text=alt,
            char_start=start,
            char_end=end,
        ))
        cursor = end
    return occurrences


def resolve_image_reference(markdown_path: str, reference: str) -> str:
    """把 Markdown 图片引用解析为 ZIP 根目录内的 NFC 相对路径。"""
    if "?" in reference or "#" in reference:
        raise UploadValidationError(f"图片相对路径暂不支持查询参数或片段：{reference}")
    try:
        decoded = unquote_to_bytes(reference).decode("utf-8")
    except UnicodeDecodeError:
        raise UploadValidationError(f"图片路径不是有效 UTF-8：{reference}") from None
    decoded = unicodedata.normalize("NFC", decoded)
    if "?" in decoded or "#" in decoded:
        raise UploadValidationError(f"图片相对路径暂不支持查询参数或片段：{reference}")
    if not decoded or "\x00" in decoded or "\\" in decoded:
        raise UploadValidationError(f"图片路径无效：{reference}")
    if decoded.startswith(("/", "//")) or _URI_SCHEME_RE.match(decoded):
        raise UploadValidationError(f"一期只支持 ZIP 包内相对图片路径：{reference}")
    parent = PurePosixPath(markdown_path).parent.as_posix()
    resolved = posixpath.normpath(posixpath.join(parent, decoded))
    if resolved.startswith("../") or resolved == "..":
        raise UploadValidationError(f"图片路径越出 ZIP 根目录：{reference}")
    return PurePosixPath(resolved).as_posix()


def _inline_destination(body: str) -> str | None:
    body = body.lstrip()
    if not body:
        return None
    if body.startswith("<"):
        index = 1
        while index < len(body):
            if body[index] == ">" and not _is_escaped(body, index):
                return _unescape_markdown(body[1:index])
            index += 1
        return None
    chars: list[str] = []
    escaped = False
    for char in body:
        if escaped:
            chars.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char.isspace():
            break
        else:
            chars.append(char)
    if escaped:
        chars.append("\\")
    return "".join(chars) or None


def _code_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    lines = text.splitlines(keepends=True)
    offset = 0
    fence_start: int | None = None
    fence_char = ""
    fence_size = 0
    for line in lines:
        match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if fence_start is None:
                fence_start = offset
                fence_char = marker[0]
                fence_size = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_size:
                ranges.append((fence_start, offset + len(line)))
                fence_start = None
        offset += len(line)
    if fence_start is not None:
        ranges.append((fence_start, len(text)))

    index = 0
    while index < len(text):
        if text[index] != "`" or _in_ranges(index, ranges):
            index += 1
            continue
        run_end = index + 1
        while run_end < len(text) and text[run_end] == "`":
            run_end += 1
        marker = text[index:run_end]
        close = text.find(marker, run_end)
        if close >= 0 and not _in_ranges(close, ranges):
            ranges.append((index, close + len(marker)))
            index = close + len(marker)
        else:
            index = run_end
    return sorted(ranges)


def _matching_delimiter(text: str, opening: int, left: str, right: str) -> int | None:
    depth = 0
    index = opening
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == left:
            depth += 1
        elif char == right:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip()).casefold()


def _unescape_markdown(value: str) -> str:
    return re.sub(r"\\([!\"#$%&'()*+,./:;<=>?@\[\\\]^_`{|}~-])", r"\1", value)


def _is_escaped(text: str, index: int) -> bool:
    slashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        slashes += 1
        index -= 1
    return slashes % 2 == 1


def _in_ranges(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in ranges)
