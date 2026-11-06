#!/usr/bin/env python3
"""GGUF 模型信息识别：给图形界面的「选择本地模型」用。

只读文件头部的 metadata，不加载权重、不复制文件、不执行模型：

    general.name            模型内登记的名称（最可靠的显示名来源）
    general.architecture    架构（qwen3 / llama / gemma3 ...）
    general.size_label      参数量标注（如 15B）
    general.file_type       量化类型枚举（18 = Q6_K，30 = IQ4_XS）
    <arch>.context_length   上下文长度
    tokenizer.chat_template 是否有聊天模板
    split.count / split.no  分片模型

文件名作为兜底：sakura-14b-qwen3-v1.5-q6k.gguf → 量化 Q6_K、参数 14B、
分片后缀 -00001-of-00002。识别结果里同时给出原始信息和可供界面显示的
「自动名称」，用户可以在设置页里改名，最终写进 LRC 的 [by:]。
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- GGUF 常量
GGUF_MAGIC = b"GGUF"
DEFAULT_BUDGET = 64 * 1024 * 1024          # 只读前 64MB metadata，避免误读 11GB 权重
MAX_KV = 100_000
MAX_STRING = 16 * 1024 * 1024

# GGUF metadata value types
T_UINT8, T_INT8, T_UINT16, T_INT16 = 0, 1, 2, 3
T_UINT32, T_INT32, T_FLOAT32, T_BOOL = 4, 5, 6, 7
T_STRING, T_ARRAY, T_UINT64, T_INT64, T_FLOAT64 = 8, 9, 10, 11, 12

_FIXED = {
    T_UINT8: ("<B", 1),
    T_INT8: ("<b", 1),
    T_UINT16: ("<H", 2),
    T_INT16: ("<h", 2),
    T_UINT32: ("<I", 4),
    T_INT32: ("<i", 4),
    T_FLOAT32: ("<f", 4),
    T_BOOL: ("<?", 1),
    T_UINT64: ("<Q", 8),
    T_INT64: ("<q", 8),
    T_FLOAT64: ("<d", 8),
}

# llama.cpp 的 llama_ftype 枚举（common 值；未知值显示成 ft<N>）
FILE_TYPE_NAMES: dict[int, str] = {
    0: "F32",
    1: "F16",
    2: "Q4_0",
    3: "Q4_1",
    4: "Q4_1_SOME_F16",
    7: "Q8_0",
    8: "Q5_0",
    9: "Q5_1",
    10: "Q2_K",
    11: "Q3_K_S",
    12: "Q3_K_M",
    13: "Q3_K_L",
    14: "Q4_K_S",
    15: "Q4_K_M",
    16: "Q5_K_S",
    17: "Q5_K_M",
    18: "Q6_K",
    19: "IQ2_XXS",
    20: "IQ2_XS",
    21: "Q2_K_S",
    22: "IQ3_XS",
    23: "IQ3_XXS",
    24: "IQ1_S",
    25: "IQ4_NL",
    26: "IQ3_S",
    27: "IQ3_M",
    28: "IQ2_S",
    29: "IQ2_M",
    30: "IQ4_XS",
    31: "IQ1_M",
    32: "BF16",
    33: "Q4_0_4_4",
    34: "Q4_0_4_8",
    35: "Q4_0_8_8",
    36: "TQ1_0",
    37: "TQ2_0",
    38: "IQ4_NL_4_4",
    39: "IQ4_NL_4_8",
    40: "IQ4_NL_8_8",
}

# 文件名里的量化：优先完整写法（Q4_K_M / IQ4_XS / Q8_0），再兼容紧凑写法（q6k / iq4xs）
_QUANT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])("
    r"IQ\d(?:_[A-Z]{2,3}){1,3}"
    r"|Q\d(?:_[A-Z0-9]{1,3}){1,3}"
    r"|Q\d_[A-Z0-9]"
    r"|BF16|F16|F32"
    r")(?![A-Za-z0-9])"
)
_COMPACT_QUANT_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(iq\d[a-z]{1,4}|q\d[a-z]{1,4})(?![A-Za-z0-9])")
_COMPACT_QUANT_MAP = {
    "q2k": "Q2_K",
    "q3ks": "Q3_K_S",
    "q3km": "Q3_K_M",
    "q3kl": "Q3_K_L",
    "q4ks": "Q4_K_S",
    "q4km": "Q4_K_M",
    "q5ks": "Q5_K_S",
    "q5km": "Q5_K_M",
    "q6k": "Q6_K",
    "q8_0": "Q8_0",
    "iq4xs": "IQ4_XS",
    "iq3xxs": "IQ3_XXS",
    "iq2xxs": "IQ2_XXS",
    "iq2xs": "IQ2_XS",
    "iq1s": "IQ1_S",
    "iq1m": "IQ1_M",
    "iq3s": "IQ3_S",
    "iq3m": "IQ3_M",
    "iq2s": "IQ2_S",
    "iq2m": "IQ2_M",
    "iq4nl": "IQ4_NL",
}

_PARAM_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(\d{1,3}(?:\.\d)?)B(?![A-Za-z0-9])")
_VERSION_RE = re.compile(r"(?i)^v?\d+(?:\.\d+)*$")
_SPLIT_RE = re.compile(r"(?i)^(?P<prefix>.+?)-(?P<no>\d{5})-of-(?P<count>\d{5})(?P<ext>\.gguf)$")

_FAMILY_TOKENS = {
    "sakura": "Sakura",
    "galtransl": "GalTransl",
    "qwen": "Qwen",
    "gemma": "Gemma",
    "llama": "Llama",
    "mistral": "Mistral",
    "deepseek": "DeepSeek",
    "internlm": "InternLM",
    "yi": "Yi",
    "phi": "Phi",
    "gemma2": "Gemma2",
    "gemma3": "Gemma3",
    "instruct": "Instruct",
    "chat": "Chat",
    "base": "Base",
}


class _BudgetExceeded(Exception):
    pass


class _Reader:
    """带字节预算的 metadata 读取器：超过预算就停止，绝不把整个模型读进来。"""

    def __init__(self, fh, budget: int = DEFAULT_BUDGET):
        self.fh = fh
        self.remaining = int(budget)

    def _take(self, n: int) -> None:
        if n < 0:
            raise ValueError("negative read")
        if n > self.remaining:
            raise _BudgetExceeded
        self.remaining -= n

    def read(self, n: int) -> bytes:
        self._take(n)
        data = self.fh.read(n)
        if len(data) != n:
            raise EOFError("unexpected end of metadata")
        return data

    def skip(self, n: int) -> None:
        self._take(n)
        self.fh.seek(n, 1)

    def unpack(self, fmt: str, size: int):
        self._take(size)
        data = self.fh.read(size)
        if len(data) != size:
            raise EOFError("unexpected end of metadata")
        return struct.unpack(fmt, data)[0]

    def position(self) -> int:
        return self.fh.tell()


def _read_string(reader: _Reader, *, capture: bool = True, max_len: int = MAX_STRING) -> str | None:
    length = reader.unpack("<Q", 8)
    if length > max_len:
        raise ValueError(f"string too long: {length}")
    if not capture:
        reader.skip(length)
        return None
    return reader.read(length).decode("utf-8", errors="replace")


def _skip_value(reader: _Reader, vtype: int, depth: int = 0) -> None:
    if vtype in _FIXED:
        reader.skip(_FIXED[vtype][1])
        return
    if vtype == T_STRING:
        _read_string(reader, capture=False)
        return
    if vtype == T_ARRAY:
        if depth > 4:
            raise ValueError("nested array too deep")
        etype = reader.unpack("<I", 4)
        count = reader.unpack("<Q", 8)
        if count > 50_000_000:
            raise ValueError("array too large")
        if etype in _FIXED:
            reader.skip(_FIXED[etype][1] * count)
            return
        if etype == T_STRING:
            for _ in range(count):
                _read_string(reader, capture=False)
            return
        if etype == T_ARRAY:
            for _ in range(count):
                _skip_value(reader, T_ARRAY, depth + 1)
            return
        raise ValueError(f"unknown array element type: {etype}")
    raise ValueError(f"unknown metadata type: {vtype}")


@dataclass
class ModelInfo:
    """一份 GGUF 的识别结果（可直接 JSON 化给前端）。"""

    ok: bool
    path: str
    selected_path: str = ""
    load_path: str = ""
    file_name: str = ""
    size_bytes: int = 0
    size_gb: float = 0.0
    grammar: str = ""
    metadata_ok: bool = False
    metadata_complete: bool = False
    name: str = ""
    auto_display_name: str = ""
    architecture: str = ""
    params: str = ""
    params_source: str = ""
    quant: str = ""
    quant_source: str = ""
    context_length: int | None = None
    block_count: int | None = None
    has_chat_template: bool = False
    file_type: int | None = None
    split_count: int = 1
    split_no: int = 1
    split_missing: list[str] = field(default_factory=list)
    family: str = ""
    translation_specialized: bool = False
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "selected_path": self.selected_path or self.path,
            "load_path": self.load_path or self.path,
            "file_name": self.file_name,
            "size_bytes": self.size_bytes,
            "size_gb": round(self.size_gb, 2),
            "grammar": self.grammar,
            "metadata_ok": self.metadata_ok,
            "metadata_complete": self.metadata_complete,
            "name": self.name,
            "auto_display_name": self.auto_display_name,
            "architecture": self.architecture,
            "params": self.params,
            "params_source": self.params_source,
            "quant": self.quant,
            "quant_source": self.quant_source,
            "context_length": self.context_length,
            "block_count": self.block_count,
            "has_chat_template": self.has_chat_template,
            "file_type": self.file_type,
            "split_count": self.split_count,
            "split_no": self.split_no,
            "split_missing": list(self.split_missing),
            "family": self.family,
            "translation_specialized": self.translation_specialized,
            "warnings": list(self.warnings),
            "error": self.error,
            "raw": dict(self.raw),
        }


# ---------------------------------------------------------------- metadata
def _parse_metadata(path: Path, budget: int = DEFAULT_BUDGET) -> tuple[dict[str, Any], bool, bool]:
    """返回 (metadata, metadata_ok, complete)。

    只支持 GGUF v2/v3（v1 极少见）；解析失败也尽量返回已经读到的键。
    """
    meta: dict[str, Any] = {}
    complete = True
    with path.open("rb") as fh:
        header = fh.read(4)
        if header != GGUF_MAGIC:
            return meta, False, False
        reader = _Reader(fh, budget)
        try:
            version = reader.unpack("<I", 4)
            if version < 2:
                return meta, False, False
            tensor_count = reader.unpack("<Q", 8)
            kv_count = reader.unpack("<Q", 8)
            if kv_count > MAX_KV:
                return meta, True, False
            meta["_version"] = version
            meta["_tensor_count"] = tensor_count
            meta["_kv_count"] = kv_count
            for _ in range(int(kv_count)):
                key = _read_string(reader, capture=True, max_len=4096) or ""
                vtype = reader.unpack("<I", 4)

                # 只解析需要的键；其余按类型跳过（tokenizer 大数组不读进内存）
                if key == "tokenizer.chat_template":
                    _read_string(reader, capture=False)
                    meta[key] = "<present>"
                elif key.startswith("general.") or key.startswith("split."):
                    if vtype == T_STRING:
                        meta[key] = _read_string(reader, capture=True)
                    elif vtype in (T_UINT32, T_UINT64, T_INT32, T_INT64, T_FLOAT32, T_FLOAT64, T_BOOL):
                        meta[key] = reader.unpack(_FIXED[vtype][0], _FIXED[vtype][1])
                    else:
                        _skip_value(reader, vtype)
                elif key.endswith(".context_length") or key.endswith(".block_count"):
                    if vtype in (T_UINT32, T_UINT64, T_INT32, T_INT64):
                        meta[key] = reader.unpack(_FIXED[vtype][0], _FIXED[vtype][1])
                    else:
                        _skip_value(reader, vtype)
                else:
                    _skip_value(reader, vtype)
        except _BudgetExceeded:
            complete = False
        except (EOFError, ValueError, struct.error):
            complete = False
    return meta, True, complete


# ---------------------------------------------------------------- 文件名
def _filename_parts(path: Path) -> dict[str, Any]:
    name = path.name
    m = _SPLIT_RE.match(name)
    split_count = int(m.group("count")) if m else 1
    split_no = int(m.group("no")) if m else 1
    stem = m.group("prefix") if m else path.stem
    if stem.lower().endswith(".gguf"):
        stem = stem[:-5]

    quant = ""
    qm = _QUANT_RE.search(stem)
    if qm:
        quant = qm.group(1).upper()
    else:
        cm = _COMPACT_QUANT_RE.search(stem)
        if cm:
            quant = _COMPACT_QUANT_MAP.get(cm.group(1).lower(), cm.group(1).upper())
    params = ""
    pm = _PARAM_RE.search(stem)
    if pm:
        params = f"{pm.group(1)}B"
    return {
        "stem": stem,
        "quant": quant,
        "params": params,
        "split_count": split_count,
        "split_no": split_no,
    }


def _strip_quant(name: str) -> str:
    """从显示名候选里去掉量化/分片后缀，避免标题重复。"""
    name = _QUANT_RE.sub(" ", name)
    name = _COMPACT_QUANT_RE.sub(" ", name)
    name = _SPLIT_RE.sub("", name)
    name = re.sub(r"(?i)(?<![A-Za-z0-9])gguf(?![A-Za-z0-9])", " ", name)
    return re.sub(r"\s+", " ", name).strip(" -_.·")


def humanize_model_name(name: str) -> str:
    """把 general.name / 文件名转成温和可读的显示名。

    不做夸张的标题化：只映射常见家族词、参数量大写、版本保留原样，
    未知词原样输出，用户可在设置页改名。
    """
    raw = _strip_quant(name or "")
    if not raw:
        return "未命名模型"
    # 按 -/_/空格 切；点号只在不是版本号时切（v1.5 / qwen2.5 保持完整）
    tokens: list[str] = []
    for tok in re.split(r"[-_\s]+", raw):
        if not tok:
            continue
        if re.fullmatch(r"(?i)(?:v?\d+(?:\.\d+)+|[a-z]+\d+(?:\.\d+)+)", tok):
            tokens.append(tok)
        else:
            tokens.extend(x for x in tok.split(".") if x)
    out: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low in _FAMILY_TOKENS:
            out.append(_FAMILY_TOKENS[low])
            continue
        if re.fullmatch(r"(?i)\d{1,3}(?:\.\d)?b", tok):
            out.append(tok.upper())
            continue
        if re.fullmatch(r"(?i)v\d+(?:\.\d+)*", tok):
            out.append(tok if tok.lower().startswith("v") else f"v{tok}")
            continue
        if low.startswith("qwen") and re.fullmatch(r"(?i)qwen[\d.]*", tok):
            out.append("Qwen" + tok[4:])
            continue
        out.append(tok)
    text = " ".join(out).strip()
    return text or "未命名模型"


def _family(name: str) -> str:
    low = name.lower()
    for key, label in (
        ("sakura", "Sakura"),
        ("galtransl", "GalTransl"),
        ("qwen", "Qwen"),
        ("gemma", "Gemma"),
        ("llama", "Llama"),
        ("mistral", "Mistral"),
        ("deepseek", "DeepSeek"),
        ("internlm", "InternLM"),
        ("yi", "Yi"),
        ("phi", "Phi"),
    ):
        if key in low:
            return label
    return ""


def _display_name(base: str, quant: str) -> str:
    text = humanize_model_name(base)
    if quant and quant.lower() not in text.lower():
        text = f"{text} · {quant}"
    return text


# ---------------------------------------------------------------- 分片
def _split_paths(path: Path) -> list[Path]:
    m = _SPLIT_RE.match(path.name)
    if not m:
        return [path]
    prefix, count, ext = m.group("prefix"), int(m.group("count")), m.group("ext")
    return [path.with_name(f"{prefix}-{idx:05d}-of-{count:05d}{ext}") for idx in range(1, count + 1)]


def find_split_first(path: Path) -> tuple[Path, list[str]]:
    """选到分片的任一片时，返回第 1 片和缺失的分片名。"""
    parts = _split_paths(path)
    if len(parts) <= 1:
        return path, []
    missing = [p.name for p in parts if not p.is_file()]
    first = parts[0]
    if first.is_file():
        return first, missing
    # 第 1 片不在，但用户选的那片在：仍然返回用户选的（llama.cpp 可能拒绝，界面会警告）
    return path, missing


# ---------------------------------------------------------------- 对外接口
def inspect_model(path: str | Path, *, budget: int = DEFAULT_BUDGET) -> ModelInfo:
    """识别单个 .gguf 文件。非法文件返回 ok=False，不抛异常给界面。"""
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    info = ModelInfo(ok=False, path=str(p), selected_path=str(p), file_name=p.name)
    try:
        st = p.stat()
    except OSError as exc:
        info.error = f"无法读取文件：{exc}"
        return info
    if not p.is_file():
        info.error = "不是普通文件"
        return info
    info.size_bytes = int(st.st_size)
    info.size_gb = info.size_bytes / (1024 ** 3)
    if info.size_bytes < 32:
        info.error = "文件太小，不像有效的 GGUF"
        return info

    parts = _split_paths(p)
    if len(parts) > 1:
        info.split_count = len(parts)
        info.split_no = int(_SPLIT_RE.match(p.name).group("no"))  # type: ignore[union-attr]
        info.split_missing = [q.name for q in parts if not q.is_file()]
        first = parts[0] if parts[0].is_file() else p
        info.load_path = str(first)
        if info.split_no != 1:
            info.warnings.append(
                f"这是分片模型的第 {info.split_no} 片；已改用第 1 片 {first.name} 加载"
                if first != p else f"缺少第 1 片分片，无法可靠加载（共 {len(parts)} 片）"
            )
        if info.split_missing:
            info.warnings.append(f"缺少 {len(info.split_missing)} 个分片：{'、'.join(info.split_missing[:4])}")
    else:
        info.load_path = str(p)

    fname = _filename_parts(p)
    info.name = fname["stem"]
    info.params = fname["params"]
    info.params_source = "filename" if fname["params"] else ""
    info.quant = fname["quant"]
    info.quant_source = "filename" if fname["quant"] else ""

    meta, metadata_ok, complete = {}, False, False
    try:
        meta, metadata_ok, complete = _parse_metadata(p, budget=budget)
    except OSError as exc:
        info.error = f"读取文件失败：{exc}"
        return info
    info.metadata_ok = metadata_ok
    info.metadata_complete = complete
    if not metadata_ok:
        info.error = "不是有效的 GGUF 文件（魔数不匹配）"
        return info

    general_name = str(meta.get("general.name") or "").strip()
    if general_name:
        info.name = general_name
    info.architecture = str(meta.get("general.architecture") or "").strip()
    size_label = str(meta.get("general.size_label") or "").strip()
    param_count = meta.get("general.parameter_count")
    if size_label:
        info.params = size_label
        info.params_source = "metadata"
    elif isinstance(param_count, int) and param_count > 0:
        info.params = _format_param_count(param_count)
        info.params_source = "metadata"

    file_type = meta.get("general.file_type")
    if isinstance(file_type, int):
        info.file_type = file_type
        mapped = FILE_TYPE_NAMES.get(file_type)
        if mapped:
            info.quant = mapped
            info.quant_source = "metadata"

    ctx = meta.get("context_length")
    if ctx is None and info.architecture:
        ctx = meta.get(f"{info.architecture}.context_length")
    if isinstance(ctx, int):
        info.context_length = ctx
    block = meta.get("block_count")
    if block is None and info.architecture:
        block = meta.get(f"{info.architecture}.block_count")
    if isinstance(block, int):
        info.block_count = block

    info.has_chat_template = bool(meta.get("tokenizer.chat_template"))
    split_count = meta.get("split.count")
    if isinstance(split_count, int) and split_count > 1:
        info.split_count = max(info.split_count, split_count)
        split_no = meta.get("split.no")
        if isinstance(split_no, int) and split_no > 0:
            info.split_no = split_no

    info.family = _family(f"{general_name} {p.name}")
    info.translation_specialized = info.family in ("Sakura", "GalTransl")
    info.auto_display_name = _display_name(info.name, info.quant)
    info.raw = {k: v for k, v in meta.items() if not k.startswith("_")}
    info.raw["_version"] = meta.get("_version")
    info.raw["_tensor_count"] = meta.get("_tensor_count")
    info.raw["_kv_count"] = meta.get("_kv_count")

    if not info.has_chat_template:
        info.warnings.append("没有 tokenizer.chat_template：模型可能无法按对话模板工作")
    if not info.translation_specialized:
        info.warnings.append("不是 Sakura/GalTransl 日中翻译特化模型：仍可尝试，但翻译质量可能不稳定")
    if not complete:
        info.warnings.append("metadata 未完整读取（超过读取上限或文件截断），部分信息来自文件名")

    info.ok = True
    return info


def _format_param_count(n: int) -> str:
    if n >= 1_000_000_000:
        val = n / 1_000_000_000
        return f"{val:.0f}B" if val >= 10 else f"{val:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M"
    return str(n)


def scan_gguf_files(directory: str | Path) -> list[dict[str, Any]]:
    """扫描目录里的 GGUF，分片模型只列第一片一次。"""
    d = Path(directory).expanduser()
    if not d.is_dir():
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.gguf"), key=lambda x: x.name.lower()):
        if not p.is_file():
            continue
        m = _SPLIT_RE.match(p.name)
        key = m.group("prefix") if m else p.name
        if key.lower() in seen:
            continue
        seen.add(key.lower())
        first = p
        if m:
            first, missing = find_split_first(p)
            if not first.is_file():
                first = p
        else:
            missing = []
        try:
            size = first.stat().st_size
        except OSError:
            size = 0
        out.append({
            "path": str(first),
            "file_name": first.name,
            "size_bytes": size,
            "size_gb": round(size / (1024 ** 3), 2),
            "split_count": int(m.group("count")) if m else 1,
            "split_missing": len(missing),
        })
    return out


def inspect_path(path: str | Path, *, budget: int = DEFAULT_BUDGET) -> dict[str, Any]:
    """给 UI 的统一入口：文件 → 识别；目录 → 候选列表。"""
    p = Path(path).expanduser()
    if p.is_dir():
        candidates = scan_gguf_files(p)
        if not candidates:
            return {"ok": False, "is_dir": True, "path": str(p), "error": "该文件夹里没有 .gguf 文件", "candidates": []}
        return {"ok": True, "is_dir": True, "path": str(p), "candidates": candidates}
    info = inspect_model(p, budget=budget)
    return {"ok": info.ok, "is_dir": False, "path": str(p), "model": info.as_dict(), "error": info.error}


def short_error(info: ModelInfo) -> str:
    return info.error or "；".join(info.warnings[:2]) or "识别完成"
