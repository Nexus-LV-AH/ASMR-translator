#!/usr/bin/env python3
"""音声字幕工房 · 桌面启动器（PyInstaller 打包成 .app/.exe/可执行）。

普通用户双击它；它负责：
  1. 找项目根目录（发布包/源码目录）
  2. 已运行的 GUI → 直接打开；正在安装 → 打开安装页
  3. 首次启动 → 本地安装页（内置 Python/uv + requirements）
  4. 启动 venv 里的 GUI 服务 → Chrome/Edge app 窗口（失败回退默认浏览器）

不打包 torch；不要求系统 Python；不出现终端窗口。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_DIR = HERE.parent
PROJECT_ROOT = APP_DIR.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(APP_DIR))

try:
    import gui_platform
except Exception as exc:  # noqa: BLE001
    gui_platform = None  # type: ignore[assignment]
    _GUI_PLATFORM_ERROR = exc
else:
    _GUI_PLATFORM_ERROR = None

import setup_env  # noqa: E402
from setup_server import SetupServer  # noqa: E402

APP_NAME = "音声字幕工房"


def _show_error(msg: str) -> None:
    if gui_platform is not None:
        gui_platform.show_error(msg, APP_NAME)
    else:
        print(f"[{APP_NAME}] {msg}", file=sys.stderr)


def _version(root: Path) -> str:
    try:
        vp = root / "gui_app" / "VERSION"
        if vp.is_file():
            return vp.read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        pass
    return "dev"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _log(root: Path, msg: str) -> None:
    try:
        d = root / "runtime"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "launcher.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except OSError:
        pass


def _manual_find_root(start: str | Path) -> Path | None:
    p = Path(start).resolve()
    if p.is_file():
        p = p.parent
    for _ in range(9):
        if (p / "gui_app" / "gui_server.py").is_file():
            return p
        if (p / "gui_server.py").is_file() and (p / "webui").is_dir():
            return p.parent
        # 兼容旧布局
        if (p / "scripts" / "gui_server.py").is_file():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def find_root(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if gui_platform is not None:
            root = gui_platform.find_project_root(p)
            if root:
                return root
        return _manual_find_root(p)
    starts: list[Path] = []
    if getattr(sys, "frozen", False):
        starts.append(Path(sys.executable))
        starts.append(Path(sys.executable).parent)
    starts.append(HERE)
    starts.append(APP_DIR)
    starts.append(PROJECT_ROOT)
    starts.append(Path.cwd())
    for start in starts:
        try:
            if gui_platform is not None:
                root = gui_platform.find_project_root(start)
                if root:
                    return root
            root = _manual_find_root(start)
            if root:
                return root
        except OSError:
            continue
    return None


def _open(url: str, root: Path, no_open: bool = False) -> None:
    if no_open:
        print(url, flush=True)
        return
    if gui_platform is None:
        _show_error(f"界面地址：\n{url}\n\n（自动打开浏览器失败：{_GUI_PLATFORM_ERROR}）")
        return
    mode = gui_platform.open_ui(url)
    if mode == "none":
        _show_error(f"无法自动打开浏览器，请手动访问：\n{url}")


def check_existing_gui(root: Path) -> str | None:
    data = _read_json(root / "runtime" / "launcher.json")
    if not data:
        return None
    port = data.get("port")
    token = data.get("token") or ""
    url = data.get("url") or ""
    if not port or not token:
        return None
    if gui_platform is None:
        return None
    health = gui_platform.http_json(f"http://127.0.0.1:{int(port)}/api/health", token=str(token), timeout=2.5)
    if health and health.get("ok"):
        return url or f"http://127.0.0.1:{int(port)}/?token={urllib.parse.quote(str(token))}"
    return None


def check_existing_setup(root: Path) -> str | None:
    data = _read_json(root / "runtime" / "setup.json")
    if not data:
        return None
    port = data.get("port")
    token = data.get("token") or ""
    url = data.get("url") or ""
    if not port or not token or gui_platform is None:
        return None
    status = gui_platform.http_json(f"http://127.0.0.1:{int(port)}/api/status", token=str(token), timeout=2.5)
    if status and status.get("ok"):
        return url or f"http://127.0.0.1:{int(port)}/?token={urllib.parse.quote(str(token))}"
    return None


def start_gui(root: Path, *, timeout: float = 60.0) -> str:
    """启动 venv 里的 GUI 服务（脱离父进程），返回它的 URL。"""
    py = setup_env.venv_python(root)
    if not py.is_file():
        raise RuntimeError(f"找不到运行环境：{py}")
    server = root / "gui_app" / "gui_server.py"
    if not server.is_file():
        raise RuntimeError(f"找不到 GUI 服务：{server}")
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    try:
        (runtime / "launcher.json").unlink()
    except OSError:
        pass
    log_path = runtime / "gui-server.log"
    log_file = log_path.open("a", encoding="utf-8")
    log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 GUI =====\n")
    log_file.flush()
    env = gui_platform.child_env(root) if gui_platform is not None else os.environ.copy()
    cmd = [str(py), str(server), "--config", str(root / "config.yaml"), "--no-browser"]
    kwargs = gui_platform.detached_kwargs() if gui_platform is not None else {}
    _log(root, "启动 GUI：" + " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                stdout=log_file, stderr=subprocess.STDOUT, **kwargs)
    except OSError as exc:
        raise RuntimeError(f"无法启动 GUI 服务：{exc}") from exc
    finally:
        try:
            log_file.close()
        except OSError:
            pass
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            last_error = f"GUI 服务退出（退出码 {proc.returncode}）"
            break
        data = _read_json(runtime / "launcher.json")
        port = data.get("port")
        token = data.get("token") or ""
        url = data.get("url") or ""
        if port and token and gui_platform is not None:
            health = gui_platform.http_json(f"http://127.0.0.1:{int(port)}/api/health", token=str(token), timeout=2.0)
            if health and health.get("ok"):
                return url or f"http://127.0.0.1:{int(port)}/?token={urllib.parse.quote(str(token))}"
        time.sleep(0.35)
    tail = ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        tail = "\n".join(lines)
    except OSError:
        pass
    raise RuntimeError(
        f"{last_error or '等待 GUI 服务超时'}。\n日志：{log_path}\n\n{tail}"
    )


def run_setup(root: Path, *, no_open: bool = False, force: bool = False) -> int:
    def on_ready() -> str:
        _log(root, "安装完成，启动 GUI")
        return start_gui(root)

    server = SetupServer(root, on_ready)
    url = server.start_http()
    _log(root, f"安装页：{url}")
    print(f"安装页：{url}", flush=True)
    _open(url, root, no_open=no_open)
    try:
        while True:
            snap = server.state.snapshot()
            if snap["state"] == "done":
                import time as _t
                _t.sleep(1.6)
                return 0
            if snap["state"] == "error" and no_open:
                return 1
            time.sleep(0.6)
    except KeyboardInterrupt:
        return 130
    finally:
        server.shutdown()


def self_check(root: Path, *, no_open: bool = True) -> int:
    """CI/打包冒烟：只验证安装页服务能启动、页面可访问，不安装依赖。"""
    server = SetupServer(root, lambda: "http://127.0.0.1:1/")
    url = server.start_http()
    ok = False
    try:
        if gui_platform is not None:
            data = gui_platform.http_json(f"http://127.0.0.1:{server.port}/api/status",
                                          token=server.token, timeout=3.0)
            ok = bool(data and data.get("ok"))
        print(f"self-check: setup server on {url} -> {'OK' if ok else 'FAIL'}")
        # 页面
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"Host": f"127.0.0.1:{server.port}"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            if "开始安装" not in body:
                ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"self-check page error: {exc}")
            ok = False
    finally:
        server.shutdown()
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=f"{APP_NAME} 桌面启动器")
    p.add_argument("--root", default=None, help="项目根目录（调试用）")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器（调试/CI）")
    p.add_argument("--force-setup", action="store_true", help="忽略已就绪环境，重新进入安装页")
    p.add_argument("--self-check", action="store_true", help="只检查安装页服务（CI）")
    p.add_argument("--version", action="store_true")
    args = p.parse_args(argv)
    root = find_root(args.root)
    if root is None:
        msg = ("找不到项目文件。请把启动图标放在解压后的音声字幕工房文件夹里再双击；"
               "如果你移动过文件，请把整个文件夹一起移动。")
        _show_error(msg)
        _log(Path.cwd(), msg)
        return 1
    if args.version:
        print(f"{APP_NAME} {_version(root)}")
        return 0
    _log(root, f"启动器开始；root={root}")
    if args.self_check:
        return self_check(root)
    # 1) 已经在跑
    existing = check_existing_gui(root)
    if existing:
        _log(root, f"复用已运行的界面：{existing}")
        _open(existing, root, no_open=args.no_open)
        return 0
    # 2) 正在安装
    setup_url = check_existing_setup(root)
    if setup_url:
        _log(root, f"打开进行中的安装页：{setup_url}")
        _open(setup_url, root, no_open=args.no_open)
        return 0
    # 3) 环境已就绪
    if not args.force_setup and setup_env.venv_ready(root):
        try:
            url = start_gui(root)
        except Exception as exc:  # noqa: BLE001
            _log(root, f"启动 GUI 失败：{exc}")
            _show_error(str(exc))
            return 1
        _log(root, f"界面：{url}")
        _open(url, root, no_open=args.no_open)
        return 0
    # 4) 首次安装
    return run_setup(root, no_open=args.no_open, force=args.force_setup)


if __name__ == "__main__":
    raise SystemExit(main())
