"""Studio-facing helpers for prompt review, subtitles, and assembly."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .h3_prompt import validate_rendered_prompt
from .human_orchestration import load_orchestration, normalize_mode
from .state import utc_now, write_json_atomic
from .subtitle_align import generate_planned_subtitles, _write_srt
from .subtitle_studio import (
    align_cues_to_speech,
    available_fonts,
    build_ass,
    burn_ass,
    cue_window,
    cues_from_speech,
    extract_speech_audio,
    ffmpeg_binaries,
    load_style,
    probe_video,
    reset_style,
    save_style,
    transcribe,
)
from .validators import load_json



def collect_shot_videos(run_dir: Path) -> list[dict[str, Any]]:
    shots_path = run_dir / "03_shots/shots.json"
    shots = load_json(shots_path).get("shots", []) if shots_path.is_file() else []
    jobs_path = run_dir / "05_videos/studio_jobs.json"
    jobs = load_json(jobs_path).get("jobs", {}) if jobs_path.is_file() else {}
    latest: dict[str, dict[str, Any]] = {}
    for job in jobs.values():
        if job.get("status") != "completed" or not job.get("video"):
            continue
        shot_id = str(job.get("shot_id") or "")
        previous = latest.get(shot_id)
        if previous is None or str(job.get("completed_at") or "") >= str(previous.get("completed_at") or ""):
            latest[shot_id] = job
    rows = []
    for shot in shots:
        shot_id = shot["shot_id"]
        studio = latest.get(shot_id)
        cli = run_dir / "05_videos" / f"{shot_id}.mp4"
        video = None
        source = None
        if studio and Path(studio["video"]).is_file():
            video = Path(studio["video"])
            source = "studio"
        elif cli.is_file():
            video = cli
            source = "cli"
        rows.append({
            "shot_id": shot_id,
            "duration_s": shot.get("duration_s"),
            "title": shot.get("story_purpose") or shot_id,
            "video": str(video) if video else None,
            "source": source,
            "ready": video is not None,
        })
    return rows


def prompt_review(run_dir: Path) -> dict[str, Any]:
    shots_path = run_dir / "03_shots/shots.json"
    shots = load_json(shots_path).get("shots", []) if shots_path.is_file() else []
    orchestration = load_orchestration(run_dir)
    items = []
    passed = 0
    for shot in shots:
        shot_id = shot["shot_id"]
        decision = (orchestration.get("shots") or {}).get(shot_id) or {}
        mode = normalize_mode(decision.get("generation_mode") or shot.get("generation_mode") or "t2va")
        prompt = str(decision.get("prompt") or "").strip()
        check_shot = {**shot, "generation_mode": mode, "speaker_mappings": shot.get("speaker_mappings") or []}
        try:
            errors = validate_rendered_prompt(prompt, check_shot) if prompt else ["尚未生成官方结构 Prompt"]
        except Exception as exc:  # noqa: BLE001 — review page must never 500
            errors = [f"{shot_id}: 校验异常 {type(exc).__name__}: {exc}"]
        ok = not errors
        if ok:
            passed += 1
        items.append({
            "shot_id": shot_id,
            "title": shot.get("story_purpose") or shot_id,
            "generation_mode": mode,
            "approved": bool(decision.get("approved")),
            "has_prompt": bool(prompt),
            "prompt": prompt,
            "errors": errors,
            "ok": ok,
        })
    return {
        "shot_count": len(items),
        "passed": passed,
        "blocked": len(items) - passed,
        "approved": sum(1 for item in items if item["approved"]),
        "shots": items,
    }


def subtitle_master(run_dir: Path) -> Path | None:
    """The clean cut that hard subtitles get burned onto, never a burned result."""
    for candidate in (
        run_dir / "07_final/studio_master.mp4",
        run_dir / "07_final/studio_concat.mp4",
        run_dir / "07_final/studio_final.mp4",
        run_dir / "07_final/final.mp4",
    ):
        if candidate.is_file():
            return candidate
    return None


def _film_geometry(run_dir: Path) -> dict[str, Any]:
    master = subtitle_master(run_dir)
    if master is None:
        shots = load_json(run_dir / "03_shots/shots.json").get("shots", []) if (run_dir / "03_shots/shots.json").is_file() else []
        planned = max((float(shot.get("planned_end_s") or 0) for shot in shots), default=0.0)
        return {"width": 0, "height": 0, "duration_s": planned, "video": None}
    try:
        probe = probe_video(master)
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        probe = {"width": 0, "height": 0, "duration_s": 0.0}
    return {**probe, "video": str(master)}


def write_subtitle_files(
    run_dir: Path,
    cues: list[dict[str, Any]],
    *,
    style: dict[str, Any] | None = None,
    method: str = "studio_manual_or_planned",
    asr: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output_dir = run_dir / "06_subtitles"
    output_dir.mkdir(parents=True, exist_ok=True)
    geometry = _film_geometry(run_dir)
    resolved_style = save_style(run_dir, style) if style else load_style(run_dir, height=geometry["height"] or 0)

    by_shot: dict[str, list[dict[str, Any]]] = {}
    for cue in cues:
        start, end = cue_window(cue)
        cue["film_start_s"], cue["film_end_s"] = round(start, 3), round(end, 3)
        offset = float(cue.get("planned_start_s") or 0)
        cue["start_s"] = round(max(0.0, start - offset), 3)
        cue["end_s"] = round(max(cue["start_s"] + 0.4, end - offset), 3)
        if cue.get("shot_id"):
            by_shot.setdefault(str(cue["shot_id"]), []).append(cue)
    for shot_id, shot_cues in by_shot.items():
        _write_srt(output_dir / f"{shot_id}.srt", shot_cues, global_time=False)

    srt_cues = [{**cue, "planned_start_s": cue["film_start_s"], "start_s": 0.0, "end_s": cue["film_end_s"] - cue["film_start_s"]} for cue in cues]
    _write_srt(output_dir / "full.srt", srt_cues, global_time=True)
    ass_path = output_dir / "full.ass"
    ass_path.write_text(
        build_ass(
            cues,
            style=resolved_style,
            width=geometry["width"],
            height=geometry["height"],
            title=str(load_json(run_dir / "02_story/story.json").get("title") or run_dir.name)
            if (run_dir / "02_story/story.json").is_file() else run_dir.name,
        ),
        encoding="utf-8",
    )
    write_json_atomic(output_dir / "timeline.json", {
        "schema_version": "2.0",
        "generated_at": utc_now(),
        "method": method,
        "asr_used": bool(asr),
        "asr": asr or {},
        "subtitle_cue_count": len(cues),
        "video": geometry["video"],
        "video_duration_s": geometry["duration_s"],
        "video_width": geometry["width"],
        "video_height": geometry["height"],
        "full_srt": str(output_dir / "full.srt"),
        "full_ass": str(ass_path),
        "cues": cues,
    })
    return load_subtitles(run_dir)


def load_subtitles(run_dir: Path) -> dict[str, Any]:
    timeline = run_dir / "06_subtitles/timeline.json"
    srt = run_dir / "06_subtitles/full.srt"
    ass = run_dir / "06_subtitles/full.ass"
    data = load_json(timeline) if timeline.is_file() else {"cues": [], "subtitle_cue_count": 0}
    cues = [cue for cue in data.get("cues") or [] if isinstance(cue, dict)]
    for cue in cues:
        start, end = cue_window(cue)
        cue["film_start_s"], cue["film_end_s"] = round(start, 3), round(end, 3)
    data["cues"] = cues
    data["srt_text"] = srt.read_text(encoding="utf-8") if srt.is_file() else ""
    data["ass_text"] = ass.read_text(encoding="utf-8") if ass.is_file() else ""
    data["exists"] = bool(cues)
    data["aligned"] = any(str(cue.get("timing_source") or "").startswith("asr") for cue in cues)
    data["style"] = load_style(run_dir, height=int(data.get("video_height") or 0))
    data["available_fonts"] = available_fonts()
    data["burned"] = (run_dir / "07_final/studio_final_sub.mp4").is_file()
    if not data.get("video_duration_s"):
        data["video_duration_s"] = _film_geometry(run_dir)["duration_s"]
    return data


def save_subtitle_cues(run_dir: Path, cues: list[dict[str, Any]], style: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = {cue.get("shot_id"): cue for cue in load_subtitles(run_dir).get("cues") or []}
    cleaned = []
    for cue in cues:
        if not isinstance(cue, dict) or not str(cue.get("text") or "").strip():
            continue
        shot_id = str(cue.get("shot_id") or "")
        base = previous.get(shot_id) or {}
        item = {
            "shot_id": shot_id,
            "speaker_id": str(cue.get("speaker_id") or base.get("speaker_id") or ""),
            "text": str(cue.get("text") or "").strip(),
            "planned_start_s": float(cue.get("planned_start_s") or base.get("planned_start_s") or 0),
            "planned_end_s": float(cue.get("planned_end_s") or base.get("planned_end_s") or 0),
            "film_start_s": float(cue.get("film_start_s") or 0),
            "film_end_s": float(cue.get("film_end_s") or 0),
            "timing_source": str(cue.get("timing_source") or "manual"),
        }
        if item["film_end_s"] <= item["film_start_s"]:
            raise ValueError(f"{shot_id or '字幕'} 的结束时间必须大于开始时间")
        cleaned.append(item)
    return write_subtitle_files(run_dir, cleaned, style=style)


def generate_studio_subtitles(run_dir: Path) -> dict[str, Any]:
    generate_planned_subtitles(run_dir, require_videos=False)
    planned = load_json(run_dir / "06_subtitles/timeline.json").get("cues") or []
    return write_subtitle_files(run_dir, planned, method="planned_dialogue_deterministic_timing")


def align_subtitles_to_speech(run_dir: Path, config: ProjectConfig | None = None) -> dict[str, Any]:
    """Re-time the scripted captions against the speech actually present in the film."""
    geometry = _film_geometry(run_dir)
    if not geometry["video"]:
        raise FileNotFoundError("还没有可分析的成片。请先在合片验收生成成片，或导入已有成片。")
    audio = extract_speech_audio(Path(geometry["video"]), run_dir / "06_subtitles/audio/film_16k.wav")
    transcript = transcribe(audio, config=config, log_path=run_dir / "logs/subtitle_asr.log")
    segments = transcript.get("segments") or []
    if not segments:
        raise ValueError("语音识别没有找到人声片段，无法对齐字幕。")

    existing = load_subtitles(run_dir).get("cues") or []
    if not existing:
        generate_studio_subtitles(run_dir)
        existing = load_subtitles(run_dir).get("cues") or []

    shots = load_json(run_dir / "03_shots/shots.json").get("shots", []) if (run_dir / "03_shots/shots.json").is_file() else []
    if existing:
        stats = align_cues_to_speech(existing, segments, video_duration_s=geometry["duration_s"])
        cues = existing
    else:
        cues = cues_from_speech(segments, shots, video_duration_s=geometry["duration_s"])
        stats = {"matched_cue_count": len(cues), "cue_count": len(cues), "segment_count": len(segments)}

    asr_report = {
        "model": transcript.get("model"),
        "segment_count": len(segments),
        "matched_cue_count": stats["matched_cue_count"],
        "audio": str(audio),
        "segments_file": str(audio.parent / "asr_segments.json"),
    }
    payload = write_subtitle_files(run_dir, cues, method="asr_aligned_script_wording", asr=asr_report)
    return {**payload, **stats}


def reset_subtitle_style(run_dir: Path) -> dict[str, Any]:
    reset_style(run_dir)
    cues = load_subtitles(run_dir).get("cues") or []
    return write_subtitle_files(run_dir, cues) if cues else load_subtitles(run_dir)


def burn_studio_subtitles(run_dir: Path) -> dict[str, Any]:
    """Burn 06_subtitles/full.ass onto the clean master, leaving the master in place."""
    ass_path = run_dir / "06_subtitles/full.ass"
    if not ass_path.is_file():
        raise FileNotFoundError("还没有字幕文件。请先生成或对齐字幕。")
    master = subtitle_master(run_dir)
    if master is None:
        raise FileNotFoundError("还没有可烧录的成片。请先在合片验收生成成片。")
    preserved = run_dir / "07_final/studio_master.mp4"
    if not preserved.is_file():
        shutil.copyfile(master, preserved)
    target = burn_ass(preserved, ass_path, run_dir / "07_final/studio_final_sub.mp4")
    probe = probe_video(target)
    report_path = run_dir / "07_final/studio_assemble.json"
    report = load_json(report_path) if report_path.is_file() else {}
    report.update({
        "burned_subtitles": True,
        "burned_at": utc_now(),
        "subtitle_file": str(ass_path),
        "duration_s": probe["duration_s"],
        "outputs": {**(report.get("outputs") or {}), "master": str(preserved), "final_with_subtitles": str(target)},
    })
    write_json_atomic(report_path, report)
    return {"video": str(target), "duration_s": probe["duration_s"], "report": report}


def _ffmpeg() -> tuple[str, str]:
    return ffmpeg_binaries()


def assemble_studio_run(run_dir: Path, *, burn_subtitles: bool = True) -> dict[str, Any]:
    ffmpeg, ffprobe = _ffmpeg()
    rows = collect_shot_videos(run_dir)
    ready = [row for row in rows if row["ready"]]
    if not ready:
        raise FileNotFoundError("还没有可拼接的镜头视频。请先在人工编排或视频生成页完成至少一镜。")
    output_dir = run_dir / "07_final"
    normalized_dir = output_dir / "studio_normalized"
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized_dir.mkdir(parents=True, exist_ok=True)
    normalized: list[Path] = []
    for row in ready:
        source = Path(row["video"])
        target = normalized_dir / f"{row['shot_id']}.mp4"
        duration = float(row.get("duration_s") or 6)
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                "-t", f"{duration:g}",
                "-vf", "fps=24,scale=864:480:flags=lanczos,setsar=1,format=yuv420p",
                "-af", "aresample=32000,apad",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", "-ar", "32000", "-ac", "2",
                "-movflags", "+faststart", str(target),
            ],
            check=True,
        )
        normalized.append(target)
    concat_list = output_dir / "studio_concat.txt"
    concat_list.write_text("".join(f"file '{path.as_posix()}'\n" for path in normalized), encoding="utf-8")
    no_subs = output_dir / "studio_concat.mp4"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
         "-c", "copy", "-movflags", "+faststart", str(no_subs)],
        check=True,
    )
    final_video = output_dir / "studio_final.mp4"
    shutil.copyfile(no_subs, final_video)
    shutil.copyfile(no_subs, output_dir / "studio_master.mp4")
    probe = json.loads(subprocess.check_output(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(final_video)],
        text=True,
    ))
    duration = float((probe.get("format") or {}).get("duration") or 0)
    report = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "shot_count": len(ready),
        "missing": [row["shot_id"] for row in rows if not row["ready"]],
        "duration_s": duration,
        "burned_subtitles": False,
        "outputs": {
            "without_subtitles": str(no_subs),
            "master": str(output_dir / "studio_master.mp4"),
            "final": str(final_video),
        },
    }
    write_json_atomic(output_dir / "studio_assemble.json", report)
    subtitled = None
    if burn_subtitles and (run_dir / "06_subtitles/full.ass").is_file():
        subtitled = burn_studio_subtitles(run_dir)
        report = subtitled["report"]
    delivered = subtitled["video"] if subtitled else str(final_video)
    return {**report, "final_url_path": delivered}


def final_status(run_dir: Path) -> dict[str, Any]:
    report_path = run_dir / "07_final/studio_assemble.json"
    report = load_json(report_path) if report_path.is_file() else {}
    video = next(
        (
            candidate
            for candidate in (
                run_dir / "07_final/studio_final_sub.mp4",
                run_dir / "07_final/studio_final.mp4",
                run_dir / "07_final/final.mp4",
            )
            if candidate.is_file()
        ),
        None,
    )
    return {
        "ready": video is not None,
        "video": str(video) if video else None,
        "burned_subtitles": bool(report.get("burned_subtitles")),
        "report": report,
        "videos": collect_shot_videos(run_dir),
    }
