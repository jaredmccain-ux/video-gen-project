#!/usr/bin/env bash
set -euo pipefail
source /root/miniconda3/etc/profile.d/conda.sh
conda activate base
source /root/.rivo_env
cd /root/autodl-tmp/minimax_short_drama
export PYTHONPATH=/root/autodl-tmp/minimax_short_drama
RUN=/root/autodl-tmp/minimax_short_drama/runs/20260809T053248Z-minimax-short-drama-mvp

python <<'PY'
import json
from pathlib import Path
import yaml

run = Path("/root/autodl-tmp/minimax_short_drama/runs/20260809T053248Z-minimax-short-drama-mvp")
story_path = run / "02_story/story.json"
d = json.loads(story_path.read_text(encoding="utf-8"))
three = {"B02", "B07", "B08", "B11"}
for b in d["beats"]:
    b["duration_s"] = 12 if b["beat_id"] in three else 9
total = sum(b["duration_s"] for b in d["beats"])
assert total == 120, total
story_path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("durations", [(b["beat_id"], b["duration_s"]) for b in d["beats"]], "total", total)

snap = run / "project.config.yaml"
cfg = yaml.safe_load(snap.read_text(encoding="utf-8"))
cfg["llm"]["model"] = "gpt-5.6-terra"
snap.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
print("model", cfg["llm"]["model"])
PY

rm -f "$RUN/03_shots/"*
python -m short_drama.cli plan-shots --run "$RUN"
ls -la "$RUN/03_shots/"

if [[ -f "$RUN/03_shots/shots.json" ]]; then
  python <<'PY'
import json
from pathlib import Path
d = json.loads(Path("/root/autodl-tmp/minimax_short_drama/runs/20260809T053248Z-minimax-short-drama-mvp/03_shots/shots.json").read_text())
shots = d.get("shots", [])
print("num_shots", len(shots))
print("total_dur", sum(s.get("duration_s", 0) for s in shots))
for s in shots[:5]:
    print(s.get("shot_id"), s.get("beat_id"), s.get("duration_s"), (s.get("story_purpose") or "")[:50])
PY
else
  echo "FAIL"
  head -c 1200 "$RUN/03_shots/raw_response.txt" || true
  echo
  cat "$RUN/03_shots/error.json" || true
  exit 1
fi
