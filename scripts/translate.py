#!/usr/bin/env python3
"""日中字幕翻译：Sakura-GalTransl（视觉小说/Galgame 翻译特化模型）+ 本地 llama.cpp / LM Studio。

用法：
    python scripts/translate.py 中间文件/日文字幕/xxx.ja.txt          # 翻译单个文件
    python scripts/translate.py 中间文件/日文字幕/                    # 翻译目录下所有 .ja.txt
    python scripts/translate.py 中间文件/日文字幕/ --bilingual        # LRC 中日对照
    python scripts/translate.py 中间文件/日文字幕/ --model sakura-galtransl-7b
    python scripts/translate.py 中间文件/日文字幕/ --limit 20         # 只翻前 20 行（试跑）
    python scripts/translate.py 中间文件/日文字幕/ --force            # 忽略缓存重翻

设计要点：
- 请求格式遵循 Sakura 官方模板：system + [History] / [Glossary] / [Input]
- 采样：temperature 0.3 / top_p 0.8（官方推荐）
- 逐行对应：要求输出行数与输入行数一致；不一致时自动拆分重试，最后回退逐行翻译
- 缓存：<名>.zh.cache.json，日文→中文，重复台词不重复请求
- 服务：优先复用已运行的 llama-server :8080 / LM Studio :1234；都没有则自动拉起 llama-server
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logger = logging.getLogger("translate")

LINE_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s+(.+)$")


# ============================================================ 数据结构
@dataclass
class SubLine:
    index: int
    start_ms: int
    end_ms: int | None
    text: str


# ============================================================ 配置
def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 覆盖 base（用于 config.local.yaml 的个性化覆盖）。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path) -> dict:
    """读 config.yaml；同目录若有 config.local.yaml（不入库），递归覆盖在上面。"""
    import yaml

    path = Path(path)
    if not path.exists():
        raise SystemExit(f"找不到配置文件：{path}（仓库自带 config.yaml）")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    local = path.with_name("config.local.yaml")
    if local.exists():
        try:
            with open(local, "r", encoding="utf-8") as f:
                cfg = _deep_merge(cfg, yaml.safe_load(f) or {})
            logger.info("已叠加本地配置：%s", local.name)
        except yaml.YAMLError as exc:
            logger.warning("config.local.yaml 解析失败（%s），已忽略", exc)
    return cfg


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """把项目根目录的 .env 读进 os.environ（不覆盖已存在的环境变量）。

    格式：`KEY=VALUE`，支持 `#` 注释、值两侧引号、`export ` 前缀。
    """
    import os

    env_path = path or (PROJECT_ROOT / ".env")
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if not k:
            continue
        out[k] = v
        os.environ.setdefault(k, v)
    return out


class FatalApiError(RuntimeError):
    """API 明确拒绝（模型名/鉴权/参数错误）：重试无意义，应立刻中止本轮而不是逐批硬撞。"""


def _api_model_names(base_url: str, api_key: str, timeout: float = 8.0) -> list[str] | None:
    """GET {base_url}/models，拿端点支持的模型名；端点不可用时返回 None（跳过预检）。"""
    import urllib.request

    try:
        req = urllib.request.Request(f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]
    except Exception:  # noqa: BLE001
        return None


def _model_name_hint(body: str) -> str:
    """从错误体里抠出"支持的模型名"，直接给用户可复制的答案。"""
    m = re.search("supported API model names are ([^\"}\n]+)", body)
    if not m:
        return ""
    names = re.split(r",?\s*but\b|[.。]", m.group(1).strip())[0].strip()
    return ("\n该端点支持的模型名：" + names
            + "\n→ 改 config 的 translation.registry.<条目>.model，或在 .env 里设 DEEPSEEK_MODEL=<名字>")


def model_by_tag(cfg: dict, key: str) -> str:
    """给 LRC [by:] 用的"翻译来源"标签：跟随实际选择的模型/通道。"""
    tcfg = cfg.get("translation") or {}
    entry = (tcfg.get("registry") or {}).get(key) or {}
    custom = str(entry.get("by_tag") or (tcfg.get("output") or {}).get("by_tag") or "")
    if custom:
        return custom
    if entry.get("api"):
        kind = str(entry.get("api") or "API")
        return f"{kind.capitalize()} API · {entry.get('model') or key}"
    name = Path(str(entry.get("path") or key)).stem or key
    return f"Sakura · {name}" if "sakura" in name.lower() else name


def api_entry(cfg: dict, name: str | None) -> tuple[str, dict] | None:
    """如果选中的是 API 模型（registry 里带 `api:` 字段）→ 返回 (注册名, 条目)，否则 None。"""
    tcfg = cfg.get("translation") or {}
    registry = tcfg.get("registry") or {}
    key = name or tcfg.get("default") or ""
    entry = registry.get(key) or {}
    return (key, entry) if entry.get("api") else None


def resolve_api_client(cfg: dict, key: str, entry: dict):
    """按 registry 里的 api 条目建一个远程客户端（与 ChatClient 同接口）。"""
    load_dotenv()
    import os

    kind = str(entry.get("api") or "").lower()
    preset = {
        "deepseek": {"base_url": "https://api.deepseek.com/v1", "key_env": "DEEPSEEK_API_KEY"},
    }.get(kind, {})
    # 环境变量优先于 config，便于临时切代理/换模型而不用改文件
    base_url = str(os.environ.get(f"{kind.upper()}_BASE_URL") or entry.get("base_url") or preset.get("base_url") or "")
    model = str(os.environ.get(f"{kind.upper()}_MODEL") or entry.get("model") or "")
    key_env = str(entry.get("api_key_env") or preset.get("key_env") or f"{kind.upper()}_API_KEY")
    # key 有两个来源：config 里的 api_key（最直接）> 环境变量 / .env
    api_key = str(entry.get("api_key") or "").strip() or os.environ.get(key_env, "").strip()
    if not base_url or not model:
        raise SystemExit(f"API 模型 {key!r} 配置不完整（需要 base_url 与 model，见 config translation.registry.{key}）")
    if not api_key:
        raise SystemExit(
            f"没有找到 API key：请在 config.yaml 的 translation.registry.{key}.api_key 里填 sk-...，"
            f"或在项目根目录的 .env 里填 {key_env}=sk-..."
        )
    names = _api_model_names(base_url, api_key)
    if names and model not in names:
        raise SystemExit(
            f"API 不支持模型 {model!r}；{base_url}/models 返回可用模型：{'、'.join(names)}\n"
            f"→ 改 config 的 translation.registry.{key}.model，或在 .env 里设 {kind.upper()}_MODEL=<名字>"
        )
    if names:
        logger.info("API 预检通过：%s 可用（该端点共 %d 个模型）", model, len(names))
    sampling = (cfg.get("translation") or {}).get("sampling") or {}
    timeout = float(((cfg.get("translation") or {}).get("translate") or {}).get("request_timeout_s", 600))
    logger.info("使用 API 模型：%s（%s，%s）", key, model, base_url)
    return ApiClient(base_url, model, api_key, sampling, timeout,
                     extra_body=entry.get("extra_body") or {}, thinking_param=entry.get("thinking_param"),
                     reasoning_effort=entry.get("reasoning_effort"))


class ApiClient:
    """OpenAI 兼容的远程 API 客户端（与 ChatClient 同接口，供批次/补漏/复检复用）。

    与本地 llama-server 的区别：不占本机内存/显存；受网络与速率限制影响，
    429/5xx 会退避重试。
    """

    def __init__(self, base_url: str, model: str, api_key: str, sampling: dict, timeout: float = 600.0,
                 extra_body: dict | None = None, thinking_param: dict | None = None,
                 reasoning_effort: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.sampling = sampling or {}
        self.timeout = timeout
        self.extra_body = dict(extra_body or {})
        self.thinking_param = thinking_param or {}
        self.reasoning_effort = reasoning_effort
        self.model_id = model
        self.last_finish_reason: str | None = None
        self.last_usage: dict = {}

    def chat(self, messages: list[dict], max_tokens: int | None = None, enable_thinking: bool = False) -> str:
        import urllib.error
        import urllib.request

        cap = int(self.sampling.get("max_tokens", 2048))
        if max_tokens:
            cap = min(cap, max(64, int(max_tokens)))
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": float(self.sampling.get("temperature", 0.3)),
            "top_p": float(self.sampling.get("top_p", 0.8)),
            "max_tokens": cap,
            "stream": False,
        }
        # 有些 API（如带思考的模型）需要显式关闭思考，参数名各异 → 由 config 的 thinking_param 指定
        if self.thinking_param:
            name = str(self.thinking_param.get("name") or "thinking")
            # YAML 1.1 会把 on/off 解析成布尔 True/False → 两种键都认
            on = self.thinking_param.get("on", self.thinking_param.get(True))
            off = self.thinking_param.get("off", self.thinking_param.get(False))
            # off 缺失时不要发 thinking: null（有的端点会 400），退化为 {"type": "disabled"}
            if not enable_thinking:
                payload[name] = off if off is not None else {"type": "disabled"}
            elif on is not None:
                payload[name] = on
        if self.reasoning_effort and payload.get("thinking") not in ({}, None) and \
                (isinstance(payload.get("thinking"), dict) and payload["thinking"].get("type") != "disabled"):
            payload["reasoning_effort"] = self.reasoning_effort
        payload.update(self.extra_body)

        last: Exception | None = None
        expanded = False          # 是否已因"只回思考"放大过配额（与重试次数无关）
        for attempt in range(4):
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                choice = (data.get("choices") or [{}])[0] or {}
                msg = choice.get("message") or {}
                content = str(msg.get("content") or "")
                reasoning = str(msg.get("reasoning_content") or "")
                finish = choice.get("finish_reason")
                usage = data.get("usage") or {}
                self.last_finish_reason, self.last_usage = finish, usage

                if not content.strip():
                    detail = (f"finish_reason={finish} ｜ completion_tokens={usage.get('completion_tokens')}"
                              f"（其中思考 {usage.get('completion_tokens_details', {}).get('reasoning_tokens')}）")
                    if finish == "content_filter":
                        raise FatalApiError("API 内容审核拦截（finish_reason=content_filter）：该端点不接受这段内容，"
                                            "换模型或换措辞后重试")
                    if reasoning.strip():
                        # 已知故障模式：思考模式把配额吃光，只回思考不回正文 → 加大配额再试一次
                        if not expanded:
                            expanded = True
                            bigger = max(int(payload.get("max_tokens") or cap) * 4, 1024)
                            logger.warning("模型只返回了思考内容、正文为空（%s）→ 把 max_tokens 从 %d 提到 %d 重试一次",
                                           detail, cap, bigger)
                            payload["max_tokens"] = bigger
                            continue
                        raise FatalApiError(
                            f"连续两次只返回思考内容、正文为空（{detail}）。该端点可能忽略了 thinking 参数；"
                            "请在 config 的 translation.registry.deepseek-api 里确认 "
                            'thinking_param: {name: thinking, "off": {type: disabled}}，或改用其它模型')
                    logger.warning("模型返回空正文（%s）", detail)
                    return ""
                return content
            except FatalApiError:
                raise                      # 内容审核/配置类错误：立刻上报，不做网络重试
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:300]
                except Exception:  # noqa: BLE001
                    pass
                last = RuntimeError(f"HTTP {exc.code}: {body}")
                if exc.code in (429, 500, 502, 503, 504):
                    wait = 5 * (attempt + 1)
                    logger.warning("API 返回 %s，%ds 后重试（第 %d/4 次）：%s", exc.code, wait, attempt + 1, body[:120])
                    time.sleep(wait)
                    continue
                hint = _model_name_hint(body)
                if exc.code in (401, 403):
                    hint = hint or ("\n→ 检查 API key 是否正确、是否还有额度"                        "（config 的 api_key 或 .env 的 DEEPSEEK_API_KEY）")
                raise FatalApiError(f"HTTP {exc.code}: {body[:300]}{hint}")
            except Exception as exc:  # noqa: BLE001  网络抖动/超时
                last = exc
                wait = 3 * (attempt + 1)
                logger.warning("API 请求失败（%s），%ds 后重试（第 %d/4 次）", type(exc).__name__, wait, attempt + 1)
                time.sleep(wait)
        raise RuntimeError(f"API 请求失败（已重试 4 次）：{last}")


def resolve_translation_model(cfg: dict, name: str | None) -> tuple[str, Path]:
    """返回 (注册名, GGUF 路径)。

    三种注册方式：
      path: /abs/or/relative.gguf   → 直接用本地已有模型（不下载）
      local_dir + gguf             → 本地缺失时自动下载
    """
    tcfg = cfg.get("translation") or {}
    registry = tcfg.get("registry") or {}
    key = name or tcfg.get("default") or "sakura-14b-qwen3-v1.5"

    # 允许直接给 GGUF 路径
    if name and ("/" in name or name.endswith(".gguf")):
        candidate = Path(name).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        if candidate.exists():
            return candidate.stem, candidate

    if key not in registry:
        raise SystemExit(f"未知翻译模型 {key!r}，可选：{', '.join(registry)}")
    if not (registry[key] or {}).get("path") and not name:
        # 只有显式选择（--mt-model / config default）才会走到下载；这里明确提醒
        logger.warning("本地没有 %r，将从网上下载（可能好几个 GB）；想取消请 Ctrl-C", key)

    entry = registry[key]

    # 路径留空 = 还没配置。必须先说清楚，否则会走到"自动下载"分支（可能误下好几个 GB）
    wants_download = bool(entry.get("local_dir") or entry.get("gguf") or entry.get("hf_id"))
    if entry.get("path") is not None and not str(entry.get("path")).strip() and not wants_download:
        raise SystemExit(
            f"翻译模型 {key!r} 还没配置：请打开 config.yaml，在 "
            f"translation.registry.{key}.path 里填你的 GGUF 路径\n"
            "（也可以改成 local_dir + gguf 让它自动下载；或者用菜单第 3 项走云端 API）"
        )

    # 1) 直接指定本地模型文件
    if entry.get("path"):
        path = Path(entry["path"]).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.exists():
            raise SystemExit(f"本地模型不存在：{path}（请检查 config translation.registry.{key}.path）")
        logger.info("使用本地模型：%s（%.2f GB）", path, path.stat().st_size / 1073741824)
        return key, path

    # 2) 本地目录（缺失则下载）
    local_dir = Path(entry.get("local_dir") or f".models/{key}")
    if not local_dir.is_absolute():
        local_dir = PROJECT_ROOT / local_dir
    gguf_name = entry.get("gguf")
    gguf_path = local_dir / gguf_name if gguf_name else None

    if gguf_path is None or not gguf_path.exists():
        existing = sorted(local_dir.glob("*.gguf")) if local_dir.exists() else []
        if existing:
            logger.warning("使用目录中现有的 GGUF：%s", existing[0].name)
            return key, existing[0]
        logger.info("本地未找到模型 %s，开始下载（支持续传）...", local_dir)
        from fetch_model import fetch, resolve_files

        fetch(entry["hf_id"], local_dir, files=resolve_files(entry))

    if gguf_path is None or not gguf_path.exists():
        candidates = sorted(local_dir.glob("*.gguf"))
        if not candidates:
            raise SystemExit(f"下载后仍未找到 GGUF：{local_dir}")
        gguf_path = candidates[0]
    logger.info("使用模型：%s（%.2f GB）", gguf_path.name, gguf_path.stat().st_size / 1073741824)
    return key, gguf_path


# ============================================================ 推理服务
class ServerManager:
    """探测 / 启动本地 OpenAI 兼容服务（llama.cpp llama-server 或 LM Studio）。"""

    def __init__(self, cfg: dict, gguf_path: Path):
        s = (cfg.get("translation") or {}).get("server") or {}
        self.backend = str(s.get("backend", "auto"))
        self.llamacpp_url = str(s.get("llamacpp_url", "http://127.0.0.1:8080")).rstrip("/")
        self.lmstudio_url = str(s.get("lmstudio_url", "http://127.0.0.1:1234")).rstrip("/")
        self.auto_start = bool(s.get("auto_start", True))
        self.bin = str(s.get("llama_server_bin", "llama-server"))
        self.ctx_size = int(s.get("ctx_size", 8192))
        self.n_gpu_layers = int(s.get("n_gpu_layers", 99))
        self.extra_args = [str(x) for x in (s.get("extra_args") or [])]
        self.gguf_path = gguf_path
        self.proc: subprocess.Popen | None = None
        self.base_url: str | None = None
        self.model_id: str | None = None
        self.log_path = PROJECT_ROOT / ".models" / "llama-server.log"

    # ---------------- 探测 ----------------
    @staticmethod
    def _list_models(base_url: str, timeout: float = 2.0) -> list[str] | None:
        try:
            req = urllib.request.Request(f"{base_url}/v1/models")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [m.get("id", "") for m in data.get("data", [])]
        except Exception:
            return None

    def _candidates(self) -> list[str]:
        if self.backend == "llamacpp":
            return [self.llamacpp_url]
        if self.backend == "lmstudio":
            return [self.lmstudio_url]
        return [self.llamacpp_url, self.lmstudio_url]

    def ensure(self) -> tuple[str, str]:
        """返回 (base_url, model_id)。"""
        for url in self._candidates():
            models = self._list_models(url)
            if models:
                model_id = models[0]
                # 复用的服务里装的可能不是本次选的模型 → 明确提醒（否则会以为"选了没用"）
                if self.gguf_path and Path(str(model_id)).name != self.gguf_path.name:
                    logger.warning("复用了已在运行的服务：%s（它加载的是 %s，不是本次选择的 %s）",
                                   url, model_id, self.gguf_path.name)
                    logger.warning("要换成本次选择的模型，请先停掉那个服务（例如 pkill -f llama-server）再重跑")
                else:
                    logger.info("复用已运行的服务：%s（模型 %s）", url, model_id)
                self.base_url, self.model_id = url, model_id
                return url, model_id

        if not self.auto_start:
            raise SystemExit(
                "没有检测到可用的推理服务。请先启动 llama-server 或 LM Studio 服务，"
                "或把 translation.server.auto_start 设为 true。"
            )

        if self.backend == "lmstudio":
            raise SystemExit("LM Studio 服务未启动：请在 LM Studio 里 Start Server，或改用 backend: llamacpp")
        return self._start_llamacpp()

    def server_cmd(self, host: str, port: str) -> list[str]:
        """拼 llama-server 命令行（独立出来便于测试/排查，不启动进程）。"""
        exe = shutil.which(self.bin) or self.bin
        return [
            exe,
            "-m", str(self.gguf_path),
            "--host", host,
            "--port", str(port),
            "-c", str(self.ctx_size),
            "-ngl", str(self.n_gpu_layers),
            "--no-warmup",
            *self.extra_args,
        ]

    def _start_llamacpp(self) -> tuple[str, str]:
        exe = shutil.which(self.bin)
        if not exe:
            raise SystemExit(
                f"找不到 {self.bin}。请安装 llama.cpp（brew install llama.cpp）"
                "，或在 config 里设置 translation.server.llama_server_bin"
            )
        port = self.llamacpp_url.rsplit(":", 1)[-1]
        host = self.llamacpp_url.split("//", 1)[-1].rsplit(":", 1)[0]
        base_cmd = self.server_cmd(host, port)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        # 先试 --jinja（完整 Jinja 模板，支持 chat_template_kwargs 关闭 thinking），失败则退回默认模板
        for extra in (["--jinja"], []):
            cmd = base_cmd + extra
            logger.info("启动 llama-server：%s", " ".join(cmd))
            log_file = open(self.log_path, "a", encoding="utf-8")
            log_file.write(f"\n\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{cmd}\n")
            self.proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)

            deadline = time.time() + 300
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    if extra:
                        logger.warning("%s 启动失败（退出码 %s），改为默认模板重试", "--jinja", self.proc.returncode)
                        break
                    raise SystemExit(
                        f"llama-server 启动失败（退出码 {self.proc.returncode}），日志见 {self.log_path}"
                    )
                models = self._list_models(self.llamacpp_url, timeout=2.0)
                if models:
                    logger.info("llama-server 就绪：%s（模型 %s）", self.llamacpp_url, models[0])
                    self.base_url, self.model_id = self.llamacpp_url, models[0]
                    return self.llamacpp_url, models[0]
                time.sleep(2)
            if not extra:
                raise SystemExit(f"等待 llama-server 就绪超时（300s），日志见 {self.log_path}")
        raise SystemExit(f"llama-server 无法启动，日志见 {self.log_path}")

    def shutdown(self):
        if self.proc and self.proc.poll() is None:
            logger.info("关闭 llama-server（pid=%s）", self.proc.pid)
            if os.name == "nt":
                # Windows 上 send_signal(SIGINT) 对子进程不可用；terminate 更可靠
                self.proc.terminate()
            else:
                self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class ChatClient:
    def __init__(self, base_url: str, model_id: str, sampling: dict, timeout: float = 600.0):
        self.base_url = base_url
        self.model_id = model_id
        self.sampling = sampling
        self.timeout = timeout
        self._support_template_kwargs = True

    def chat(self, messages: list[dict], max_tokens: int | None = None, enable_thinking: bool = False) -> str:
        cap = int(self.sampling.get("max_tokens", 2048))
        if max_tokens:
            # 按批量封顶：单次失控生成最多只烧掉这么多，避免 2048 token 级别的空转
            cap = min(cap, max(64, int(max_tokens)))
        payload = {
            "model": self.model_id,
            "messages": messages,
            "temperature": float(self.sampling.get("temperature", 0.3)),
            "top_p": float(self.sampling.get("top_p", 0.8)),
            "max_tokens": cap,
            "stream": False,
        }
        # Qwen3 基座默认可能开启 thinking（会让翻译变慢且输出带思考过程），显式关闭。
        # 注意：标题翻译这类短任务开 thinking 反而更稳（实测），按调用方要求切换。
        # 服务端不支持该参数时自动降级重试。
        if self._support_template_kwargs:
            payload["chat_template_kwargs"] = {"enable_thinking": bool(enable_thinking)}

        try:
            return self._post(payload)
        except urllib.error.HTTPError as exc:
            if self._support_template_kwargs and exc.code in (400, 422, 500):
                logger.warning("服务端不接受 chat_template_kwargs（HTTP %s），去掉该参数重试", exc.code)
                self._support_template_kwargs = False
                payload.pop("chat_template_kwargs", None)
                return self._post(payload)
            raise

    def _post(self, payload: dict) -> str:
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        message = data["choices"][0]["message"]
        # 只取 content；reasoning_content（思考过程）一律丢弃
        return message.get("content") or ""


# ============================================================ 字幕读写
SUBTITLE_SUFFIX_RE = re.compile(r"\.(?:ja(?:\.refined)?\.txt|srt|lrc|txt)$", re.I)


def norm_work(name: str) -> str:
    """作品文件夹名归一化：去首尾空白 + NFKC（兼容历史遗留的尾随空格目录名）。"""
    import unicodedata

    return unicodedata.normalize("NFKC", name or "").strip()


def subtitle_stem(path: Path) -> str:
    """去掉 .ja.txt / .ja.refined.txt 后缀，得到统一的作品内轨道名。"""
    return SUBTITLE_SUFFIX_RE.sub("", path.name)


def parse_ja_txt(path: Path) -> list[SubLine]:
    """读取日文字幕。

    支持两种来源（精修版优先由调用方决定）：
      <轨道>.ja.txt          + <轨道>.timing.json        ← ASR 原文
      <轨道>.ja.refined.txt  + <轨道>.ja.refined.json    ← 台本精修（毫秒时间戳）
    """
    name = path.name
    if name.endswith(".ja.refined.txt"):
        side = path.with_name(name[: -len(".ja.refined.txt")] + ".ja.refined.json")
    else:
        side = path.with_name(name[: -len(".ja.txt")] + ".timing.json")
    if side.exists():
        try:
            data = json.loads(side.read_text(encoding="utf-8"))
            rows = data.get("lines", data) if isinstance(data, dict) else data
            out: list[SubLine] = []
            for i, x in enumerate(rows):
                if not isinstance(x, dict) or "text" not in x:
                    continue
                out.append(SubLine(index=len(out) + 1, start_ms=int(x.get("start_ms") or 0),
                                   end_ms=int(x.get("end_ms") or 0), text=str(x["text"]).strip()))
            if out:
                return out
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            # 旁挂时间戳文件坏了不该让整轨翻译失败：退回解析 .ja.txt 里的 [HH:MM:SS]
            logger.warning("时间戳文件损坏：%s（%s）→ 改用 .ja.txt 里的时间戳", side.name, exc)

    lines: list[SubLine] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = LINE_RE.match(raw.strip())
        if not m:
            continue
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        lines.append(SubLine(index=len(lines) + 1, start_ms=(h * 3600 + mi * 60 + s) * 1000, end_ms=None, text=m.group(4).strip()))
    return lines


SRT_TIME_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
ANY_TS_RE = re.compile(r"^\[?(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,](\d{1,3}))?\]?\s*(.*)$")


def _ms(h: str, m: str, s: str, frac: str = "0") -> int:
    ms = int(frac.ljust(3, "0")[:3])
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + ms


def parse_srt(path: Path) -> list[SubLine]:
    """标准 SRT：序号 / 时间轴 / 文本（多行文本用空格合并）。"""
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    out: list[SubLine] = []
    for block in re.split(r"\n\s*\n", text):
        rows = [r.strip("\ufeff").strip() for r in block.splitlines() if r.strip()]
        if not rows:
            continue
        ts = next((r for r in rows if SRT_TIME_RE.search(r)), None)
        if not ts:
            continue
        m = SRT_TIME_RE.search(ts)
        body = [r for r in rows if r != ts and not r.isdigit()]
        body_text = " ".join(body).strip()
        if not body_text:
            continue
        out.append(SubLine(index=len(out) + 1,
                           start_ms=_ms(m.group(1), m.group(2), m.group(3), m.group(4)),
                           end_ms=_ms(m.group(5), m.group(6), m.group(7), m.group(8)),
                           text=body_text))
    return out


def parse_lrc(path: Path) -> list[SubLine]:
    """LRC：[mm:ss.xx]文本；忽略 [ti:]/[by:]/[ar:] 等元数据。结束时间取下一行开始。"""
    out: list[SubLine] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        m = re.match(r"^\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\](.*)$", line)
        if not m:
            continue
        start = (int(m.group(1)) * 60 + int(m.group(2))) * 1000 + int((m.group(3) or "0").ljust(3, "0")[:3])
        body = m.group(4).strip()
        if body:
            out.append(SubLine(index=len(out) + 1, start_ms=start, end_ms=None, text=body))
    for i in range(len(out) - 1):
        out[i].end_ms = out[i + 1].start_ms
    return out


def parse_plain_txt(path: Path) -> list[SubLine]:
    """带时间戳的纯文本：`[HH:MM:SS] 文本` / `[mm:ss.xx] 文本`；无时间戳则返回空。"""
    out: list[SubLine] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = ANY_TS_RE.match(raw.strip())
        if not m or not m.group(5).strip():
            continue
        h, mi = m.group(1), m.group(2)
        if m.group(3) is not None:
            start = _ms(h, mi, m.group(3), m.group(4) or "0")
        else:                       # [mm:ss]
            start = (int(h) * 60 + int(mi)) * 1000 + int((m.group(4) or "0").ljust(3, "0")[:3])
        out.append(SubLine(index=len(out) + 1, start_ms=start, end_ms=None, text=m.group(5).strip()))
    return out


def parse_subtitle(path: Path) -> list[SubLine]:
    """按后缀选择解析器：本工具 .ja.txt / .srt / .lrc / 带时间戳 .txt。"""
    name = path.name.lower()
    if name.endswith(".ja.txt") or name.endswith(".ja.refined.txt"):
        return parse_ja_txt(path)
    if name.endswith(".srt"):
        return parse_srt(path)
    if name.endswith(".lrc"):
        return parse_lrc(path)
    if name.endswith(".txt"):
        return parse_plain_txt(path)
    return []


def lrc_timestamp(ms: int, offset_ms: int = 0) -> str:
    total = max(0, ms + offset_ms)
    minutes, rem = divmod(total, 60000)
    seconds, millis = divmod(rem, 1000)
    return f"[{minutes:02d}:{seconds:02d}.{millis // 10:02d}]"


def hhmmss(ms: int) -> str:
    total = max(0, ms) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def write_outputs(
    stem: str,
    lines: list[SubLine],
    zh: list[str],
    output_dir: Path,
    oc: dict,
    title_zh: str | None = None,
    lrc_dir: Path | None = None,
) -> dict[str, Path]:
    """写中文产物：文本类（.zh.txt）进 output_dir，LRC 进独立的 lrc_dir。"""
    lrc_dir = lrc_dir or output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    if oc.get("write_lrc", True):
        lrc_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    offset = int(oc.get("lrc_offset_ms", 0))

    if oc.get("write_zh_txt", True):
        p = output_dir / f"{stem}.zh.txt"
        p.write_text("\n".join(f"{hhmmss(l.start_ms)} {t}" for l, t in zip(lines, zh)) + "\n", encoding="utf-8")
        written["zh_txt"] = p

    if oc.get("write_lrc", True):
        p = lrc_dir / f"{stem}.lrc"
        # [ti:] 保留标题元数据；[by:] 标明这次实际用的翻译模型（本地/云端不同）
        by_tag = str(oc.get("by_tag") or "Sakura-GalTransl")
        out: list[str] = [f"[ti:{title_zh or stem}]", f"[by:{by_tag}]"]
        # 标题同时作为**第一行字幕**写出来：很多播放器不显示 [ti:] 元数据，只显示正文行
        if title_zh and oc.get("lrc_title_line", True):
            at = int(oc.get("lrc_title_at_ms", 0))
            out.append(f"{lrc_timestamp(at, 0)}{title_zh}")
            if lines and lines[0].start_ms <= at + 500:
                # 首条字幕就从 0 秒开始 → 标题行会被它顶掉（播放器只显示同一时刻的最后一行）
                logger.warning("首条字幕从 0 秒开始，LRC 标题行可能被它盖住（%s）；"
                               "可设 lrc_title_at_ms 或关掉 lrc_title_line", stem)
        bilingual = bool(oc.get("lrc_bilingual", False))
        for l, t in zip(lines, zh):
            ts = lrc_timestamp(l.start_ms, offset)
            out.append(f"{ts}{t}")
            if bilingual:
                out.append(f"{lrc_timestamp(l.start_ms, offset)}{l.text}")
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
        written["lrc"] = p

    return written


# ============================================================ 提示词
DEFAULT_SYSTEM = (
    "你是一个视觉小说翻译模型，可以通顺地使用给定的术语表以指定的风格将日文翻译成简体中文，"
    "并联系上下文正确使用人称代词，注意不要混淆使役态和被动态的主语和宾语，"
    "不要擅自添加原文中没有的特殊符号，也不要擅自增加或减少换行。"
)

DEFAULT_TASK = "根据以上术语表的对应关系和备注，结合历史剧情和上下文，将下面的文本从日文翻译成简体中文："

SUBTITLE_RULES = (
    "本次任务是音声/视频字幕：请严格逐行翻译，输出必须与输入编号一一对应。"
    "输出格式固定为 `[编号. 译文]`：每行都要用方括号包起来、方括号必须成对闭合、"
    "编号必须与输入完全相同（输入是 `[1. 日文]`，你就输出 `[1. 中文]`）；"
    "输入多少行就输出多少行，不得合并、拆分、省略或调换顺序，不要输出注释、空行或任何其他内容。"
    "方括号里面只放编号、点、空格和译文本身：不要再套第二层括号或引号，也不要在译文里加标记符号。"
    "即使某行只是「ん」「はぁ」这类拟声，也必须单独占一行给出中文拟声写法，不得与相邻行合并或省略。"
    "只输出这些 `[编号. 译文]` 行：不要输出 Input、History、Glossary 这类提示词标签，"
    "也不要输出\"仅供参考\"之类的说明文字（程序会自动剥掉方括号与编号）。"
    "拟声词（喘息、亲吻声、水声、呻吟等，如 はぁ、んっ、ちゅっ）必须用对应的中文拟声保留，"
    "不得省略或改写为文字描述。"
)

# 模型偶尔会把译文整行包裹在括号/引号里（见过 [] 「」 【】 『』 ""），统一剥掉
_WRAP_CHARS = [
    ("[", "]"), ("【", "】"), ("「", "」"), ("『", "』"), ("（", "）"), ("(", ")"),
    ('"', '"'), ("“", "”"), ("'", "'"), ("‘", "’"),
]


def strip_wrapping(text: str) -> str:
    """去掉整行包裹的对称符号（可能嵌套两层）。"""
    s = text.strip()
    changed = True
    while changed and len(s) >= 2:
        changed = False
        for left, right in _WRAP_CHARS:
            if s.startswith(left) and s.endswith(right) and len(s) > len(left) + len(right):
                s = s[len(left) : -len(right)].strip()
                changed = True
                break
    return s


# 模型偶尔把提示词的"脚手架"当成译文吐出来（实测：`[Input]`、`仅供参考`、`【台本】` 等）
_SCAFFOLD_PATTERNS = [
    r"\[(?:Input|History|Glossary|Output|Context|System|User|Assistant)\]",
    r"\[(?:输入|输出|历史|术语表|上下文)\]",
    r"【(?:音频识别|台本候选|台本|输入|输出|上下文|历史翻译|术语表)】",
    r"仅供参考",
    r"不要重复输出",
    r"不要翻译",
    r"已定译文",
    r"上文对照",
]
_SCAFFOLD_RE = re.compile("|".join(_SCAFFOLD_PATTERNS))
# 行首的"标签式"脚手架：模型把提示词里的说明整段照抄进译文，例如
#   `下一行原文，仅供参考，不要翻译：果然在。别摆出一副…`
# 这类值即使剥掉标签，正文也是**下一行**的译文（错行），因此调用方应视为
# 缓存未命中并重译；clean_scaffolding 仍会把它清干净，作为兜底。
_SCAFFOLD_LABEL_CHUNKS = [
    r"下一行原文", r"下一行", r"上一行原文", r"上一行", r"上文原文", r"上文对照",
    r"日文原文", r"原文", r"参考译文", r"译文", r"输入文本", r"输入", r"台词", r"台本",
    r"仅供参考", r"不要翻译", r"请勿翻译", r"不要重复输出", r"不要输出原文", r"不要输出说明",
    r"注意", r"说明", r"注",
]
_SCAFFOLD_LEAD_RE = re.compile(
    r"^\s*[\[【（(]?\s*(?:" + "|".join(_SCAFFOLD_LABEL_CHUNKS) + r")"
    r"(?:\s*[，,、;；:：]?\s*[\[【（(]?\s*(?:" + "|".join(_SCAFFOLD_LABEL_CHUNKS) + r"))*"
    r"\s*[\]】）)]?\s*[：:，,、;；]\s*"
)
# 行首的方括号标签本体：`【台本】原文：…` / `[Input] 中文` 之类
_SCAFFOLD_WRAP_RE = re.compile(
    r"^\s*[\[【]\s*(?:台本候选|音频识别|台本|输入文本|输入|输出|上下文|历史翻译|术语表"
    r"|input|output|history|glossary|context)\s*[\]】]\s*[：:，,、]?\s*",
    re.I,
)
# 整行就是一个裸关键词（如单独一行 "Input"）也算脚手架
_BARE_KEYWORD_RE = re.compile(
    r"^\s*\[?\s*(?:input|output|history|glossary|context|system|user|assistant|译文|输入|输出|历史)\s*\]?\s*$",
    re.I,
)
# 括号包裹的提示词注释：如 （注：下一行原文，仅供参考：…）
_SCAFFOLD_NOTE_RE = re.compile(r"[（(]\s*(?:注|说明|上文|下一行原文)[：:][^）)]*[）)]")
_EMPTY_WRAPPER_RE = re.compile(r"[\[【（(]\s*[\]】）)]")


def _trim_unbalanced_closers(text: str) -> str:
    """去掉多余的右括号（脚手架被切开后常留一个孤零零的 `）`）。"""
    s = text.rstrip()
    for left, right in (("(", ")"), ("（", "）"), ("[", "]"), ("【", "】")):
        while s.endswith(right) and s.count(right) > s.count(left):
            s = s[: -len(right)].rstrip()
    return s


def scrub_scaffolding(text: str) -> tuple[str, bool]:
    """返回 (清理后的文本, 是否发现"提示词脚手架"痕迹)。

    `leaked=True` 说明这行译文被模型的思考/提示词内容污染过：
      - 整行都是脚手架 → 清空串（调用方回退日文原文）
      - 带行首标签（`下一行原文，仅供参考，不要翻译：…`）→ 标签后的正文其实是
        **下一行**的译文，属于错行内容，缓存里遇到这种值应当作未命中重译
    """
    if not text:
        return text, False
    if _BARE_KEYWORD_RE.match(text):
        return "", True
    s = text
    for _ in range(4):                      # 标签可能叠了两三层
        t = _SCAFFOLD_WRAP_RE.sub("", s)
        t = _SCAFFOLD_LEAD_RE.sub("", t)
        t = _SCAFFOLD_NOTE_RE.sub("", t)
        if t == s:
            break
        s = t
    leaked = (s != text) or bool(_SCAFFOLD_RE.search(text))
    s = _SCAFFOLD_RE.sub("", s)
    s = _EMPTY_WRAPPER_RE.sub("", s)
    s = _trim_unbalanced_closers(s)
    s = re.sub(r"^[\s:：、,，.。\-]+|[\s:：、,，]+$", "", s)
    return s.strip(), leaked


def clean_scaffolding(text: str) -> str:
    """去掉模型把提示词框架当译文输出的残留。

    覆盖：`[Input]`/`【台本】` 等标签、"仅供参考"等说明词、单独成行的裸关键词、
    `（注：下一行原文，仅供参考：…）` 这类提示词注释，以及行首标签
    （`下一行原文，仅供参考，不要翻译：…`）。
    整行都是脚手架时返回空串（调用方据此回退为日文原文，并计入 untranslated）。
    """
    return scrub_scaffolding(text)[0]


# 提示词文件：都在 prompts/ 下，可任意修改（config.translation.prompt 可改文件名/目录）
PROMPT_FILES = {
    "system": "system.txt",
    "rules": "subtitle_rules.txt",
    "repair": "repair.txt",
    "title": "title.txt",
    "refine_judge": "refine_judge.txt",
    "refine_judge_batch": "refine_judge_batch.txt",
}


def prompt_dir(cfg: dict) -> Path:
    pcfg = (cfg.get("translation") or {}).get("prompt") or {}
    d = Path(str(pcfg.get("dir") or "prompts")).expanduser()
    return d if d.is_absolute() else PROJECT_ROOT / d


def prompt_text(cfg: dict, key: str, default: str) -> str:
    """读 prompts/<key>.txt；缺失就回退内置默认（首次读取后缓存进 cfg）。"""
    pcfg = (cfg.get("translation") or {}).get("prompt") or {}
    cache = pcfg.setdefault("_texts", {})
    if key in cache:
        return cache[key]
    name = str(pcfg.get(key) or PROMPT_FILES.get(key) or f"{key}.txt")
    f = prompt_dir(cfg) / name
    text = ""
    if f.exists():
        try:
            text = f.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("读取提示词失败：%s（%s），改用内置默认", f, exc)
    if not text:
        text = default.strip()
    cache[key] = text
    return text


def load_style(cfg: dict, path: str | None = None, enabled: bool = True) -> str:
    """读取风格规范文件（默认 config translation.prompt.style_file），失败只告警不中断。"""
    if not enabled:
        return ""
    pcfg = (cfg.get("translation") or {}).get("prompt") or {}
    raw = path if path is not None else pcfg.get("style_file")
    if not raw:
        return ""
    f = Path(str(raw)).expanduser()
    if not f.is_absolute():
        candidate = prompt_dir(cfg) / f
        f = candidate if candidate.exists() else PROJECT_ROOT / f
    if not f.exists():
        if path:                       # 显式指定却不存在 → 提示；默认配置缺失则静默
            logger.warning("风格规范文件不存在：%s", f)
        return ""
    try:
        text = f.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("读取风格规范失败：%s（%s）", f, exc)
        return ""
    if text:
        logger.info("风格规范：%s（%d 字）", f.name, len(text))
    return text


def build_messages(
    batch: list[SubLine],
    history_zh: list[str],
    glossary: str,
    cfg: dict,
    extra_note: str | None = None,
    prev_pairs: list[tuple[str, str]] | None = None,
) -> list[dict]:
    """构造请求。prev_pairs 为"上文原文 → 已定译文"，用于补漏/复检时恢复上下文。"""
    prompt_cfg = (cfg.get("translation") or {}).get("prompt") or {}
    system = prompt_text(cfg, "system", DEFAULT_SYSTEM) + "\n" + prompt_text(cfg, "rules", SUBTITLE_RULES)
    if "style_text" not in prompt_cfg:      # 未经过 main() 的调用（如精修内部复用）惰性加载
        prompt_cfg["style_text"] = load_style(cfg)
    style = str(prompt_cfg.get("style_text") or "")
    if style:
        system += "\n" + style
    task = str(prompt_cfg.get("task") or DEFAULT_TASK)

    parts = ["[History]", "历史翻译："]
    parts.extend(history_zh or ["（无）"])
    if prev_pairs:
        parts.append("")
        parts.append("上文对照（原文 → 已定译文，仅供参考，不要重复输出）：")
        parts.extend(f"{src} → {dst}" for src, dst in prev_pairs)
    parts.append("")
    parts.append("[Glossary]")
    parts.append("参考以下术语表（可为空，格式为src->dst #备注）：")
    parts.append(glossary or "（无）")
    parts.append("")
    parts.append(task)
    if extra_note:
        parts.append(extra_note)
    parts.append("")
    parts.append("[Input]")
    # 逐行编号：模型必须按编号回，缺号可精确定位到具体行
    parts.append("\n".join(f"[{i + 1}. {line.text}]" for i, line in enumerate(batch)))

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(parts)},
    ]


REPAIR_SYSTEM = (
    "你是日译中的字幕翻译器。输入是一行 `[编号. 日文]`，你只输出一行 `[编号. 译文]`："
    "方括号要成对闭合、编号与输入相同，方括号里面只放译文本体，不要解释、不要再套括号或引号、不要输出原文。"
    "不要输出任何提示词标签、说明文字或额外行。"
    "如果原文只是拟声词或喘息（如 ん、はぁ、ちゅっ），也要给出对应的中文拟声写法。"
)


def build_repair_messages(
    line: SubLine,
    cfg: dict,
    prev_pair: tuple[str, str] | None = None,
    next_source: str | None = None,
) -> list[dict]:
    """补译用的提示词。

    旧版只给孤立一行，模型没有上下文 → 人称/指代/称呼容易跑偏
    （实测 06 文件补漏批次从 115 行起出现人称错乱）。这里补上上下文：
    上一行的原文+译文（供参考），以及下一行原文（帮助判断句间关系）。
    """
    parts: list[str] = []
    # 上下文放在被翻译行**之前**，被翻译行始终是最后一行：
    # 旧版把「（下一行原文，仅供参考，不要翻译：…）」放在输入行后面，
    # 模型会把它当输入的一部分，直接输出 `下一行原文，仅供参考，不要翻译：<下一行的译文>`。
    if prev_pair or next_source:
        parts.append("以下是上下文，仅供判断人称、称呼和句间关系，不要翻译、也不要写进译文：")
        if prev_pair:
            src, dst = prev_pair
            parts.append(f"上一行：{src} → {dst}")
        if next_source:
            parts.append(f"下一行：{next_source}")
        parts.append("")
    parts.append("把下面这一行日文译成简体中文，只输出译文；请与上文的人称、称呼、语气保持一致。")
    parts.append("只翻译最后这一条编号行，不要输出任何标签、说明或原文：")
    parts.append(f"[1. {line.text}]")
    pcfg = (cfg.get("translation") or {}).get("prompt") or {}
    if "style_text" not in pcfg:
        pcfg["style_text"] = load_style(cfg)
    style = str(pcfg.get("style_text") or "")
    return [
        {"role": "system", "content": prompt_text(cfg, "repair", REPAIR_SYSTEM) + ("\n" + style if style else "")},
        {"role": "user", "content": "\n".join(parts)},
    ]


# ============================================================ 翻译


NUM_RE = re.compile(
    r"^\s*[\[【（(]?\s*(\d{1,4})\s*(?:[-~〜–]\s*(\d{1,4}))?\s*[.、)）．:：\]]?\s*(.*)$"
)
KANA_RE = re.compile(r"[ぁ-んァ-ヶｦ-ﾟ]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 行首编号前缀：优先要求数字后跟标点；仅在我们明确要求过编号的场景才允许"数字+空格"
_NUM_PREFIX_PUNCT_RE = re.compile(r"^\s*\d{1,4}\s*[.、)）．:：,，]\s*")
_NUM_PREFIX_SPACE_RE = re.compile(r"^\s*\d{1,4}\s+")
# 方括号/圆括号包着的编号：[1] 【2】 (3) （4）
_NUM_PREFIX_BRACKET_RE = re.compile(
    r"^\s*[\[【（(]\s*\d{1,4}\s*(?:[-~〜–]\s*\d{1,4})?\s*[.、,，)）．:：\]]?\s*[\]】）)]?\s*"
)


def strip_line_number(text: str, allow_space_sep: bool = True) -> str:
    """剥掉行首**重复**出现的编号前缀。

    覆盖：`1. 中文`、`1、中文`、`1) 中文`、`[1] 中文`、`1. 1. 中文`（模型重复编号）等。
    旧逻辑只剥一层，残留的 `1. ` 会写进 zh.txt / LRC，也会污染台本精修的判定。

    allow_space_sep=False 时只剥"数字+标点/括号"形式（用于清洗历史产物与缓存），
    避免把 "3 人で歩いた" 这类正常译文误剥成 "人で歩いた"。
    """
    s = (text or "").strip()
    for depth in range(4):
        m = _NUM_PREFIX_PUNCT_RE.match(s) or _NUM_PREFIX_BRACKET_RE.match(s)
        # "数字+空格" 只在最外层允许（那是模型加的行号标记）；
        # 后续层只认"数字+标点/括号"，避免把 "3. 3 人で歩いた" 连剥成 "人で歩いた"
        if m is None and allow_space_sep and depth == 0:
            m = _NUM_PREFIX_SPACE_RE.match(s)
        if m is None:
            break
        new = s[m.end():].strip()
        if not new:  # 整行只有编号 → 视为空
            return ""
        if new == s:
            break
        s = new
    return s


# 提示词要求模型统一输出 `[编号. 译文]`；清洗后仍像"编号/包裹"的残留（说明没剥干净）
_LEFTOVER_NUM_RE = re.compile(r"^\s*[\[【（(]?\s*\d{1,4}\s*[.、)）．:：\]]")


def strip_output_wrapper(text: str) -> str:
    """把模型输出统一成"纯译文"：剥掉 `[]` 包裹与行首编号。

    提示词里要求模型每行写成 `[编号. 译文]`（见 SUBTITLE_RULES），这里负责在
    输出阶段把外层方括号和编号去掉。要能容忍模型的三种偏差：
      - 右括号缺失：`[10. 译文`      ← 实测真的出现过（track01 第 58~60 行）
      - 只用编号不用括号：`10. 译文`、`【10】译文`、`（10）译文`
      - 重复编号/重复包裹：`[1. [1. 译文]]`
    注意 allow_space_sep=False：只剥"数字+标点/括号"，避免把 `3 人で歩いた` 误剥。
    """
    s = strip_wrapping(text or "")
    for _ in range(3):
        t = strip_line_number(s, allow_space_sep=False)
        t = _trim_unbalanced_closers(t)
        t = strip_wrapping(t)
        if t == s:
            break
        s = t
    return s.strip()


def _bump(stats: dict | None, key: str, n: int = 1) -> None:
    if stats is not None:
        stats[key] = stats.get(key, 0) + n


def _clean_raw(raw: str) -> list[str]:
    """去掉思考块与 Markdown 围栏，返回非空行。"""
    text = raw.strip()
    text = re.sub(r"<think\b.*?</think\s*>", "", text, flags=re.S | re.I)
    text = re.sub(r"<thinking\b.*?</thinking\s*>", "", text, flags=re.S | re.I)
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def parse_numbered(raw: str, expected: int) -> tuple[dict[int, str], list[int]]:
    """解析模型输出 → ({0 基索引: 译文}, 缺失索引)。

    优先按行首编号对准（缺号可精确定位到具体行）；完全没编号时按位置对应兜底。
    """
    lines = _clean_raw(raw)
    resolved: dict[int, str] = {}
    numbered_hits = 0
    ranges: list[tuple[int, int, str]] = []

    for ln in lines:
        m = NUM_RE.match(ln)
        if not m:
            continue
        numbered_hits += 1
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        body = strip_output_wrapper(m.group(3) or "")
        if not body:
            continue
        if end > start:  # 模型把多行合并成一个编号范围
            ranges.append((start, end, body))
            continue
        idx = start - 1
        if 0 <= idx < expected and idx not in resolved:
            resolved[idx] = body

    # 编号范围：能按分隔符拆回就拆，拆不开则视为这些行都缺失（交给补漏）
    for start, end, body in ranges:
        idxs = [i - 1 for i in range(start, end + 1) if 0 <= i - 1 < expected]
        pieces = [p.strip() for p in re.split(r"[/／|｜；;]", body) if p.strip()]
        if len(pieces) == len(idxs):
            for i, piece in zip(idxs, pieces):
                resolved.setdefault(i, piece)

    if numbered_hits == 0:
        cleaned = [strip_output_wrapper(ln) for ln in lines]
        cleaned = [ln for ln in cleaned if ln]
        # 行数对得上、且没有"没剥干净的编号/包裹"残留，才按位置采用；
        # 否则交给补漏（避免把 `[10. 译文` 这类当正文静默写进产物）
        if len(cleaned) == expected and not any(_LEFTOVER_NUM_RE.match(t) for t in cleaned):
            return {i: t for i, t in enumerate(cleaned)}, []
        return {}, list(range(expected))

    return resolved, [i for i in range(expected) if i not in resolved]


def is_suspect(src: str, zh: str) -> bool:
    """判断译文是否可疑（空、与原文字面相同、或仍是日文）。

    原文本身没有可译内容（只有 `…`/`。`/`？`/♪ 这类符号）时不算可疑：
    这种行译文就是符号本身，模型返回空属正常。
    """
    if not re.search(r"[ぁ-ゖァ-ヺ一-龥a-zA-Z0-9]", src):
        return False                      # 原文无实词 → 无需翻译
    if not zh or not zh.strip():
        return True
    if zh.strip() == src.strip():
        # 与原文完全相同：只有原文含假名时才算"照抄日文"。
        # 汉字-only 行（如「安心…」「学校」）中日文写法本来就一样，回显是正常结果。
        return bool(KANA_RE.search(src))
    if KANA_RE.search(src):
        if not CJK_RE.search(zh):
            return True
        kana = len(KANA_RE.findall(zh))
        total = len(re.sub(r"\s", "", zh))
        if total and kana / total > 0.5:
            return True
    return False


def parse_glossary_map(glossary: str) -> dict[str, str]:
    """把术语表解析成 {规范化的日文: 中文}，用于单字/拟声行优先查表。"""
    out: dict[str, str] = {}
    for ln in (glossary or "").splitlines():
        if "->" not in ln:
            continue
        src, dst = ln.split("->", 1)
        dst = dst.split("#", 1)[0].strip()
        key = re.sub(r"[\s、。，．…‥♪♥♡]+", "", src)
        if key and dst:
            out[key] = dst
    return out


def repair_one(
    client: ChatClient,
    line: SubLine,
    cfg: dict,
    attempts: int = 2,
    gmap: dict[str, str] | None = None,
    context: tuple[tuple[str, str] | None, str | None] | None = None,
) -> str | None:
    """单行补译：先查术语表，再用提示词重试（带上下文以避免人称错乱）。"""
    if gmap:
        mapped = gmap.get(re.sub(r"[\s、。，．…‥♪♥♡]+", "", line.text))
        if mapped:
            return mapped
    prev_pair, next_source = context or (None, None)
    for attempt in range(attempts):
        try:
            raw = client.chat(
                build_repair_messages(line, cfg, prev_pair=prev_pair, next_source=next_source),
                max_tokens=160,
            )
        except FatalApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("补译请求失败（第 %d 次）：%s", attempt + 1, exc)
            time.sleep(1.5 * (attempt + 1))
            continue
        if not raw.strip():
            # 模型对这类输入（如无意义的纯拟声串）直接给空，重试也没用，立即止损
            logger.info("补译返回空内容（%r…），不再重试", line.text[:16])
            return None
        got, _ = parse_numbered(raw, 1)
        text = got.get(0)
        if not text:
            # 兜底取原始首行时也要剥编号（模型可能回 "5. 中文" 这类与请求编号不一致的前缀）
            parts = _clean_raw(raw)
            text = strip_output_wrapper(parts[0]) if parts else ""
        text = strip_output_wrapper(text)
        if _LEFTOVER_NUM_RE.match(text):   # 仍是 `[10. …` 这种 → 判失败，别写进缓存
            continue
        if text and not is_suspect(line.text, text):
            return text
    return None


def translate_batch(
    client: ChatClient,
    batch: list[SubLine],
    history_zh: list[str],
    glossary: str,
    cfg: dict,
    retries: int,
    stats: dict | None = None,
    gmap: dict[str, str] | None = None,
    idxs: list[int] | None = None,
    dead: set[int] | None = None,
    ctx_provider=None,
    prev_pairs: list[tuple[str, str]] | None = None,
) -> list[str]:
    """翻译一批。

    行数不符时**不再整批重跑**：按编号定位缺失行 → 只补那几行 → 仍缺才单行补译。
    补漏与单行补译都会**带上上下文**（上文原文/译文、下一行原文），避免人称与指代跑偏。
    ctx_provider: 可调用对象，输入本批首行的全局下标，返回供参考的上文对照。
    """
    if not batch:
        return []

    tcfg = (cfg.get("translation") or {}).get("translate") or {}
    per_line = int(tcfg.get("max_tokens_per_line", 30))
    repair_attempts = int(tcfg.get("repair_max_attempts", 2))
    cap = max(64, int(len(batch) * per_line))

    resolved: dict[int, str] = {}
    empty_batch = False
    messages = build_messages(batch, history_zh, glossary, cfg)

    for attempt in range(retries + 1):
        try:
            _bump(stats, "requests")
            raw = client.chat(messages, max_tokens=cap)
        except FatalApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("请求失败（第 %d 次）：%s: %s", attempt + 1, type(exc).__name__, exc)
            time.sleep(2 * (attempt + 1))
            continue
        if not raw.strip():
            _bump(stats, "empty_responses")
            logger.warning("模型返回空内容（本批 %d 行）→ 跳过整批补漏，直接单行补译", len(batch))
            empty_batch = True
            break
        got, missing = parse_numbered(raw, len(batch))
        resolved.update(got)
        if not missing:
            return [resolved[i] for i in range(len(batch))]
        break  # 行数不符 → 转精确补漏

    missing = [i for i in range(len(batch)) if i not in resolved]
    whole_batch_failed = missing and len(missing) == len(batch)
    if whole_batch_failed:
        # 整批没解析出来时，补漏请求的提示词与原来完全相同（同样 12 行），重发只是浪费一次请求，
        # 还容易再触发一次重复编号。直接转单行补译。
        logger.warning(
            "整批 %d 行均未解析成功 → 跳过重复的整批补漏，直接单行补译", len(batch)
        )
    if missing and not empty_batch and not whole_batch_failed:
        _bump(stats, "mismatch_batches")
        logger.warning(
            "行数不符：缺 %d/%d 行（第 %s 行）→ 只补这几行，不整批重跑",
            len(missing),
            len(batch),
            "、".join(str(i + 1) for i in missing[:8]),
        )
        sub = [batch[i] for i in missing]
        try:
            _bump(stats, "recovery_requests")
            # 关键修正：补漏请求必须带上文上下文，否则人称/指代会跑偏
            raw2 = client.chat(
                build_messages(
                    sub,
                    history_zh,
                    glossary,
                    cfg,
                    extra_note="只翻译下面这些行；请与上文的人称、称呼、语气保持一致。",
                    prev_pairs=prev_pairs,
                ),
                max_tokens=max(64, len(sub) * per_line),
            )
            got2, still = parse_numbered(raw2, len(sub)) if raw2.strip() else ({}, list(range(len(sub))))
            for k, text in got2.items():
                resolved[missing[k]] = text
            if still:
                logger.warning("补漏后仍缺 %d 行 → 转单行补译", len(still))
        except FatalApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("补漏请求失败：%s", exc)

    # 仍缺的行 → 单行补译；再不行才保留日文
    for i in range(len(batch)):
        if i in resolved:
            continue
        _bump(stats, "recovery_requests")
        ctx = None
        if ctx_provider is not None and idxs is not None:
            try:
                ctx = ctx_provider(idxs[i])
            except Exception:  # noqa: BLE001
                ctx = None
        text = repair_one(client, batch[i], cfg, attempts=repair_attempts, gmap=gmap, context=ctx)
        if text:
            resolved[i] = text
        else:
            _bump(stats, "untranslated")
            resolved[i] = batch[i].text
            if dead is not None and idxs is not None:
                dead.add(idxs[i])

    # 记录"走过补漏/单行补译"的窗口，供结尾复检
    if stats is not None and idxs and (missing or empty_batch):
        lo = idxs[0] - 1
        hi = idxs[-1] + 1
        stats.setdefault("recovery_windows", []).append([max(0, lo), hi])

    return [resolved[i] for i in range(len(batch))]


def _merge_windows(windows: list[list[int]], total: int) -> list[tuple[int, int]]:
    """合并重叠/相邻的窗口。"""
    spans = sorted((max(0, int(a)), min(total - 1, int(b))) for a, b in windows if total > 0)
    merged: list[list[int]] = []
    for a, b in spans:
        if a > b:
            continue
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def verify_windows(
    client: ChatClient,
    lines: list[SubLine],
    zh: list[str],
    windows: list[list[int]],
    cfg: dict,
    glossary: str,
    stats: dict,
    batch_lines: int = 12,
) -> int:
    """对"走过补漏/单行补译"的窗口带上下文复检一遍，返回被改写的行数。

    补漏与单行补译天然缺少上下文，容易出现人称/指代错乱（实测 06 文件 115 行起）。
    这里把这些窗口连同前文一起重发，用完整上下文重译一遍，取更可靠的一版，
    并在 meta 里记录改动行号，方便人工复核。
    """
    if not windows or not lines:
        return 0
    tcfg = (cfg.get("translation") or {}).get("translate") or {}
    per_line = int(tcfg.get("max_tokens_per_line", 30))
    ctx_lines = int(tcfg.get("verify_context_lines", 2))
    max_windows = int(tcfg.get("verify_max_windows", 20))

    spans = _merge_windows(windows, len(lines))
    if len(spans) > max_windows:
        logger.warning("补漏窗口 %d 个，超过上限 %d，只复检前 %d 个", len(spans), max_windows, max_windows)
        spans = spans[:max_windows]

    changed_total = 0
    changed_lines: list[int] = []
    for lo, hi in spans:
        sub = lines[lo : hi + 1]
        if not sub:
            continue
        prev_pairs = [
            (lines[j].text, zh[j]) for j in range(max(0, lo - ctx_lines), lo) if zh[j]
        ]
        try:
            _bump(stats, "verify_requests")
            raw = client.chat(
                build_messages(
                    sub,
                    [],
                    glossary,
                    cfg,
                    extra_note="这些行此前是分开补译的，请结合上文重新翻译，确保人称、称呼、语气与上文一致。",
                    prev_pairs=prev_pairs,
                ),
                max_tokens=max(64, len(sub) * per_line),
            )
        except FatalApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("复检请求失败（第 %d~%d 行）：%s", lo + 1, hi + 1, exc)
            continue
        if not raw.strip():
            logger.warning("复检返回空内容（第 %d~%d 行），跳过", lo + 1, hi + 1)
            continue
        got, missing = parse_numbered(raw, len(sub))
        if missing:
            logger.warning("复检输出不完整（第 %d~%d 行，缺 %d 行），跳过该窗口", lo + 1, hi + 1, len(missing))
            continue
        for k, idx in enumerate(range(lo, hi + 1)):
            new = strip_output_wrapper(got.get(k, ""))
            if not new or is_suspect(lines[idx].text, new):
                continue
            if new != zh[idx]:
                zh[idx] = new
                changed_lines.append(idx + 1)
                changed_total += 1
        logger.info("复检第 %d~%d 行：窗口内改动见 meta", lo + 1, hi + 1)

    stats["verify_changed_lines"] = changed_lines
    stats["verify_windows"] = len(spans)
    if changed_total:
        logger.info("复检共改写 %d 行：%s", changed_total, "、".join(map(str, changed_lines[:12])))
    return changed_total


def load_glossary(path: Path | None, cfg: dict) -> str:
    tcfg = cfg.get("translation") or {}
    raw_path = path or Path(((tcfg.get("translate") or {}).get("glossary_file") or "glossary.txt"))
    if not raw_path.is_absolute():
        raw_path = PROJECT_ROOT / raw_path
    if not raw_path.exists():
        logger.info("未找到术语表 %s（可创建：每行 `日文->中文 #备注`）", raw_path)
        return ""
    entries = [
        ln.strip()
        for ln in raw_path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    logger.info("术语表 %s：%d 条", raw_path.name, len(entries))
    return "\n".join(entries)


TITLE_SYSTEM = (
    "你是日本音声/轻小说作品的标题翻译器。只输出译文本身，不要书名号，不要解释，不要重复原文。"
)
TITLE_PREFIX_RE = re.compile(r"^(?:track|TRACK|Track)?\s*\d+\s*[-_.．、)）]\s*")


def extract_title(stem: str) -> str:
    """从文件名提取作品标题：剥掉 track/数字前缀。"""
    title = TITLE_PREFIX_RE.sub("", stem).strip()
    return title or stem


def translate_title(client: ChatClient, stem: str, cfg: dict, cache: dict) -> str | None:
    """把文件名里的日文标题译成中文（写入 LRC [ti:]），结果进缓存。

    实测：该模型在标题这类短任务上**开 thinking 更稳**（关 thinking 会空返回或回显原文），
    所以先用 thinking 请求，失败再关 thinking 重试一次，仍失败则保留原名。
    """
    title = extract_title(stem)
    if not (KANA_RE.search(title) or CJK_RE.search(title)):
        return None
    key = f"__title__:{title}"
    if key in cache and cache[key]:
        return cache[key]

    tcfg = cfg.get("translation") or {}
    title_cfg = tcfg.get("title") or {}
    max_chars = int(title_cfg.get("max_chars", 40))
    entry = (tcfg.get("registry") or {}).get(tcfg.get("_active_model") or "") or {}
    # 本地模型实测"标题开思考更稳"；远程 API（思考会吃掉配额、更慢更贵）默认不开。
    # 想强制就设 title_thinking: true/false（写在 registry 的条目里或 translation.title 里）
    think_first = bool(entry.get("title_thinking", title_cfg.get("thinking", not entry.get("api"))))
    think_cap = int(title_cfg.get("max_tokens", 1024 if think_first else 256))

    attempts = [
        (
            True,
            f"把下面的日文作品标题译成自然的简体中文，保留 ♥ !? ～ ・ 等风格标点，"
            f"不要加书名号，不要解释，只输出译文（不超过 {max_chars} 字）：\n{title}",
        ),
        (
            False,
            f"请把下面这段日文翻译成中文，只输出译文：\n{title}",
        ),
    ]
    if not think_first:                       # API 默认：先不思考，再考虑思考
        attempts.reverse()

    for thinking, prompt in attempts:
        try:
            raw = client.chat(
                [{"role": "system", "content": prompt_text(cfg, "title", TITLE_SYSTEM)},
                 {"role": "user", "content": prompt}],
                max_tokens=think_cap if thinking else 256,
                enable_thinking=thinking,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("标题翻译请求失败（thinking=%s）：%s", thinking, exc)
            continue
        parts = _clean_raw(raw)
        # 标题也可能被模型加上编号/包裹（`[1. 雨声]`）→ 与正文用同一套剥离
        zh = strip_output_wrapper(parts[0]) if parts else ""
        zh = re.sub(r'^["\'“”「」【】]+|["\'“”「」【】]+$', "", zh).strip()
        # 空返回或原样回显（含带编号回显）都算失败，换下一套提示词
        if not zh or zh == title.strip() or not CJK_RE.search(zh):
            logger.info("标题翻译第 %d 次未得到有效译文（%r），换提示词重试", 1 if thinking else 2, zh[:20])
            continue
        cache[key] = zh
        logger.info("标题翻译：%s → %s", title, zh)
        return zh

    logger.warning("标题翻译失败，LRC [ti:] 保留原名：%s", title)
    return None


# ============================================================ 主流程
def translate_file(
    path: Path,
    client: ChatClient,
    cfg: dict,
    glossary: str,
    output_dir: Path,
    force: bool,
    limit: int | None,
    dry_run: bool = False,
    lrc_dir: Path | None = None,
    no_lrc: bool = False,
    model_key: str = "",
) -> dict:
    tcfg = cfg.get("translation") or {}
    tcfg_t = tcfg.get("translate") or {}
    batch_lines = int(tcfg_t.get("batch_lines", 12))
    history_lines = int(tcfg_t.get("history_lines", 2))
    retries = int(tcfg_t.get("retries", 2))
    adaptive = bool(tcfg_t.get("adaptive_batch", True))
    repair_on = bool(tcfg_t.get("repair_untranslated", True))
    repair_attempts = int(tcfg_t.get("repair_max_attempts", 2))
    verify_on = bool(tcfg_t.get("verify_recovered", True))
    title_on = bool((tcfg.get("title") or {}).get("translate", True))
    oc = dict(tcfg.get("output") or {})
    if model_key:
        oc["by_tag"] = model_by_tag(cfg, model_key)
    if no_lrc:
        oc["write_lrc"] = False       # 保护手动改过的 LRC，只更新中文文本

    stem = subtitle_stem(path)
    output_dir.mkdir(parents=True, exist_ok=True)
    lines = parse_subtitle(path)
    if limit:
        lines = lines[:limit]
    if not lines:
        hint = ""
        if path.suffix.lower() in (".txt", ".srt", ".lrc"):
            hint = "（需要带时间轴：本工具 .ja.txt / .srt / .lrc / `[HH:MM:SS]` 文本；纯文本无时间轴不支持）"
        logger.warning("%s 没有可翻译的字幕行%s", path.name, hint)
        return {"file": path.name, "status": "empty"}

    cache_path = output_dir / f"{stem}.zh.cache.json"
    cache: dict[str, str] = {}
    if cache_path.exists() and not force:
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            # 旧缓存里可能存着带行号的译文（"1. 中文" / "1.1. 中文"）→ 读入时就洗净，
            # 这样即使全命中缓存、不再调用模型，重跑也能产出干净字幕
            fixed_cache = 0
            polluted_cache = 0
            identity_cache = 0
            for k, v in list(cache.items()):
                if isinstance(v, str) and not k.startswith("__"):
                    cleaned_v, leaked = scrub_scaffolding(strip_output_wrapper(v))
                    if leaked:
                        # 提示词脚手架污染过的译文属于错行内容（标签后的正文是下一行的译文）
                        # → 直接删掉这条缓存，本次运行会重新翻译该行
                        del cache[k]
                        polluted_cache += 1
                        continue
                    if cleaned_v != v:
                        cache[k] = cleaned_v
                        fixed_cache += 1
                    elif cleaned_v.strip() == k.strip() and KANA_RE.search(k):
                        # 译文与原文一模一样且含假名 → 当时其实没译出来（模型原样回显）。
                        # 留在缓存里会永远命中，每次重跑都跳过这一行（实测 05 的三行呼吸音）。
                        del cache[k]
                        identity_cache += 1
            if fixed_cache:
                logger.info("缓存清理：%d 条旧译文带行号，已自动剥掉（无需 --force）", fixed_cache)
            if polluted_cache:
                logger.warning("缓存清理：%d 条译文被提示词脚手架污染（错行内容），已作废并将在本次重译", polluted_cache)
            if identity_cache:
                logger.info("缓存清理：%d 条译文与原文相同（未译出），已作废并将在本次重译", identity_cache)
        except Exception:  # noqa: BLE001
            cache = {}

    logger.info("=" * 68)
    logger.info("翻译 %s：%d 行，每批 %d 行，缓存命中 %d", path.name, len(lines), batch_lines, sum(1 for l in lines if l.text in cache))

    t0 = time.time()
    zh: list[str] = [""] * len(lines)
    cache_hits = 0
    pending: list[int] = []
    for i, line in enumerate(lines):
        cached = cache.get(line.text)
        if cached and is_usable_cache_value(line.text, cached):
            zh[i] = cached
            cache_hits += 1
        elif not line_needs_translation(line.text):
            # `…` 这类纯符号行：译文就是原文本身，不必调用模型（模型只会返回空）
            zh[i] = line.text
        else:
            pending.append(i)

    done = 0
    cur_batch = batch_lines
    clean_streak = 0
    gmap = parse_glossary_map(glossary)
    stats: dict[str, int] = {
        "requests": 0,
        "expected_batches": (len(pending) + batch_lines - 1) // batch_lines if pending else 0,
        "mismatch_batches": 0,
        "recovery_requests": 0,
        "untranslated": 0,
    }

    pos = 0
    dead: set[int] = set()

    if dry_run:
        # 只打印提示词样本就返回：**绝不写任何文件**（旧实现放在循环里，
        # 全命中缓存时跳过该分支，会把产物写成截断版）
        sample = lines[: max(1, min(batch_lines, len(lines)))]
        for m in build_messages(sample, [], glossary, cfg):
            print(f"--- {m['role']} ---\n{m['content']}\n")
        logger.info("dry-run：仅打印提示词，未写入任何文件")
        return {"file": path.name, "status": "dry-run"}

    if pending and client is None:
        # 兜底：调用方以为"全部命中缓存"而没加载模型，但清洗缓存后仍有待译行。
        # 直接报错，别让每一行都去撞 NoneType.chat（旧版会连撞几十次才结束）
        raise RuntimeError(
            f"有 {len(pending)} 行需要翻译，但本次运行没有加载模型"
            f"（第 {[i + 1 for i in pending[:10]]} 行）。请重新运行 translate（不要用 --dry-run）。"
        )

    def ctx_provider(gi: int):
        """给单行补译提供上下文：(上一行原文+当前译文, 下一行原文)。"""
        prev_pair = (lines[gi - 1].text, zh[gi - 1]) if gi > 0 and zh[gi - 1] else None
        next_src = lines[gi + 1].text if gi + 1 < len(lines) else None
        return prev_pair, next_src

    while pos < len(pending):
        idxs = pending[pos : pos + cur_batch]
        pos += len(idxs)
        batch = [lines[i] for i in idxs]
        history = [zh[j] for j in range(max(0, idxs[0] - history_lines), idxs[0]) if zh[j]]

        mismatch_before = stats["mismatch_batches"]
        out = translate_batch(
            client, batch, history, glossary, cfg, retries,
            stats=stats, gmap=gmap, idxs=idxs, dead=dead,
            ctx_provider=ctx_provider,
            prev_pairs=[
                (lines[j].text, zh[j])
                for j in range(max(0, idxs[0] - history_lines), idxs[0])
                if zh[j]
            ],
        )
        for i, text in zip(idxs, out):
            zh[i] = text
            cache[lines[i].text] = text
        done += len(idxs)

        # 自适应批量：本批出现不匹配就减半，连续干净批次后回升（从源头减少不匹配）
        if adaptive:
            if stats["mismatch_batches"] > mismatch_before:
                clean_streak = 0
                if cur_batch > 6:
                    cur_batch = max(6, cur_batch // 2)
                    logger.info("  自适应：批量降为 %d 行", cur_batch)
            else:
                clean_streak += 1
                if clean_streak >= 3 and cur_batch < batch_lines:
                    cur_batch = min(batch_lines, cur_batch * 2)
                    clean_streak = 0
                    logger.info("  自适应：批量回升到 %d 行", cur_batch)

        elapsed = time.time() - t0
        speed = done / elapsed if elapsed else 0
        eta = (len(pending) - done) / speed if speed else 0
        logger.info("  进度 %d/%d 行（%.2f 行/秒，剩余约 %.0f 秒）", done, len(pending), speed, eta)

    # ---- 补漏窗口复检：带上下文重译"走过补漏/单行补译"的片段（防止人称/指代错乱）----
    if verify_on and client is not None and stats.get("recovery_windows"):
        logger.info(
            "复检补漏窗口：%d 个（这些行此前缺少上下文）",
            len(stats["recovery_windows"]),
        )
        verify_windows(client, lines, zh, stats["recovery_windows"], cfg, glossary, stats, batch_lines=batch_lines)
        for i, text in enumerate(zh):
            if text:
                cache[lines[i].text] = text

    # ---- 补漏扫描：空译文 / 与原文相同 / 仍是日文（跳过已确认无解的行）----
    if repair_on and client is not None:
        suspects = [
            i for i, l in enumerate(lines) if i not in dead and is_suspect(l.text, zh[i])
        ]
        if suspects:
            logger.warning(
                "补漏扫描：%d 行可疑（第 %s 行），逐行补译",
                len(suspects),
                "、".join(str(i + 1) for i in suspects[:10]),
            )
            fixed = 0
            for i in suspects:
                prev_pair = (lines[i - 1].text, zh[i - 1]) if i > 0 and zh[i - 1] else None
                next_src = lines[i + 1].text if i + 1 < len(lines) else None
                text = repair_one(
                    client, lines[i], cfg, attempts=repair_attempts, gmap=gmap,
                    context=(prev_pair, next_src),
                )
                if text:
                    zh[i] = text
                    cache[lines[i].text] = text
                    fixed += 1
                    stats["recovery_requests"] += 1
            logger.info("补漏完成：修复 %d/%d 行", fixed, len(suspects))

    # ---- 标题翻译（写入 LRC [ti:]）----
    # 先取缓存：全命中缓存重跑时 client 为 None，必须能复用已译好的中文标题，
    # 否则 [ti:] 会退回日文文件名
    title_zh = None
    if title_on:
        cached_title = cache.get(f"__title__:{extract_title(stem)}")
        if cached_title:
            title_zh = cached_title
        elif client is not None:
            title_zh = translate_title(client, stem, cfg, cache)

    # 写入前最后一道清洗：① 剥掉漏出的行号 ② 去掉提示词脚手架残留
    cleaned: list[str] = []
    removed = 0
    for line, text in zip(lines, zh):
        t = strip_output_wrapper(text)
        t2 = clean_scaffolding(t)
        if t2 != t:
            removed += 1
        cleaned.append(t2 if t2 else line.text)   # 整行都是脚手架 → 回退日文原文
    zh = cleaned
    if removed:
        logger.warning("清理了 %d 行混入的提示词脚手架（如 [Input]、仅供参考）", removed)
    stats["scaffolding_lines_removed"] = removed

    untranslated = [i + 1 for i, l in enumerate(lines) if is_suspect(l.text, zh[i])]
    stats["untranslated"] = len(untranslated)
    if untranslated:
        logger.warning("仍有 %d 行未译出（第 %s 行），已保留日文原文", len(untranslated), "、".join(map(str, untranslated[:10])))

    # 试跑保护：--limit 会把产物截断成前 N 行。若该轨道已有更长的成品，直接拒绝写入，
    # 避免"只想试 20 行，结果把 200 行的成品覆盖掉"（历史事故）。确实要截断请加 --force。
    if limit:
        existing = output_dir / f"{stem}.zh.txt"
        if existing.exists():
            old_n = len([ln for ln in existing.read_text(encoding="utf-8").splitlines() if ln.strip()])
            if old_n > len(lines):
                logger.error("试跑模式（--limit %d）会把已有的 %d 行产物截断为 %d 行 → 已跳过写入以保护数据。",
                             limit, old_n, len(lines))
                logger.error("确实要截断请加 --force；只想试译请换一个没有产物的文件，或用 --dry-run 看提示词。")
                return {"file": path.name, "status": "skipped-limit", "lines": 0,
                        "cache_hits": 0, "elapsed_s": 0, "seconds_per_line": 0}

    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    written = write_outputs(stem, lines, zh, output_dir, oc, title_zh=title_zh, lrc_dir=lrc_dir)
    elapsed = time.time() - t0
    logger.info("完成：%s（%s，用时 %.1fs）", path.name, "、".join(p.name for p in written.values()), elapsed)

    meta = {
        "file": path.name,
        "lines": len(lines),
          "cache_hits": cache_hits,
        "elapsed_s": round(elapsed, 1),
        "seconds_per_line": round(elapsed / max(1, len(lines)), 2),
        "title_zh": title_zh,
        "model": model_key,
        "output_dir": str(output_dir),
        "requests": stats["requests"],
        "expected_batches": stats["expected_batches"],
        "mismatch_batches": stats["mismatch_batches"],
        "recovery_requests": stats["recovery_requests"],
        "recovery_windows": [[a + 1, b + 1] for a, b in stats.get("recovery_windows", [])],
        "verify_windows": stats.get("verify_windows", 0),
        "verify_changed_lines": stats.get("verify_changed_lines", []),
        "scaffolding_lines_removed": stats.get("scaffolding_lines_removed", 0),
        "untranslated_lines": untranslated,
        "outputs": {k: str(v) for k, v in written.items()},
    }
    (output_dir / f"{stem}.zh.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"file": path.name, "status": "done", **meta}


# 需要翻译的实词（假名/汉字/字母/数字）。整行只有 `…`、`。`、`？`、`♪` 这类符号时，
# 译文就是它本身，调用模型只会拿到空返回。
_TRANSLATABLE_RE = re.compile(r"[ぁ-ゖァ-ヺ一-龥a-zA-Z0-9]")


def line_needs_translation(text: str) -> bool:
    """原文是否含可译内容（`…` 这类纯符号行不需要翻译）。"""
    return bool(_TRANSLATABLE_RE.search(text or ""))


def is_usable_cache_value(key: str, value) -> bool:
    """这条缓存值是否可以直接当译文用（与 translate_file 的读入清洗保持同一标准）。

    作废的两类：
      - 提示词脚手架污染（`下一行原文，仅供参考，不要翻译：…`）→ 标签后是下一行的译文
      - 译文与原文完全相同且原文含假名 → 当时没译出来
    这类键必须算"未命中"，否则 is_fully_cached 会误判成全部命中而跳过加载模型。
    """
    if not isinstance(value, str):
        return False
    cleaned, leaked = scrub_scaffolding(strip_output_wrapper(value))
    if leaked:
        return False
    return not (cleaned.strip() == key.strip() and KANA_RE.search(key))


def is_fully_cached(path: Path, output_dir: Path, force: bool, limit: int | None) -> bool:
    """所有行都在缓存里（且缓存值可用）→ 不需要启动模型。"""
    if force:
        return False
    stem = subtitle_stem(path)
    cache_path = output_dir / f"{stem}.zh.cache.json"
    if not cache_path.exists():
        return False
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    lines = parse_subtitle(path)
    if limit:
        lines = lines[:limit]
    return bool(lines) and all(
        not line_needs_translation(line.text)
        or (line.text in cache and is_usable_cache_value(line.text, cache[line.text]))
        for line in lines
    )


def main() -> int:
    p = argparse.ArgumentParser(description="日中字幕翻译（Sakura-GalTransl + llama.cpp / LM Studio）")
    p.add_argument("inputs", nargs="*", help=".ja.txt 文件或目录（默认 中间文件/日文字幕/）")
    p.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    p.add_argument("--model", default=None, help="翻译模型注册名（默认 sakura-galtransl-14b）")
    p.add_argument("--output-dir", default=None, help="中文文本输出目录（默认与输入目录相同）")
    p.add_argument("--batch-lines", type=int, default=None)
    p.add_argument("--glossary", default=None, help="术语表文件（每行 日文->中文 #备注）")
    p.add_argument("--subtitles-dir", default=None, help="日文字幕输入根（默认 config paths.work_dir 下的「日文字幕」）")
    p.add_argument("--output-root", default=None, help="中文文本输出根（默认 config 中间目录下的「中文字幕」）")
    p.add_argument("--lrc-root", default=None, help="LRC 输出根（默认 config 中间目录下的「LRC」）")
    p.add_argument("--no-refined", action="store_true",
                   help="忽略台本精修版（<轨道>.ja.refined.txt），只用 ASR 原文")
    p.add_argument("--lrc-flat", action="store_true", help="所有 LRC 平铺到 lrc 根目录（不镜像作品文件夹）")
    p.add_argument("--lrc-with-text", action="store_true", help="与 --output-dir 同用时，LRC 也写进该目录")
    p.add_argument("--bilingual", action="store_true", help="LRC 中日对照（中文行 + 日文行）")
    p.add_argument("--no-lrc", action="store_true",
                   help="只更新 .zh.txt / 缓存，不重写 LRC（保护手动改过的 LRC）")
    p.add_argument("--no-consolidate", action="store_true",
                   help="不把产物归档到 output/<作品>/{字幕文件,音频,台本}，全部留在顶层工作区")
    p.add_argument("--style", default=None,
                   help="翻译风格规范文件（默认取 config translation.prompt.style_file，如 style_asmr.txt）")
    p.add_argument("--no-style", action="store_true", help="不加载风格规范（用内置默认提示词）")
    p.add_argument("--limit", type=int, default=None, help="只翻译前 N 行（试跑）")
    p.add_argument("--force", action="store_true", help="忽略缓存重新翻译")
    p.add_argument("--dry-run", action="store_true", help="只打印提示词，不调用模型")
    p.add_argument("--no-auto-server", action="store_true", help="不自动启动 llama-server")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format=LOG_FORMAT, stream=sys.stdout
    )

    cfg = load_config(Path(args.config))
    if args.batch_lines:
        cfg.setdefault("translation", {}).setdefault("translate", {})["batch_lines"] = args.batch_lines
    if args.bilingual:
        cfg.setdefault("translation", {}).setdefault("output", {})["lrc_bilingual"] = True
    if args.no_auto_server:
        cfg.setdefault("translation", {}).setdefault("server", {})["auto_start"] = False

    # 三个目录：日文输入根 / 中文文本根 / LRC 根（都镜像 input 的作品文件夹结构）
    paths_cfg = cfg.get("paths") or {}

    import paths_util

    wp = paths_util.work_paths(cfg)

    def _resolve(raw: str | None, default: Path) -> Path:
        if not raw:
            return default
        p = Path(str(raw)).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p

    subtitles_root = _resolve(args.subtitles_dir, wp["ja"])   # 提取输出 = 翻译输入
    output_root = _resolve(args.output_root, wp["zh"])
    lrc_root = _resolve(args.lrc_root, wp["lrc"])
    flat_out = _resolve(args.output_dir, wp["zh"]) if args.output_dir else None

    raw_inputs = args.inputs or [str(subtitles_root)]
    # (输入文件, 决定输出目录的相对路径)
    items: list[tuple[Path, Path]] = []

    for raw in raw_inputs:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if path.is_dir():
            def _rel(f: Path) -> Path:
                try:
                    return f.parent.relative_to(subtitles_root)
                except ValueError:
                    return f.parent.relative_to(path) if f.parent != path else Path(".")

            # 台本精修版与 ASR 版放在同一目录：<轨道>.ja.refined.txt 优先
            asr_files = [f for f in paths_util.iter_subtitles(path)
                         if lrc_root.resolve() not in f.resolve().parents]   # 别把 LRC 输出目录里的文件当输入
            if len(asr_files) < len(paths_util.iter_subtitles(path)):
                logger.warning("已跳过 LRC 输出目录里的字幕文件（避免自己覆盖自己）")
            # 只保留"真的有带时间轴字幕行"的文件：纯文本/坏文件跳过，避免卡住归档
            usable = []
            for f in asr_files:
                try:
                    has_lines = bool(parse_subtitle(f))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("跳过硬读失败的字幕：%s（%s）", f.name, exc)
                    has_lines = False
                if has_lines:
                    usable.append(f)
                else:
                    logger.warning("跳过（没有带时间轴的字幕行）：%s", f.name)
            asr_files = usable

            refined_files = sorted(path.rglob("*.ja.refined.txt"))
            refined_by_stem = {f.parent / subtitle_stem(f): f for f in refined_files}

            for f in asr_files:
                rf = refined_by_stem.pop(f.parent / subtitle_stem(f), None) if not args.no_refined else None
                if rf is not None:
                    items.append((rf, _rel(rf)))
                    logger.info("使用台本精修版：%s", rf.name)
                else:
                    items.append((f, _rel(f)))
            # 只有精修版、没有 ASR 版的文件也翻译
            for rf in refined_by_stem.values():
                items.append((rf, _rel(rf)))
        elif path.exists():
            if path.suffix.lower() == ".lrc" and lrc_root.resolve() in path.resolve().parents:
                logger.error("输入的 .lrc 位于 LRC 输出目录（%s）里，翻译会覆盖原文件；"
                             "请先把日文字幕放到别处（如 中间文件/日文字幕/<作品>/）", lrc_root)
                return 2
            # 显式指定文件：同样遵守"精修版优先"，否则会拿 ASR 原文去查缓存
            # （缓存键是精修后的行文本）→ 大面积未命中、把产物写成 ASR 行数并重新翻译。
            if not args.no_refined and path.name.endswith(".ja.txt"):
                rf = path.with_name(subtitle_stem(path) + ".ja.refined.txt")
                if rf.exists():
                    logger.info("使用台本精修版：%s", rf.name)
                    path = rf
            # 按它在作品结构里的位置决定产物目录
            rel = Path(".")
            try:
                rel = path.resolve().parent.relative_to(subtitles_root.resolve())
            except ValueError:
                rel = Path(".")
            items.append((path, rel))
        else:
            logger.warning("跳过不存在的路径：%s", path)

    files = [p for p, _ in items]
    if not files:
        logger.error("没有找到 .ja.txt 字幕文件（%s）。", subtitles_root)
        logger.error("若还没提取：python3 subtitle.py extract；若这个任务已归档，直接翻作品包里的日文字幕，例如：")
        logger.error("    python3 subtitle.py translate \"output/<作品>/字幕文件/日文/<轨道>.ja.txt\"")
        return 2

    def dirs_for(rel: Path) -> tuple[Path, Path]:
        """返回 (中文文本目录, LRC 目录)。默认按相对 subtitles_root 的子路径镜像。"""
        zh_dir = flat_out if flat_out else output_root / rel
        if args.lrc_flat:
            lrc_dir = lrc_root
        elif flat_out and args.lrc_with_text:
            lrc_dir = flat_out
        else:
            lrc_dir = lrc_root / rel
        return zh_dir, lrc_dir

    logger.info("日文字幕输入根：%s（有 <轨道>.ja.refined.txt 时自动优先使用）", subtitles_root)
    logger.info("中文文本输出根：%s ｜ LRC 输出根：%s%s", flat_out or output_root, lrc_root, "（平铺）" if args.lrc_flat else "")

    glossary = load_glossary(Path(args.glossary) if args.glossary else None, cfg)

    if args.dry_run:
        client = None
        manager = None
    elif all(is_fully_cached(f, dirs_for(rel)[0], args.force, args.limit) for f, rel in items):
        logger.info("所有行均命中缓存，跳过模型加载，直接产出字幕文件")
        client = None
        manager = None
    else:
        api = api_entry(cfg, args.model)
        if api:                                   # 第三选项：走远程 API（不占本机内存）
            api_key, api_cfg = api
            client = resolve_api_client(cfg, api_key, api_cfg)
            model_key, manager = api_key, None
        else:
            model_key, gguf_path = resolve_translation_model(cfg, args.model)
            logger.info("翻译模型：%s（%s）", model_key, gguf_path.name)
            manager = ServerManager(cfg, gguf_path)
            base_url, model_id = manager.ensure()
            sampling = (cfg.get("translation") or {}).get("sampling") or {}
            timeout = float(((cfg.get("translation") or {}).get("translate") or {}).get("request_timeout_s", 600))
            client = ChatClient(base_url, model_id, sampling, timeout)

    model_key = args.model or ((cfg.get("translation") or {}).get("default") or "")
    cfg.setdefault("translation", {})["_active_model"] = model_key
    # 风格规范（成人向音声用）：读一次放进 cfg，供 build_messages / build_repair_messages 取用
    _style = load_style(cfg, args.style, enabled=not args.no_style)
    cfg.setdefault("translation", {}).setdefault("prompt", {})["style_text"] = _style
    if args.no_style:
        logger.info("已按 --no-style 关闭风格规范")
    results = []
    bundle_inputs: set[Path] = set()
    try:
        for f, rel in items:
            try:
                zh_dir, lrc_dir = dirs_for(rel)
                in_bundle = None
                try:
                    import bundle as B

                    in_bundle = B.bundle_of_input(cfg, f)
                except Exception:  # noqa: BLE001
                    in_bundle = None
                if in_bundle:      # 重跑已归档的作品：读作品包、写回作品包
                    _, bdirs = in_bundle
                    zh_dir, lrc_dir = bdirs["zh"], bdirs["zh"]     # LRC 与中文文本同目录
                    bundle_inputs.add(f)
                    try:
                        _shown = bdirs["root"].relative_to(PROJECT_ROOT)
                    except ValueError:
                        _shown = bdirs["root"]
                    logger.info("输入来自作品包 %s/：产物写回同一作品包", _shown)
                results.append(
                    translate_file(
                        f, client, cfg, glossary, zh_dir, args.force, args.limit, args.dry_run,
                        lrc_dir=lrc_dir, no_lrc=args.no_lrc, model_key=model_key,
                    )
                )
            except KeyboardInterrupt:
                logger.warning("用户中断")
                break
            except FatalApiError as exc:
                logger.error("=" * 68)
                logger.error("API 拒绝了请求，已中止本轮（不会写入半成品，可修好后重跑，已翻译的行会走缓存）")
                logger.error("%s", exc)
                results.append({"file": f.name, "status": "failed", "error": str(exc)})
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception("翻译失败：%s", f.name)
                results.append({"file": f.name, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        if manager:
            manager.shutdown()

    logger.info("=" * 68)
    for r in results:
        if r["status"] == "done":
            logger.info(
                "  ✅ %s → %d 行（缓存命中 %d，%.1fs，%.2f 秒/行；请求 %s 次/理论 %s 批，不匹配 %s 批，补漏 %s 次，未译 %d 行）",
                r["file"], r["lines"], r["cache_hits"], r["elapsed_s"], r["seconds_per_line"],
                r.get("requests"), r.get("expected_batches"), r.get("mismatch_batches"),
                r.get("recovery_requests"), len(r.get("untranslated_lines") or []),
            )
        elif r["status"] == "dry-run":
            logger.info("  🧪 %s（dry-run）", r["file"])
        elif r["status"] == "skipped-limit":
            logger.info("  ⏭️  %s（试跑保护：已有更长的产物，未覆盖）", r["file"])
        else:
            logger.info("  ❌ %s → %s", r["file"], r.get("error") or r["status"])

    # ============ 整轮任务结束 → 一次性归档工作区（提取模式不走这里）============
    if args.limit and not args.no_consolidate:
        logger.warning("试跑模式（--limit）不归档：产物可能不完整，确认无误后再正常跑一次")
    if not args.no_consolidate and not args.dry_run and not args.limit:
        try:
            import bundle as B

            works: list[str] = []
            bundle_works: list[str] = []                      # 输入来自作品包 → 只刷新 LRC 成品库
            for f, rel in items:
                if bundle_inputs and f in bundle_inputs:
                    bw = B.bundle_of_input(cfg, f)
                    if bw and bw[0] not in bundle_works:
                        bundle_works.append(bw[0])
                    continue
                w = rel.parts[0] if rel.parts else ""         # "" = 扁平工作区（字幕直接在根目录下）
                if w not in works:
                    works.append(w)
            if works or bundle_works:
                logger.info("=" * 68)
            if works:
                logger.info("任务结束，开始归档工作区（%s）",
                            "、".join(w or "（扁平工作区）" for w in works))
                for w in works:
                    try:
                        res = B.consolidate_work(cfg, w) or {}
                        if res.get("status") == "incomplete":
                            continue          # 没翻完的作品：留在工作区，等补完再入库
                        # 归档后把该作品的中文 LRC 复制进「只进不出」的成品库
                        B.collect_lrc(cfg, w, name=res.get("bundle") or None)
                    except Exception as exc:  # noqa: BLE001  单个作品失败不影响其它作品
                        logger.warning("作品 %r 归档/入库失败：%s: %s", w, type(exc).__name__, exc)
            for bw in bundle_works:                           # 重跑已归档作品：刷新成品库
                B.collect_lrc(cfg, bw, name=bw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("归档失败（产物仍在工作区，可手动跑 scripts/bundle.py）：%s: %s", type(exc).__name__, exc)

    return 0 if all(r["status"] in ("done", "dry-run", "skipped-limit") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
