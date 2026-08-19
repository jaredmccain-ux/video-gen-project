"""Checks the voiced cut: narration energy lands inside each scripted slot,
gaps stay quiet, and the film excerpt keeps its own audio."""

import json
import pathlib
import subprocess
import tempfile

import numpy as np
import soundfile as sf

ROOT = pathlib.Path(__file__).resolve().parents[1]
VIDEO = ROOT / "out/sceneflow-demo-voiced.mp4"
lines = json.loads((ROOT / "narration.json").read_text(encoding="utf-8"))

with tempfile.TemporaryDirectory() as tmp:
    wav_path = pathlib.Path(tmp) / "mix.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(VIDEO),
         "-ac", "1", "-ar", "16000", str(wav_path)],
        check=True,
    )
    audio, rate = sf.read(wav_path, dtype="float32")

step = rate // 10
frames = np.array([
    float(np.sqrt(np.mean(np.square(audio[i:i + step]))))
    for i in range(0, len(audio) - step, step)
])


def window_rms(start: float, end: float) -> float:
    a, b = int(start * 10), int(end * 10)
    chunk = frames[a:b]
    return float(chunk.max()) if chunk.size else 0.0


print(f"duration={len(audio) / rate:.2f}s  overall_peak={float(np.max(np.abs(audio))):.2f}")
print("\n--- narration slots ---")
quiet = []
for index, line in enumerate(lines):
    nxt = lines[index + 1]["start"] if index + 1 < len(lines) else 180.0
    speech = window_rms(line["start"], line["start"] + 1.0)
    flag = "OK " if speech > 0.02 else "!! "
    if speech <= 0.02:
        quiet.append(line["id"])
    print(f"{flag}{line['id']:02d} @{line['start']:6.1f}s  peak_rms={speech:.3f}")

print("\n--- film excerpt (should stay loud) ---")
for start in (154, 158, 162, 166, 170):
    print(f"  {start}s peak_rms={window_rms(start, start + 3):.3f}")

print("\n--- expected silence ---")
for start, end in ((32.0, 35.5), (99.5, 102.5), (171.5, 173.5)):
    print(f"  {start}-{end}s peak_rms={window_rms(start, end):.3f}")

print("\nsilent narration slots:", quiet or "none")
