#!/usr/bin/env bash
# 启动 MLX 模型服务（对照试验用；Apple Silicon）
#
#   ./scripts/serve_mlx.sh                                  # 默认加载 qwen3.5 9b8bit，端口 8081
#   ./scripts/serve_mlx.sh "/path/to/model" 8081            # 指定模型目录与端口
#
# 注意：
#   1) 请求里的 model 字段必须与这里给的模型路径**完全一致**（mlx_lm.server 会据此匹配已加载模型）
#   2) 必须禁用思考模式：请求带 chat_template_kwargs={"enable_thinking": false}
#      （默认思考模式下 content 会是空的，token 全烧在 reasoning 上）
#   3) HF_HOME 指向已有目录，否则 /v1/models 会因找不到 HF 缓存报错
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL="${1:-$HOME/models/qwen3.5-9b-8bit}"
PORT="${2:-8081}"

MLX_SERVER="$(command -v mlx_lm.server || true)"
if [ -z "$MLX_SERVER" ]; then
  echo "找不到 mlx_lm.server。请安装：pip install mlx-lm" >&2
  exit 1
fi

export HF_HOME="${HF_HOME:-$PROJECT_ROOT/.models}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
mkdir -p "$HF_HOME"

echo "==> 加载 MLX 模型：$MODEL"
echo "==> 监听：http://127.0.0.1:$PORT  （请求时 model 字段请填：$MODEL）"
exec "$MLX_SERVER" --model "$MODEL" --host 127.0.0.1 --port "$PORT"
