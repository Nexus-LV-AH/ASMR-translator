#!/usr/bin/env python3
"""图形界面的跨平台底座：所有 OS 差异集中在这里。

只依赖标准库，不导入项目里任何会拉模型/拉 torch 的模块，方便被启动器
（PyInstaller 小壳）和 GUI 服务共用。

    venv_python()           .venv/bin/python 或 .venv\\Scripts\\python.exe
    pick_model_file/folder  系统原生选择框（模型路径必须由系统给，浏览器拿不到）
    open_in_file_manager()  在 Finder / 资源管理器 / xdg 中打开
    open_ui()               优先 Chrome/Edge app 模式窗口，失败回退默认浏览器
    spawn_kwargs()          独立进程组/进程树
    kill_process_tree()     取消任务（POSIX killpg / Windows taskkill /T）
    SleepInhibitor          任务/安装期间阻止系统休眠（macOS/Windows/Linux）
    child_env()             子进程统一 UTF-8 / HF 缓存环境
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)


# ---------------------------------------------------------------- 路径
def venv_python(root: str | Path) -> Path:
    root = Path(root)
    if IS_WINDOWS:
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def find_project_root(start: str | Path | None = None, *, max_up: int = 8) -> Path | None:
    """从 start 向上找项目根（含 gui_app/gui_server.py，兼容旧布局）。"""
    if start is None:
        start = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else Path(__file__)
    p = Path(start).resolve()
    if p.is_file():
        p = p.parent
    for _ in range(max_up + 1):
        if (p / "gui_app" / "gui_server.py").is_file():
            return p
        if (p / "gui_server.py").is_file() and (p / "webui").is_dir():
            return p.parent
        # 兼容把 GUI 文件放在 scripts/ 下的旧布局
        if (p / "scripts" / "gui_server.py").is_file():
            return p
        parent = p.parent
        if parent == p:
            break
        p = parent
    return None


def norm_compare_path(p: str | Path) -> str:
    s = os.path.normcase(os.path.abspath(str(p)))
    return os.path.normpath(s)


def is_within(path: str | Path, root: str | Path) -> bool:
    """resolve 后是否位于 root 内（Windows 用 normcase 比较，防大小写绕过）。"""
    try:
        target = norm_compare_path(Path(path).resolve())
        base = norm_compare_path(Path(root).resolve())
    except OSError:
        return False
    if target == base:
        return True
    return target.startswith(base + os.sep)


def chmod_private(path: str | Path) -> None:
    if IS_WINDOWS:
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- 进程
def spawn_kwargs() -> dict[str, Any]:
    if IS_WINDOWS:
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def detached_kwargs() -> dict[str, Any]:
    """让 GUI 服务在启动器退出后继续运行。"""
    if IS_WINDOWS:
        return {"creationflags": _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(proc: subprocess.Popen, *, timeout: float = 8.0) -> None:
    """终止整个进程树：POSIX 进程组；Windows taskkill /T。幂等。"""
    if proc is None or proc.poll() is not None:
        return
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        return

    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
    else:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    else:
        try:
            proc.kill()
        except OSError:
            pass


def child_env(root: str | Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """子进程环境：UTF-8、HF 缓存、PATH；保留用户已有变量。"""
    root = Path(root).expanduser().resolve()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("HF_HOME", str(root / ".models"))
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    env["ORT_DISABLE_TELEMETRY"] = "1"
    if IS_MACOS:
        path = env.get("PATH", "")
        prefix = "/opt/homebrew/bin:/usr/local/bin"
        if prefix not in path:
            env["PATH"] = f"{prefix}:{path}" if path else prefix
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


# ---------------------------------------------------------------- 原生对话框
def _run_quiet(cmd: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"找不到命令：{cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "等待用户选择超时"
    except OSError as exc:
        return 1, "", str(exc)


def _apple_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def _pick_macos(kind: str, title: str) -> dict[str, Any]:
    if kind == "folder":
        script = f'POSIX path of (choose folder with prompt "{_apple_escape(title)}")'
    else:
        script = f'POSIX path of (choose file with prompt "{_apple_escape(title)}")'
    rc, out, err = _run_quiet(["osascript", "-e", script])
    if rc == 0 and out:
        return {"path": out.rstrip("/"), "cancelled": False, "error": ""}
    if "User canceled" in err or rc == 1 and "cancel" in (err + out).lower():
        return {"path": None, "cancelled": True, "error": ""}
    return {"path": None, "cancelled": False, "error": err or "系统选择框不可用"}


def _pick_windows(kind: str, title: str) -> dict[str, Any]:
    if kind == "folder":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            f"$d.Description = '{title}';"
            "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
            "{ [Console]::Out.Write($d.SelectedPath) }"
        )
    else:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
            "$d = New-Object System.Windows.Forms.OpenFileDialog;"
            f"$d.Title = '{title}';"
            "$d.Filter = 'GGUF 模型 (*.gguf)|*.gguf|所有文件 (*.*)|*.*';"
            "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
            "{ [Console]::Out.Write($d.FileName) }"
        )
    for exe in ("powershell.exe", "powershell", "pwsh"):
        rc, out, err = _run_quiet([exe, "-NoProfile", "-STA", "-Command", script], timeout=180)
        if rc == 0:
            return {"path": out or None, "cancelled": not bool(out), "error": "" if out else "未选择文件"}
        if rc == 127:
            continue
        return {"path": None, "cancelled": False, "error": err or "系统选择框不可用"}
    return {"path": None, "cancelled": False, "error": "找不到 PowerShell，无法打开系统选择框"}


def _pick_linux(kind: str, title: str) -> dict[str, Any]:
    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", f"--title={title}"]
        if kind == "folder":
            cmd.append("--directory")
        else:
            cmd.append("--file-filter=GGUF 模型 | *.gguf")
            cmd.append("--file-filter=所有文件 | *")
        rc, out, err = _run_quiet(cmd)
        if rc == 0 and out:
            return {"path": out, "cancelled": False, "error": ""}
        if rc == 1:
            return {"path": None, "cancelled": True, "error": ""}
        return {"path": None, "cancelled": False, "error": err or "zenity 调用失败"}
    if shutil.which("kdialog"):
        cmd = ["kdialog", "--getexistingdirectory" if kind == "folder" else "--getopenfilename",
               str(Path.home()), "*.gguf" if kind != "folder" else ""]
        rc, out, err = _run_quiet([c for c in cmd if c != ""])
        if rc == 0 and out:
            return {"path": out, "cancelled": False, "error": ""}
        if rc == 1:
            return {"path": None, "cancelled": True, "error": ""}
        return {"path": None, "cancelled": False, "error": err or "kdialog 调用失败"}
    return {"path": None, "cancelled": False, "error": "没有 zenity/kdialog，无法打开系统选择框；请直接输入路径"}


def pick_model_path(kind: str = "file", title: str = "选择 GGUF 模型文件") -> dict[str, Any]:
    """返回 {path, cancelled, error}。kind: file|folder。"""
    if IS_MACOS:
        return _pick_macos(kind, title)
    if IS_WINDOWS:
        return _pick_windows(kind, title)
    if IS_LINUX:
        return _pick_linux(kind, title)
    return {"path": None, "cancelled": False, "error": "当前系统不支持原生选择框，请直接输入路径"}


def show_error(message: str, title: str = "音声字幕工房") -> None:
    """启动器没有终端时用原生对话框显示致命错误。"""
    message = (message or "").strip()
    if IS_WINDOWS:
        try:
            ctypes.windll.user32.MessageBoxW(0, message[:2000], title[:200], 0x10)
            return
        except Exception:  # noqa: BLE001
            pass
    elif IS_MACOS:
        try:
            script = (
                f'display alert "{_apple_escape(title)}" message "{_apple_escape(message)}" '
                'as critical buttons {"好"} default button 1'
            )
            subprocess.run(["osascript", "-e", script], timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:  # noqa: BLE001
            pass
    elif IS_LINUX and shutil.which("zenity"):
        try:
            subprocess.run(["zenity", "--error", f"--title={title}", f"--text={message[:2000]}"],
                           timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:  # noqa: BLE001
            pass
    print(f"[{title}] {message}", file=sys.stderr)


def open_in_file_manager(path: str | Path, *, select: bool = False) -> tuple[bool, str]:
    """在系统文件管理器中打开文件/文件夹。失败返回 (False, 原因)。"""
    p = Path(path).expanduser()
    if not p.exists():
        return False, f"路径不存在：{p}"
    target = p
    if select and p.is_file():
        if IS_MACOS:
            try:
                subprocess.Popen(["open", "-R", str(p)])
                return True, ""
            except OSError as exc:
                return False, str(exc)
        if IS_WINDOWS:
            try:
                subprocess.Popen(["explorer", f"/select,{p}"])
                return True, ""
            except OSError as exc:
                return False, str(exc)
        target = p.parent
    try:
        if IS_WINDOWS:
            os.startfile(str(target))  # type: ignore[attr-defined]
            return True, ""
        if IS_MACOS:
            subprocess.Popen(["open", str(target)])
            return True, ""
        opener = shutil.which("xdg-open") or shutil.which("gio")
        if not opener:
            return False, "系统没有 xdg-open / gio，无法打开文件夹"
        cmd = [opener, str(target)] if opener.endswith("xdg-open") else [opener, "open", str(target)]
        subprocess.Popen(cmd)
        return True, ""
    except OSError as exc:
        return False, str(exc)


# ---------------------------------------------------------------- 浏览器窗口
def _browser_candidates() -> list[Path]:
    out: list[Path] = []
    if IS_MACOS:
        roots = [Path("/Applications"), Path.home() / "Applications"]
        names = [
            "Google Chrome.app/Contents/MacOS/Google Chrome",
            "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "Chromium.app/Contents/MacOS/Chromium",
            "Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
        for root in roots:
            for name in names:
                p = root / name
                if p.is_file():
                    out.append(p)
    elif IS_WINDOWS:
        env = os.environ
        bases = [
            env.get("PROGRAMFILES", r"C:\Program Files"),
            env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            env.get("LOCALAPPDATA", ""),
        ]
        rels = [
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"Chromium\Application\chrome.exe",
        ]
        for base in filter(None, bases):
            for rel in rels:
                p = Path(base) / rel
                if p.is_file():
                    out.append(p)
        for name in ("chrome.exe", "msedge.exe"):
            found = shutil.which(name)
            if found:
                out.append(Path(found))
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                     "microsoft-edge", "microsoft-edge-stable", "brave-browser"):
            found = shutil.which(name)
            if found:
                out.append(Path(found))
    # 去重
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        key = norm_compare_path(p)
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def open_ui(url: str, *, prefer_app: bool = True) -> str:
    """优先无地址栏的 app 窗口；失败回退默认浏览器。返回实际方式。"""
    if prefer_app:
        for exe in _browser_candidates():
            try:
                subprocess.Popen([str(exe), f"--app={url}"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return "app"
            except OSError:
                continue
    try:
        if webbrowser.open(url):
            return "browser"
    except Exception:  # noqa: BLE001
        pass
    return "none"


# ---------------------------------------------------------------- 防休眠
class SleepInhibitor:
    """with SleepInhibitor(enabled=...): 期间阻止系统空闲休眠。

    macOS: caffeinate -i 子进程
    Windows: SetThreadExecutionState（当前线程；退出时恢复）
    Linux: systemd-inhibit + sleep infinity（有 systemd 的桌面）
    """

    def __init__(self, enabled: bool = True, log: Callable[[str], None] | None = None):
        self.enabled = bool(enabled)
        self.log = log or (lambda _m: None)
        self.proc: subprocess.Popen | None = None
        self._win_set = False
        self._previous: int | None = None

    def __enter__(self) -> "SleepInhibitor":
        if not self.enabled:
            return self
        try:
            if IS_MACOS and shutil.which("caffeinate"):
                self.proc = subprocess.Popen(["caffeinate", "-i"],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif IS_WINDOWS:
                self._previous = ctypes.windll.kernel32.SetThreadExecutionState(
                    0x80000000 | 0x00000001  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                )
                self._win_set = True
            elif IS_LINUX and shutil.which("systemd-inhibit"):
                self.proc = subprocess.Popen(
                    ["systemd-inhibit", "--what=idle:sleep", "--who=ASMR-Translator",
                     "--why=正在提取或翻译字幕", "--mode=block", "sleep", "infinity"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except Exception as exc:  # noqa: BLE001
            self.log(f"无法阻止系统休眠（{type(exc).__name__}: {exc}），继续运行")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self.proc = None
        if self._win_set:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # ES_CONTINUOUS only
            except Exception:  # noqa: BLE001
                pass
            self._win_set = False


# ---------------------------------------------------------------- HTTP 小工具
def http_json(url: str, *, token: str = "", timeout: float = 3.0) -> dict[str, Any] | None:
    try:
        headers = {"X-GUI-Token": token} if token else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
        return json.loads(data)
    except Exception:  # noqa: BLE001
        return None


def wait_for_http(url: str, *, token: str = "", timeout: float = 40.0) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = http_json(url, token=token, timeout=2.0)
        if data:
            return data
        time.sleep(0.3)
    return None
