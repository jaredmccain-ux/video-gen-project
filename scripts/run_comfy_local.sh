#!/usr/bin/env bash
set -euo pipefail
ROOT=/data/yyli/ComfyUI
cd "$ROOT"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
# Dedicated MiniMax H3 + SageAttention inference env
PY="${COMFY_PYTHON:-/home/ipad_3d/.conda/envs/minimax-h3-comfy/bin/python}"
if curl -sf -m 2 http://127.0.0.1:6006/system_stats >/dev/null; then
  echo "ComfyUI already up on :6006"
  exit 0
fi
nohup "$PY" main.py --listen 127.0.0.1 --port 6006 \
  --disable-pinned-memory --fp16-intermediates --use-sage-attention \
  > /tmp/comfyui_local.log 2>&1 &
echo "STARTED_PID=$! log=/tmp/comfyui_local.log py=$PY"
