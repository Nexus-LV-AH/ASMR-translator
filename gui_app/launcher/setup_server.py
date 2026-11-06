#!/usr/bin/env python3
"""首次启动的安装页服务（只绑定 127.0.0.1）。

launcher 在缺少 .venv 时启动它：浏览器显示进度/日志，安装完成后回调
launcher 启动 GUI 服务，页面自动跳转。不暴露任意命令执行：只有预定义的
setup_env 流程。
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from setup_env import PYPI_MIRROR, setup_env

PAGE_FILE = Path(__file__).with_name("setup_page.html")


def _page_html() -> str:
    candidates = [PAGE_FILE, Path(__file__).resolve().parent / "setup_page.html"]
    mei = getattr(sys, "_MEIPASS", "")
    if mei:
        candidates += [Path(mei) / "launcher" / "setup_page.html", Path(mei) / "setup_page.html"]
    for p in candidates:
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8")
        except OSError:
            continue
    return "<!doctype html><meta charset='utf-8'><h1>安装页缺失</h1><p>请重新下载完整发布包。</p>"


class SetupState:
    def __init__(self, root: Path, on_ready: Callable[[], str]):
        self.root = Path(root)
        self.on_ready = on_ready
        self.lock = threading.RLock()
        self.state = "idle"
        self.stage = "等待开始"
        self.error = ""
        self.gui_url = ""
        self.started_at = ""
        self.finished_at = ""
        self.logs: list[str] = []
        self.thread: threading.Thread | None = None

    def log(self, line: str) -> None:
        text = str(line or "").rstrip("\n")
        if not text:
            return
        with self.lock:
            self.logs.append(f"{time.strftime('%H:%M:%S')}  {text}")
            if len(self.logs) > 800:
                del self.logs[: len(self.logs) - 800]
        try:
            runtime = self.root / "runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            with (runtime / "setup.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
        except OSError:
            pass

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "ok": True,
                "state": self.state,
                "stage": self.stage,
                "logs": list(self.logs[-400:]),
                "error": self.error,
                "gui_url": self.gui_url,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }

    def start(self, mirror: str = "") -> bool:
        with self.lock:
            if self.state == "running":
                return False
            self.state = "running"
            self.stage = "准备开始"
            self.error = ""
            self.gui_url = ""
            self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self.finished_at = ""
            self.logs = []
            index = PYPI_MIRROR if mirror in ("tuna", "mirror", "cn") else ""
            self.thread = threading.Thread(target=self._run, args=(index,), daemon=True)
            self.thread.start()
            return True

    def _run(self, index: str) -> None:
        try:
            self.log("开始安装运行环境（首次启动只需一次）")
            if index:
                self.log(f"使用 PyPI 镜像：{index}")
            setup_env(self.root, log=self.log, mirror=index,
                      stage=lambda s: self._set_stage(s))
            self._set_stage("启动图形界面")
            url = self.on_ready()
            with self.lock:
                self.gui_url = url
                self.state = "done"
                self.stage = "完成"
                self.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self.log("界面已启动，正在跳转…")
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                self.state = "error"
                self.error = f"{type(exc).__name__}: {exc}"
                self.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self.log(f"安装失败：{self.error}")
            self.log("可以检查网络/磁盘后点「重试」；日志也可以复制出来反馈。")

    def _set_stage(self, stage: str) -> None:
        with self.lock:
            self.stage = stage
        self.log(f"== {stage} ==")


class SetupServer:
    def __init__(self, root: str | Path, on_ready: Callable[[], str], *,
                 token: str = "", host: str = "127.0.0.1"):
        self.root = Path(root).resolve()
        self.state = SetupState(self.root, on_ready)
        self.token = token or secrets.token_urlsafe(24)
        self.host = host
        self.port = 0
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.runtime_dir = self.root / "runtime"
        self.setup_file = self.runtime_dir / "setup.json"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={urllib.parse.quote(self.token)}"

    def start_http(self) -> str:
        handler = type("SetupHandlerBound", (SetupHandler,), {"app": self})
        self.server = _Server((self.host, 0), handler)
        self.port = int(self.server.server_address[1])
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._write_setup_file()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.url

    def _write_setup_file(self) -> None:
        data = {
            "pid": os.getpid(),
            "port": self.port,
            "token": self.token,
            "url": self.url,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            tmp = self.setup_file.with_name(self.setup_file.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.setup_file)
        except OSError:
            pass

    def remove_setup_file(self) -> None:
        try:
            self.setup_file.unlink()
        except OSError:
            pass

    def shutdown(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.server = None
        self.remove_setup_file()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class SetupHandler(BaseHTTPRequestHandler):
    app: SetupServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return host in {f"127.0.0.1:{self.app.port}", f"localhost:{self.app.port}"}

    def _token_ok(self) -> bool:
        token = self.headers.get("X-GUI-Token") or ""
        if not token:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = (qs.get("token") or [""])[0]
        return bool(token) and secrets.compare_digest(token, self.app.token)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _guard(self, token: bool = True) -> bool:
        if not self._host_ok():
            self._json(403, {"ok": False, "error": "Host 不被允许"})
            return False
        if token and not self._token_ok():
            self._json(403, {"ok": False, "error": "token 无效"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                if not self._guard(token=True):
                    return
                self._send(200, _page_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not self._guard(token=True):
                return
            if path == "/api/status":
                self._json(200, self.app.state.snapshot())
                return
            self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if not self._guard(token=True):
                return
            if path != "/api/start":
                self._json(404, {"ok": False, "error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or "0")
            payload: dict[str, Any] = {}
            if length > 0:
                raw = self.rfile.read(min(length, 1024 * 1024))
                try:
                    payload = json.loads(raw.decode("utf-8")) or {}
                except ValueError:
                    payload = {}
            mirror = str(payload.get("mirror") or "")
            ok = self.app.state.start(mirror)
            if not ok:
                self._json(409, {"ok": False, "error": "安装已经在进行中"})
                return
            self._json(200, {"ok": True, "state": self.app.state.snapshot()})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
