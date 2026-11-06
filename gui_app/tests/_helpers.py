"""测试辅助：合成 GGUF、临时 config.yaml。"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import yaml

APP_DIR = Path(__file__).resolve().parent.parent
ROOT = APP_DIR.parent
SCRIPTS = ROOT / "scripts"
for _p in (str(SCRIPTS), str(APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _s(v: str) -> bytes:
    b = v.encode("utf-8")
    return struct.pack("<Q", len(b)) + b


def write_gguf(path: Path, *, name: str = "sakura-7b-test", arch: str = "qwen3",
               size_label: str = "7B", file_type: int | None = 18, context: int = 32768,
               chat_template: bool = True, split_count: int = 1, split_no: int = 1) -> Path:
    """写一个只有 metadata、没有 tensor 的合法 GGUF v3（足够识别）。"""
    kvs: list[tuple[str, int, object]] = []
    kvs.append(("general.architecture", 8, arch))
    kvs.append(("general.name", 8, name))
    kvs.append(("general.size_label", 8, size_label))
    if file_type is not None:
        kvs.append(("general.file_type", 4, int(file_type)))
    kvs.append((f"{arch}.context_length", 4, int(context)))
    kvs.append((f"{arch}.block_count", 4, 40))
    if chat_template:
        kvs.append(("tokenizer.chat_template", 8, "{% for m in messages %}x{% endfor %}"))
    if split_count > 1:
        kvs.append(("split.count", 4, int(split_count)))
        kvs.append(("split.no", 4, int(split_no)))
    body = b""
    for key, typ, value in kvs:
        body += _s(key) + struct.pack("<I", typ)
        if typ == 8:
            body += _s(str(value))
        elif typ == 4:
            body += struct.pack("<I", int(value))
        else:
            raise ValueError(typ)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(kvs)) + body)
    return path


def make_config(root: Path, *, registry: dict | None = None, default: str = "",
                extra: dict | None = None) -> Path:
    root = Path(root)
    cfg = {
        "paths": {
            "input_dir": str(root / "input"),
            "work_dir": str(root / "中间文件"),
            "work_dirs": {"ja": "日文字幕", "zh": "中文字幕", "lrc": "LRC"},
            "promptbook_dir": str(root / "promptbook"),
            "output_dir": str(root / "output"),
            "collect_dir": str(root / "LRC成品库"),
        },
        "translation": {
            "default": default,
            "registry": registry or {},
            "server": {"llama_server_bin": "llama-server"},
            "sampling": {"temperature": 0.3, "top_p": 0.8, "max_tokens": 128},
            "translate": {"request_timeout_s": 30},
        },
        "bundle": {
            "consolidate": True,
            "collect_lrc": True,
            "move_audio": True,
            "move_promptbook": True,
            "dirs": {"subs": "字幕文件", "ja": "日文", "zh": "中文", "audio": "音频", "promptbook": "台本"},
        },
    }
    if extra:
        def deep(base, over):
            for k, v in over.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    deep(base[k], v)
                else:
                    base[k] = v
        deep(cfg, extra)
    path = root / "config.yaml"
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path
