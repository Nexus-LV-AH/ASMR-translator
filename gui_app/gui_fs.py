#!/usr/bin/env python3
"""图形界面的文件层：路径解析、作品名、上传暂存、落盘、结果扫描。

和 CLI 共用同一套 config.yaml / paths_util，所以 GUI 看到的目录与
extract_ja.py / translate.py / bundle.py 完全一致；不另造一套目录。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import paths_util  # noqa: E402
from audio_io import SUPPORTED_EXTS as AUDIO_EXTS  # noqa: E402

MODEL_EXTS = {".gguf", ".safetensors", ".bin", ".pt", ".onnx"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
PROMPTBOOK_EXTS = {".txt"}
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
WORK_NAME_MAX = 80
COMPONENT_MAX = 180
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_INVALID_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RJ_RE = re.compile(r"(?i)\bRJ\d{5,}\b")
_UPLOAD_KINDS = ("audio", "subtitle", "promptbook")


# ---------------------------------------------------------------- 路径
@dataclass
class GuiPaths:
    root: Path
    config: Path
    input: Path
    work: Path
    ja: Path
    zh: Path
    lrc: Path
    output: Path
    collect: Path
    promptbook: Path
    gui: Path
    staging: Path
    logs: Path
    runtime: Path

    def ensure(self) -> None:
        for d in (self.input, self.work, self.ja, self.zh, self.lrc, self.output,
                  self.collect, self.promptbook, self.gui, self.staging, self.logs, self.runtime):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def data_roots(self) -> list[Path]:
        """可预览/可打开的根目录（不含项目根，避免预览 .env）。"""
        return [self.input, self.work, self.output, self.collect, self.promptbook]

    def as_dict(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "config": str(self.config),
            "input": str(self.input),
            "work": str(self.work),
            "ja": str(self.ja),
            "zh": str(self.zh),
            "lrc": str(self.lrc),
            "output": str(self.output),
            "collect": str(self.collect),
            "promptbook": str(self.promptbook),
            "gui": str(self.gui),
            "logs": str(self.logs),
            "runtime": str(self.runtime),
        }


def load_config(root: str | Path, config_path: str | Path | None = None) -> tuple[dict, Path]:
    """读 config.yaml（会合并 config.local.yaml）。"""
    import translate as T

    root = Path(root).resolve()
    cfg_path = Path(config_path).expanduser() if config_path else root / "config.yaml"
    if not cfg_path.is_absolute():
        cfg_path = (root / cfg_path).resolve()
    cfg = T.load_config(cfg_path)
    return cfg, cfg_path


def _resolve_under(root: Path, raw, default: str) -> Path:
    p = Path(str(raw or default)).expanduser()
    return p if p.is_absolute() else (root / p)


def build_paths(root: str | Path, cfg: dict, config_path: str | Path | None = None) -> GuiPaths:
    """按 GUI 根目录解析 config.paths；相对路径与 CLI 一样相对项目根。

    不复用 paths_util.work_paths() 是因为它绑定到 paths_util 模块自己的
    PROJECT_ROOT；这样 --config 指向别的目录或测试用临时根时不会串到真实项目。
    """
    root = Path(root).resolve()
    pc = cfg.get("paths") or {}
    work = _resolve_under(root, pc.get("work_dir"), "中间文件")
    wd = dict(paths_util.DEFAULT_WORK_DIRS)
    wd.update({k: str(v) for k, v in (pc.get("work_dirs") or {}).items() if v})
    cfg_path = Path(config_path).expanduser() if config_path else root / "config.yaml"
    if not cfg_path.is_absolute():
        cfg_path = (root / cfg_path).resolve()
    return GuiPaths(
        root=root,
        config=cfg_path,
        input=_resolve_under(root, pc.get("input_dir"), "input"),
        work=work,
        ja=work / wd["ja"],
        zh=work / wd["zh"],
        lrc=work / wd["lrc"],
        output=_resolve_under(root, pc.get("output_dir"), "output"),
        collect=_resolve_under(root, pc.get("collect_dir"), "LRC成品库"),
        promptbook=_resolve_under(root, pc.get("promptbook_dir"), "promptbook"),
        gui=work / "图形界面",
        staging=work / "图形界面" / "上传暂存",
        logs=work / "图形界面" / "日志",
        runtime=root / "runtime",
    )


def load_paths(root: str | Path, config_path: str | Path | None = None) -> tuple[GuiPaths, dict]:
    cfg, cfg_path = load_config(root, config_path)
    paths = build_paths(root, cfg, cfg_path)
    paths.ensure()
    return paths, cfg


# ---------------------------------------------------------------- JSON / 小工具
def read_json(path: str | Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: str | Path, data: Any, *, private: bool = False) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if private:
        from gui_platform import chmod_private
        chmod_private(tmp)
    os.replace(tmp, p)
    if private:
        from gui_platform import chmod_private
        chmod_private(p)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def human_size(n: int | float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def is_junk(name: str) -> bool:
    low = name.lower()
    return low in JUNK_NAMES or low.startswith("._") or low.endswith((".tmp", ".part"))


def is_audio(name: str | Path) -> bool:
    return Path(str(name)).suffix.lower() in AUDIO_EXTS


def is_model_file(name: str | Path) -> bool:
    return Path(str(name)).suffix.lower() in MODEL_EXTS


def is_image(name: str | Path) -> bool:
    return Path(str(name)).suffix.lower() in IMAGE_EXTS


def is_subtitle_file(name: str | Path) -> bool:
    return paths_util.is_subtitle_input(Path(str(name)))


def is_promptbook_file(name: str | Path) -> bool:
    return Path(str(name)).suffix.lower() in PROMPTBOOK_EXTS


# ---------------------------------------------------------------- 名称清洗
def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _is_reserved(name: str) -> bool:
    stem = name.split(".", 1)[0].strip().upper()
    return stem in WIN_RESERVED


def sanitize_work_name(raw: str) -> str:
    """作品名：跨平台安全（按 Windows 规则），保留中文/日文/空格。"""
    s = _nfc(str(raw or "")).strip()
    if not s:
        return ""
    s = _INVALID_NAME_RE.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    s = s.rstrip(". ")
    if s in {".", ".."} or not s:
        return ""
    if _is_reserved(s):
        return ""
    if len(s) > WORK_NAME_MAX:
        s = s[:WORK_NAME_MAX].rstrip(" .")
    return s


def validate_work_name(raw: str, paths: GuiPaths) -> tuple[str, str]:
    """返回 (清洗后的作品名, 错误)。"""
    name = sanitize_work_name(raw)
    if not name:
        return "", "作品名不能为空、不能是 Windows 保留名，也不能包含路径分隔符/非法字符"
    if os.name == "nt":
        for base in (paths.input / name, paths.output / name):
            if len(str(base)) > 235:
                return "", "作品名太长，Windows 路径会超过限制；请换一个短一点的名字"
    return name, ""


def detect_work_hint(file_names: Iterable[str], top_folder: str = "") -> str:
    """从文件夹名/文件名推断作品名；返回空串表示需要用户自己填。"""
    top = sanitize_work_name(top_folder)
    if top:
        return top
    found: list[str] = []
    for name in file_names:
        for m in _RJ_RE.finditer(Path(str(name)).name):
            rj = m.group(0).upper()
            if rj not in found:
                found.append(rj)
    return sanitize_work_name(found[0]) if len(found) == 1 else ""


def default_work_name() -> str:
    return f"作品_{datetime.now().strftime('%Y%m%d_%H%M')}"


def sanitize_component(raw: str) -> tuple[str, str]:
    """单个文件名/相对路径段：清洗；返回 (name, error)。"""
    s = _nfc(str(raw or "")).strip()
    if not s or s in {".", ".."}:
        return "", "文件名不能为空"
    if s.startswith("."):
        return "", "隐藏文件不会被处理"
    s = _INVALID_NAME_RE.sub("_", s)
    s = s.rstrip(" .")
    if not s:
        return "", "清洗后文件名为空"
    if _is_reserved(s):
        s = "_" + s
    if len(s) > COMPONENT_MAX:
        p = Path(s)
        stem, ext = p.stem, p.suffix
        keep = max(1, COMPONENT_MAX - len(ext))
        s = stem[:keep] + ext
    return s, ""


def split_relpath(raw: str) -> tuple[list[str], str]:
    """把上传相对路径拆成清洗后的段；返回 (parts, error)。"""
    text = str(raw or "").replace("\\", "/").strip()
    if not text:
        return [], "缺少文件相对路径"
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return [], "不允许绝对路径"
    parts: list[str] = []
    for seg in text.split("/"):
        seg = seg.strip()
        if not seg or seg == ".":
            continue
        if seg == "..":
            return [], "不允许 .. 路径"
        name, err = sanitize_component(seg)
        if err:
            return [], err
        parts.append(name)
    if not parts:
        return [], "缺少文件名"
    return parts, ""


def safe_join(base: Path, parts: Iterable[str]) -> Path:
    """把清洗后的段拼到 base 下，并确认 resolve 后仍在 base 内。"""
    base = Path(base).resolve()
    target = base.joinpath(*[str(p) for p in parts])
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target.absolute()
    try:
        if resolved != base and base not in resolved.parents:
            raise ValueError("路径越界")
    except ValueError:
        raise
    return target


# ---------------------------------------------------------------- 暂存
def manifest_path(paths: GuiPaths) -> Path:
    return paths.staging / "manifest.json"


def read_manifest(paths: GuiPaths) -> dict[str, Any]:
    data = read_json(manifest_path(paths), {})
    return data if isinstance(data, dict) else {}


def write_manifest(paths: GuiPaths, *, work_hint: str = "") -> None:
    cur = read_manifest(paths)
    hint = sanitize_work_name(work_hint or cur.get("work_hint") or "")
    data = {
        "work_hint": hint,
        "created_at": cur.get("created_at") or now_iso(),
        "updated_at": now_iso(),
    }
    write_json(manifest_path(paths), data)


def clear_staged(paths: GuiPaths, kind: str | None = None) -> None:
    if kind and kind in _UPLOAD_KINDS:
        shutil.rmtree(paths.staging / kind, ignore_errors=True)
    else:
        shutil.rmtree(paths.staging, ignore_errors=True)
        paths.staging.mkdir(parents=True, exist_ok=True)
        # manifest 一起清掉
        return
    # 某个 kind 清空后，若全空则清 manifest
    left = any(any((paths.staging / k).rglob("*")) for k in _UPLOAD_KINDS)
    if not left:
        try:
            manifest_path(paths).unlink()
        except OSError:
            pass


def list_staged(paths: GuiPaths) -> dict[str, Any]:
    files: dict[str, list[dict[str, Any]]] = {k: [] for k in _UPLOAD_KINDS}
    total = 0
    for kind in _UPLOAD_KINDS:
        root = paths.staging / kind
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or is_junk(p.name):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            rel = p.relative_to(root).as_posix()
            files[kind].append({
                "name": p.name,
                "relpath": rel,
                "size": size,
                "kind": kind,
                "is_audio": kind == "audio" and is_audio(p.name),
                "is_image": kind == "audio" and is_image(p.name),
                "is_subtitle": kind == "subtitle" and is_subtitle_file(p.name),
            })
            total += size
    return {
        "files": files,
        "audio": files["audio"],
        "subtitle": files["subtitle"],
        "promptbook": files["promptbook"],
        "total_size": total,
        "work_hint": read_manifest(paths).get("work_hint", ""),
        "has_audio": any(f["is_audio"] for f in files["audio"]),
        "has_subtitle": bool(files["subtitle"]),
        "has_promptbook": bool(files["promptbook"]),
    }


def save_upload(paths: GuiPaths, kind: str, relpath: str, stream, length: int,
                work_hint: str = "") -> dict[str, Any]:
    """把 HTTP 请求体流式写入暂存；返回保存信息。抛 ValueError 给上层转 4xx。"""
    if kind not in _UPLOAD_KINDS:
        raise ValueError("未知的上传类型")
    parts, err = split_relpath(relpath)
    if err:
        raise ValueError(err)
    suffix = Path(parts[-1]).suffix.lower()
    if kind == "audio" and suffix in MODEL_EXTS:
        raise ValueError("模型文件请到「设置 → 本地模型」里选择，不要通过音频上传")
    if kind == "subtitle" and not is_subtitle_file(parts[-1]):
        raise ValueError("字幕只接受 .srt / .lrc / .txt / .ja.txt / .ja.refined.txt（不接受 .zh.txt、缓存、json）")
    if kind == "promptbook" and not is_promptbook_file(parts[-1]):
        raise ValueError("台本只接受 .txt")
    root = (paths.staging / kind).resolve()
    target = safe_join(root, parts)
    if target.exists() and target.is_dir():
        raise ValueError("目标已存在同名目录")
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + f".{os.getpid()}.part")
    written = 0
    try:
        with part.open("wb") as fh:
            while written < length:
                chunk = stream.read(min(1024 * 1024, length - written))
                if not chunk:
                    raise ValueError("上传中断：请求体不完整")
                fh.write(chunk)
                written += len(chunk)
        os.replace(part, target)
    except Exception:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    try:
        write_manifest(paths, work_hint=work_hint)
    except OSError:
        pass
    return {
        "name": target.name,
        "relpath": target.relative_to(root).as_posix(),
        "kind": kind,
        "size": written,
    }


def cleanup_stale_staging(paths: GuiPaths, *, hours: int = 24) -> int:
    """启动时清理过期的暂存/残留 .part。返回删除的条目数。"""
    cutoff = time.time() - hours * 3600
    n = 0
    if not paths.staging.is_dir():
        return 0
    for p in sorted(paths.staging.rglob("*")):
        try:
            if p.is_file() and (p.name.endswith(".part") or p.stat().st_mtime < cutoff):
                p.unlink()
                n += 1
        except OSError:
            pass
    # 空目录
    for d in sorted((p for p in paths.staging.rglob("*") if p.is_dir()), reverse=True):
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    if not any(paths.staging.rglob("*")):
        try:
            manifest_path(paths).unlink()
        except OSError:
            pass
    return n


# ---------------------------------------------------------------- 落盘
def existing_counts(paths: GuiPaths, work: str) -> dict[str, Any]:
    audio: list[str] = []
    subs: list[str] = []
    pbs: list[str] = []
    root = paths.input / work
    if root.is_dir():
        audio = [p.name for p in root.rglob("*") if p.is_file() and is_audio(p.name)]
    ja_root = paths.ja / work
    if ja_root.is_dir():
        subs = [p.name for p in ja_root.rglob("*") if p.is_file() and is_subtitle_file(p.name)]
    pb_root = paths.promptbook / work
    if pb_root.is_dir():
        pbs = [p.name for p in pb_root.rglob("*") if p.is_file() and is_promptbook_file(p.name)]
    return {
        "audio": len(audio),
        "audio_names": audio[:20],
        "subtitle": len(subs),
        "subtitle_names": subs[:20],
        "promptbook": len(pbs),
        "input_exists": root.is_dir(),
        "output_exists": (paths.output / work).is_dir(),
        "collect_exists": (paths.collect / work).is_dir(),
    }


def validate_staged_for_mode(staged: dict[str, Any], mode: str, *, use_existing: bool,
                             existing: dict[str, Any]) -> tuple[list[str], str]:
    """返回要搬移的 kinds 和错误。"""
    kinds: list[str] = []
    has_audio = bool(staged.get("has_audio") or existing.get("audio"))
    has_sub = bool(staged.get("has_subtitle") or existing.get("subtitle"))
    if mode in ("extract", "all"):
        if staged.get("has_subtitle"):
            return [], "暂存里有字幕文件；当前是音频模式。请清空字幕，或切换到「制作翻译」"
        if not has_audio:
            return [], "没有音频文件：请拖入作品文件夹或音频"
        kinds.append("audio")
        if staged.get("has_promptbook"):
            kinds.append("promptbook")
    elif mode == "translate":
        if staged.get("files", {}).get("audio") or staged.get("has_promptbook"):
            return [], "制作翻译模式不需要音频/台本；请清空它们，或切换到「完整运行」"
        if not has_sub:
            return [], "没有字幕文件：请拖入 .srt / .lrc / .txt（带时间轴），或先提取字幕"
        kinds.append("subtitle")
    else:
        return [], f"未知任务类型：{mode}"
    if not use_existing and not any(staged.get("files", {}).get(k) for k in kinds):
        return [], "暂存是空的"
    return kinds, ""


def _move_tree(src: Path, dst: Path) -> int:
    if not src.is_dir():
        return 0
    n = 0
    for p in sorted(src.rglob("*")):
        if not p.is_file() or is_junk(p.name):
            continue
        rel = p.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        shutil.move(str(p), str(target))
        n += 1
    shutil.rmtree(src, ignore_errors=True)
    return n


def move_staged_to_workspace(paths: GuiPaths, work: str, kinds: list[str]) -> dict[str, int]:
    """把暂存搬到 CLI 期望的位置：input/<作品>、日文字幕/<作品>、promptbook/<作品>。"""
    targets = {
        "audio": paths.input / work,
        "subtitle": paths.ja / work,
        "promptbook": paths.promptbook / work,
    }
    counts: dict[str, int] = {}
    for kind in kinds:
        if kind not in targets:
            continue
        dst = targets[kind]
        dst.mkdir(parents=True, exist_ok=True)
        counts[kind] = _move_tree(paths.staging / kind, dst)
    # 清掉空暂存
    if not any(paths.staging.rglob("*")):
        try:
            manifest_path(paths).unlink()
        except OSError:
            pass
    return counts


# ---------------------------------------------------------------- 结果扫描
def _bundle_dirs(cfg: dict, work: str) -> dict[str, Path] | None:
    try:
        import bundle as B

        bpaths = B.load_paths(cfg)
        bcfg = B.load_cfg(cfg)
        return B.bundle_dirs(bpaths, bcfg, work)
    except Exception:  # noqa: BLE001
        return None


def _missing_translations(cfg: dict, work: str) -> list[str]:
    try:
        import bundle as B

        bpaths = B.load_paths(cfg)
        return B.missing_translations(bpaths, work)
    except Exception:  # noqa: BLE001
        return []


def _iter_files(root: Path | None, pattern: str) -> list[Path]:
    if not root or not root.is_dir():
        return []
    return sorted(p for p in root.rglob(pattern) if p.is_file())


def scan_results(paths: GuiPaths, cfg: dict, work: str, mode: str) -> dict[str, Any]:
    """扫描作品包 / LRC 成品库 / 工作区，给结果卡片和 ZIP 下载用。"""
    bundle = _bundle_dirs(cfg, work)
    bundle_root = bundle["root"] if bundle else paths.output / work
    collect_root = paths.collect / work
    ja_dir = bundle["ja"] if bundle else paths.ja / work
    zh_dir = bundle["zh"] if bundle else paths.zh / work
    lrc_dir = bundle["zh"] if bundle else paths.lrc / work

    ja_files = sorted([p for p in _iter_files(ja_dir, "*.txt") if p.name.endswith((".ja.txt", ".ja.refined.txt"))])
    zh_txt = _iter_files(zh_dir, "*.zh.txt")
    lrc_bundle = _iter_files(lrc_dir, "*.lrc")
    lrc_collect = _iter_files(collect_root, "*.lrc")
    lrc_workspace = _iter_files(paths.lrc / work, "*.lrc")
    # LRC 去重：优先作品包，其次成品库，最后工作区
    lrc_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, files in (("bundle", lrc_bundle), ("collect", lrc_collect), ("workspace", lrc_workspace)):
        for p in files:
            name = p.name
            if name in seen:
                continue
            seen.add(name)
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            lrc_items.append({"name": name, "path": str(p), "size": size, "source": source})
    missing = _missing_translations(cfg, work) if mode in ("all", "translate") else []
    total_lrc = len(lrc_bundle) + len(lrc_collect) + len(lrc_workspace)
    incomplete = bool(mode in ("all", "translate") and missing)
    return {
        "work": work,
        "mode": mode,
        "bundle": str(bundle_root) if bundle_root.is_dir() else "",
        "collect": str(collect_root) if collect_root.is_dir() else "",
        "workspace": {
            "ja": str(paths.ja / work) if (paths.ja / work).is_dir() else "",
            "zh": str(paths.zh / work) if (paths.zh / work).is_dir() else "",
            "lrc": str(paths.lrc / work) if (paths.lrc / work).is_dir() else "",
        },
        "lrc": lrc_items,
        "zh_txt": [{"name": p.name, "path": str(p), "size": p.stat().st_size} for p in zh_txt],
        "ja": [{"name": p.name, "path": str(p), "size": p.stat().st_size} for p in ja_files],
        "counts": {"lrc": total_lrc, "zh_txt": len(zh_txt), "ja": len(ja_files)},
        "missing": missing,
        "incomplete": incomplete,
        "has_bundle": bundle_root.is_dir(),
        "has_collect": collect_root.is_dir(),
    }


def zip_lrcs(result: dict[str, Any]) -> bytes | None:
    """把扫描到的 LRC 打包成 ZIP（只收 LRC）。"""
    items = result.get("lrc") or []
    if not items:
        return None
    buf = BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            p = Path(str(item.get("path") or ""))
            if not p.is_file():
                continue
            name = p.name
            base = name
            i = 2
            while name in used:
                stem, ext = os.path.splitext(base)
                name = f"{stem}_{i}{ext}"
                i += 1
            used.add(name)
            try:
                zf.write(p, arcname=name)
            except OSError:
                continue
    if not used:
        return None
    return buf.getvalue()


def preview_text(path: str | Path, roots: Iterable[Path], *, max_bytes: int = 200 * 1024) -> dict[str, Any]:
    """安全预览文本文件；只允许文本后缀与根目录白名单。"""
    from gui_platform import is_within

    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        return {"ok": False, "error": "文件不存在"}
    if p.suffix.lower() not in {".lrc", ".txt", ".srt", ".log", ".md", ".json"}:
        return {"ok": False, "error": "这个文件不支持预览"}
    if not any(is_within(p, root) for root in roots):
        return {"ok": False, "error": "路径不在允许的目录内"}
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    truncated = len(data) > max_bytes or size > max_bytes
    text = data[:max_bytes].decode("utf-8", errors="replace")
    return {"ok": True, "name": p.name, "path": str(p), "text": text, "truncated": truncated, "size": size}
