#!/usr/bin/env python3
"""模型对照试验：把台本/字幕交给任意 OpenAI 兼容端点翻译，检测是否拒绝、是否可用。

用途：
    - 换模型前先小样本试跑（例如比对 Qwen3.5-9B 与 Sakura-14B）
    - 检查模型是否会拒答、是否会只输出思考过程、是否漏行、是否仍输出日文

用法：
    # 1) 先起服务（MLX 示例，端口 8081）
    ./scripts/serve_mlx.sh "~/models/qwen3.5-9b-8bit" 8081
    # 2) 试验：翻译 promptbook 里某个台本的前 20 行
    .venv/bin/python scripts/probe_model.py promptbook/降水記録/04.txt --limit 20 \
        --endpoint http://127.0.0.1:8081 --model "~/models/qwen3.5-9b-8bit"
    # 3) 换 Sakura 对照（llama-server :8080）
    .venv/bin/python scripts/probe_model.py promptbook/降水記録/04.txt --limit 20 \
        --endpoint http://127.0.0.1:8080 --model sakura

产出：
    --out 指定报告文件（默认 对照试验/probe_<文件名>.md）：逐行对照 + 统计 + 首响应样本
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import translate as T  # noqa: E402  复用编号解析/语言判定/提示词构造

logger = logging.getLogger("probe")

# 拒答特征（中/日/英）——必须带"拒绝语境"，否则台词里的"抱歉"会被误判
REFUSAL_PATTERNS = [
    r"我不能(帮你|为您|提供|翻译|协助|继续)",
    r"我无法(帮你|为您|提供|翻译|协助|满足|完成)",
    r"(抱歉|对不起)[，,、：:\s]*(我)?(不能|无法|不便|不予|拒绝)",
    r"作为(一个)?(AI|人工智能|语言模型|助手)",
    r"不(便|适合|能)提供",
    r"违反.*(政策|准则|规定|使用条款|安全)",
    r"恕难",
    r"无法满足(你的|您的|该)",
    r"(申し訳|もうしわけ)(ございません|ありません).*(できません|対応|お答え)",
    r"お答えできません",
    r"対応できません",
    r"禁止されています",
    r"I (can'?t|cannot|am unable|won'?t) (help|assist|comply|provide|translate|continue)",
    r"I'?m sorry[,.]? (but )?I (can'?t|cannot|am unable)",
    r"as an AI",
]
REFUSAL_RE = re.compile("|".join(REFUSAL_PATTERNS), re.I)


def is_refusal(text: str) -> bool:
    return bool(REFUSAL_RE.search(text or ""))


def looks_japanese(text: str) -> bool:
    """判断译文是否仍是日文（假名占比高且几乎无汉字）。"""
    if not text:
        return False
    kana = len(re.findall(r"[ぁ-んァ-ヶ]", text))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return kana >= 2 and kana > cjk * 2


def load_source_lines(path: Path, limit: int | None = None) -> list[str]:
    """读台本（纯文本）或 .ja.txt（带时间戳），返回待翻译的日文行。"""
    raw = path.read_text(encoding="utf-8")
    lines: list[str] = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or s.startswith(("#", "//", "※")):
            continue
        m = re.match(r"^\[\d{2}:\d{2}(?::\d{2})?\]\s*(.+)$", s)   # .ja.txt 格式
        if m:
            s = m.group(1).strip()
        elif s.startswith(("【", "《")):
            continue
        if s:
            lines.append(s)
    return lines[:limit] if limit else lines


def chat(endpoint: str, model: str, messages: list[dict], temperature: float, max_tokens: int,
         thinking: bool, timeout: float) -> tuple[str, dict]:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if not thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(
        f"{endpoint.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    usage = data.get("usage") or {}
    # 有些服务端把思考内容放在 reasoning_content / reasoning
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    usage["_reasoning_len"] = len(reasoning)
    return content, usage


def main() -> int:
    p = argparse.ArgumentParser(description="模型对照试验：检测拒答/漏行/可用性")
    p.add_argument("source", help="台本 txt 或 .ja.txt")
    p.add_argument("--endpoint", default="http://127.0.0.1:8081", help="OpenAI 兼容端点")
    p.add_argument("--model", default=None, help="请求里的 model 名（MLX 必须与启动路径一致）")
    p.add_argument("--limit", type=int, default=30, help="只试前 N 行")
    p.add_argument("--batch-lines", type=int, default=12)
    p.add_argument("--temperature", type=float, default=0.3)
    p.add_argument("--max-tokens-per-line", type=int, default=40)
    p.add_argument("--thinking", action="store_true", help="不禁用思考模式（默认禁用）")
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--out", default=None, help="报告路径（默认 对照试验/probe_<名>.md）")
    p.add_argument("--text-out", default=None,
                   help="额外输出纯中文译文 txt（一行对应原文一行），如 对照试验/05_qwen_zh.txt")
    p.add_argument("--pairs-out", default=None,
                   help="额外输出中日对照 txt（原文 → 译文），如 对照试验/05_qwen_pairs.txt")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)

    src = Path(args.source)
    if not src.is_absolute():
        src = PROJECT_ROOT / src
    if not src.exists():
        logger.error("找不到文件：%s", src)
        return 2
    model = args.model or str(src)     # MLX 服务的 model 名默认用启动路径

    lines = load_source_lines(src, args.limit)
    if not lines:
        logger.error("没有可翻译的行")
        return 2
    logger.info("试验文件：%s（%d 行）", src.name, len(lines))
    logger.info("端点：%s ｜ model=%s ｜ 思考模式=%s", args.endpoint, model, "开" if args.thinking else "关")

    cfg = {"translation": {}}
    sublines = [T.SubLine(i + 1, i * 5000, 0, t) for i, t in enumerate(lines)]
    results: list[dict] = []
    stats = {"requests": 0, "refusals": 0, "empty": 0, "still_ja": 0, "ok": 0, "missing": 0, "seconds": 0.0}
    first_raw = ""

    for start in range(0, len(sublines), args.batch_lines):
        batch = sublines[start : start + args.batch_lines]
        msgs = T.build_messages(batch, [], "", cfg)
        t0 = time.time()
        try:
            raw, usage = chat(args.endpoint, model, msgs, args.temperature,
                              max(64, len(batch) * args.max_tokens_per_line), args.thinking, args.timeout)
        except Exception as exc:  # noqa: BLE001
            logger.error("请求失败（第 %d~%d 行）：%s", start + 1, start + len(batch), exc)
            for line in batch:
                results.append({"ja": line.text, "zh": "", "verdict": f"请求失败: {exc}"})
            stats["empty"] += len(batch)
            continue
        elapsed = time.time() - t0
        stats["requests"] += 1
        stats["seconds"] += elapsed
        if not first_raw and raw.strip():
            first_raw = raw[:800]

        got, missing = T.parse_numbered(raw, len(batch))
        for k, line in enumerate(batch):
            zh = T.strip_output_wrapper(got.get(k, ""))
            if not raw.strip():
                verdict = "空响应（模型未输出内容，可能是思考模式未关闭）"
                stats["empty"] += 1
            elif not zh:
                verdict = "该行缺失（行数不匹配）"
                stats["missing"] += 1
            elif is_refusal(zh):
                verdict = "疑似拒答"
                stats["refusals"] += 1
            elif looks_japanese(zh):
                verdict = "仍是日文"
                stats["still_ja"] += 1
            else:
                verdict = "正常"
                stats["ok"] += 1
            results.append({"ja": line.text, "zh": zh, "verdict": verdict})
        logger.info(
            "第 %d~%d 行：解析 %d/%d，用时 %.1fs，输出 %d token（推理 %d 字符）",
            start + 1, start + len(batch), len(got), len(batch), elapsed,
            usage.get("completion_tokens", 0), usage.get("_reasoning_len", 0),
        )
        if missing:
            logger.warning("缺失行：%s", "、".join(str(m + 1) for m in missing[:8]))

    # ---------------- 纯文本产出 ----------------
    def _resolve(path_str: str) -> Path:
        q = Path(path_str)
        return q if q.is_absolute() else PROJECT_ROOT / q

    written: list[Path] = []
    if args.text_out:
        t = _resolve(args.text_out)
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("\n".join(r["zh"] or r["ja"] for r in results) + "\n", encoding="utf-8")
        written.append(t)
    if args.pairs_out:
        q = _resolve(args.pairs_out)
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text("\n".join(f"{r['ja']}\t{r['zh'] or '（未译出）'}" for r in results) + "\n", encoding="utf-8")
        written.append(q)

    # ---------------- 报告 ----------------
    out = Path(args.out) if args.out else (PROJECT_ROOT / "对照试验" / f"probe_{src.stem}.md")
    if not out.is_absolute():
        out = PROJECT_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)

    total = len(results)
    lines_md = [
        f"# 模型对照试验：{src.name}",
        "",
        f"- 端点：`{args.endpoint}`",
        f"- model：`{model}`",
        f"- 思考模式：{'开' if args.thinking else '关（enable_thinking=false）'}",
        f"- 试跑：{total} 行，每批 {args.batch_lines} 行",
        f"- 用时：{stats['seconds']:.1f}s（{stats['seconds'] / max(1, stats['requests']):.1f}s/批）",
        "",
        "## 结论摘要",
        "",
        f"| 判定 | 行数 | 占比 |",
        f"|---|---|---|",
        f"| 正常翻译 | {stats['ok']} | {stats['ok'] / max(1, total) * 100:.0f}% |",
        f"| 疑似拒答 | {stats['refusals']} | {stats['refusals'] / max(1, total) * 100:.0f}% |",
        f"| 仍是日文 | {stats['still_ja']} | {stats['still_ja'] / max(1, total) * 100:.0f}% |",
        f"| 空响应 | {stats['empty']} | {stats['empty'] / max(1, total) * 100:.0f}% |",
        f"| 行缺失 | {stats['missing']} | {stats['missing'] / max(1, total) * 100:.0f}% |",
        "",
    ]
    if stats["refusals"] == 0 and stats["empty"] == 0 and stats["ok"] > 0:
        lines_md.append(f"**结论：本次试跑未发现拒答**（{stats['ok']}/{total} 行正常译出）。")
    elif stats["refusals"]:
        lines_md.append(f"**结论：出现 {stats['refusals']} 行疑似拒答**，需人工确认。")
    elif stats["empty"]:
        lines_md.append("**结论：出现空响应**，通常是思考模式未关闭或 max_tokens 太小。")
    lines_md += ["", "## 首响应样本（前 800 字）", "", "```", first_raw or "（无输出）", "```", "",
                 "## 逐行对照", "", "| # | 日文 | 中文 | 判定 |", "|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        ja = r["ja"].replace("|", "\\|")[:60]
        zh = (r["zh"] or "").replace("|", "\\|")[:60]
        lines_md.append(f"| {i} | {ja} | {zh} | {r['verdict']} |")
    out.write_text("\n".join(lines_md) + "\n", encoding="utf-8")

    logger.info("=" * 60)
    logger.info("正常 %d ｜ 拒答 %d ｜ 仍日文 %d ｜ 空响应 %d ｜ 行缺失 %d（共 %d 行）",
                stats["ok"], stats["refusals"], stats["still_ja"], stats["empty"], stats["missing"], total)
    logger.info("报告已写入：%s", out)
    for w in written:
        logger.info("译文已写入：%s", w)
    return 0 if stats["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
