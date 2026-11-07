# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：只打启动器（小壳），不打 torch/模型/网页。

    pyinstaller --noconfirm --clean gui_app/launcher/launcher.spec --distpath dist --workpath build

产物：
    macOS   dist/音声字幕工房.app
    Windows dist/ASMR-Translator.exe
    Linux   dist/ASMR-Translator
启动器会从发布文件夹里找 gui_app/gui_server.py、gui_app/webui/、.tools/python、.tools/uv，
用自己的 Python 运行安装页，用 .venv 的 Python 运行 GUI 服务。
"""
import os
import sys
from pathlib import Path

APP_DIR = Path(SPECPATH).resolve().parent
ROOT = APP_DIR.parent
LAUNCHER_DIR = APP_DIR / "launcher"
ICON_ENV = os.environ.get("LAUNCHER_ICON", "").strip()

if ICON_ENV:
    icon = ICON_ENV
elif sys.platform == "darwin":
    icon = str(LAUNCHER_DIR / "assets" / "icon.icns")
elif sys.platform.startswith("win"):
    icon = str(LAUNCHER_DIR / "assets" / "icon.ico")
else:
    icon = str(LAUNCHER_DIR / "assets" / "icon.png")
if not Path(icon).is_file():
    icon = None

a = Analysis(
    [str(LAUNCHER_DIR / "launcher.py")],
    pathex=[str(ROOT), str(APP_DIR), str(ROOT / "scripts"), str(LAUNCHER_DIR)],
    binaries=[],
    datas=[(str(LAUNCHER_DIR / "setup_page.html"), "launcher")],
    hiddenimports=["setup_env", "setup_server", "gui_platform"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch", "transformers", "faster_whisper", "numpy", "av", "yaml",
        "tkinter", "matplotlib", "scipy", "pandas",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ASMR-Translator",
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
    icon=icon,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="音声字幕工房.app",
        icon=icon,
        bundle_identifier="com.asmrtranslator.gui",
        info_plist={
            "CFBundleName": "音声字幕工房",
            "CFBundleDisplayName": "音声字幕工房",
            "CFBundleShortVersionString": os.environ.get("APP_VERSION", "dev"),
            "CFBundleVersion": os.environ.get("APP_VERSION", "dev"),
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "LSBackgroundOnly": False,
        },
    )
