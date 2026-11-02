#!/usr/bin/env python3
"""音声 → 日文字幕 → 中文字幕（三任务入口，放在项目最外层）

用法（在本文件夹直接运行）：
    python subtitle.py                        # 菜单选择任务 1/2/3
    python subtitle.py extract    [音频...]   # 任务1：只提取字幕（含台本精修）
    python subtitle.py all        [音频...]   # 任务2：完整运行（提取 + 精修 + 翻译 → 中文 LRC）
    python subtitle.py translate  [字幕...]   # 任务3：制作翻译（已有日文字幕 → 中文 LRC）

不带文件参数时：extract/all 递归扫描 config 里的 input/（为空会提示后退出，不加载模型）；
translate 扫描 subtitles_ja/（不依赖 input/，已有日文字幕时用它）。
常用附加参数：--limit 20 先试翻 20 行 ｜ --bilingual 中日对照 ｜ --force 重跑
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "scripts" / "subtitle.py"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"

if not TARGET.exists():
    sys.exit(f"找不到核心脚本 {TARGET}（请确认项目结构完整）")

python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
sys.exit(subprocess.call([python, str(TARGET), *sys.argv[1:]]))
