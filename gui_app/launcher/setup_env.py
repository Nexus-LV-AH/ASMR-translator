#!/usr/bin/env python3
"""首次启动：用发布包内置的 Python + uv 安装运行环境。

发布目录：
    .tools/python/...   内置 Python 3.12（standalone）
    .tools/uv[-...]/uv  内置 uv（或系统 PATH 里的 uv）
    requirements.txt
    .venv/              第一次启动后创建

不依赖系统 Python；安装期间日志会实时送到 Setup 页面。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

MARKER = ".app-ready"
PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


class SetupError(RuntimeError):
    pass


def venv_python(root: str | Path) -> Path:
    root = Path(root)
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _is_exec(p: Path) -> bool:
    try:
        if not p.is_file():
            return False
        return os.name == "nt" or os.access(p, os.X_OK)
    except OSError:
        return False


def find_bundled_python(root: str | Path) -> Path | None:
    root = Path(root)
    base = root / ".tools" / "python"
    if not base.is_dir():
        return None
    names = ("python.exe",) if os.name == "nt" else ("python3.12", "python3", "python")
    candidates: list[Path] = []
    for name in names:
        candidates.extend(p for p in base.rglob(name) if _is_exec(p))
    # 优先 bin/ 下、路径最短的
    candidates.sort(key=lambda p: (0 if p.parent.name in ("bin", "Scripts") else 1, len(str(p))))
    return candidates[0] if candidates else None


def find_uv(root: str | Path) -> Path | None:
    root = Path(root)
    tools = root / ".tools"
    if tools.is_dir():
        names = ("uv.exe",) if os.name == "nt" else ("uv",)
        found = [p for name in names for p in tools.rglob(name) if _is_exec(p)]
        found.sort(key=lambda p: len(str(p)))
        if found:
            return found[0]
    which = shutil.which("uv")
    return Path(which) if which else None


def _requirements_hash(root: Path) -> str:
    try:
        data = (root / "requirements.txt").read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()[:16]


def venv_ready(root: str | Path) -> bool:
    root = Path(root)
    py = venv_python(root)
    if not py.is_file():
        return False
    code = "import yaml, numpy"
    try:
        r = subprocess.run([str(py), "-c", code], cwd=str(root),
                           env=_run_env(root), capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.setdefault("HF_HOME", str(root / ".models"))
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    env.setdefault("ORT_DISABLE_TELEMETRY", "1")
    env["UV_PYTHON_INSTALL_DIR"] = str(root / ".tools" / "python")
    env["UV_CACHE_DIR"] = str(root / ".tools" / "uv-cache")
    env["UV_LINK_MODE"] = "copy"
    env["UV_NO_PROGRESS"] = "1"
    env["NO_COLOR"] = "1"
    if sys.platform == "darwin":
        path = env.get("PATH", "")
        prefix = "/opt/homebrew/bin:/usr/local/bin"
        if prefix not in path:
            env["PATH"] = f"{prefix}:{path}" if path else prefix
    return env


def _run_stream(cmd: list[str], *, root: Path, log: Callable[[str], None], timeout: float = 3600.0) -> int:
    log("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(root), env=_run_env(root),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except OSError as exc:
        raise SetupError(f"无法启动 {cmd[0]}：{exc}") from exc
    assert proc.stdout is not None
    for line in proc.stdout:
        text = line.rstrip("\n")
        if text:
            log(text)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise SetupError(f"命令超时：{cmd[0]}")


def setup_env(root: str | Path, *, log: Callable[[str], None], mirror: str = "",
              stage: Callable[[str], None] | None = None) -> None:
    """创建 .venv 并安装 requirements.txt；失败抛 SetupError。"""
    root = Path(root).resolve()
    stage = stage or (lambda _s: None)
    py = find_bundled_python(root)
    uv = find_uv(root)
    if uv is None:
        raise SetupError(
            "找不到 uv。发布包应该内置 .tools/uv；如果你是源码运行，请先安装 uv（https://docs.astral.sh/uv/）"
        )
    if py is None:
        # 发布包没有内置 Python 时，让 uv 自己下载 3.12（需要联网）
        log("未找到内置 Python，改用 uv 下载 Python 3.12（需要联网）…")
        stage("准备 Python 3.12")
        rc = _run_stream([str(uv), "python", "install", "3.12"], root=root, log=log)
        if rc != 0:
            raise SetupError("下载 Python 3.12 失败；请检查网络，或重新下载完整发布包")
        py_arg = "3.12"
    else:
        py_arg = str(py)
        log(f"使用内置 Python：{py}")

    venv = root / ".venv"
    if venv.exists() and not venv_ready(root):
        log("检测到不完整/损坏的 .venv，准备重建…")
        shutil.rmtree(venv, ignore_errors=True)

    stage("创建虚拟环境")
    if not venv.exists():
        rc = _run_stream([str(uv), "venv", "--python", py_arg, str(venv)], root=root, log=log)
        if rc != 0:
            raise SetupError("创建虚拟环境失败；请查看上面的日志")
    else:
        log("虚拟环境已存在，直接安装/更新依赖")

    vpy = venv_python(root)
    stage("安装依赖（torch / transformers / faster-whisper 等，可能需要几分钟）")
    cmd = [str(uv), "pip", "install", "--python", str(vpy), "-r", str(root / "requirements.txt")]
    if mirror:
        cmd += ["--index-url", mirror]
    rc = _run_stream(cmd, root=root, log=log)
    if rc != 0:
        raise SetupError("安装依赖失败；请检查网络/代理，或切换下载源后重试")

    stage("检查环境")
    if not venv_ready(root):
        raise SetupError("依赖安装完成，但检查 yaml/numpy 失败；请重试或查看日志")
    marker = root / MARKER
    marker.write_text(json.dumps({
        "requirements": _requirements_hash(root),
        "python": str(vpy),
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log("运行环境安装完成。")
    stage("完成")


def marker_info(root: str | Path) -> dict:
    try:
        return json.loads((Path(root) / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
