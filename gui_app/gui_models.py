#!/usr/bin/env python3
"""本地模型注册表 + 设置页 + 云端 API 配置。

设计原则：
- 只写 config.local.yaml（个人覆盖）和 .env（Key），绝不改 tracked 的 config.yaml。
- 本地模型原地使用：选择路径 → 读 GGUF 头识别名称/量化/参数 → 动态写一条 registry。
- 云端 API 复用 translate.py 已有的 api_entry / ApiClient / _api_model_names，
  不重造请求逻辑；测试连接与正式任务走同一套配置。
- API Key 只存在 .env（0600），接口/日志都不回显。
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
from pathlib import Path
from typing import Any

import yaml

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import gguf_meta
import gui_fs
from gui_platform import child_env, chmod_private

import translate as T

# 复用 CLI 的模型可用性判断，避免 GUI 和 CLI 对“这个模型能不能用”有不同答案
try:
    import subtitle as CLI  # scripts/subtitle.py（运行 GUI 服务时 scripts 在 sys.path[0]）
    if not hasattr(CLI, "load_model_registry"):
        raise ImportError
except ImportError:  # 兜底：按文件路径加载，避免被根目录的同名 subtitle.py 遮住
    _spec = importlib.util.spec_from_file_location(
        "gui_subtitle_cli", SCRIPTS_DIR / "subtitle.py")
    CLI = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(CLI)  # type: ignore[union-attr]

API_PRESETS = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "custom": {"label": "自定义 OpenAI 兼容", "base_url": "", "key_env": "CUSTOM_API_KEY"},
}

_LOCAL_EXTRA_KEYS = ("path", "by_tag", "note", "vram_gb", "gui")


def _slug(text: str, *, fallback: str = "local") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return (s or fallback)[:48].strip("-") or fallback


def _read_yaml(path: Path, *, backup_bad: bool = False) -> dict:
    try:
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        if backup_bad and path.exists():
            bak = path.with_name(path.name + ".bak")
            try:
                shutil.copy2(path, bak)
            except OSError:
                pass
        raise
    return {}


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def _env_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _env_quote(value: str) -> str:
    if re.search(r"[\s#\"'=]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def env_get(path: Path, key: str) -> str:
    for line in _env_lines(path):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[len("export "):].strip()
        k, v = s.split("=", 1)
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return ""


def env_set(path: Path, key: str, value: str) -> None:
    lines = _env_lines(path)
    found = False
    out: list[str] = []
    for line in lines:
        s = line.strip()
        raw = s[len("export "):].strip() if s.startswith("export ") else s
        if raw and not raw.startswith("#") and "=" in raw:
            k = raw.split("=", 1)[0].strip()
            if k == key:
                out.append(f"{key}={_env_quote(value)}")
                found = True
                continue
        out.append(line)
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key}={_env_quote(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    chmod_private(tmp)
    os.replace(tmp, path)
    chmod_private(path)


def env_remove(path: Path, key: str) -> None:
    lines = _env_lines(path)
    out: list[str] = []
    for line in lines:
        s = line.strip()
        raw = s[len("export "):].strip() if s.startswith("export ") else s
        if raw and not raw.startswith("#") and "=" in raw:
            if raw.split("=", 1)[0].strip() == key:
                continue
        out.append(line)
    if not path.exists():
        return
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    chmod_private(tmp)
    os.replace(tmp, path)
    chmod_private(path)


class ConfigStore:
    """读取合并配置、构建模型视图、写本地覆盖和 Key。"""

    def __init__(self, root: str | Path, config_path: str | Path | None = None):
        self.root = Path(root).resolve()
        cfg = Path(config_path).expanduser() if config_path else self.root / "config.yaml"
        self.config_path = cfg if cfg.is_absolute() else (self.root / cfg).resolve()
        self.local_path = self.config_path.with_name("config.local.yaml")
        self.env_path = self.root / ".env"
        self.runtime_dir = self.root / "runtime"
        self.meta_cache_path = self.runtime_dir / "model_meta.json"
        self._lock = threading.RLock()
        self._cfg: dict = {}
        self._base: dict = {}
        self._local: dict = {}
        self._sig: tuple[float, float, float] | None = None
        _raw_cache = gui_fs.read_json(self.meta_cache_path, {})
        self._meta_cache: dict[str, Any] = _raw_cache if isinstance(_raw_cache, dict) else {}
        self._last_error = ""
        self.refresh(force=True)

    # ------------------------------------------------------------ 读取
    def refresh(self, *, force: bool = False) -> dict:
        with self._lock:
            try:
                sig = (
                    self.config_path.stat().st_mtime if self.config_path.exists() else 0.0,
                    self.local_path.stat().st_mtime if self.local_path.exists() else 0.0,
                    self.env_path.stat().st_mtime if self.env_path.exists() else 0.0,
                )
            except OSError:
                sig = (0.0, 0.0, 0.0)
            if not force and self._sig == sig and self._cfg:
                return self._cfg
            if not self.config_path.exists():
                raise RuntimeError(f"找不到配置文件：{self.config_path}")
            self._cfg = T.load_config(self.config_path)
            self._base = _read_yaml(self.config_path)
            try:
                self._local = _read_yaml(self.local_path, backup_bad=True)
            except yaml.YAMLError as exc:
                raise RuntimeError(
                    f"config.local.yaml 格式错误，已备份为 {self.local_path.name}.bak：{exc}") from exc
            # .env 先按原样加载，再把生效 Key 明确覆盖进进程环境（避免 setdefault 残留旧值）
            T.load_dotenv()
            self._sig = sig
            self._last_error = ""
            self._refresh_env_locked()
            return self._cfg

    @property
    def cfg(self) -> dict:
        return self._cfg

    @property
    def base(self) -> dict:
        return self._base

    @property
    def local(self) -> dict:
        return self._local

    def base_registry(self) -> dict:
        return ((self._base.get("translation") or {}).get("registry") or {})

    def local_registry(self) -> dict:
        return ((self._local.get("translation") or {}).get("registry") or {})

    def merged_registry(self) -> dict:
        return ((self._cfg.get("translation") or {}).get("registry") or {})

    def config_default(self) -> str:
        t = self._cfg.get("translation") or {}
        return str(t.get("default") or t.get("model") or "")

    def last_model(self) -> str:
        try:
            return CLI.recall_model() or ""
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------ 环境变量
    def _entry_key_env(self, entry: dict) -> str:
        kind = str(entry.get("api") or "").lower()
        preset = API_PRESETS.get(kind, {})
        return str(entry.get("api_key_env") or preset.get("key_env") or f"{kind.upper()}_API_KEY")

    def _refresh_env_locked(self) -> None:
        for key, entry in self.merged_registry().items():
            if not (entry or {}).get("api"):
                continue
            env_name = self._entry_key_env(entry)
            value = str(entry.get("api_key") or "").strip() or env_get(self.env_path, env_name)
            if value:
                os.environ[env_name] = value
            else:
                os.environ.pop(env_name, None)

    def refresh_env(self) -> None:
        with self._lock:
            self._refresh_env_locked()

    def key_source(self, entry: dict) -> str:
        if str(entry.get("api_key") or "").strip():
            return "config"
        env_name = self._entry_key_env(entry)
        if env_get(self.env_path, env_name) or os.environ.get(env_name):
            return "env"
        return "none"

    def effective_api(self, entry: dict) -> dict[str, str]:
        kind = str(entry.get("api") or "").lower()
        preset = API_PRESETS.get(kind, {})
        base_url = (os.environ.get(f"{kind.upper()}_BASE_URL")
                    or str(entry.get("base_url") or "").strip()
                    or preset.get("base_url", ""))
        model = (os.environ.get(f"{kind.upper()}_MODEL")
                 or str(entry.get("model") or "").strip())
        env_base = os.environ.get(f"{kind.upper()}_BASE_URL", "")
        env_model = os.environ.get(f"{kind.upper()}_MODEL", "")
        return {
            "base_url": str(base_url).rstrip("/"),
            "model": str(model),
            "key_env": self._entry_key_env(entry),
            "env_base_override": env_base if env_base and env_base != str(entry.get("base_url") or "") else "",
            "env_model_override": env_model if env_model and env_model != str(entry.get("model") or "") else "",
        }

    # ------------------------------------------------------------ 模型路径/识别
    def entry_path(self, entry: dict) -> Path | None:
        raw = str(entry.get("path") or "").strip()
        if raw:
            p = Path(raw).expanduser()
            return p if p.is_absolute() else (self.root / p)
        local_dir = str(entry.get("local_dir") or "").strip()
        gguf = str(entry.get("gguf") or "").strip()
        if local_dir:
            d = Path(local_dir).expanduser()
            if not d.is_absolute():
                d = self.root / d
            return (d / gguf) if gguf else None
        return None

    def inspect(self, path: str | Path, *, force: bool = False) -> dict:
        """带缓存的 GGUF 识别（按路径+大小+mtime）。"""
        p = Path(path).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        if not p.exists():
            info = gguf_meta.inspect_model(p).as_dict()
            info["ok"] = False
            info["error"] = info.get("error") or "文件不存在"
            return info
        try:
            st = p.stat()
        except OSError as exc:
            return {"ok": False, "path": str(p), "error": f"无法读取：{exc}"}
        key = str(p)
        with self._lock:
            cached = self._meta_cache.get(key)
            if (not force and isinstance(cached, dict)
                    and cached.get("size") == st.st_size
                    and abs(float(cached.get("mtime") or 0) - st.st_mtime) < 1.0
                    and isinstance(cached.get("info"), dict)):
                return cached["info"]
        info = gguf_meta.inspect_model(p).as_dict()
        with self._lock:
            self._meta_cache[key] = {"size": st.st_size, "mtime": st.st_mtime, "info": info}
            try:
                gui_fs.write_json(self.meta_cache_path, self._meta_cache)
            except OSError:
                pass
        return info

    # ------------------------------------------------------------ 视图
    def _local_view(self, key: str, entry: dict) -> dict:
        path = self.entry_path(entry)
        base_reg = self.base_registry()
        local_reg = self.local_registry()
        info: dict = {}
        available = False
        reason = ""
        if path is None:
            reason = "还没配置本地模型路径"
        elif not path.exists():
            reason = f"文件不存在：{path}"
        elif path.is_dir():
            available = False
            reason = "路径是文件夹；请选择具体的 .gguf 文件"
        else:
            info = self.inspect(path)
            available = bool(info.get("ok"))
            if not available:
                reason = info.get("error") or "无法识别这个 GGUF"
        auto_display = str(info.get("auto_display_name") or "") or (path.stem if path else key)
        by_tag = str(entry.get("by_tag") or "").strip() or auto_display
        display = by_tag or key
        quant = str(info.get("quant") or "")
        params = str(info.get("params") or "")
        size_gb = float(info.get("size_gb") or 0)
        if not size_gb and path and path.is_file():
            try:
                size_gb = path.stat().st_size / (1024 ** 3)
            except OSError:
                pass
        note = str(entry.get("note") or "").strip()
        if not note and (params or quant or size_gb):
            note = "本地模型｜" + "｜".join(x for x in (params, quant, f"{size_gb:.2f}GB") if x)
        warnings = list(info.get("warnings") or [])
        return {
            "key": key,
            "kind": "local",
            "display_name": display,
            "by_tag": by_tag,
            "auto_display_name": auto_display,
            "name": str(info.get("name") or (path.stem if path else key)),
            "path": str(path) if path else "",
            "file_name": str(info.get("file_name") or (path.name if path else "")),
            "size_bytes": int(info.get("size_bytes") or 0),
            "size_gb": round(size_gb, 2),
            "available": available,
            "reason": reason,
            "note": note,
            "params": params,
            "quant": quant,
            "architecture": str(info.get("architecture") or ""),
            "context_length": info.get("context_length"),
            "block_count": info.get("block_count"),
            "has_chat_template": bool(info.get("has_chat_template")),
            "file_type": info.get("file_type"),
            "split_count": int(info.get("split_count") or 1),
            "split_no": int(info.get("split_no") or 1),
            "split_missing": list(info.get("split_missing") or []),
            "family": str(info.get("family") or ""),
            "translation_specialized": bool(info.get("translation_specialized")),
            "warnings": warnings,
            "builtin": key in base_reg,
            "local_override": key in local_reg,
            "load_path": str(info.get("load_path") or (path if path else "")),
        }

    def _api_view(self, key: str, entry: dict) -> dict:
        eff = self.effective_api(entry)
        source = self.key_source(entry)
        key_configured = source != "none"
        kind = str(entry.get("api") or "").lower()
        preset = API_PRESETS.get(kind, {})
        provider = preset.get("label") or (kind.capitalize() if kind else "API")
        base_url = eff["base_url"]
        model = eff["model"]
        available = bool(key_configured and base_url and model)
        reason = ""
        if not key_configured:
            reason = "还没配置 API Key"
        elif not base_url or not model:
            reason = "Base URL 或模型名还没填"
        by_tag = str(entry.get("by_tag") or "").strip()
        display = by_tag or f"{provider} · {model or key}"
        warnings: list[str] = []
        if eff["env_base_override"]:
            warnings.append(f"环境变量 {kind.upper()}_BASE_URL 会覆盖界面里的 Base URL：{eff['env_base_override']}")
        if eff["env_model_override"]:
            warnings.append(f"环境变量 {kind.upper()}_MODEL 会覆盖界面里的模型：{eff['env_model_override']}")
        if str(entry.get("api_key") or "").strip():
            warnings.append("Key 写在 config.yaml/config.local 的 api_key 里（优先于 .env）")
        note = str(entry.get("note") or "").strip() or f"云端 API｜{model or '未填模型'}"
        return {
            "key": key,
            "kind": "api",
            "display_name": display,
            "by_tag": display,
            "auto_display_name": display,
            "name": model,
            "path": "",
            "file_name": "",
            "size_bytes": 0,
            "size_gb": 0,
            "available": available,
            "reason": reason,
            "note": note,
            "params": "",
            "quant": "",
            "architecture": "",
            "context_length": None,
            "block_count": None,
            "has_chat_template": False,
            "file_type": None,
            "split_count": 1,
            "split_no": 1,
            "split_missing": [],
            "family": provider,
            "translation_specialized": True,
            "warnings": warnings,
            "builtin": key in self.base_registry(),
            "local_override": key in self.local_registry(),
            "provider": kind or "api",
            "provider_label": provider,
            "base_url": base_url,
            "model": model,
            "key_env": eff["key_env"],
            "key_configured": key_configured,
            "key_source": source,
        }

    def models_payload(self) -> dict[str, Any]:
        with self._lock:
            registry = self.merged_registry()
            views: list[dict] = []
            for key, entry in registry.items():
                entry = entry or {}
                if entry.get("api"):
                    views.append(self._api_view(key, entry))
                else:
                    views.append(self._local_view(key, entry))
            available = [v["key"] for v in views if v.get("available")]
            candidates: list[str] = []
            last = self.last_model()
            if last:
                candidates.append(last)
            cfg_default = self.config_default()
            if cfg_default:
                candidates.append(cfg_default)
            candidates.extend(available)
            default_key = ""
            for key in candidates:
                if key in available:
                    default_key = key
                    break
            for v in views:
                v["is_default"] = v["key"] == default_key
                v["is_last"] = v["key"] == last
            warnings = self.config_warnings(views, available)
            return {
                "models": views,
                "default_key": default_key,
                "available_keys": available,
                "config_default": cfg_default,
                "last_model": last,
                "config_warnings": warnings,
            }

    def config_warnings(self, views: list[dict], available: list[str]) -> list[str]:
        warnings: list[str] = []
        if not available:
            warnings.append("当前没有可用的翻译模型：请在设置页配置云端 API Key，或选择本地 .gguf 模型")
        for v in views:
            if v.get("available"):
                continue
            if v.get("kind") == "local":
                if v.get("reason"):
                    warnings.append(f"本地模型 {v.get('display_name') or v.get('key')}：{v['reason']}")
            elif v.get("kind") == "api":
                if v.get("reason"):
                    warnings.append(f"云端 API {v.get('display_name') or v.get('key')}：{v['reason']}")
        local_available = any(v.get("kind") == "local" and v.get("available") for v in views)
        if local_available:
            server = (self._cfg.get("translation") or {}).get("server") or {}
            bin_name = str(server.get("llama_server_bin") or "llama-server")
            if not shutil.which(bin_name):
                warnings.append(
                    f"找不到 {bin_name}：本地模型需要 llama.cpp（macOS: brew install llama.cpp；"
                    "Windows: 安装 llama-server.exe 并加入 PATH），或改用云端 API")
        missing_deps = []
        for mod, label in (("torch", "torch"), ("transformers", "transformers"),
                           ("faster_whisper", "faster-whisper"), ("numpy", "numpy"), ("yaml", "PyYAML")):
            if importlib.util.find_spec(mod) is None:
                missing_deps.append(label)
        if missing_deps:
            warnings.append("缺少运行依赖：" + "、".join(missing_deps) + "；请重新安装运行环境")
        for v in views:
            for w in v.get("warnings") or []:
                if w not in warnings:
                    warnings.append(w)
        return warnings

    # ------------------------------------------------------------ 写配置
    def _ensure_local_registry(self) -> dict:
        trans = self._local.setdefault("translation", {})
        if not isinstance(trans, dict):
            trans = {}
            self._local["translation"] = trans
        reg = trans.setdefault("registry", {})
        if not isinstance(reg, dict):
            reg = {}
            trans["registry"] = reg
        return reg

    def _write_local_and_reload(self) -> None:
        _write_yaml(self.local_path, self._local)
        self.refresh(force=True)

    def _display_name(self, raw: str) -> str:
        name = re.sub(r"[\r\n\t]+", " ", str(raw or "")).strip()
        return name[:120]

    def add_local_model(self, path: str | Path, *, key: str = "", display_name: str = "",
                        make_default: bool = False) -> dict:
        info = self.inspect(path, force=True)
        if not info.get("ok"):
            raise ValueError(info.get("error") or "这个文件不是有效的 GGUF")
        load_path = Path(str(info.get("load_path") or info.get("path") or path)).expanduser()
        with self._lock:
            registry = self.merged_registry()
            base_reg = self.base_registry()
            target_key = (key or "").strip()
            if not target_key:
                same = [k for k, e in registry.items()
                        if not (e or {}).get("api")
                        and self.entry_path(e or {}) is not None
                        and str(self.entry_path(e or {})) == str(load_path)]
                if same:
                    target_key = same[0]
            if not target_key:
                seed = str(info.get("auto_display_name") or info.get("name") or load_path.stem)
                target_key = "local-" + _slug(seed)
            existing = registry.get(target_key) or {}
            if existing.get("api"):
                raise ValueError(f"注册名 {target_key!r} 已被云端 API 使用，请换一个名字")
            if target_key in base_reg and str((base_reg[target_key] or {}).get("api") or ""):
                raise ValueError(f"注册名 {target_key!r} 是 config.yaml 里的 API 条目")
            display = self._display_name(display_name) or str(existing.get("by_tag") or "").strip()
            display = display or str(info.get("auto_display_name") or info.get("name") or target_key)
            params = str(info.get("params") or "")
            quant = str(info.get("quant") or "")
            size_gb = float(info.get("size_gb") or 0)
            note = "本地模型｜" + "｜".join(x for x in (params, quant, f"{size_gb:.2f}GB") if x)
            entry = dict(existing)
            entry.update({
                "path": str(load_path),
                "by_tag": display,
                "note": note,
                "vram_gb": round(size_gb, 2),
            })
            entry.pop("local_dir", None)
            entry.pop("gguf", None)
            entry.pop("hf_id", None)
            self._ensure_local_registry()[target_key] = entry
            if make_default:
                self._local.setdefault("translation", {})["default"] = target_key
            self._write_local_and_reload()
            if make_default:
                try:
                    CLI.remember_model(target_key)
                except Exception:  # noqa: BLE001
                    pass
            return next((v for v in self.models_payload()["models"] if v["key"] == target_key),
                        {"key": target_key, "display_name": display, "available": True})

    def remove_local_model(self, key: str) -> dict:
        key = str(key or "").strip()
        if not key:
            raise ValueError("缺少模型 key")
        with self._lock:
            base_entry = self.base_registry().get(key) or {}
            if not base_entry.get("api") and str(base_entry.get("path") or "").strip():
                raise ValueError("这个模型路径写在 config.yaml 里，界面不能删除；请直接修改 config.yaml")
            local_reg = self.local_registry()
            if key not in local_reg:
                raise ValueError("这个模型不在 config.local.yaml 里，没有可删除的本地覆盖")
            self._ensure_local_registry().pop(key, None)
            trans = self._local.setdefault("translation", {})
            if str(trans.get("default") or "") == key:
                trans.pop("default", None)
            self._write_local_and_reload()
            return {"ok": True, "key": key}

    def set_default(self, key: str) -> dict:
        key = str(key or "").strip()
        if key not in self.merged_registry():
            raise ValueError(f"未知模型：{key}")
        with self._lock:
            self._local.setdefault("translation", {})["default"] = key
            self._write_local_and_reload()
            try:
                CLI.remember_model(key)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "key": key}

    # ------------------------------------------------------------ API 设置
    def save_api(self, *, key: str = "", provider: str = "deepseek", api_key: str = "",
                 base_url: str = "", model: str = "", display_name: str = "",
                 api_key_env: str = "", clear_key: bool = False,
                 make_default: bool = False) -> dict:
        provider = (provider or "deepseek").lower()
        if provider not in API_PRESETS:
            provider = "custom"
        base_url = str(base_url or "").strip().rstrip("/")
        model = str(model or "").strip()
        if not base_url:
            base_url = API_PRESETS[provider].get("base_url", "")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Base URL 需要以 http:// 或 https:// 开头")
        if not model:
            raise ValueError("模型名不能为空（如 deepseek-flash）")
        with self._lock:
            registry = self.merged_registry()
            target_key = str(key or "").strip()
            if not target_key:
                if provider == "deepseek":
                    target_key = "deepseek-api"
                else:
                    seed = display_name or model
                    target_key = "api-" + _slug(seed, fallback="custom")
            existing = registry.get(target_key) or {}
            if existing and not existing.get("api"):
                raise ValueError(f"注册名 {target_key!r} 已被本地模型使用，请换一个名字")
            env_name = (api_key_env or str(existing.get("api_key_env") or "").strip()
                        or API_PRESETS[provider].get("key_env") or f"{provider.upper()}_API_KEY")
            env_name = re.sub(r"[^A-Za-z0-9_]", "_", env_name).upper() or "CUSTOM_API_KEY"
            display = self._display_name(display_name) or str(existing.get("by_tag") or "").strip()
            if not display:
                display = ("DeepSeek" if provider == "deepseek" else "自定义 API") + f" · {model}"
            entry = dict(existing)
            entry.update({
                "api": provider,
                "base_url": base_url,
                "model": model,
                "api_key_env": env_name,
                "by_tag": display,
                "note": f"云端 API｜{model}｜{base_url}",
            })
            entry.pop("api_key", None)
            self._ensure_local_registry()[target_key] = entry
            if api_key.strip():
                env_set(self.env_path, env_name, api_key.strip())
                os.environ[env_name] = api_key.strip()
            elif clear_key:
                env_remove(self.env_path, env_name)
                os.environ.pop(env_name, None)
            if make_default:
                self._local.setdefault("translation", {})["default"] = target_key
            self._write_local_and_reload()
            if make_default:
                try:
                    CLI.remember_model(target_key)
                except Exception:  # noqa: BLE001
                    pass
            view = next((v for v in self.models_payload()["models"] if v["key"] == target_key), {})
            base_key = self.base_registry().get(target_key) or {}
            warnings = []
            if str(base_key.get("api_key") or "").strip():
                warnings.append("config.yaml 里的 api_key 非空，会优先于 .env；建议清空它")
            return {"ok": True, "key": target_key, "model": view, "warnings": warnings}

    def clear_api_key(self, key: str) -> dict:
        key = str(key or "").strip()
        entry = self.merged_registry().get(key) or {}
        if not entry.get("api"):
            raise ValueError("这个条目不是云端 API")
        env_name = self._entry_key_env(entry)
        with self._lock:
            env_remove(self.env_path, env_name)
            os.environ.pop(env_name, None)
            local_entry = self._ensure_local_registry().get(key)
            if isinstance(local_entry, dict):
                local_entry.pop("api_key", None)
            self._write_local_and_reload()
            view = next((v for v in self.models_payload()["models"] if v["key"] == key), {})
            return {"ok": True, "key": key, "model": view}

    def test_api(self, *, key: str = "", api_key: str = "", base_url: str = "",
                 model: str = "", provider: str = "") -> dict:
        """测试云端 API：/models 预检 + 一条极短翻译。不返回 Key。"""
        entry = self.merged_registry().get(key) or {}
        if not entry.get("api"):
            # 没有指定条目时找第一个 API
            for k, e in self.merged_registry().items():
                if (e or {}).get("api"):
                    key, entry = k, e
                    break
        eff = self.effective_api(entry) if entry else {"base_url": "", "model": "", "key_env": "", "env_base_override": "", "env_model_override": ""}
        base_url = str(base_url or eff.get("base_url") or "").strip().rstrip("/")
        model = str(model or eff.get("model") or "").strip()
        if not base_url.startswith(("http://", "https://")):
            return {"ok": False, "error": "Base URL 需要以 http:// 或 https:// 开头", "base_url": base_url, "model": model}
        if not model:
            return {"ok": False, "error": "请填写模型名", "base_url": base_url, "model": model}
        env_name = self._entry_key_env(entry) if entry else "DEEPSEEK_API_KEY"
        api_key = str(api_key or "").strip() or str(entry.get("api_key") or "").strip() or env_get(self.env_path, env_name) or os.environ.get(env_name, "")
        if not api_key:
            return {"ok": False, "error": f"还没有 API Key（.env 的 {env_name}）", "base_url": base_url, "model": model, "key_env": env_name}
        names = T._api_model_names(base_url, api_key)
        warnings: list[str] = []
        model_available = True
        if names:
            model_available = model in names
            if not model_available:
                warnings.append(f"模型 {model!r} 不在端点列表里；可用：{'、'.join(names[:12])}")
        else:
            warnings.append("端点没有返回 /models 列表（可能不支持）；下面直接发一条测试翻译")
        sampling = (self._cfg.get("translation") or {}).get("sampling") or {}
        timeout = 60.0
        client = T.ApiClient(
            base_url, model, api_key, sampling, timeout,
            extra_body=entry.get("extra_body") or {} if entry else {},
            thinking_param=entry.get("thinking_param") if entry else None,
            reasoning_effort=entry.get("reasoning_effort") if entry else None,
        )
        messages = [
            {"role": "system", "content": "你是日译中字幕翻译器，只输出译文。"},
            {"role": "user", "content": "[Input]\n[1. おはよう。]\n[2. 今日はいい天気だね。]\n\n输出格式：[1. 译文]"},
        ]
        try:
            text = client.chat(messages, max_tokens=64, enable_thinking=False)
        except T.FatalApiError as exc:
            return {"ok": False, "error": f"API 拒绝了请求：{exc}", "base_url": base_url, "model": model,
                    "models": names or [], "key_env": env_name, "warnings": warnings}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"HTTP {exc.code}：{exc.reason}", "base_url": base_url,
                    "model": model, "models": names or [], "key_env": env_name, "warnings": warnings}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "base_url": base_url,
                    "model": model, "models": names or [], "key_env": env_name, "warnings": warnings}
        if not str(text or "").strip():
            warnings.append("正文为空：若是思考模型，请把 thinking_param 的 off 设为 {type: disabled}（项目默认已关）")
        return {
            "ok": True,
            "base_url": base_url,
            "model": model,
            "models": names or [],
            "model_available": model_available,
            "sample": str(text or "")[:500],
            "finish_reason": getattr(client, "last_finish_reason", None),
            "usage": getattr(client, "last_usage", {}) or {},
            "key_env": env_name,
            "warnings": warnings,
        }

    # ------------------------------------------------------------ 任务辅助
    def remember(self, key: str) -> None:
        if not key:
            return
        try:
            CLI.remember_model(key)
        except Exception:  # noqa: BLE001
            pass

    def by_tag(self, key: str) -> str:
        try:
            return T.model_by_tag(self._cfg, key)
        except Exception:  # noqa: BLE001
            entry = self.merged_registry().get(key) or {}
            return str(entry.get("by_tag") or key)

    def settings_payload(self, paths: gui_fs.GuiPaths) -> dict:
        payload = self.models_payload()
        version = ""
        try:
            vp = APP_DIR / "VERSION"
            if vp.is_file():
                version = vp.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        server = (self._cfg.get("translation") or {}).get("server") or {}
        bin_name = str(server.get("llama_server_bin") or "llama-server")
        api_kinds = {"deepseek", "custom"}
        for entry in self.merged_registry().values():
            api_kind = str((entry or {}).get("api") or "").lower()
            if api_kind:
                api_kinds.add(api_kind)
        env_overrides: dict[str, str] = {}
        for api_kind in sorted(api_kinds):
            for field in ("BASE_URL", "MODEL"):
                env_name = f"{api_kind.upper()}_{field}"
                if os.environ.get(env_name):
                    env_overrides[env_name] = os.environ[env_name]
        payload.update({
            "paths": paths.as_dict(),
            "config_path": str(self.config_path),
            "local_config_path": str(self.local_path),
            "env_path": str(self.env_path),
            "version": version or "dev",
            "llama_server": shutil.which(bin_name) or "",
            "llama_server_bin": bin_name,
            "env_overrides": env_overrides,
        })
        return payload
