#!/usr/bin/env python3
"""图形界面本地服务：主界面 + 设置页 + 任务/模型/API 接口。

只绑定 127.0.0.1，随机 token；页面是 webui/ 下的静态文件。
启动方式（开发者）：python gui_app/gui.py [--port 8765] [--no-browser]
应用壳由 gui_app/launcher/ 负责拉起本脚本。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import mimetypes
import os
import re
import secrets
import signal
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import gguf_meta  # noqa: E402
import gui_fs  # noqa: E402
import gui_models  # noqa: E402
import gui_platform  # noqa: E402
from gui_jobs import JobManager  # noqa: E402

WEBUI_DIR = SCRIPT_DIR / "webui"
APP_DIR = SCRIPT_DIR
LOG = logging.getLogger("gui")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.status = int(status)
        self.code = code


class GuiApp:
    def __init__(self, root: str | Path, config_path: str | Path | None = None,
                 *, token: str = "", port: int = 8765, no_browser: bool = False):
        self.root = Path(root).resolve()
        self.config_path = Path(config_path).expanduser() if config_path else self.root / "config.yaml"
        if not self.config_path.is_absolute():
            self.config_path = (self.root / self.config_path).resolve()
        self.token = token or secrets.token_urlsafe(24)
        self.port = int(port)
        self.no_browser = bool(no_browser)
        self.stop = threading.Event()
        self.server: ThreadingHTTPServer | None = None
        self.started_at = gui_fs.now_iso()
        self.store = gui_models.ConfigStore(self.root, self.config_path)
        self.paths = gui_fs.build_paths(self.root, self.store.cfg, self.config_path)
        self.paths.ensure()
        self.launcher_path = self.paths.runtime / "launcher.json"
        self.jobs = JobManager(self.root, self.paths)
        self._cleanup_stale()

    # ------------------------------------------------------------ 启动/退出
    def _cleanup_stale(self) -> None:
        try:
            gui_fs.cleanup_stale_staging(self.paths)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("清理过期暂存失败：%s", exc)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={urllib.parse.quote(self.token)}"

    def write_launcher(self) -> None:
        data = {
            "pid": os.getpid(),
            "port": self.port,
            "token": self.token,
            "url": self.url,
            "version": self.version(),
            "started_at": self.started_at,
        }
        try:
            gui_fs.write_json(self.launcher_path, data, private=True)
        except OSError as exc:
            LOG.warning("写入 launcher.json 失败：%s", exc)

    def remove_launcher(self) -> None:
        try:
            self.launcher_path.unlink()
        except OSError:
            pass

    def version(self) -> str:
        try:
            vp = APP_DIR / "VERSION"
            if vp.is_file():
                return vp.read_text(encoding="utf-8").strip() or "dev"
        except OSError:
            pass
        return "dev"

    def request_shutdown(self) -> None:
        self.stop.set()
        srv = self.server
        if srv is not None:
            threading.Thread(target=srv.shutdown, daemon=True).start()

    def cleanup(self) -> None:
        try:
            self.jobs.shutdown()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("关闭任务管理器失败：%s", exc)
        self.remove_launcher()

    # ------------------------------------------------------------ 状态
    def state(self, since: int = 0) -> dict[str, Any]:
        try:
            self.store.refresh()
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"读取配置失败：{exc}", 500, "config") from exc
        settings = self.store.settings_payload(self.paths)
        staged = gui_fs.list_staged(self.paths)
        hint = staged.get("work_hint") or ""
        existing = gui_fs.existing_counts(self.paths, hint) if hint else {}
        snapshot = self.jobs.snapshot(since)
        return {
            "ok": True,
            "app": {
                "version": self.version(),
                "root": str(self.root),
                "platform": sys.platform,
                "pid": os.getpid(),
                "port": self.port,
                "started_at": self.started_at,
            },
            "settings": settings,
            "paths": settings.get("paths", {}),
            "staged": staged,
            "existing": existing,
            "job": snapshot.get("job"),
            "logs": snapshot.get("logs", {"entries": [], "last_seq": 0, "truncated": False}),
            "config_warnings": settings.get("config_warnings", []),
            "now": gui_fs.now_iso(),
        }

    def settings(self) -> dict[str, Any]:
        try:
            self.store.refresh()
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"读取配置失败：{exc}", 500, "config") from exc
        payload = self.store.settings_payload(self.paths)
        payload["ok"] = True
        return payload

    # ------------------------------------------------------------ 任务
    def _model_available(self, key: str, mode: str) -> bool:
        if not key:
            return False
        for view in self.store.models_payload().get("models", []):
            if view.get("key") == key:
                return bool(view.get("available"))
        return False

    def start_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.jobs.is_running():
            raise ApiError("已有任务在运行，请先等它结束或取消", 409, "busy")
        try:
            self.store.refresh()
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"读取配置失败：{exc}", 500, "config") from exc
        mode = str(payload.get("mode") or "").strip()
        if mode not in ("extract", "all", "translate"):
            raise ApiError("未知任务类型", 400, "mode")
        raw_work = str(payload.get("work") or "")
        work, err = gui_fs.validate_work_name(raw_work, self.paths)
        if err:
            raise ApiError(err, 400, "work")
        model = str(payload.get("model") or "").strip()
        if model and model not in self.store.merged_registry():
            raise ApiError(f"未知模型：{model}", 400, "model")
        if mode in ("all", "translate"):
            if not model:
                raise ApiError("请先在设置页配置翻译模型（云端 API 或本地模型）", 400, "model")
            if not self._model_available(model, mode):
                raise ApiError("选中的翻译模型当前不可用，请到设置页检查", 400, "model")
        elif model and not self._model_available(model, mode):
            raise ApiError("选中的模型当前不可用，请到设置页检查或改用「只用规则精修」", 400, "model")
        raw_opts = payload.get("options") or {}
        try:
            limit = int(raw_opts["limit"]) if raw_opts.get("limit") else None
        except (TypeError, ValueError):
            raise ApiError("试翻行数必须是整数", 400, "options")
        if limit is not None and limit <= 0:
            limit = None
        options = {
            "force": bool(raw_opts.get("force")),
            "bilingual": bool(raw_opts.get("bilingual")),
            "limit": limit,
            "no_consolidate": bool(raw_opts.get("no_consolidate")),
            "prevent_sleep": bool(raw_opts.get("prevent_sleep", True)),
            "refine_no_model": bool(raw_opts.get("refine_no_model")) or (mode == "extract" and not model),
        }
        use_existing = bool(payload.get("use_existing"))
        staged = gui_fs.list_staged(self.paths)
        existing = gui_fs.existing_counts(self.paths, work)
        kinds, err = gui_fs.validate_staged_for_mode(
            staged, mode, use_existing=use_existing, existing=existing)
        if err:
            raise ApiError(err, 400, "staged")
        try:
            counts = gui_fs.move_staged_to_workspace(self.paths, work, kinds)
        except OSError as exc:
            raise ApiError(f"搬移上传文件失败：{exc}", 500, "staged") from exc
        if model:
            try:
                self.store.remember(model)
            except Exception:  # noqa: BLE001
                pass
        try:
            job = self.jobs.start(mode=mode, work=work, model=model, options=options)
        except RuntimeError as exc:
            raise ApiError(str(exc), 409, "busy") from exc
        except ValueError as exc:
            raise ApiError(str(exc), 400, "start") from exc
        job["moved"] = counts
        return {"ok": True, "job": job, "moved": counts}

    def cancel_job(self) -> dict[str, Any]:
        ok = self.jobs.cancel()
        if not ok:
            raise ApiError("当前没有运行中的任务", 409, "idle")
        return {"ok": True}

    # ------------------------------------------------------------ 上传/暂存
    def upload(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> dict[str, Any]:
        if self.jobs.is_running():
            raise ApiError("任务运行中不能继续上传，请等任务结束后再添加", 409, "busy")
        kind = (query.get("kind") or [""])[0]
        relpath = (query.get("relpath") or [""])[0]
        work_hint = (query.get("work_hint") or [""])[0]
        try:
            length = int(handler.headers.get("Content-Length") or "0")
        except ValueError:
            raise ApiError("Content-Length 无效", 400, "length")
        if length <= 0:
            raise ApiError("上传内容为空", 400, "length")
        try:
            saved = gui_fs.save_upload(self.paths, kind, relpath, handler.rfile, length, work_hint)
        except ValueError as exc:
            raise ApiError(str(exc), 400, "upload") from exc
        except OSError as exc:
            raise ApiError(f"写入失败：{exc}", 500, "upload") from exc
        return {"ok": True, "file": saved, "staged": gui_fs.list_staged(self.paths)}

    def clear_staged(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.jobs.is_running():
            raise ApiError("任务运行中不能清空暂存", 409, "busy")
        kind = str(payload.get("kind") or "").strip() or None
        if kind and kind not in ("audio", "subtitle", "promptbook"):
            raise ApiError("未知的暂存类型", 400, "kind")
        gui_fs.clear_staged(self.paths, kind)
        return {"ok": True, "staged": gui_fs.list_staged(self.paths)}

    # ------------------------------------------------------------ 模型
    def _resolve_model_path(self, raw: str) -> Path:
        p = Path(str(raw or "")).expanduser()
        if not str(raw or "").strip():
            raise ApiError("缺少模型路径", 400, "path")
        if not p.is_absolute():
            p = self.root / p
        return p

    def pick_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind") or "file")
        if kind not in ("file", "folder"):
            raise ApiError("kind 只能是 file 或 folder", 400, "kind")
        result = gui_platform.pick_model_path(
            kind, "选择 GGUF 模型文件" if kind == "file" else "选择模型文件夹")
        result["ok"] = bool(result.get("path")) or bool(result.get("cancelled"))
        return result

    def inspect_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        p = self._resolve_model_path(str(payload.get("path") or ""))
        if p.is_dir():
            candidates = gguf_meta.scan_gguf_files(p)
            if not candidates:
                raise ApiError("这个文件夹里没有 .gguf 文件", 400, "not_found")
            return {"ok": True, "is_dir": True, "path": str(p), "candidates": candidates}
        if not p.exists():
            raise ApiError(f"文件不存在：{p}", 404, "not_found")
        info = self.store.inspect(p, force=bool(payload.get("force")))
        return {"ok": bool(info.get("ok")), "is_dir": False, "path": str(p),
                "model": info, "error": info.get("error") or ""}

    def add_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_if_running("任务运行中不能修改模型设置")
        p = self._resolve_model_path(str(payload.get("path") or ""))
        try:
            model = self.store.add_local_model(
                p,
                key=str(payload.get("key") or ""),
                display_name=str(payload.get("display_name") or ""),
                make_default=bool(payload.get("make_default")),
            )
        except ValueError as exc:
            raise ApiError(str(exc), 400, "model") from exc
        except OSError as exc:
            raise ApiError(f"保存模型配置失败：{exc}", 500, "model") from exc
        return {"ok": True, "model": model, "settings": self.store.settings_payload(self.paths)}

    def remove_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_if_running("任务运行中不能修改模型设置")
        try:
            self.store.remove_local_model(str(payload.get("key") or ""))
        except ValueError as exc:
            raise ApiError(str(exc), 400, "model") from exc
        return {"ok": True, "settings": self.store.settings_payload(self.paths)}

    def set_default_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_if_running("任务运行中不能修改默认模型")
        try:
            self.store.set_default(str(payload.get("key") or ""))
        except ValueError as exc:
            raise ApiError(str(exc), 400, "model") from exc
        return {"ok": True, "settings": self.store.settings_payload(self.paths)}

    def test_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.store.test_api(
                key=str(payload.get("key") or ""),
                api_key=str(payload.get("api_key") or ""),
                base_url=str(payload.get("base_url") or ""),
                model=str(payload.get("model") or ""),
                provider=str(payload.get("provider") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"{type(exc).__name__}: {exc}", 500, "api") from exc

    def save_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_if_running("任务运行中不能修改 API 设置")
        try:
            saved = self.store.save_api(
                key=str(payload.get("key") or ""),
                provider=str(payload.get("provider") or "deepseek"),
                api_key=str(payload.get("api_key") or ""),
                base_url=str(payload.get("base_url") or ""),
                model=str(payload.get("model") or ""),
                display_name=str(payload.get("display_name") or ""),
                api_key_env=str(payload.get("api_key_env") or ""),
                clear_key=bool(payload.get("clear_key")),
                make_default=bool(payload.get("make_default")),
            )
        except ValueError as exc:
            raise ApiError(str(exc), 400, "api") from exc
        except OSError as exc:
            raise ApiError(f"保存 API 设置失败：{exc}", 500, "api") from exc
        saved["settings"] = self.store.settings_payload(self.paths)
        return {"ok": True, **saved}

    def clear_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_if_running("任务运行中不能修改 API 设置")
        try:
            result = self.store.clear_api_key(str(payload.get("key") or ""))
        except ValueError as exc:
            raise ApiError(str(exc), 400, "api") from exc
        result["settings"] = self.store.settings_payload(self.paths)
        return {"ok": True, **result}

    def _reject_if_running(self, message: str) -> None:
        if self.jobs.is_running():
            raise ApiError(message, 409, "busy")

    # ------------------------------------------------------------ 文件
    def reveal(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = str(payload.get("path") or "")
        if not raw:
            raise ApiError("缺少路径", 400, "path")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.root / p
        try:
            p = p.resolve()
        except OSError:
            pass
        roots = self.paths.data_roots() + [self.root]
        if not any(gui_platform.is_within(p, root) for root in roots):
            raise ApiError("路径不在允许的目录内", 400, "path")
        if not p.exists():
            raise ApiError("路径不存在", 404, "path")
        ok, err = gui_platform.open_in_file_manager(p, select=p.is_file())
        if not ok:
            return {"ok": False, "path": str(p), "error": err}
        return {"ok": True, "path": str(p)}

    def preview(self, query: dict[str, list[str]]) -> dict[str, Any]:
        raw = (query.get("path") or [""])[0]
        if not raw:
            raise ApiError("缺少 path", 400, "path")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.root / p
        result = gui_fs.preview_text(p, self.paths.data_roots())
        if not result.get("ok"):
            raise ApiError(result.get("error") or "无法预览", 404, "preview")
        return result

    def download_zip(self, query: dict[str, list[str]]) -> tuple[bytes, str]:
        work_raw = (query.get("work") or [""])[0]
        work, err = gui_fs.validate_work_name(work_raw, self.paths)
        if err:
            raise ApiError(err, 400, "work")
        try:
            self.store.refresh()
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"读取配置失败：{exc}", 500, "config") from exc
        result = gui_fs.scan_results(self.paths, self.store.cfg, work, "all")
        data = gui_fs.zip_lrcs(result)
        if not data:
            raise ApiError("没有可下载的 LRC", 404, "empty")
        return data, f"{work}_LRC.zip"

    # ------------------------------------------------------------ 健康
    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "pid": os.getpid(),
            "port": self.port,
            "version": self.version(),
            "job_running": self.jobs.is_running(),
            "url": self.url,
        }


# ---------------------------------------------------------------- HTTP
class GuiHandler(BaseHTTPRequestHandler):
    app: GuiApp
    server_version = "ASMRTranslatorGUI/1.0"
    protocol_version = "HTTP/1.1"

    # -- 基础
    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.app.port
        return host in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _origin_ok(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip().lower()
        if not origin:
            return True
        port = self.app.port
        return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def _token_ok(self) -> bool:
        token = self.headers.get("X-GUI-Token") or ""
        if not token:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = (qs.get("token") or [""])[0]
        return bool(token) and secrets.compare_digest(token, self.app.token)

    def _guard(self, *, token: bool = True) -> bool:
        if not self._host_ok():
            self._send_json(403, {"ok": False, "error": "Host 不被允许"})
            return False
        if token and not self._token_ok():
            self._send_json(403, {"ok": False, "error": "token 无效；请从终端/启动器打印的地址打开"})
            return False
        return True

    def _send(self, code: int, body: bytes, content_type: str, *,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, code: int, obj: Any, *, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=None).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", extra=extra)

    def _api_error(self, exc: Exception) -> None:
        if isinstance(exc, ApiError):
            self._send_json(exc.status, {"ok": False, "error": str(exc), "code": exc.code})
            return
        LOG.exception("API 未处理异常")
        self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}", "code": "internal"})

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            raise ApiError("Content-Length 无效", 400, "length")
        if length <= 0:
            return {}
        if length > 2 * 1024 * 1024:
            self.close_connection = True
            raise ApiError("请求体太大", 413, "length")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError("请求体不是有效 JSON", 400, "json") from exc
        if not isinstance(data, dict):
            raise ApiError("请求体必须是 JSON 对象", 400, "json")
        return data

    def _query(self) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, keep_blank_values=True)

    # -- 静态文件
    def _serve_file(self, path: Path, *, content_type: str | None = None,
                    download_name: str = "") -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._send_json(404, {"ok": False, "error": "文件不存在", "code": "not_found"})
            return
        ctype = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() in (".html", ".css", ".js", ".svg"):
            ctype += "; charset=utf-8"
        extra = {}
        if download_name:
            ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", download_name) or "download"
            quoted = urllib.parse.quote(download_name)
            extra["Content-Disposition"] = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"
        self._send(200, data, ctype, extra=extra)

    def _serve_static(self, rel: str) -> None:
        try:
            decoded = urllib.parse.unquote(rel)
        except Exception:  # noqa: BLE001
            decoded = rel
        if "\x00" in decoded or ".." in decoded.split("/"):
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        base = WEBUI_DIR.resolve()
        target = (base / decoded).resolve()
        if not (target == base or base in target.parents) or not target.is_file():
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if target.suffix.lower() not in {".html", ".css", ".js", ".svg", ".png", ".ico", ".woff2", ".json"}:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        self._serve_file(target)

    # -- GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                if not self._guard(token=True):
                    return
                self._serve_file(WEBUI_DIR / "index.html")
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
                return
            if not self._guard(token=True):
                return
            if path == "/api/state":
                qs = self._query()
                try:
                    since = int((qs.get("since") or ["0"])[0] or 0)
                except ValueError:
                    since = 0
                self._send_json(200, self.app.state(since))
                return
            if path == "/api/settings":
                self._send_json(200, self.app.settings())
                return
            if path == "/api/health":
                self._send_json(200, self.app.health())
                return
            if path == "/api/preview":
                self._send_json(200, self.app.preview(self._query()))
                return
            if path == "/api/download":
                data, name = self.app.download_zip(self._query())
                self._send(200, data, "application/zip",
                           extra={"Content-Disposition":
                                  f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}"})
                return
            self._send_json(404, {"ok": False, "error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._api_error(exc)

    # -- POST
    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if not self._host_ok():
                self._send_json(403, {"ok": False, "error": "Host 不被允许"})
                return
            if not self._origin_ok():
                self._send_json(403, {"ok": False, "error": "Origin 不被允许"})
                return
            if not self._token_ok():
                self._send_json(403, {"ok": False, "error": "token 无效"})
                return
            if path == "/api/upload":
                try:
                    result = self.app.upload(self, self._query())
                except Exception:
                    # 校验失败时请求体可能没读完，关连接避免下一条请求错位
                    self.close_connection = True
                    raise
                self._send_json(200, result)
                return
            payload = self._read_json()
            if path == "/api/start":
                self._send_json(200, self.app.start_job(payload))
                return
            if path == "/api/cancel":
                self._send_json(200, self.app.cancel_job())
                return
            if path == "/api/clear":
                self._send_json(200, self.app.clear_staged(payload))
                return
            if path == "/api/models/pick":
                self._send_json(200, self.app.pick_model(payload))
                return
            if path == "/api/models/inspect":
                self._send_json(200, self.app.inspect_model(payload))
                return
            if path == "/api/models/add":
                self._send_json(200, self.app.add_model(payload))
                return
            if path == "/api/models/remove":
                self._send_json(200, self.app.remove_model(payload))
                return
            if path == "/api/models/default":
                self._send_json(200, self.app.set_default_model(payload))
                return
            if path == "/api/api/test":
                self._send_json(200, self.app.test_api(payload))
                return
            if path == "/api/api/save":
                self._send_json(200, self.app.save_api(payload))
                return
            if path == "/api/api/clear":
                self._send_json(200, self.app.clear_api(payload))
                return
            if path == "/api/reveal":
                self._send_json(200, self.app.reveal(payload))
                return
            if path == "/api/shutdown":
                self._send_json(200, {"ok": True, "message": "服务正在退出"})
                self.app.request_shutdown()
                return
            self._send_json(404, {"ok": False, "error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._api_error(exc)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(app: GuiApp, preferred_port: int) -> ThreadingHTTPServer:
    ports = [int(preferred_port or 0)]
    if preferred_port:
        ports += [preferred_port + i for i in range(1, 10)]
    ports.append(0)
    last: Exception | None = None
    for port in ports:
        try:
            handler = type("GuiHandlerBound", (GuiHandler,), {"app": app})
            srv = _Server(("127.0.0.1", int(port)), handler)
            app.port = int(srv.server_address[1])
            app.server = srv
            return srv
        except OSError as exc:
            last = exc
            continue
    raise RuntimeError(f"无法绑定本地端口：{last}")


def _banner(app: GuiApp) -> None:
    print("=" * 68)
    print(" 音声字幕工房 · 图形界面")
    print(f" 项目目录：{app.root}")
    print(f" 界面地址：{app.url}")
    print(" 关闭服务：Ctrl-C，或网页右上角「退出」")
    try:
        payload = app.store.models_payload()
        available = [m for m in payload.get("models", []) if m.get("available")]
        if available:
            print(" 可用模型：" + "、".join(str(m.get("display_name")) for m in available))
        else:
            print(" 可用模型：暂无（打开界面后在设置页配置）")
    except Exception:  # noqa: BLE001
        pass
    print("=" * 68, flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="音声字幕工房 图形界面服务（只绑定 127.0.0.1）")
    p.add_argument("--config", default=None, help="config.yaml 路径（默认项目根下）")
    p.add_argument("--port", type=int, default=8765, help="首选端口（被占用会自动顺延）")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器（由启动器负责）")
    p.add_argument("--token", default="", help="固定 token（仅测试/调试用；默认随机）")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    try:
        app = GuiApp(PROJECT_ROOT, args.config, token=args.token, port=args.port,
                     no_browser=args.no_browser)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("启动失败")
        gui_platform.show_error(f"图形界面启动失败：\n{type(exc).__name__}: {exc}")
        return 1
    try:
        srv = make_server(app, int(args.port or 0))
    except Exception as exc:  # noqa: BLE001
        LOG.exception("无法启动本地服务")
        gui_platform.show_error(f"无法启动本地服务：\n{exc}")
        return 1
    app.write_launcher()
    _banner(app)
    if not app.no_browser:
        threading.Timer(0.6, lambda: gui_platform.open_ui(app.url)).start()

    def _stop(signum, frame):  # noqa: ANN001
        LOG.info("收到信号 %s，准备退出", signum)
        app.request_shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGHUP"):
        try:
            signal.signal(signal.SIGHUP, _stop)
        except (ValueError, OSError):
            pass

    serve_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    serve_thread.start()
    try:
        while not app.stop.wait(timeout=0.5):
            pass
    except KeyboardInterrupt:
        app.request_shutdown()
    finally:
        LOG.info("正在关闭图形界面服务…")
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass
        app.cleanup()
        LOG.info("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
