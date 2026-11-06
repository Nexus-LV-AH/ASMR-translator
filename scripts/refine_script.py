#!/usr/bin/env python3
"""台本精修字幕：以台本为准 + 提取的时间戳，生成精准日文字幕（模型只做判定，不生成内容）。

流程：
    1) 台本发现：中间文件/日文字幕/<作品> ↔ promptbook/<作品>（文件夹名归一化后比对）+ 轨道号配对
    2) 对齐：单调 DP（支持"多条 ASR 片段 → 一条台本"，也支持跳过），打分用 F1 形式的覆盖度
    3) 重组：以台本行为字幕单位，时间 = 该台本行所辖 ASR 片段的起止范围（合并片段）
    4) 判定：置信度处于中段的条目交给模型判"是否同一句"（只答 1/0，不产出内容）
    5) 报告：逐行（识别 / 台本 / 采用 / 判定）、提取准确率、专有词（人名）歧义清单

用法：
    python3 scripts/refine_script.py                 # 全部作品，产物落 对照试验/
    python3 scripts/refine_script.py --track 05      # 只跑 05
    python3 scripts/refine_script.py --no-model      # 纯规则（不加载模型）
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

logger = logging.getLogger("refine")

PUNCT_RE = re.compile(r"[\s、。，．…‥♪♥♡ｗ！？!?・〜～「」『』（）()\[\]【】\-—]+")
# 轨道号识别：既能认 `01.xxx` / `01_xxx`，也能认 `track01_xxx` / `track 1 xxx` / `トラック01` / `第01話`
# （此前只认"数字开头"，遇到音声包常见的 `track01_标题` 命名会全部解析成 None → 配对数为 0）
TRACK_RE = re.compile(r"^(?:track|tr|トラック|第)?[\s._-]*(\d{1,3})(?!\d)", re.I)
TRACK_ANY_RE = re.compile(r"(?:track|tr|トラック)[\s._-]*(\d{1,3})(?!\d)", re.I)
KATAKANA_RUN = re.compile(r"[ァ-ヶー]{2,}")
KANJI_RUN = re.compile(r"[\u4e00-\u9fff]{2,6}")


@dataclass
class AsrLine:
    index: int
    start_ms: int
    end_ms: int
    text: str


@dataclass
class Entry:
    """最终字幕条目：以台本行（或孤立 ASR 行）为单位。"""
    start_ms: int
    end_ms: int
    text: str
    script_idx: int | None
    asr_idxs: list[int] = field(default_factory=list)
    status: str = ""          # certain / model / extra / split
    score: float = 0.0

    def as_dict(self) -> dict:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms, "text": self.text,
                "script_idx": self.script_idx, "asr_idxs": self.asr_idxs,
                "status": self.status, "score": round(self.score, 3)}


# ============================================================ 基础工具
def norm_text(s: str) -> str:
    return PUNCT_RE.sub("", s)


def norm_work(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip()


def track_number(filename: str) -> str | None:
    name = filename.strip()
    m = TRACK_RE.match(name) or TRACK_ANY_RE.search(name)
    return m.group(1).zfill(2) if m else None


def coverage(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(bl.size for bl in sm.get_matching_blocks()) / len(a)


def pair_score(a: str, b: str) -> float:
    """F1 形式的双向覆盖度：单向覆盖会让短片段乱配到长行上。"""
    if not a or not b:
        return 0.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(bl.size for bl in sm.get_matching_blocks())
    ca, cb = matched / len(a), matched / len(b)
    return 0.0 if ca + cb == 0 else 2 * ca * cb / (ca + cb)


def fmt_ts(ms: int) -> str:
    sec = max(0, ms) // 1000
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def load_asr(path: Path) -> list[AsrLine]:
    timing = path.with_name(path.name.replace(".ja.txt", ".timing.json"))
    lines: list[AsrLine] = []
    if timing.exists():
        for x in json.loads(timing.read_text(encoding="utf-8")).get("lines", []):
            lines.append(AsrLine(len(lines), int(x["start_ms"]), int(x.get("end_ms") or 0), str(x["text"]).strip()))
        if lines:
            return lines
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s*(.+)$", raw.strip())
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
            lines.append(AsrLine(len(lines), (h * 3600 + mi * 60 + s) * 1000, 0, m.group(4).strip()))
    return lines


def load_script(path: Path) -> list[str]:
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = raw.strip()
        if not s or s.startswith(("//", "※", "#", "【", "《")):
            continue
        if norm_text(s):
            out.append(s)
    return out


# ============================================================ 对齐
def align(asr: list[AsrLine], script: list[str], min_score: float = 0.35, min_chars: int = 3) -> list[tuple[int, int]]:
    """单调 DP，允许"多条 ASR → 一条台本"，返回 [(asr_idx, script_idx)]。"""
    n, m = len(asr), len(script)
    A = [norm_text(a.text) for a in asr]
    S = [norm_text(s) for s in script]

    score = [[0.0] * m for _ in range(n)]
    for i in range(n):
        for j in range(m):
            if min(len(A[i]), len(S[j])) < min_chars:
                continue
            sm = difflib.SequenceMatcher(None, A[i], S[j], autojunk=False)
            matched = sum(bl.size for bl in sm.get_matching_blocks())
            if matched < min_chars:
                continue
            ca, cb = matched / len(A[i]), matched / len(S[j])
            f1 = 2 * ca * cb / (ca + cb) if ca + cb else 0.0
            if f1 >= min_score:
                score[i][j] = f1

    NEG = -1e9
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    choice = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            s = score[i - 1][j - 1]
            cands = [
                (dp[i - 1][j - 1] + s, "new") if s > 0 else (NEG, "new"),
                (dp[i - 1][j] + s * 0.6, "cont") if s > 0 else (NEG, "cont"),
                (dp[i - 1][j], "skip_asr"),
                (dp[i][j - 1], "skip_script"),
            ]
            best = max(cands, key=lambda t: t[0])
            dp[i][j], choice[i][j] = best[0], best[1]

    pairs: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        c = choice[i][j]
        if c in ("new", "cont"):
            pairs.append((i - 1, j - 1))
            if c == "new":
                i, j = i - 1, j - 1
            else:
                i -= 1
        elif c == "skip_asr":
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def build_entries(asr: list[AsrLine], script: list[str], pairs: list[tuple[int, int]]) -> list[Entry]:
    """把对齐结果重组成"以台本行为单位"的字幕条目（时间取所辖 ASR 片段范围）。"""
    by_script: dict[int, list[int]] = {}
    for ai, sj in pairs:
        by_script.setdefault(sj, []).append(ai)

    used_asr = {ai for ai, _ in pairs}
    entries: list[Entry] = []
    tail_extras: list[Entry] = []

    # 台本行顺序输出
    for sj in sorted(by_script):
        ais = sorted(by_script[sj])
        start = min(asr[ai].start_ms for ai in ais)
        end = max((asr[ai].end_ms or asr[ai].start_ms) for ai in ais)
        if end <= start:
            end = start + 4000
        best = max(pair_score(norm_text(asr[ai].text), norm_text(script[sj])) for ai in ais)
        entries.append(Entry(start, end, script[sj], sj, ais, "certain" if best >= 0.75 else "model", best))

    # 没有对上台本的 ASR 行：原样保留（标记 extra）
    for ai, a in enumerate(asr):
        if ai in used_asr:
            continue
        end = a.end_ms or (a.start_ms + 4000)
        entries.append(Entry(a.start_ms, end, a.text, None, [ai], "extra", 0.0))

    entries.sort(key=lambda e: (e.start_ms, e.end_ms))
    return entries


# ============================================================ 模型判定（只答 1/0）
def translate_lines(client, texts: list[str], cfg: dict) -> list[str]:
    """用翻译模型批量把日文行译成中文（复用 translate.py 的提示词与编号解析）。"""
    import translate as T

    if not texts:
        return []
    sub = [T.SubLine(i + 1, 0, 0, t) for i, t in enumerate(texts)]
    msgs = T.build_messages(sub, [], "", cfg)
    raw = client.chat(msgs, max_tokens=max(64, len(texts) * 40))
    got, _ = T.parse_numbered(raw, len(texts))
    return [T.strip_output_wrapper(got.get(i, "")) for i in range(len(texts))]


def judge_batch(client, items: list[tuple[str, str]], cfg: dict | None = None,
                threshold: float = 0.55) -> list[bool | None]:
    """判断"音频识别"与"台本"是否同一句。

    做法：让模型把两边都译成中文，再比较译文相似度——
    这是翻译模型的本职（比让它做 1/0 判断稳得多，实测判 1/0 时结果基本不可用）。
    """
    if not items:
        return []
    asr_texts = [a for a, _ in items]
    script_texts = [b for _, b in items]
    try:
        zh_asr = translate_lines(client, asr_texts, cfg or {"translation": {}})
        zh_script = translate_lines(client, script_texts, cfg or {"translation": {}})
    except Exception as exc:  # noqa: BLE001
        logger.warning("对照翻译失败：%s", exc)
        return [None] * len(items)

    out: list[bool | None] = []
    for a, b, za, zb in zip(asr_texts, script_texts, zh_asr, zh_script):
        if not za or not zb:
            out.append(None)
            continue
        sim = difflib.SequenceMatcher(None, norm_text(za), norm_text(zb)).ratio()
        out.append(sim >= threshold)
    return out


# ============================================================ 专有词检查
def name_report(script: list[str], asr: list[AsrLine]) -> list[tuple[str, str, str]]:
    cnt: Counter[str] = Counter()
    for line in script:
        for w in KATAKANA_RUN.findall(line) + KANJI_RUN.findall(line):
            cnt[w] += 1
    candidates = [w for w, c in cnt.most_common() if c >= 2 and len(w) >= 2][:40]

    joined = norm_text("".join(a.text for a in asr))
    rows: list[tuple[str, str, str]] = []
    for w in candidates:
        key = norm_text(w)
        if key in joined:
            rows.append((w, "命中", ""))
            continue
        best, best_r = "", 0.0
        for a in asr:
            t = norm_text(a.text)
            for k in range(max(1, len(key) - 1), len(key) + 2):
                for off in range(0, max(1, len(t) - k + 1)):
                    r = difflib.SequenceMatcher(None, key, t[off:off + k]).ratio()
                    if r > best_r:
                        best, best_r = a.text, r
        if best_r >= 0.6:
            rows.append((w, "疑似误听", f"字幕最接近：{best[:22]}（{best_r:.2f}）"))
        else:
            rows.append((w, "未出现", "台本有、字幕完全找不到"))
    return rows


# ============================================================ 单轨处理
def refine_track(ja_path: Path, script_path: Path, out_dir: Path, client=None,
                 min_score: float = 0.35, judge_band: float = 0.75) -> dict:
    asr = load_asr(ja_path)
    script = load_script(script_path)
    if not asr or not script:
        logger.warning("跳过 %s（字幕 %d / 台本 %d 行）", ja_path.name, len(asr), len(script))
        return {"file": ja_path.name, "status": "empty"}

    pairs = align(asr, script, min_score=min_score)
    entries = build_entries(asr, script, pairs)

    # 中段置信度的条目交给模型判"是否同一句"；否 → 退回识别文本
    judged = reverted = 0
    if client is not None:
        pending = [e for e in entries if e.status == "model" and e.script_idx is not None]
        batch_size = 10
        for start in range(0, len(pending), batch_size):
            chunk = pending[start : start + batch_size]
            items = [(" ".join(asr[ai].text for ai in e.asr_idxs), e.text) for e in chunk]
            try:
                verdicts = judge_batch(client, items)
            except Exception as exc:  # noqa: BLE001
                logger.warning("判定请求失败（第 %d 批）：%s", start // batch_size + 1, exc)
                continue
            for e, ok in zip(chunk, verdicts):
                judged += 1
                if ok:
                    e.status = "certain"
                else:
                    e.text = " ".join(asr[ai].text for ai in e.asr_idxs)
                    e.status = "extra"
                    reverted += 1
            logger.info("判定进度 %d/%d（确认 %d，退回 %d）", min(start + batch_size, len(pending)),
                        len(pending), judged - reverted, reverted)
    else:
        # 纯规则：中段置信度一律退回识别文本，避免误改
        for e in entries:
            if e.status == "model":
                e.text = " ".join(asr[ai].text for ai in e.asr_idxs)
                e.status = "extra"

    matched = sum(1 for e in entries if e.script_idx is not None)
    stats = Counter(e.status for e in entries)
    changed = sum(1 for e in entries if e.script_idx is not None)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = ja_path.name.replace(".ja.txt", "")
    (out_dir / f"{stem}.ja.refined.txt").write_text(
        "\n".join(f"{fmt_ts(e.start_ms)} {e.text}" for e in entries) + "\n", encoding="utf-8")
    (out_dir / f"{stem}.ja.refined.json").write_text(
        json.dumps([e.as_dict() for e in entries], ensure_ascii=False, indent=1), encoding="utf-8")

    names = name_report(script, asr)
    nmiss = [r for r in names if r[1] != "命中"]

    report = [
        f"# 台本精修报告：{stem}",
        "",
        f"- 字幕（ASR）：`{ja_path.relative_to(PROJECT_ROOT)}`，{len(asr)} 行",
        f"- 台本：`{script_path.relative_to(PROJECT_ROOT)}`，{len(script)} 行",
        f"- 判定模型：{'启用（中段置信度比对）' if client is not None else '未启用（纯规则）'}",
        "",
        "## 提取准确性与重构结果",
        "",
        f"- 台本行被用上：**{matched}/{len(script)} = {matched / len(script) * 100:.1f}%**（台本覆盖度）",
        f"- ASR 行对上台本：{len({ai for e in entries if e.script_idx is not None for ai in e.asr_idxs})}/{len(asr)}"
        f" = {len({ai for e in entries if e.script_idx is not None for ai in e.asr_idxs}) / len(asr) * 100:.1f}%",
        f"- 精修后字幕行数：{len(entries)}（ASR {len(asr)} 行 → 以台本句为单位重组）",
        f"- 直接采用台本：{stats.get('certain', 0)} 行 ｜ 模型确认：{judged - reverted}/{judged} 行确认采用",
        f"- 退回识别文本（疑似即兴/初稿差异）：{stats.get('extra', 0)} 行",
        f"- 专有词疑点：{len(nmiss)} 个",
        "",
        "## 专有词 / 人名检查",
        "",
        "| 候选词 | 状态 | 说明 |",
        "|---|---|---|",
    ]
    for w, st, note in (nmiss or names)[:24] or [("（未抽到候选）", "-", "")]:
        report.append(f"| {w} | {st} | {note} |")

    report += ["", "## 逐行明细（前 120 行）", "", "| 时间 | 采用文本 | 判定 | 对应 ASR | 台本行号 |", "|---|---|---|---|---|"]
    for e in entries[:120]:
        asr_txt = " / ".join(asr[ai].text for ai in e.asr_idxs)[:40].replace("|", "/")
        report.append(
            f"| {fmt_ts(e.start_ms)} | {e.text[:46].replace('|', '/')} | {e.status} | {asr_txt} "
            f"| {e.script_idx + 1 if e.script_idx is not None else '—'} |"
        )
    (out_dir / f"{stem}.refine.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    logger.info(
        "%s：台本 %d 行用上 %d（%.0f%%）｜ASR %d 行 → 精修 %d 行｜采用台本 %d｜退回识别 %d｜专有词疑点 %d",
        stem, len(script), matched, matched / len(script) * 100, len(asr), len(entries),
        stats.get("certain", 0), stats.get("extra", 0), len(nmiss),
    )
    return {"file": ja_path.name, "status": "done", "asr_lines": len(asr), "entries": len(entries),
            "script_lines": len(script), "matched": matched, "certain": stats.get("certain", 0),
            "extra": stats.get("extra", 0), "judged": judged, "reverted": reverted,
            "name_issues": len(nmiss)}


# ============================================================ 发现与入口
def discover(sub_root: Path, pb_root: Path) -> list[tuple[str, list[tuple[Path, Path]]]]:
    """返回 [(作品名, [(字幕, 台本), ...])]。

    支持两种布局：
      A) 作品子文件夹：中间文件/日文字幕/<作品>/xx.ja.txt  ↔  promptbook/<作品>/<轨道>.txt
      B) 平铺：字幕直接放在 中间文件/日文字幕/ 下，按"轨道号"在 promptbook 里唯一匹配（产物落 out_root 根）
    """
    pb_dirs = {norm_work(d.name): d for d in pb_root.iterdir() if d.is_dir()}
    out: list[tuple[str, list[tuple[Path, Path]]]] = []

    # A) 作品子文件夹
    # 所有台本目录的"轨道号 → 台本"索引，用于作品目录名对不上时的兜底配对
    all_scripts: dict[str, list[Path]] = {}
    for d in pb_dirs.values():
        for f in d.rglob("*.txt"):
            num = track_number(f.name)
            if num:
                all_scripts.setdefault(num, []).append(f)

    for work_dir in sorted(d for d in sub_root.iterdir() if d.is_dir()):
        pb_dir = pb_dirs.get(norm_work(work_dir.name))
        if pb_dir is None:
            # 例子：字幕在 中间文件/日文字幕/wav/（跟着音频目录名），台本在 promptbook/耳朵/。
            # 目录名不同不该让整轮失败：按轨道号在每个台本目录里找唯一匹配，命中最多者胜出。
            best, best_hits = None, 0
            for cand in pb_dirs.values():
                hits = 0
                for ja in work_dir.rglob("*.ja.txt"):
                    num = track_number(ja.name)
                    same = [f for f in cand.rglob("*.txt") if track_number(f.name) == num] if num else []
                    if len(same) == 1:
                        hits += 1
                if hits > best_hits:
                    best, best_hits = cand, hits
            if best is None or not best_hits:
                logger.warning("作品 %r 找不到对应台本目录（promptbook 下有：%s），跳过", work_dir.name, list(pb_dirs) or "无")
                continue
            logger.info("作品 %r 没有同名台本目录，按轨道号匹配到 %r（%d 轨）",
                        work_dir.name, best.name, best_hits)
            pb_dir = best
        scripts = {}
        for f in pb_dir.rglob("*.txt"):
            num = track_number(f.name)
            if num:
                scripts.setdefault(num, f)
        pairs = [(ja, scripts[track_number(ja.name)]) for ja in sorted(work_dir.rglob("*.ja.txt"))
                 if track_number(ja.name) in scripts]
        if pairs:
            out.append((norm_work(work_dir.name), pairs))

    # B) 平铺字幕：按轨道号在所有台本目录里找唯一匹配
    flat = sorted(sub_root.glob("*.ja.txt"))
    if flat:
        by_num: dict[str, list[Path]] = {}
        for d in pb_dirs.values():
            for f in d.rglob("*.txt"):
                num = track_number(f.name)
                if num:
                    by_num.setdefault(num, []).append(f)
        pairs = []
        for ja in flat:
            num = track_number(ja.name)
            cands = by_num.get(num, [])
            if len(cands) == 1:
                pairs.append((ja, cands[0]))
            elif len(cands) > 1:
                logger.warning("轨道 %s 在 promptbook 下有 %d 个候选台本，跳过（可用 --promptbook 指定具体目录）",
                               num, len(cands))
            else:
                logger.warning("字幕 %s 在 promptbook 里找不到轨道 %s 的台本", ja.name, num)
        if pairs:
            logger.info("检测到平铺布局：%d 个字幕按轨道号配对台本", len(pairs))
            out.append(("", pairs))
    return out


def _write_summary(summary_path: Path, results: list[dict]) -> int:
    """写台本精修汇总（同一文件以最新一次为准，支持历次运行合并）。返回累计轨道数。"""
    header = ["# 台本精修汇总", "", f"- 产物目录：`{summary_path.parent}`", "",
              "| 文件 | 台本行 | 用上台本 | ASR 行 | 精修后 | 采用台本 | 退回识别 | 专有词疑点 |",
              "|---|---|---|---|---|---|---|---|"]
    rows: dict[str, str] = {}
    if summary_path.exists():
        for line in summary_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("| ") and not line.startswith("| 文件"):
                name = line.split("|")[1].strip()
                if name:
                    rows[name] = line
    for r in results:
        if r.get("status") != "done":
            continue
        rows[r["file"]] = (f"| {r['file']} | {r['script_lines']} | {r['matched']} | {r['asr_lines']} | {r['entries']} "
                           f"| {r['certain']} | {r['extra']} | {r['name_issues']} |")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(header + [rows[k] for k in sorted(rows)]) + "\n", encoding="utf-8")
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser(description="台本精修字幕（台本为准 + 提取时间戳）")
    p.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"), help="配置文件路径")
    p.add_argument("--subtitles", default=None)
    p.add_argument("--promptbook", default=None, help="台本目录（默认 config paths.promptbook_dir）")
    p.add_argument("--out-root", default=None,
                   help="产物根目录（默认与 ASR 版日文字幕同目录：中间文件/日文字幕）")
    p.add_argument("--work", default=None, help="只处理指定作品")
    p.add_argument("--track", default=None, help="只处理指定轨道号")
    p.add_argument("--min-score", type=float, default=0.35, help="对齐最低分（F1 覆盖度）")
    p.add_argument("--judge-band", type=float, default=0.75, help="高于此分直接采用台本，低于则交模型判定")
    p.add_argument("--no-model", action="store_true", help="纯规则，不加载模型")
    p.add_argument("--skip-if-empty", action="store_true",
                   help="没有台本（或没有配对）时不报错、直接跳过——供 all/extract 流程使用")
    p.add_argument("--model", default=None, help="模型注册名（默认 config translation.default）")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)

    import translate as T

    cfg = T.load_config(Path(args.config).expanduser()
                        if Path(args.config).is_absolute() else PROJECT_ROOT / args.config)
    import paths_util

    wp = paths_util.work_paths(cfg)
    sub_root = Path(args.subtitles).expanduser() if args.subtitles else wp["ja"]
    sub_root = sub_root if sub_root.is_absolute() else PROJECT_ROOT / sub_root
    pb_root = Path(args.promptbook).expanduser() if args.promptbook else wp["promptbook"]
    pb_root = pb_root if pb_root.is_absolute() else PROJECT_ROOT / pb_root
    out_root = Path(args.out_root).expanduser() if args.out_root else wp["ja"]
    out_root = out_root if out_root.is_absolute() else PROJECT_ROOT / out_root
    if not pb_root.is_dir() and args.skip_if_empty:
        logger.warning("没有台本目录 %s → 跳过台本精修", pb_root)
        return 0
    if not sub_root.is_dir() or not pb_root.is_dir():
        logger.error("目录不存在：subtitles=%s promptbook=%s", sub_root, pb_root)
        return 2

    works = discover(sub_root, pb_root)
    if args.work:
        works = [w for w in works if norm_work(w[0]) == norm_work(args.work)]
    if args.track:
        want = args.track.zfill(2)
        works = [(name, [(j, s) for j, s in pairs if track_number(j.name) == want]) for name, pairs in works]
    total = sum(len(pairs) for _, pairs in works)
    if not total:
        if args.skip_if_empty:
            logger.warning("没有台本（或没有「字幕 + 台本」配对）→ 跳过台本精修，继续后面的流程")
            return 0
        logger.error("没有找到「字幕 + 台本」配对（检查作品文件夹名与轨道号）")
        return 2
    logger.info("待精修 %d 个轨道", total)

    client = manager = None
    if not args.no_model:
        api = T.api_entry(cfg, args.model)
        if api:
            api_key, api_cfg = api
            client = T.resolve_api_client(cfg, api_key, api_cfg)
            model_key, gguf_path = api_key, None
        else:
            model_key, gguf_path = T.resolve_translation_model(cfg, args.model)
        logger.info("判定模型：%s（%s）", model_key, gguf_path.name if gguf_path else "远程 API")
        manager = None if gguf_path is None else T.ServerManager(cfg, gguf_path)
        base_url, model_id = manager.ensure()
        client = T.ChatClient(base_url, model_id, (cfg.get("translation") or {}).get("sampling") or {}, 600)

    results = []
    try:
        for work, pairs in works:
            logger.info("=" * 60)
            logger.info("作品 %s：%d 个轨道", work, len(pairs))
            work_results = []
            for ja, script in pairs:
                out_dir = (out_root / work) if work else out_root
                work_results.append(refine_track(ja, script, out_dir, client=client,
                                                 min_score=args.min_score, judge_band=args.judge_band))
            # 汇总写进各自作品目录（归档时会一起进作品包），不留在中间目录根部
            summary_path = (out_root / work / "refine_summary.md") if work else (out_root / "refine_summary.md")
            n = _write_summary(summary_path, work_results)
            logger.info("汇总（累计 %d 个轨道）：%s", n, summary_path)
            results.extend(work_results)
    finally:
        if manager:
            manager.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
