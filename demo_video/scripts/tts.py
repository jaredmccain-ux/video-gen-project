"""Synthesises the demo narration with the local VoxCPM2 weights.

Must run inside the conda env that has voxcpm installed:
  /home/ipad_3d/miniconda3/envs/vc/bin/python scripts/tts.py

The first line is generated free-form and then reused as the voice prompt for
every later line, otherwise VoxCPM picks a new timbre on each call.
"""

import json
import pathlib
import sys

import soundfile as sf
from voxcpm import VoxCPM

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "public/voice"
TOTAL = 180.0

lines = json.loads((ROOT / "narration.json").read_text(encoding="utf-8"))
only = {int(x) for x in sys.argv[1:] if x.isdigit()}
OUT.mkdir(parents=True, exist_ok=True)

model = VoxCPM.from_pretrained(local_files_only=True, load_denoiser=False, device="cuda:0")
rate = model.tts_model.sample_rate
print(f"model ready · sample_rate={rate}", flush=True)


def synth(text: str, anchor: tuple[pathlib.Path, str] | None):
    kwargs = {"text": text, "cfg_value": 2.0, "inference_timesteps": 10, "retry_badcase": True}
    if anchor:
        kwargs["prompt_wav_path"] = str(anchor[0])
        kwargs["prompt_text"] = anchor[1]
    try:
        return model.generate(normalize=True, **kwargs)
    except Exception as exc:  # text normaliser is optional
        print(f"  normalize=True failed ({exc}); retrying raw", flush=True)
        return model.generate(normalize=False, **kwargs)


anchor: tuple[pathlib.Path, str] | None = None
anchor_path = OUT / "01.wav"
if anchor_path.exists() and only:
    anchor = (anchor_path, lines[0]["text"])

report = []
for index, line in enumerate(lines):
    path = OUT / f"{line['id']:02d}.wav"
    if only and line["id"] not in only:
        if path.exists():
            report.append((line, sf.info(path).duration))
        continue
    wav = synth(line["text"], anchor)
    sf.write(path, wav, rate)
    seconds = len(wav) / rate
    if anchor is None:
        anchor = (path, line["text"])
    report.append((line, seconds))
    print(f"{line['id']:02d}  {seconds:6.2f}s  {line['text'][:30]}", flush=True)

print("\n=== fit check ===")
for index, (line, seconds) in enumerate(report):
    nxt = report[index + 1][0]["start"] if index + 1 < len(report) else TOTAL
    room = nxt - line["start"] - 0.15
    flag = "OK " if seconds <= room else "LONG"
    print(f"{flag} {line['id']:02d} start={line['start']:6.1f} dur={seconds:5.2f} room={room:5.2f}")
