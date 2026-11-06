#!/usr/bin/env python3
"""任务归档：**整轮任务结束后**把工作区里的文件整体收进
output/<作品>/字幕文件/{日文,中文} + 音频/ + 台本/（LRC 归到「中文」，音频保持原格式）。

设计原则（避免"搬到一半"的状态）：
  - **按目录整体搬移**，不按文件名逐个匹配 → 不会因为文件名差异（假名合成/分解、重命名）漏搬。
  - 只在**整轮翻译结束后**做一次；条件不满足就什么都不搬（宁可不整理，也不留半套）。
  - 归档前提：该作品的每一轨都已有中文字幕（`.ja.txt` ↔ `.zh.txt` 一一对应）；缺轨则跳过并提示。
  - 只移动、不删除：音频、台本都能在作品包里找到。
  - 提取模式（只提取 / 只精修）不归档，方便继续加工。

作品包名：优先用**音频所在目录名**（input/wav/… → output/wav/），找不到则用日文字幕目录名。

命令行：
    .venv/bin/python scripts/bundle.py --dry-run     # 预览会搬什么
    .venv/bin/python scripts/bundle.py               # 归档全部作品
    .venv/bin/python scripts/bundle.py --work 耳朵   # 只归档指定作品（日文字幕目录名）
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import paths_util

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import translate as T  # noqa: E402

logger = logging.getLogger("bundle")

TRACK_RE = re.compile(r"^(?:track|tr|トラック|第)?[\s._-]*(\d{1,3})(?!\d)", re.I)
MEDIA_EXT = {".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".wma", ".mp4", ".mkv", ".mov", ".webm"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
# 系统垃圾文件（Finder/Windows 缓存），清空工作目录时顺手删掉，不算用户数据
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized", ".fseventsd"}


def show(path: Path) -> str:
    """给日志用的路径显示：项目内用相对路径，项目外（如成品库配到别的盘）原样显示。"""
    try:
        return str(Path(path).relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def is_junk(name: str) -> bool:
    return name in JUNK_NAMES or name.startswith("._") or name.startswith(".Spotlight")
DEFAULT_DIRS = {"subs": "字幕文件", "ja": "日文", "zh": "中文", "audio": "音频", "promptbook": "台本"}


def _norm(s: str) -> str:
    """NFKC 规范化：同一轨道的文件名可能混用合成/分解假名（ウィスパー vs ウィスパー）。"""
    return unicodedata.normalize("NFKC", s)


def track_number(name: str) -> str | None:
    m = TRACK_RE.match(Path(name).name)
    return m.group(1).zfill(2) if m else None


def subtitle_stem(name: str) -> str:
    return T.subtitle_stem(Path(name))


@dataclass
class Paths:
    input: Path
    work: Path          # 统一中间目录
    subtitles: Path     # work/日文字幕（提取输出 = 翻译输入）
    zh: Path            # work/中文字幕
    lrc: Path           # work/LRC
    promptbook: Path
    output: Path        # 最终作品包
    collect: Path


@dataclass
class BundleCfg:
    enabled: bool = True
    collect_lrc: bool = True
    move_audio: bool = True
    move_promptbook: bool = True
    prune_empty_dirs: bool = True
    remove_junk: bool = True
    dirs: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DIRS))


def load_paths(cfg: dict) -> Paths:
    p = paths_util.work_paths(cfg)
    return Paths(input=p["input"], work=p["work"], subtitles=p["ja"], zh=p["zh"], lrc=p["lrc"],
                 promptbook=p["promptbook"], output=p["output"], collect=p["collect"])


def load_cfg(cfg: dict) -> BundleCfg:
    b = cfg.get("bundle") or {}
    dirs = dict(DEFAULT_DIRS)
    dirs.update({k: str(v) for k, v in (b.get("dirs") or {}).items() if v})
    return BundleCfg(enabled=bool(b.get("consolidate", True)),
                     collect_lrc=bool(b.get("collect_lrc", True)),
                     move_audio=bool(b.get("move_audio", True)),
                     move_promptbook=bool(b.get("move_promptbook", True)),
                     prune_empty_dirs=bool(b.get("prune_empty_dirs", True)),
                     remove_junk=bool(b.get("remove_junk", True)), dirs=dirs)


# ------------------------------------------------------------------ 查询
FLAT = ""      # 扁平工作区：音频/字幕直接放在根目录下（没有作品子文件夹）


def _has_lines(f: Path) -> bool:
    """该字幕文件是否有带时间轴的可翻译行（纯文本/坏文件不算轨道）。"""
    try:
        import translate as T

        return bool(T.parse_subtitle(f))
    except Exception:  # noqa: BLE001
        return False


def work_dirs(paths: Paths) -> list[str]:
    """待归档的作品列表（有任意可翻译字幕的作品）。

    - 子文件夹布局：返回作品目录名（如 "RJ01012345"）
    - **扁平布局**（音频直接丢在 input/ 下，字幕也直接落在 中间文件/日文字幕/ 下）：返回 [""]
    """
    if not paths.subtitles.is_dir():
        return []
    out = [d.name for d in sorted(p for p in paths.subtitles.iterdir() if p.is_dir())
           if any(_has_lines(f) for f in paths_util.iter_subtitles(d))]
    if any(_has_lines(f) for f in paths_util.iter_subtitles(paths.subtitles, recursive=False)):
        out.append(FLAT)
    return out


def _iter_ja(paths: Paths, work: str) -> list[Path]:
    """该作品的字幕输入（.ja.txt / .srt / .lrc / 带时间戳的 .txt；不含精修版与中文产物）。"""
    root = paths.subtitles / work if work else paths.subtitles
    return paths_util.iter_subtitles(root)


def _iter_zh(paths: Paths, work: str) -> list[Path]:
    root = paths.zh / work if work else paths.zh
    return sorted(root.rglob("*.zh.txt")) if work else sorted(root.glob("*.zh.txt"))


def missing_translations(paths: Paths, work: str) -> list[str]:
    """该作品里"有日文字幕但没有中文字幕"的轨道（返回轨道名列表）。"""
    zh_stems = {_norm(f.name[: -len(".zh.txt")]) for f in _iter_zh(paths, work)}
    missing, seen = [], set()
    for sub in _iter_ja(paths, work):
        if not _has_lines(sub):        # 纯文本/坏文件不算"缺翻译的轨道"
            continue
        stem = paths_util.strip_subtitle_suffix(sub.name)
        if stem in seen:
            continue
        seen.add(stem)
        if _norm(stem) not in zh_stems:
            missing.append(stem)
    return missing


def find_media(paths: Paths, work: str) -> tuple[list[Path], str | None]:
    """找属于该作品的音频：全 input 递归，按"轨道号"或"轨道名"匹配。

    返回 (音频文件列表, 音频所在的作品目录名)。
    目录名只在音频确实位于某个子目录里时才有值（input/wav/*.wav → "wav"）；
    音频直接堆在 input/ 根下时返回 None（此时作品包名退回用日文字幕目录名）。
    """
    if not paths.input.is_dir():
        return [], None
    subs = _iter_ja(paths, work) + [
        f for f in ((paths.subtitles / work).rglob("*.ja.refined.txt") if work
                    else paths.subtitles.glob("*.ja.refined.txt"))]
    nums = {track_number(f.name) for f in subs} - {None}
    stems = {_norm(subtitle_stem(f.name)) for f in subs}

    # 1) 先按"轨道号/轨道名"找出所有候选音频
    cands = [f for f in sorted(paths.input.rglob("*"))
             if f.is_file() and f.suffix.lower() in MEDIA_EXT
             and ((track_number(f.name) in nums) or (_norm(f.stem) in stems))]
    if not cands:
        # 兜底：用户自带字幕的文件名可能和音频完全不同 → 按**作品目录名**（如 RJ 号）整目录取
        if work:
            d = paths.input / work
            if d.is_dir():
                media = [f for f in sorted(d.rglob("*"))
                         if f.is_file() and f.suffix.lower() in MEDIA_EXT]
                if media:
                    logger.info("字幕名与音频不匹配，按作品目录名匹配：input/%s/（%d 个音频）",
                                work, len(media))
                    return media, work
        return [], None

    # 2) 按"所在目录"分组择优，避免把别的批次/别的作品的同名音频顺手牵羊：
    #    目录名与该作品同名 > 子目录（优先浅层）> input 根（扁平布局）
    groups: dict[Path, list[Path]] = {}
    for f in cands:
        groups.setdefault(f.parent, []).append(f)

    def score(d: Path) -> tuple[int, int, int]:
        rel = d.relative_to(paths.input)
        name_hit = 1 if (work and rel.parts and _norm(rel.parts[0]) == _norm(work)) else 0
        is_sub = 0 if d == paths.input else 1
        return (name_hit, is_sub, -len(rel.parts))

    best = max(groups, key=score)
    if len(groups) > 1:
        logger.warning("input/ 下有 %d 个目录都含匹配音频，本次只取 %s（其余请人工确认）",
                       len(groups), show(best))
    files = groups[best]
    work_dir = best.relative_to(paths.input).parts[0] if best != paths.input else None
    return files, work_dir


def resolve_input_work(paths: Paths, work: str) -> str | None:
    """兼容旧调用：只取音频所在的作品目录名。"""
    return find_media(paths, work)[1]


def resolve_promptbook_work(paths: Paths, work: str) -> str | None:
    """找这批轨道对应的台本目录名：先同名，再按轨道号匹配最多者。"""
    if not paths.promptbook.is_dir():
        return None
    if (paths.promptbook / work).is_dir():
        return work
    nums = {track_number(f.name) for f in (paths.subtitles / work).rglob("*.ja.txt")} - {None}
    best, best_hits = None, 0
    for d in sorted(p for p in paths.promptbook.iterdir() if p.is_dir()):
        hits = len({track_number(f.name) for f in d.rglob("*.txt")} & nums)
        if hits > best_hits:
            best, best_hits = d.name, hits
    return best if best_hits else None


def bundle_dirs(paths: Paths, bcfg: BundleCfg, work: str) -> dict[str, Path]:
    """作品包目录表：

        <作品>/
          字幕文件/{日文,中文}/     ← LRC 归到「中文」
          音频/                     ← 原始音频（wav/mp3 原样）+ 封面
          台本/                     ← 有台本才建
    """
    root = paths.output / work
    subs = root / bcfg.dirs["subs"]
    return {
        "root": root,
        "subs": subs,
        "ja": subs / bcfg.dirs["ja"],
        "zh": subs / bcfg.dirs["zh"],
        "audio": root / bcfg.dirs["audio"],
        "promptbook": root / bcfg.dirs["promptbook"],
    }


def bundle_of_input(cfg: dict, path: Path) -> tuple[str, dict[str, Path]] | None:
    """输入文件若已在某个作品包里，返回 (作品名, 目录表)。

    用于"重跑已归档的任务"：从作品包读日文字幕，产物写回同一个作品包，不再二次搬移。
    """
    paths = load_paths(cfg)
    bcfg = load_cfg(cfg)
    try:
        rel = path.resolve().relative_to(paths.output.resolve())
    except ValueError:
        return None
    if not rel.parts:
        return None
    work = rel.parts[0]
    dirs = bundle_dirs(paths, bcfg, work)
    if dirs["ja"] in path.resolve().parents or dirs["zh"] in path.resolve().parents:
        return work, dirs
    return None


# ------------------------------------------------------------------ 归档
def _move_dir_contents(src: Path, dst: Path, *, dry_run: bool, moves: list[tuple[Path, Path]],
                       exclude_dirs: list[Path] | None = None, roots_only: bool = False) -> int:
    """把 src 里的文件（含子目录）整体搬到 dst，保留内部结构。

    exclude_dirs: 作品包内的各个子目录。output/<作品> 既可能是 zh 的源目录、又可能是作品包根，
    所以要跳过"已经躺在作品包子目录里的文件"，但根目录下的散文件仍要搬
    （否则会出现 zh/ja/… 这种套娃；排除整个作品包根则会把该搬的 zh 也漏掉）。
    """
    if not src.is_dir():
        return 0
    n = 0
    items = [f for f in sorted(src.iterdir()) if f.is_file()] if roots_only else sorted(src.rglob("*"))
    for f in items:
        if not f.is_file() or f.name == ".DS_Store":
            continue
            continue
        if exclude_dirs and any(d in f.parents for d in exclude_dirs):
            continue
        target = dst / f.relative_to(src)
        moves.append((f, target))
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.unlink()
            shutil.move(str(f), str(target))
        n += 1
    return n


def _free_generic_name(paths: Paths, bcfg: BundleCfg, base: str, media: list[Path]) -> str:
    """兜底作品名防撞：已有的同名作品包里若装的是**另一批**音频，就改用 作品_2、作品_3…"""
    names = {m.name for m in media}
    n = 1
    while True:
        cand = base if n == 1 else f"{base}_{n}"
        root = paths.output / cand
        if not root.is_dir():
            return cand
        audio_dir = root / bcfg.dirs["audio"]
        existing = {f.name for f in audio_dir.glob("*")} if audio_dir.is_dir() else \
            {f.name for f in root.glob("*.wav")}
        if not existing or (existing & names):
            return cand           # 空包 / 就是这批音频 → 沿用（重跑场景）
        n += 1


def _move_files(files: list[Path], dst: Path, *, dry_run: bool, moves: list[tuple[Path, Path]],
                exclude_dirs: list[Path] | None = None) -> int:
    """把指定文件搬进 dst（扁平放入，不保留原目录结构）。"""
    n = 0
    for f in files:
        if exclude_dirs and any(d in f.parents for d in exclude_dirs):
            continue
        target = dst / f.name
        if f.resolve() == target.resolve():
            continue
        moves.append((f, target))
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.unlink()
            shutil.move(str(f), str(target))
        n += 1
    return n


def _move_extras(src_dir: Path, media: list[Path], dst: Path, *, dry_run: bool,
                 moves: list[tuple[Path, Path]], exclude_dirs: list[Path] | None = None,
                 images_only: bool = False) -> int:
    """把作品目录里的非音频杂项（cover.jpg 等封面/图片）搬进 dst；音频已由 _move_files 处理。

    images_only=True 用于**扁平布局**：只搬图片，避免把 input/ 根下与作品无关的说明文件也卷走。
    """
    if not src_dir.is_dir():
        return 0
    media_set = {m.resolve() for m in media}
    n = 0
    for f in sorted(src_dir.rglob("*")):
        if not f.is_file() or is_junk(f.name) or f.resolve() in media_set:
            continue
        ext = f.suffix.lower()
        if ext in MEDIA_EXT:
            continue
        if images_only and ext not in IMAGE_EXT:
            continue
        target = dst / f.relative_to(src_dir)          # 保留子目录结构（如 特典/readme.txt）
        if exclude_dirs and any(d in f.parents for d in exclude_dirs):
            continue
        moves.append((f, target))
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.unlink()
            shutil.move(str(f), str(target))
        n += 1
    return n


def collect_lrc(cfg: dict, work: str, *, name: str | None = None, dry_run: bool = False) -> dict:
    """把该作品的中文 LRC **复制**到 <collect_dir>/<作品>/（只写入，不取出、不删除）。

    作品名默认用 input 下的文件夹名（如 RJ01012345）。
    来源优先作品包里的 字幕文件/中文/*.lrc，其次工作区 中间文件/LRC/<作品>/*.lrc。
    这个目录只增不减：同名文件覆盖为最新版本，其它一律不动。
    """
    paths = load_paths(cfg)
    bcfg = load_cfg(cfg)
    if not bcfg.collect_lrc:
        return {"status": "disabled"}
    if not name:
        media, in_work = find_media(paths, work)
        name = in_work or work or "作品"
    srcs: list[Path] = []
    zh_dir = bundle_dirs(paths, bcfg, name)["zh"]
    if zh_dir.is_dir():
        srcs += sorted(zh_dir.glob("*.lrc"))
    if not srcs and work:
        d = paths.lrc / work
        if d.is_dir():
            srcs += sorted(d.rglob("*.lrc"))
    if not srcs:
        return {"status": "empty", "name": name}
    dst = paths.collect / name
    copied = 0
    for f in srcs:
        try:
            if dry_run:
                logger.info("[dry-run] 入库 %s → %s", f.name, show(dst / f.name))
            else:
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst / f.name)
            copied += 1
        except OSError as exc:
            logger.warning("LRC 入库失败：%s（%s）", f.name, exc)
    if copied:
        if dry_run:
            logger.info("[dry-run] 将入库 %d 个 LRC → %s/", copied, show(dst))
        else:
            logger.info("  LRC 已入库 %d 个 → %s/（只写入，源文件不动）", copied,
                        show(dst))
    return {"status": "done", "name": name, "files": copied}


def consolidate_work(cfg: dict, work: str, *, dry_run: bool = False) -> dict:
    """把一个作品的整套文件收进 output/<作品包>/{ja,promptbook,zh,lrc,audio}/。

    work: 日文字幕根下的作品目录名（如 耳朵）；作品包名优先取 input 子目录名（如 wav）。
    """
    paths = load_paths(cfg)
    bcfg = load_cfg(cfg)
    if not bcfg.enabled:
        return {"status": "disabled"}

    miss = missing_translations(paths, work)
    if miss:
        logger.warning("作品 %r 还有 %d 轨没有中文字幕（%s）→ 本轮不归档，文件留在工作区，补完再跑",
                       work, len(miss), "、".join(sorted(track_number(m) or m for m in miss)[:6]))
        return {"status": "incomplete", "work": work, "missing": miss}

    media, in_work = find_media(paths, work)
    pb_work = resolve_promptbook_work(paths, work)
    bundle = in_work or (work if work else (pb_work or "作品"))
    if not work and not in_work and not pb_work:
        bundle = _free_generic_name(paths, bcfg, bundle, media)
        logger.warning("扁平批次无法推断作品名 → 作品包用 %r（想改名直接重命名该文件夹；"
                       "把音频放进 input/<作品名>/ 可获得更好的名字）", bundle)
    dst = bundle_dirs(paths, bcfg, bundle)
    logger.info("归档作品 %r → 作品包 %r（音频 %d 个%s）", work or "（扁平工作区）", bundle, len(media),
                "" if in_work else "，未在 input 子目录中定位到音频所在目录")

    moves: list[tuple[Path, Path]] = []
    counts: dict[str, int] = {}
    _sub_dirs = lambda: [dst["ja"], dst["zh"], dst["audio"], dst["promptbook"]]  # noqa: E731
    flat = not work
    # 顺序要紧：zh 的源目录可能就是作品包根（output/<作品>），必须先搬，
    # 并排除作品包内的内容，避免把刚搬进去的 ja/ 再套进 zh/ja/。
    # 扁平模式只搬根目录下的文件（roots_only），不会卷走别的作品子目录。
    counts["zh"] = _move_dir_contents(paths.zh / work, dst["zh"], dry_run=dry_run, moves=moves,
                                      exclude_dirs=_sub_dirs(), roots_only=flat)
    # 工作区里若混进了中文产物/LRC，先归到「中文」，别让它们进「日文」
    for pat in ("*.zh.txt", "*.zh.cache.json", "*.zh.meta.json", "*.lrc"):
        for stray in sorted((paths.subtitles / work).rglob(pat)) if work else []:
            counts["zh"] += _move_files([stray], dst["zh"], dry_run=dry_run, moves=moves, exclude_dirs=_sub_dirs())
    counts["ja"] = _move_dir_contents(paths.subtitles / work, dst["ja"], dry_run=dry_run, moves=moves,
                                      exclude_dirs=_sub_dirs(), roots_only=flat)
    # LRC 也是中文字幕文件 → 进「中文」
    counts["lrc"] = _move_dir_contents(paths.lrc / work, dst["zh"], dry_run=dry_run, moves=moves,
                                       exclude_dirs=_sub_dirs(), roots_only=flat)
    if bcfg.move_promptbook and pb_work:
        counts["promptbook"] = _move_dir_contents(paths.promptbook / pb_work, dst["promptbook"],
                                                  dry_run=dry_run, moves=moves, exclude_dirs=_sub_dirs())
    if bcfg.move_audio:
        if in_work:
            # 整个作品文件夹一起搬走（音频、封面、特典、甚至没提取字幕的轨道都算），
            # 保证 input/ 里不留文件；音频保持原格式，不转码。
            counts["audio"] = _move_dir_contents(paths.input / in_work, dst["audio"],
                                                 dry_run=dry_run, moves=moves, exclude_dirs=_sub_dirs())
        else:
            counts["audio"] = _move_files(media, dst["audio"], dry_run=dry_run, moves=moves,
                                          exclude_dirs=_sub_dirs())
            if flat:
                # 扁平布局（音频直接堆在 input/ 根下）：只搬匹配到的音频与图片，说明类文件不动
                counts["audio"] += _move_extras(paths.input, media, dst["audio"], dry_run=dry_run,
                                                moves=moves, exclude_dirs=_sub_dirs(), images_only=True)
        if not media and not in_work:
            logger.warning("⚠️ input/ 下没找到属于作品 %r 的音频（该作品轨道号 %s）→ 音频不会被搬动，请人工确认",
                           work, "、".join(sorted((track_number(f.name) or "?")
                                                  for f in (paths.subtitles / work).rglob("*.ja.txt"))[:8]))

    if dry_run:
        for src, target in moves:
            logger.info("[dry-run] %s → %s", show(src), show(target))
        return {"status": "dry-run", "work": work, "bundle": bundle, "files": len(moves)}

    for d in filter(None, (paths.subtitles / work if work else None,
                           paths.zh / work if work else None,
                           paths.lrc / work if work else None,
                           paths.promptbook / pb_work if pb_work else None,
                           paths.input / in_work if in_work else None)):
        if not d.is_dir():
            continue
        # 作品包本身不能当"待清空的源目录"：作品名与作品包名相同时 output/<作品> 就是作品包
        if d == dst["root"] or dst["root"] in d.parents:
            continue
        if not _prune_empty(d, remove_junk=bcfg.remove_junk):
            rest = [str(f.relative_to(d)) for f in sorted(d.rglob("*")) if f.is_file()][:5]
            logger.warning("⚠️ 目录未能清空：%s（还剩下 %s）——请人工确认这些文件是否要保留",
                           show(d), "、".join(rest) or "空目录")
    logger.info("已归档作品 %r → %s/（%s）", work, show(paths.output / bundle),
                "、".join(f"{k} {v}" for k, v in counts.items() if v))
    return {"status": "done", "work": work, "bundle": bundle, "counts": counts}


def _prune_empty(root: Path, *, remove_junk: bool = True) -> bool:
    """清空并删除目录（含子目录）。返回 True 表示目录已被删掉。

    先删 `.DS_Store` 这类系统垃圾文件（否则目录永远"非空"、删不掉——实测就是这个卡住的），
    再自下而上删空目录。
    """
    if not root.is_dir():
        return True
    if remove_junk:
        for f in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
            if f.is_file() and is_junk(f.name):
                try:
                    f.unlink()
                except OSError:
                    pass
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    try:
        if root.is_dir() and not any(root.iterdir()):
            root.rmdir()
    except OSError:
        pass
    return not root.is_dir()


def consolidate_all(cfg: dict, *, dry_run: bool = False, only: str | None = None) -> list[dict]:
    paths = load_paths(cfg)
    works = work_dirs(paths)
    if only:
        works = [w for w in works if w == only]
        if not works and (paths.subtitles / only).is_dir():
            works = [only]
    return [consolidate_work(cfg, w, dry_run=dry_run) for w in works]


# ------------------------------------------------------------------ CLI
def main() -> int:
    p = argparse.ArgumentParser(description="任务结束后归档到 output/<作品>/{ja,promptbook,zh,lrc,audio}/")
    p.add_argument("--work", default=None, help="只归档这个作品（日文字幕目录名，如 耳朵）")
    p.add_argument("--dry-run", action="store_true", help="只打印将要搬动的文件")
    p.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    cfg = T.load_config(Path(args.config))
    res = consolidate_all(cfg, dry_run=args.dry_run, only=args.work)
    if not res:
        logger.warning("工作区没有可归档的作品（%s 下没有 .ja.txt）", load_paths(cfg).subtitles)
        return 0
    logger.info("归档结束：%d 个作品%s", len(res), "（dry-run）" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
