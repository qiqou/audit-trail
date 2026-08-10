# -*- mode: python ; coding: utf-8 -*-
# 审迹 双端打包配置（PyInstaller 单一入口）
#
# 使用（macOS / Windows 同一命令）：
#   pyinstaller --noconfirm 审迹.spec
#
# 平台差异在 spec 内部分支处理：
#   - macOS: COLLECT + BUNDLE → 审迹.app（一目录形态，双击即用）
#   - Windows: 单 EXE（--onefile 等价，不用 BUNDLE）
# 产物名 / 版本 / 包 ID 统一读 backend/version.py，避免多处漂移。
import sys
from pathlib import Path

# 让 Analysis 能找到 backend 包（from database/version import ...）
BACKEND_DIR = Path(SPECPATH) / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from version import APP_NAME, APP_VERSION, BUNDLE_ID  # noqa: E402

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

# Windows 版本资源（EXE 属性里的版本信息）。
# 注意：PyInstaller 6.x 的 EXE(version=) 参数要求 VSVersionInfo 对象或资源文件路径，
# 传字符串版本号会被当成文件路径打开 → FileNotFoundError（CI Windows 实测踩坑）。
# macOS 无此概念，BUNDLE 用 info_plist 承载版本。
# 打包形态：双端都走 onedir（COLLECT）。onedir 比 onefile 稳：
#   - DLL 直接躺在 exe 旁（_internal/），无每次解压 → 无解压失败点、启动快
#   - 杀软实时扫描误杀概率大幅降低（onefile 解压 python311.dll 是经典误杀场景）
#   - macOS 本来就是 onedir（COLLECT+BUNDLE），两端行为一致
win_version_info = None
if IS_WIN:
    from PyInstaller.utils.win32.versioninfo import (  # noqa: PLC0415
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo,
    )

    _ver_parts = [int(x) for x in APP_VERSION.split(".")]
    _ver_parts += [0] * (4 - len(_ver_parts))
    win_version_info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=tuple(_ver_parts),
            prodvers=tuple(_ver_parts),
            mask=0x3F, flags=0x0, OS=0x40004,
            fileType=0x1, subtype=0x0, date=(0, 0),
        ),
        kids=[
            StringFileInfo([
                StringTable(
                    "080404B0",  # 简体中文 + Unicode
                    [
                        StringStruct("CompanyName", APP_NAME),
                        StringStruct("FileDescription", APP_NAME),
                        StringStruct("FileVersion", APP_VERSION),
                        StringStruct("InternalName", APP_NAME),
                        StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                        StringStruct("ProductName", APP_NAME),
                        StringStruct("ProductVersion", APP_VERSION),
                    ],
                ),
            ]),
            VarFileInfo([VarStruct("Translation", [0x0804, 1200])]),
        ],
    )

a = Analysis(
    ['main.py'],
    pathex=['.', 'backend'],   # backend 目录加入搜索路径（from database import ...）
    binaries=[],
    datas=[
        # Vite 已构建的 V3 正式前端。
        ('frontend-v3/dist', 'frontend-v3/dist'),
    ],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops.auto',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan.on',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pytest', 'PySide6'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # 双端 onedir：二进制由 COLLECT 收集
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed：不弹终端
    disable_windowed_traceback=False,
    # Windows 版本资源（VSVersionInfo 对象）；macOS 忽略
    version=win_version_info,
)

if IS_MAC:
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=APP_NAME,
    )
    app = BUNDLE(
        coll,
        name=f'{APP_NAME}.app',
        icon=None,
        bundle_identifier=BUNDLE_ID,
        info_plist={
            'CFBundleDisplayName': APP_NAME,
            'CFBundleShortVersionString': APP_VERSION,
            'CFBundleVersion': APP_VERSION,
            'NSHighResolutionCapable': True,
            'LSUIElement': False,
        },
    )
else:
    # Windows：onedir 形态（dist/审迹/审迹.exe + _internal/）
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=APP_NAME,
    )
