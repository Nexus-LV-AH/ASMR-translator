#!/usr/bin/env bash
# ============================================================
# 清理运行时垃圾（不会碰你的音频/字幕/台本/模型/虚拟环境）
#
#   ./clean.sh            # 清理
#   ./clean.sh --dry-run  # 只列出会删什么
#
# 清理对象：__pycache__ / .DS_Store / :memory:.ses / *.part* / *.log / *.tmp / *.pyc
# ============================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="1"

echo "==> 扫描垃圾文件"
FOUND="$(
  find . \
    \( -path ./.venv -o -path ./.tools/python -o -path ./.tools/uv-aarch64-apple-darwin \) -prune -o \
    \( -name __pycache__ -o -name .DS_Store -o -name ':memory:.ses' \
       -o -name '*.part' -o -name '*.part[0-9]' -o -name '*.log' -o -name '*.tmp' -o -name '*.pyc' \) -print \
  | sort
)"

if [ -z "$FOUND" ]; then
  echo "    没有需要清理的（已经很干净）"
  exit 0
fi

COUNT=$(printf '%s\n' "$FOUND" | wc -l | tr -d ' ')
SIZE=$(printf '%s\n' "$FOUND" | tr '\n' '\0' | xargs -0 du -ch 2>/dev/null | tail -1 | cut -f1 || echo "?")
printf '%s\n' "$FOUND" | sed 's/^/    /' | head -30
echo "    ……共 $COUNT 项，约 $SIZE"

if [ -n "$DRY" ]; then
  echo "==> --dry-run：未删除任何文件"
  exit 0
fi

echo "==> 删除中"
printf '%s\n' "$FOUND" | while IFS= read -r f; do
  [ -n "$f" ] && rm -rf -- "$f"
done
echo "==> 完成。音频 / 字幕 / 台本 / 模型 / 虚拟环境均未改动。"
