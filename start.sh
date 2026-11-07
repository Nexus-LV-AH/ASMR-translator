#!/usr/bin/env bash
# ============================================================
# 日语音声字幕提取（anime-whisper）—— 一键环境准备 + 运行
#
#   ./start.sh                         处理 input/ 里的所有音频
#   ./start.sh "某作品.mp3"             处理指定文件
#   ./start.sh input/ --force          强制重跑
#   ./start.sh input/ --model kotoba-whisper-v2.2 --device cpu
#   ./start.sh --fetch-model           只下载模型
# ============================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

# ---- 依赖与模型全部放在项目目录内，不污染系统 ----
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$PROJECT_ROOT/.tools/python}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PROJECT_ROOT/.tools/uv-cache}"
export UV_LINK_MODE=copy
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"   # HF 直连在国内常超时，默认走镜像
export HF_HOME="${HF_HOME:-$PROJECT_ROOT/.models}"
export HF_HUB_DISABLE_TELEMETRY=1
export ORT_DISABLE_TELEMETRY=1          # 防止 onnxruntime 在当前目录生成 :memory:.ses 垃圾
export PYTHONDONTWRITEBYTECODE=1       # 不生成 __pycache__ 垃圾

PYTHON="$PROJECT_ROOT/.venv/bin/python"

# ---- 1. 准备 uv（放在 .tools/ 内）----
OS_NAME="$(uname -s)"
ARCH_NAME="$(uname -m)"
case "${OS_NAME}-${ARCH_NAME}" in
  Darwin-arm64) UV_TARGET="aarch64-apple-darwin" ;;
  Darwin-x86_64) UV_TARGET="x86_64-apple-darwin" ;;
  Linux-x86_64) UV_TARGET="x86_64-unknown-linux-gnu" ;;
  Linux-aarch64|Linux-arm64) UV_TARGET="aarch64-unknown-linux-gnu" ;;
  *) echo "不支持的平台：${OS_NAME} ${ARCH_NAME}（也可以手动创建 .venv 后运行 python gui.py）"; exit 1 ;;
esac
UV_BIN="$PROJECT_ROOT/.tools/uv-${UV_TARGET}/uv"

if [ ! -x "$UV_BIN" ]; then
  echo "==> 下载 uv（Python 版本管理器，放在 .tools/ 内）"
  mkdir -p .tools
  curl -sL "https://github.com/astral-sh/uv/releases/latest/download/uv-${UV_TARGET}.tar.gz" -o .tools/uv.tar.gz
  tar xzf .tools/uv.tar.gz -C .tools
  rm -f .tools/uv.tar.gz
fi

# ---- 2. 准备 Python 3.12 环境 ----
if [ ! -x "$PYTHON" ]; then
  echo "==> 创建 Python 3.12 虚拟环境（系统 3.14 没有 torch 轮子）"
  "$UV_BIN" venv --python 3.12 "$PROJECT_ROOT/.venv"
fi

# ---- 3. 安装依赖（幂等：已装会秒过）----
echo "==> 检查依赖"
"$UV_BIN" pip install --python "$PYTHON" -q \
  torch transformers faster-whisper huggingface_hub numpy pyyaml

# ---- 4. 只下载模型 ----
if [ "${1:-}" = "--fetch-model" ]; then
  exec "$PYTHON" scripts/fetch_model.py "${2:-anime-whisper}"
fi

# ---- 4.5 本地推理依赖检查（用云端 API 可忽略）----
if ! command -v llama-server >/dev/null 2>&1; then
  if [ "$OS_NAME" = "Darwin" ]; then
    echo "提示：未找到 llama-server（本地模型需要：brew install llama.cpp；用云端 API 可忽略）"
  else
    echo "提示：未找到 llama-server（本地模型需要安装 llama.cpp；用云端 API 可忽略）"
  fi
fi

# ---- 5. 三种运行方式（统一入口 scripts/subtitle.py）----
#   ./start.sh                       全部：提取 + 翻译（input/ → 中文 LRC）
#   ./start.sh all   [输入...]       同上（显式写法）
#   ./start.sh extract [输入...]     只提取日文字幕
#   ./start.sh translate [字幕...]   只翻译（复用已提取的 .ja.txt）
#   兼容旧写法：--translate / --extract
case "${1:-}" in
  --translate) shift; exec "$PYTHON" scripts/subtitle.py translate "$@" ;;
  --extract)   shift; exec "$PYTHON" scripts/subtitle.py extract "$@" ;;
esac

echo "==> 开始（模式：${1:-all}）"
# 注意：本脚本开了 set -e，失败时会直接退出 → 用 if 捕获退出码，保证下面的验收仍会执行
if "$PYTHON" scripts/subtitle.py "$@"; then
  RC=0
else
  RC=$?
fi

# ---- 6. 自动验收（格式/单调性/拟声保留/未译行/中文标题）----
# 归档后中间目录会被清空 → 优先验收作品包
if [ -n "$(find 中间文件 -name '*.ja.txt' -maxdepth 6 -print -quit 2>/dev/null)" ]; then
  echo
  echo "==> 产出验收（工作区：中间文件/）"
  "$PYTHON" scripts/verify_output.py 中间文件/日文字幕/ --check-translation || true
elif [ -d output ]; then
  echo
  echo "==> 产出验收（作品包：output/）"
  "$PYTHON" scripts/verify_output.py output/ --check-translation || true
fi

exit $RC
