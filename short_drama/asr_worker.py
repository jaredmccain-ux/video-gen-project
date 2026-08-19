#!/usr/bin/env python3
"""Standalone SenseVoice + FSMN VAD transcriber, launched as a subprocess.

FunASR usually lives in a separate conda environment from the Studio server, so this
module must stay importable on its own: only the standard library and funasr.

Usage: python asr_worker.py <config.json>
Config keys: audio, output, sensevoice_dir, vad_dir, device, pad_s, max_segment_ms
"""

from __future__ import annotations

import json
import math
import os
import re
import struct
import sys
import tempfile
import wave


TAG_RE = re.compile(r"<\|[^|]*\|>")


def _clean(text: str) -> str:
    return TAG_RE.sub("", str(text or "")).strip()


def _read_wav(path: str) -> tuple[list[int], int]:
    with wave.open(path) as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("需要 16-bit 单声道 WAV")
        rate = handle.getframerate()
        frames = handle.getnframes()
        samples = list(struct.unpack("<" + "h" * frames, handle.readframes(frames)))
    return samples, rate


def _write_wav(path: str, samples: list[int], rate: int) -> None:
    with wave.open(path, "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<" + "h" * len(samples), *samples))


def _energy(samples: list[int], rate: int, start: float, end: float, frame_s: float) -> list[int]:
    """Per-frame RMS inside the window, so the caller can trim silence and find pauses."""
    frame = max(1, int(rate * frame_s))
    left = max(0, int(start * rate))
    right = min(len(samples), int(end * rate))
    values = []
    for offset in range(left, right - frame + 1, frame):
        chunk = samples[offset : offset + frame]
        values.append(int(math.sqrt(sum(value * value for value in chunk) / len(chunk))))
    return values


def _vad_windows(raw: object) -> list[tuple[float, float]]:
    """FunASR returns [[start_ms, end_ms], ...] nested one or two levels deep."""
    values = raw
    if isinstance(values, list) and values and isinstance(values[0], dict):
        first = values[0]
        values = first.get("value") or first.get("timestamp") or first.get("text")
    windows: list[tuple[float, float]] = []
    for item in values or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        start, end = float(item[0]), float(item[1])
        if end > 200:  # milliseconds
            start, end = start / 1000.0, end / 1000.0
        if end > start:
            windows.append((start, end))
    return sorted(windows)


def main() -> int:
    config = json.loads(open(sys.argv[1], encoding="utf-8").read())
    from funasr import AutoModel  # noqa: PLC0415 — only available in the ASR environment

    audio = config["audio"]
    device = config.get("device") or "cpu"
    pad = float(config.get("pad_s") or 0.08)

    vad = AutoModel(
        model=config["vad_dir"],
        device=device,
        disable_update=True,
        vad_kwargs={"max_single_segment_time": int(config.get("max_segment_ms") or 15000)},
    )
    windows = _vad_windows(vad.generate(input=audio))

    asr = AutoModel(model=config["sensevoice_dir"], device=device, disable_update=True)
    samples, rate = _read_wav(audio)
    duration = len(samples) / float(rate)
    if not windows:
        windows = [(0.0, duration)]

    segments = []
    frame_s = float(config.get("frame_s") or 0.02)
    with tempfile.TemporaryDirectory() as workdir:
        for index, (start, end) in enumerate(windows):
            left = max(0, int((start - pad) * rate))
            right = min(len(samples), int((end + pad) * rate))
            if right - left < int(0.12 * rate):
                continue
            chunk_path = os.path.join(workdir, f"seg{index:04d}.wav")
            _write_wav(chunk_path, samples[left:right], rate)
            result = asr.generate(input=chunk_path, language="zh", use_itn=True)
            raw = ""
            if isinstance(result, list) and result:
                raw = result[0].get("text", "") if isinstance(result[0], dict) else str(result[0])
            text = _clean(raw)
            if not text:
                continue
            segments.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "raw": raw,
                "frame_s": frame_s,
                "energy": _energy(samples, rate, start, end, frame_s),
            })
            print(f"[{start:7.2f}-{end:7.2f}] {text}", flush=True)

    with open(config["output"], "w", encoding="utf-8") as handle:
        json.dump(
            {"audio": audio, "duration_s": round(duration, 3), "model": "SenseVoiceSmall+FSMN-VAD", "segments": segments},
            handle,
            ensure_ascii=False,
            indent=2,
        )
    print(f"segments={len(segments)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
