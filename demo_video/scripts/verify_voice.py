"""Reads back the synthesised narration with SenseVoice and scores it against
the intended text, so the mix can be trusted without listening to 23 files."""

import difflib
import json
import os
import pathlib
import re

import numpy as np
import soundfile as sf
from funasr import AutoModel

ROOT = pathlib.Path(__file__).resolve().parents[1]
VOICE = ROOT / "public/voice"
SENSEVOICE = os.environ.get(
    "SENSEVOICE_DIR",
    "/home/ipad_3d/.cache/modelscope/models/iic--SenseVoiceSmall/snapshots/master",
)

lines = json.loads((ROOT / "narration.json").read_text(encoding="utf-8"))
model = AutoModel(model=SENSEVOICE, disable_update=True, device="cuda:0")


def bare(text: str) -> str:
    text = re.sub(r"<\|[^|]*\|>", "", text)
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text).lower()


worst = []
for line in lines:
    path = VOICE / f"{line['id']:02d}.wav"
    wav, rate = sf.read(path)
    peak = float(np.max(np.abs(wav)))
    rms = float(np.sqrt(np.mean(np.square(wav))))
    heard = model.generate(input=str(path), language="zh", use_itn=True)[0]["text"]
    score = difflib.SequenceMatcher(None, bare(line["text"]), bare(heard)).ratio()
    worst.append((score, line["id"]))
    print(f"{line['id']:02d} sim={score:.2f} peak={peak:.2f} rms={rms:.3f}  {bare(heard)[:44]}")

worst.sort()
print("\nlowest similarity:", worst[:4])
