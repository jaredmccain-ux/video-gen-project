"""Deterministic subtitle timing from approved shot dialogue (stage VIII A tier)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .state import utc_now, write_json_atomic


SEGMENT_RE = re.compile(r"(?P<start>\d+(?:\.\d+)?)-(?P<end>\d+(?:\.\d+)?)秒(?P<text>[^；;]+)")
SPEECH_CUES = (
    "说", "喊", "问", "答", "质问", "提醒", "说明", "表达", "解释", "提出要求",
    "承认", "回应", "告诉", "喊话", "开口", "对话",
)


def _srt_time(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def _timing_for_shot(shot: dict[str, Any]) -> tuple[float, float, str]:
    duration = float(shot["duration_s"])
    matches = list(SEGMENT_RE.finditer(shot.get("action_timeline", "")))
    candidates = [m for m in matches if any(cue in m.group("text") for cue in SPEECH_CUES)]
    if len(candidates) == 1:
        match = candidates[0]
        start = max(0.0, float(match.group("start")))
        end = min(duration, float(match.group("end")))
        if end - start >= 0.8:
            return start, end, f'action_timeline: {match.group(0)}'

    # A stable fallback for one-dialogue short shots: leave room for entry and exit actions.
    start = min(0.8, duration * 0.16)
    end = max(start + 0.8, duration - 0.5)
    return start, min(end, duration), "default_safe_window"


def _split_window(
    dialogue: list[dict[str, Any]],
    start: float,
    end: float,
    *,
    min_cue_s: float = 0.6,
    gap_s: float = 0.08,
) -> list[tuple[float, float]]:
    """Share one shot's speech window between its lines, proportional to length."""
    weights = [max(1, len(str(item.get("text") or ""))) for item in dialogue]
    total = float(sum(weights))
    usable = max(len(dialogue) * min_cue_s, end - start - gap_s * (len(dialogue) - 1))
    windows = []
    cursor = start
    for weight in weights:
        span = max(min_cue_s, usable * weight / total)
        windows.append((cursor, cursor + span))
        cursor += span + gap_s
    return windows


def _write_srt(path: Path, cues: list[dict[str, Any]], *, global_time: bool) -> None:
    lines: list[str] = []
    for index, cue in enumerate(cues, 1):
        offset = float(cue["planned_start_s"]) if global_time else 0.0
        lines.extend((
            str(index),
            f'{_srt_time(offset + cue["start_s"])} --> {_srt_time(offset + cue["end_s"])}',
            cue["text"],
            "",
        ))
    path.write_text("\n".join(lines), encoding="utf-8")


def generate_planned_subtitles(run_dir: Path, *, require_videos: bool = True) -> Path:
    """Create per-shot and full-film SRT files without ASR or generated-audio analysis."""
    shots_path = run_dir / "03_shots/shots.json"
    video_dir = run_dir / "05_videos"
    output_dir = run_dir / "06_subtitles"
    if not shots_path.is_file():
        raise FileNotFoundError(f"镜头文件不存在：{shots_path}")

    document = json.loads(shots_path.read_text(encoding="utf-8"))
    shots = document.get("shots", [])
    if not shots:
        raise ValueError("shots.json 中没有镜头")
    if require_videos:
        missing = [shot["shot_id"] for shot in shots if not (video_dir / f'{shot["shot_id"]}.mp4').is_file()]
        if missing:
            raise FileNotFoundError(f"阶段 VII 视频不完整：{', '.join(missing)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    cues: list[dict[str, Any]] = []
    for shot in shots:
        dialogue = shot.get("dialogue", [])
        text = shot.get("subtitle_text", "")
        if not dialogue:
            if text:
                raise ValueError(f'{shot["shot_id"]}: 无对白镜头不得包含字幕')
            continue
        planned_text = "".join(item["text"] for item in dialogue)
        if text != planned_text:
            raise ValueError(f'{shot["shot_id"]}: subtitle_text 与计划对白不一致')
        start, end, timing_source = _timing_for_shot(shot)
        shot_cues = [
            {
                "shot_id": shot["shot_id"],
                "speaker_id": line.get("speaker_id") or "",
                "text": str(line.get("text") or ""),
                "start_s": round(cue_start, 3),
                "end_s": round(cue_end, 3),
                "planned_start_s": float(shot["planned_start_s"]),
                "planned_end_s": float(shot["planned_end_s"]),
                "timing_source": timing_source,
            }
            for line, (cue_start, cue_end) in zip(dialogue, _split_window(dialogue, start, end))
        ]
        cues.extend(shot_cues)
        _write_srt(output_dir / f'{shot["shot_id"]}.srt', shot_cues, global_time=False)

    _write_srt(output_dir / "full.srt", cues, global_time=True)
    report_path = output_dir / "timeline.json"
    write_json_atomic(report_path, {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "method": "planned_dialogue_deterministic_timing",
        "asr_used": False,
        "source": str(shots_path),
        "shot_count": len(shots),
        "subtitle_cue_count": len(cues),
        "silent_shot_count": len(shots) - len(cues),
        "full_srt": str(output_dir / "full.srt"),
        "cues": cues,
    })
    return report_path
