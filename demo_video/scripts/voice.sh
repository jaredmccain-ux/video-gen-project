#!/usr/bin/env bash
# Regenerates the narration and remuxes the voiced cut. Edit narration.json
# first; the video itself is copied, so no Remotion render is needed.
set -euo pipefail

cd "$(dirname "$0")/.."

VC_PY=${VC_PY:-/home/ipad_3d/miniconda3/envs/vc/bin/python}
SILENT=out/sceneflow-demo.mp4
VOICED=out/sceneflow-demo-voiced.mp4

# Pass line ids to resynthesise only those, e.g. ./scripts/voice.sh 4 17
"$VC_PY" scripts/tts.py "$@"
"$VC_PY" scripts/build_voice_bus.py

ffmpeg -hide_banner -nostats -y -i "$SILENT" -i public/voice/narration.wav \
  -filter_complex "\
[1:a]volume=2.5dB,alimiter=limit=0.85:attack=5:release=60,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[nar];\
[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[film];\
[film][nar]amix=inputs=2:normalize=0:dropout_transition=0[mix]" \
  -map 0:v -map "[mix]" -c:v copy -c:a aac -b:a 192k -movflags +faststart "$VOICED"

"$VC_PY" scripts/verify_mix.py
