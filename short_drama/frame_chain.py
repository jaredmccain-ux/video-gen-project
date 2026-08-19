"""Last-frame extraction and dependency chaining for MiniMax H3 shots."""

from __future__ import annotations

import subprocess
from pathlib import Path


def extract_last_frame(video_path: Path, output_path: Path) -> Path:
    """Extract the final decoded frame with ffmpeg."""
    if not video_path.is_file():
        raise FileNotFoundError(f"视频不存在：{video_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-sseof", "-0.05",
        "-i", str(video_path),
        "-frames:v", "1",
        "-q:v", "2",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError("未找到 ffmpeg，请先安装并加入 PATH") from exc
    if not output_path.is_file() or output_path.stat().st_size == 0:
        # fallback: seek from start with frame select last
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video_path),
            "-vf", "select=eq(n\\,N-1)",
            "-frames:v", "1",
            str(output_path),
        ]
        subprocess.run(cmd, check=True)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(f"抽取末帧失败：{video_path}")
    return output_path


def last_frame_path(run_dir: Path, shot_id: str) -> Path:
    return run_dir / "05_videos" / f"{shot_id}.last_frame.png"
