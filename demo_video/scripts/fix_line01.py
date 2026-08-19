"""Line 01 has to pronounce the product name clearly. Try a few spellings,
read each back with SenseVoice, and keep the clearest one."""

import os
import pathlib
import re

import soundfile as sf
from funasr import AutoModel
from voxcpm import VoxCPM

ROOT = pathlib.Path(__file__).resolve().parents[1]
VOICE = ROOT / "public/voice"
ANCHOR = VOICE / "03.wav"
ANCHOR_TEXT = "打开服务地址，第一屏是入口终端。"
SENSEVOICE = os.environ.get(
    "SENSEVOICE_DIR",
    "/home/ipad_3d/.cache/modelscope/models/iic--SenseVoiceSmall/snapshots/master",
)

CANDIDATES = [
    "Scene Flow，跑在 MiniMax H3 上的短剧流水线。",
    "SceneFlow，跑在 MiniMax H3 上的短剧流水线。",
    "希恩弗洛，跑在 MiniMax H3 上的短剧流水线。",
]

tts = VoxCPM.from_pretrained(local_files_only=True, load_denoiser=False, device="cuda:0")
asr = AutoModel(model=SENSEVOICE, disable_update=True, device="cuda:0")
rate = tts.tts_model.sample_rate

for index, text in enumerate(CANDIDATES):
    path = VOICE / f"cand-{index}.wav"
    wav = tts.generate(
        text=text,
        prompt_wav_path=str(ANCHOR),
        prompt_text=ANCHOR_TEXT,
        cfg_value=2.0,
        inference_timesteps=10,
        normalize=True,
        retry_badcase=True,
    )
    sf.write(path, wav, rate)
    heard = asr.generate(input=str(path), language="auto", use_itn=True)[0]["text"]
    heard = re.sub(r"<\|[^|]*\|>", "", heard)
    print(f"cand-{index}  {len(wav) / rate:5.2f}s  {text[:12]!r:<20} -> {heard}", flush=True)
