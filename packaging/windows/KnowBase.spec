# -*- mode: python ; coding: utf-8 -*-
"""KnowBase Windows 单目录发布包。

单目录模式保留可检查的资源结构，避免 onefile 每次启动都解压 Python 与前端
资源。用户数据库、原文、配置和日志从不进入该目录。
"""

from pathlib import Path
import re
import tomllib

from PIL import Image, ImageDraw
from PyInstaller.utils.hooks import copy_metadata


ROOT = Path(SPEC).resolve().parents[2]
ENTRYPOINT = ROOT / "packaging" / "windows" / "entrypoint.py"
FRONTEND = ROOT / "frontend" / "dist"
CONFIG_TEMPLATE = ROOT / ".env.example"
ICON = ROOT / "build" / "pyinstaller" / "KnowBase.ico"
VERSION_FILE = ROOT / "build" / "pyinstaller" / "version_info.txt"

if not (FRONTEND / "index.html").is_file():
    raise SystemExit("frontend/dist is missing; run the frontend build first")
if not CONFIG_TEMPLATE.is_file():
    raise SystemExit(".env.example is missing")


def create_icon(path: Path) -> None:
    """从前端品牌图形生成多分辨率 ICO，不维护另一份二进制源文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (256, 256), "#173f3d")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, 255, 255), radius=58, fill="#173f3d")
    stroke = "#f4f5f2"
    width = 24
    draw.line((72, 57, 72, 199), fill=stroke, width=width)
    draw.line((184, 57, 100, 136), fill=stroke, width=width)
    draw.line((126, 108, 188, 199), fill=stroke, width=width)
    draw.ellipse((168, 41, 200, 73), fill="#d98a73")
    image.save(
        path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


def create_version_file(path: Path) -> None:
    """从 pyproject.toml 生成 Windows 文件版本，避免发布版本双写。"""
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(metadata["project"]["version"])
    numeric = [int(part) for part in re.findall(r"\d+", version)[:4]]
    numeric += [0] * (4 - len(numeric))
    tuple_text = ", ".join(str(part) for part in numeric)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({tuple_text}),
    prodvers=({tuple_text}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('FileDescription', 'KnowBase Personal Knowledge Base'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'KnowBase'),
        StringStruct('LegalCopyright', 'Copyright (c) KnowBase contributors'),
        StringStruct('OriginalFilename', 'KnowBase.exe'),
        StringStruct('ProductName', 'KnowBase'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)\n""",
        encoding="utf-8",
    )


create_icon(ICON)
create_version_file(VERSION_FILE)

datas = [
    (str(FRONTEND), "frontend/dist"),
    (str(CONFIG_TEMPLATE), "."),
]
datas += copy_metadata("knowbase")

hiddenimports = [
    "app.main",
    "uvicorn.lifespan.on",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
]

a = Analysis(
    [str(ENTRYPOINT)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "alembic",
        "cefpython3",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "pytest",
        "tkinter",
        "webview.platforms.android",
        "webview.platforms.cef",
        "webview.platforms.cocoa",
        "webview.platforms.gtk",
        "webview.platforms.qt",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="KnowBase",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
    version=str(VERSION_FILE),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="KnowBase",
)
