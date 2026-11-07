#!/usr/bin/env python3
"""图形界面开发入口（普通用户请双击启动器，不需要跑这个）。

    python3 gui_app/gui.py                 # 自动打开浏览器
    python3 gui_app/gui.py --no-browser    # 只打印地址
    python3 gui_app/gui.py --port 9000

会优先转入项目 .venv 里的 Python；没有 venv 时若当前环境有 yaml 也可直接启动。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TARGET = HERE / "gui_server.py"


def venv_python() -> Path:
    if os.name == "nt":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def main() -> int:
    if not TARGET.exists():
        sys.exit(f"找不到 {TARGET}（请确认项目结构完整）")
    py = venv_python()
    if py.exists():
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return subprocess.call([str(py), str(TARGET), *sys.argv[1:]], cwd=str(ROOT), env=env)
    try:
        import yaml  # noqa: F401
        import numpy  # noqa: F401
    except Exception:  # noqa: BLE001
        print("还没有可用的运行环境。请先运行 ./start.sh（macOS/Linux）或 start-gui.bat（Windows），")
        print("或者手动创建 .venv 并安装 requirements.txt 后再运行 python gui.py。")
        return 1
    return subprocess.call([sys.executable, str(TARGET), *sys.argv[1:]], cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
