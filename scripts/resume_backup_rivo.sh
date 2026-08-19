#!/usr/bin/env bash
# Resume pipeline using Rivo backup base URL https://rivoapi.com/v1
set -euo pipefail
cd /data/yyli/minimax_short_drama
source .venv/bin/activate
set -a
# shellcheck disable=SC1091
source .vscode/debug.env
set +a
export PYTHONPATH=/data/yyli/minimax_short_drama
export PYTHONUNBUFFERED=1
# Prefer direct path to Rivo (bypass local Clash-style proxy)
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY || true
export NO_PROXY="rivoapi.com,api.rivoapi.com,127.0.0.1,localhost"
export no_proxy="$NO_PROXY"

RUN=runs/20260810T163457Z-minimax-short-drama-mvp
rm -f "$RUN/03_shots/error.json"
mkdir -p logs
LOG="logs/resume_$(date +%Y%m%d_%H%M%S).log"

{
  echo "[resume $(date -Is)] plan-shots BACKUP https://rivoapi.com/v1 model=gpt-5.6-sol"
  if [[ -f "$RUN/03_shots/shots.json" ]]; then
    echo "[skip] shots.json exists"
  else
    python -u scripts/plan_shots_chunked.py --run "$RUN" --chunk-size 2
  fi
  echo "[resume] prepare-consistency"
  python -u -m short_drama.cli prepare-consistency --run "$RUN"
  echo "[resume] validate/approve shots"
  python -u -m short_drama.cli validate --run "$RUN" --stage shots
  python -u -m short_drama.cli approve --run "$RUN" --stage shots --yes
  echo "[resume] render-prompts"
  python -u -m short_drama.cli render-prompts --run "$RUN"
  echo "[resume] generate-videos"
  python -u -m short_drama.cli generate-videos --run "$RUN"
  echo "[resume] generate-subtitles"
  python -u -m short_drama.cli generate-subtitles --run "$RUN"
  echo "[resume] assemble"
  python -u -m short_drama.cli assemble --run "$RUN" \
    --font /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc \
    --env-prefix /usr
  echo "[resume DONE] $(date -Is)"
} 2>&1 | tee "$LOG"
echo "LOG=$LOG"
