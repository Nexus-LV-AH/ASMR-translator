#!/usr/bin/env python3
"""API 连通性/思考模式诊断：发 1 次最小请求，打印 finish_reason、思考占用与正文。

用法：
    .venv/bin/python scripts/api_probe.py                     # 按 config 的 deepseek-api 条目
    .venv/bin/python scripts/api_probe.py --thinking on       # 对比：开启思考
    .venv/bin/python scripts/api_probe.py --model deepseek-v4-pro
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import translate as T  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="DeepSeek/OpenAI 兼容 API 诊断（只发 1 次请求）")
    p.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    p.add_argument("--key", default="deepseek-api", help="config translation.registry 里的条目名")
    p.add_argument("--model", default=None, help="覆盖模型名")
    p.add_argument("--thinking", choices=["on", "off"], default=None, help="覆盖思考模式开关")
    args = p.parse_args()

    cfg = T.load_config(Path(args.config) if Path(args.config).is_absolute() else PROJECT_ROOT / args.config)
    api = T.api_entry(cfg, args.key)
    if not api:
        print(f"registry 里的 {args.key!r} 不是 API 条目（没有 api: 字段）")
        return 2
    if args.model:
        cfg["translation"]["registry"][args.key]["model"] = args.model
    if args.thinking:
        cfg["translation"]["registry"][args.key]["thinking_param"] = {
            "name": "thinking", "on": {"type": "enabled"}, "off": {"type": "disabled"}}
    _, entry = T.api_entry(cfg, args.key) or ("", {})
    client = T.resolve_api_client(cfg, args.key, entry)

    msgs = [{"role": "system", "content": "你是日译中字幕翻译器，只输出译文。"},
            {"role": "user", "content": "[Input]\n[1. おはよう。]\n[2. 今日はいい天気だね。]\n\n输出格式：[1. 译文]" }]
    print("请求：", json.dumps({"model": client.model, "thinking": client.thinking_param,
                                "max_tokens": 64}, ensure_ascii=False))
    try:
        text = client.chat(msgs, max_tokens=64, enable_thinking=(args.thinking == "on"))
    except Exception as exc:  # noqa: BLE001
        print("失败：", type(exc).__name__, exc)
        return 1
    print("finish_reason :", client.last_finish_reason)
    print("usage         :", json.dumps(client.last_usage, ensure_ascii=False))
    print("正文          :", repr(text[:300]))
    if not text.strip():
        print("\n正文为空 → 若 usage 里 reasoning_tokens 很大，就是思考模式吃掉了配额；")
        print("在 config 的该条目里设置 thinking_param（off: {type: disabled}）后重跑本诊断。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
