"""FFmpeg normalization, hard-cut assembly, and subtitle burn-in (stage IX)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .state import utc_now, write_json_atomic


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _probe(ffprobe: Path, media: Path) -> dict[str, Any]:
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(media)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def assemble_run(run_dir: Path, env_prefix: Path, font_file: Path) -> Path:
    ffmpeg = env_prefix / "bin/ffmpeg"
    ffprobe = env_prefix / "bin/ffprobe"
    for tool in (ffmpeg, ffprobe, font_file):
        if not tool.is_file():
            raise FileNotFoundError(f"阶段 IX 依赖不存在：{tool}")

    shots_path = run_dir / "03_shots/shots.json"
    subtitle_path = run_dir / "06_subtitles/full.srt"
    if not shots_path.is_file() or not subtitle_path.is_file():
        raise FileNotFoundError("阶段 IX 需要 shots.json 和阶段 VIII full.srt")
    shots = json.loads(shots_path.read_text(encoding="utf-8"))["shots"]

    source_dir = run_dir / "05_videos"
    output_dir = run_dir / "07_final"
    normalized_dir = output_dir / "normalized"
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_dir.mkdir(parents=True, exist_ok=True)

    normalized: list[Path] = []
    for shot in shots:
        shot_id = shot["shot_id"]
        source = source_dir / f"{shot_id}.mp4"
        target = normalized_dir / f"{shot_id}.mp4"
        if not source.is_file():
            raise FileNotFoundError(f"镜头视频不存在：{source}")
        if not target.is_file() or target.stat().st_size == 0:
            _run([
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                "-t", f'{float(shot["duration_s"]):g}',
                "-vf", "fps=24,scale=1344:768:flags=lanczos,setsar=1,format=yuv420p",
                "-af", "loudnorm=I=-16:LRA=11:TP=-1.5,aresample=32000,apad",
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k", "-ar", "32000", "-ac", "2",
                "-movflags", "+faststart", str(target),
            ])
        normalized.append(target)

    concat_list = output_dir / "concat.txt"
    concat_list.write_text("".join(f"file '{path.as_posix()}'\n" for path in normalized), encoding="utf-8")
    no_subtitles = output_dir / "concat_no_subtitles.mp4"
    _run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", "-movflags", "+faststart", str(no_subtitles),
    ])

    final_srt = output_dir / "final.srt"
    shutil.copyfile(subtitle_path, final_srt)
    final_video = output_dir / "final.mp4"
    subtitle_filter = (
        f"subtitles={final_srt.as_posix()}:fontsdir={font_file.parent.as_posix()}:"
        "force_style='FontName=Noto Sans CJK SC,FontSize=20,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=36'"
    )
    _run([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(no_subtitles),
        "-vf", subtitle_filter, "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "copy", "-movflags", "+faststart", str(final_video),
    ])

    probe = _probe(ffprobe, final_video)
    streams = probe.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    duration = float(probe["format"]["duration"])
    checks = {
        "duration_117_to_123_s": 117 <= duration <= 123,
        "resolution_1344x768": (video.get("width"), video.get("height")) == (1344, 768),
        "frame_rate_24": video.get("r_frame_rate") == "24/1",
        "video_h264": video.get("codec_name") == "h264",
        "audio_aac_32khz_stereo": (
            audio.get("codec_name") == "aac" and audio.get("sample_rate") == "32000" and audio.get("channels") == 2
        ),
    }
    report_path = output_dir / "validation_report.json"
    write_json_atomic(report_path, {
        "schema_version": "1.0", "generated_at": utc_now(), "passed": all(checks.values()),
        "shot_count": len(shots), "duration_s": duration, "checks": checks,
        "outputs": {"without_burned_subtitles": str(no_subtitles), "subtitle": str(final_srt), "final": str(final_video)},
        "ffprobe": probe,
        "note": "No background music was added. Final perceptual review remains manual.",
    })
    if not all(checks.values()):
        raise ValueError(f"阶段 IX 技术验收失败，详见：{report_path}")
    return report_path
