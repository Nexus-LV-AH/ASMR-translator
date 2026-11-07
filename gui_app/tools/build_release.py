#!/usr/bin/env python3
"""组装可双击的便携发布包。

本地（macOS 开发机）：
    python gui_app/tools/build_release.py --version 0.2.0

CI（三平台）：
    uv 在 PATH 里时，会自动 uv python install 3.12 到发布目录的 .tools/python，
    并把 uv 二进制复制进去；PyInstaller 构建启动器；zip 打包。

产物：
    release/ASMR-Translator-<ver>-<os>-<arch>/
    release/ASMR-Translator-<ver>-<os>-<arch>.zip
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
ROOT = APP_DIR.parent
LAUNCHER_DIR = APP_DIR / "launcher"
ASSETS = LAUNCHER_DIR / "assets"
BUILD_DIR = ROOT / ".release-build"
EXCLUDE_NAMES = {".git", ".github", ".venv", ".models", ".tools", "runtime", "release",
                 "input", "output", "中间文件", "LRC成品库", "promptbook", "对照试验",
                 "__pycache__", ".release-build", "dist", "build", "tests", "launcher",
                 "clean.sh", "start.sh"}
# 发布根目录：原项目文件 + 双击启动器；GUI 代码整体放 gui_app/
ROOT_FILES = ["subtitle.py", "config.yaml", "glossary.txt", "requirements.txt",
              "README.md", "LICENSE"]
ROOT_DIRS = ["scripts", "prompts"]
APP_FILES = ["gui.py", "gui_server.py", "gui_fs.py", "gui_jobs.py", "gui_models.py",
             "gui_platform.py", "gguf_meta.py", "README.md", "VERSION"]
APP_DIRS = ["webui", "docs"]


def os_tag() -> str:
    s = sys.platform
    if s == "darwin":
        return "macOS"
    if s.startswith("win"):
        return "Windows"
    return "Linux"


def arch_tag() -> str:
    m = platform.machine().lower()
    return {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(m, m)


def _run(cmd: list[str], *, cwd: Path = ROOT, env: dict | None = None) -> None:
    print("$ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd), env=env, check=True)


def ensure_icons() -> Path | None:
    """由 launcher/assets/icon.png 生成 macOS .icns / Windows .ico（尽力而为）。"""
    png = ASSETS / "icon.png"
    if not png.is_file():
        return None
    if sys.platform == "darwin" and shutil.which("sips") and shutil.which("iconutil"):
        iconset = BUILD_DIR / "icon.iconset"
        shutil.rmtree(iconset, ignore_errors=True)
        iconset.mkdir(parents=True, exist_ok=True)
        for size in (16, 32, 64, 128, 256, 512):
            for scale, suffix in ((1, ""), (2, "@2x")):
                px = size * scale
                out = iconset / f"icon_{size}x{size}{suffix}.png"
                subprocess.run(["sips", "-z", str(px), str(px), str(png), "--out", str(out)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        icns = ASSETS / "icon.icns"
        _run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)])
        return icns
    if sys.platform.startswith("win"):
        ico = ASSETS / "icon.ico"
        try:
            from PIL import Image  # type: ignore
        except Exception:  # noqa: BLE001
            print("提示：未安装 Pillow，Windows 图标使用默认（CI 会安装 Pillow）")
            return None
        img = Image.open(png)
        img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        return ico
    return png


def build_launcher(version: str) -> Path:
    """PyInstaller 构建启动器；返回产物路径。"""
    icon = ensure_icons()
    env = os.environ.copy()
    env["APP_VERSION"] = version
    # PyInstaller 默认把缓存放到 ~/Library；沙箱/CI 下改成项目内
    env["PYINSTALLER_CONFIG_DIR"] = str(BUILD_DIR / "pyinstaller-cache")
    if icon:
        env["LAUNCHER_ICON"] = str(icon)
    dist = BUILD_DIR / "dist"
    work = BUILD_DIR / "build"
    dist.mkdir(parents=True, exist_ok=True)
    _run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
          "--distpath", str(dist), "--workpath", str(work),
          str(LAUNCHER_DIR / "launcher.spec")], env=env)
    if sys.platform == "darwin":
        app = dist / "音声字幕工房.app"
        if not app.exists():
            raise SystemExit(f"PyInstaller 没有生成 {app}")
        return app
    if sys.platform.startswith("win"):
        exe = dist / "ASMR-Translator.exe"
        if not exe.exists():
            raise SystemExit(f"PyInstaller 没有生成 {exe}")
        return exe
    exe = dist / "ASMR-Translator"
    if not exe.exists():
        raise SystemExit(f"PyInstaller 没有生成 {exe}")
    return exe


def copy_tree(src: Path, dst: Path) -> None:
    def ignore(dirname, names):  # noqa: ANN001
        return [n for n in names if n in {"__pycache__", ".DS_Store", "*.pyc"} or n.endswith(".pyc")]
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)


def prepare_tools(release: Path, *, source: Path | None = None) -> None:
    """复制/安装内置 Python + uv 到发布目录 .tools。"""
    tools = release / ".tools"
    tools.mkdir(parents=True, exist_ok=True)
    src = Path(source) if source else ROOT / ".tools"
    py_src = src / "python"
    if py_src.is_dir() and any(py_src.rglob("python*")):
        print(f"复制内置 Python：{py_src} → {tools / 'python'}")
        copy_tree(py_src, tools / "python")
    else:
        uv = shutil.which("uv")
        if not uv:
            raise SystemExit("找不到内置 Python，也找不到 uv；无法生成可双击发布包")
        env = os.environ.copy()
        env["UV_PYTHON_INSTALL_DIR"] = str(tools / "python")
        env["UV_CACHE_DIR"] = str(tools / "uv-cache")
        print("用 uv 下载 Python 3.12 到发布目录…")
        _run([uv, "python", "install", "3.12"], env=env)
    uv_src = None
    for p in (src.rglob("uv.exe") if os.name == "nt" else src.rglob("uv")):
        if p.is_file():
            uv_src = p
            break
    if uv_src is None:
        which = shutil.which("uv")
        uv_src = Path(which) if which else None
    if uv_src is None or not uv_src.is_file():
        raise SystemExit("找不到 uv 可执行文件，无法生成发布包")
    uv_dst = tools / "uv" / ("uv.exe" if os.name == "nt" else "uv")
    uv_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(uv_src, uv_dst)
    try:
        os.chmod(uv_dst, 0o755)
    except OSError:
        pass
    print(f"uv：{uv_dst}")


def assemble(release: Path, launcher_artifact: Path | None, *, version: str) -> None:
    release.mkdir(parents=True, exist_ok=True)
    for name in ROOT_FILES:
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, release / name)
    for name in ROOT_DIRS:
        src = ROOT / name
        if src.is_dir():
            copy_tree(src, release / name)
    # GUI 代码集中到 gui_app/，和原项目文件分开
    app_out = release / "gui_app"
    app_out.mkdir(parents=True, exist_ok=True)
    for name in APP_FILES:
        src = APP_DIR / name
        if src.is_file():
            shutil.copy2(src, app_out / name)
    for name in APP_DIRS:
        src = APP_DIR / name
        if src.is_dir():
            copy_tree(src, app_out / name)
    # 使用说明放在发布根目录，方便用户先看到
    guide = APP_DIR / "使用说明.txt"
    if guide.is_file():
        shutil.copy2(guide, release / "使用说明.txt")
    (app_out / "VERSION").write_text(version + "\n", encoding="utf-8")
    for d in ("input", "output", "LRC成品库", "promptbook",
              "中间文件", "中间文件/日文字幕", "中间文件/中文字幕", "中间文件/LRC"):
        (release / d).mkdir(parents=True, exist_ok=True)
    if launcher_artifact is not None:
        if launcher_artifact.is_dir():
            copy_tree(launcher_artifact, release / launcher_artifact.name)
        else:
            shutil.copy2(launcher_artifact, release / launcher_artifact.name)
            try:
                os.chmod(release / launcher_artifact.name, 0o755)
            except OSError:
                pass


def make_zip(release: Path) -> Path:
    out = release.with_name(release.name + ".zip")
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for p in sorted(release.rglob("*")):
            if p.is_file():
                zf.write(p, arcname=str(Path(release.name) / p.relative_to(release)))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="组装音声字幕工房便携发布包")
    p.add_argument("--version", default="", help="版本号（默认读 VERSION）")
    p.add_argument("--out", default=str(ROOT / "release"), help="输出目录")
    p.add_argument("--skip-launcher", action="store_true", help="不构建启动器（测试目录结构用）")
    p.add_argument("--launcher-artifact", default="", help="已有启动器产物路径")
    p.add_argument("--skip-tools", action="store_true", help="不复制 Python/uv（测试用）")
    p.add_argument("--tools-source", default="", help="已有 .tools 目录")
    p.add_argument("--no-zip", action="store_true")
    args = p.parse_args(argv)
    version = args.version.strip()
    if not version:
        try:
            version = (APP_DIR / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
        except OSError:
            version = "0.0.0"
    name = f"ASMR-Translator-{version}-{os_tag()}-{arch_tag()}"
    out_dir = Path(args.out).expanduser().resolve()
    release = out_dir / name
    shutil.rmtree(release, ignore_errors=True)
    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"发布目录：{release}")
    artifact: Path | None = None
    if args.launcher_artifact:
        artifact = Path(args.launcher_artifact).expanduser().resolve()
        if not artifact.exists():
            raise SystemExit(f"启动器产物不存在：{artifact}")
    elif not args.skip_launcher:
        artifact = build_launcher(version)
    try:
        assemble(release, artifact, version=version)
    except Exception:
        shutil.rmtree(release, ignore_errors=True)
        raise
    if not args.skip_tools:
        try:
            prepare_tools(release, source=Path(args.tools_source) if args.tools_source else None)
        except Exception:
            shutil.rmtree(release, ignore_errors=True)
            raise
    if not args.no_zip:
        out = make_zip(release)
        print(f"ZIP：{out}（{out.stat().st_size / 1024 ** 2:.1f} MB）")
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
