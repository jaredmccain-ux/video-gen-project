"""Lays the 23 narration clips onto one 180s track at their scripted offsets.

Each clip is levelled to a common RMS and given short fades so the mix has no
clicks; the overall gain is set later by ffmpeg against the film's loudness.
"""

import json
import pathlib

import numpy as np
import soundfile as sf

ROOT = pathlib.Path(__file__).resolve().parents[1]
VOICE = ROOT / "public/voice"
TOTAL = 180.05
TARGET_RMS = 0.16
FADE_MS = 18

lines = json.loads((ROOT / "narration.json").read_text(encoding="utf-8"))
rate = sf.info(VOICE / "01.wav").samplerate
bus = np.zeros(int(TOTAL * rate), dtype=np.float32)
fade = int(rate * FADE_MS / 1000)
ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)

for line in lines:
    wav, wav_rate = sf.read(VOICE / f"{line['id']:02d}.wav", dtype="float32", always_2d=False)
    assert wav_rate == rate, f"{line['id']} sample rate mismatch"
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    rms = float(np.sqrt(np.mean(np.square(wav))))
    wav = wav * (TARGET_RMS / max(rms, 1e-6))
    wav[:fade] *= ramp
    wav[-fade:] *= ramp[::-1]
    start = int(line["start"] * rate)
    end = min(start + wav.size, bus.size)
    bus[start:end] += wav[: end - start]

peak = float(np.max(np.abs(bus)))
if peak > 0.97:
    bus *= 0.97 / peak
out = VOICE / "narration.wav"
sf.write(out, bus, rate)
print(f"{out} · {bus.size / rate:.2f}s · peak={peak:.2f} · rate={rate}")
