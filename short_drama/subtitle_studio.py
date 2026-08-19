"""Cinema-style hard subtitles: ASS styling, speech alignment, and burn-in.

Three rules hold throughout:

* wording comes from the approved shot dialogue, never from the recognizer;
* timing comes from real speech boundaries (FSMN VAD windows, SenseVoice text used
  only to decide which line sits in which window);
* rendering is ASS through libass, because SRT ``force_style`` cannot express
  per-line fades, spacing, and scaled borders reliably.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .state import utc_now, write_json_atomic


FONT_DIR = Path("/usr/share/fonts/opentype/noto")
FALLBACK_FONTS = ("Noto Sans CJK SC", "Noto Serif CJK SC", "Source Han Sans SC")

# The defaults were tuned on a 1344x768 master; every geometric value below is
# expressed at that height and scaled to the actual video on generation.
REFERENCE_HEIGHT = 768
REFERENCE_STYLE: dict[str, Any] = {
    "font_name": "Noto Sans CJK SC",
    "font_size": 44,
    "bold": True,
    "outline": 2.6,
    "shadow": 0.8,
    "spacing": 0.4,
    "margin_v": 46,
    "margin_h": 70,
    "max_chars_per_line": 18,
    "max_lines": 2,
    "fade_in_ms": 80,
    "fade_out_ms": 60,
    "primary_colour": "&H00FFFFFF",
    "outline_colour": "&H00000000",
    "back_colour": "&H64000000",
    "alignment": 2,
}
SCALED_KEYS = ("font_size", "outline", "shadow", "margin_v", "margin_h")

ASR_MIN_CUE_S = 0.6
ASR_CUE_GAP_S = 0.08
ASR_MATCH_THRESHOLD = 0.18
ASR_MAX_MERGED_WINDOWS = 3
ASR_MAX_LINES_PER_STEP = 8
ASR_VOICED_RATIO = 0.18
ASR_EDGE_PAD_S = 0.08
ASR_CUT_RADIUS_S = 0.45
PUNCTUATION_RE = re.compile(r"[\s，。、！？；：…—～·「」『』“”\"'（）()《》【】,.!?;:~\-]+")
LINE_BREAK_HINTS = "，。！？；：、,.!?;:"


# --------------------------------------------------------------------------- ffmpeg


def ffmpeg_binaries() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise FileNotFoundError("本机未找到 ffmpeg/ffprobe，无法处理字幕")
    return ffmpeg, ffprobe


def probe_video(path: Path) -> dict[str, Any]:
    _, ffprobe = ffmpeg_binaries()
    payload = json.loads(subprocess.check_output(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        text=True,
    ))
    video = next((item for item in payload.get("streams") or [] if item.get("codec_type") == "video"), {})
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "duration_s": float((payload.get("format") or {}).get("duration") or 0),
    }


def extract_speech_audio(video: Path, target: Path) -> Path:
    """16 kHz mono PCM is what both FSMN VAD and SenseVoice expect."""
    ffmpeg, _ = ffmpeg_binaries()
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
         "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(target)],
        check=True,
    )
    return target


# ----------------------------------------------------------------------------- style


def available_fonts() -> list[str]:
    binary = shutil.which("fc-list")
    if not binary:
        return list(FALLBACK_FONTS)
    try:
        output = subprocess.check_output([binary, ":lang=zh", "family"], text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return list(FALLBACK_FONTS)
    names: list[str] = []
    for line in output.splitlines():
        value = line.split(",", 1)[0].strip()
        if value and value not in names:
            names.append(value)
    ordered = [name for name in FALLBACK_FONTS if name in names]
    return ordered + [name for name in sorted(names) if name not in ordered]


def default_style(height: int = REFERENCE_HEIGHT, *, config: ProjectConfig | None = None) -> dict[str, Any]:
    scale = max(0.35, (height or REFERENCE_HEIGHT) / REFERENCE_HEIGHT)
    style = dict(REFERENCE_STYLE)
    for key in SCALED_KEYS:
        value = REFERENCE_STYLE[key] * scale
        style[key] = round(value) if key in {"font_size", "margin_v", "margin_h"} else round(value, 2)
    overrides = ((config.data.get("subtitles") if config else None) or {}).get("style") or {}
    style.update({key: value for key, value in overrides.items() if key in style})
    return style


def style_path(run_dir: Path) -> Path:
    return run_dir / "06_subtitles/style.json"


def load_style(run_dir: Path, *, height: int = REFERENCE_HEIGHT, config: ProjectConfig | None = None) -> dict[str, Any]:
    style = default_style(height, config=config)
    path = style_path(run_dir)
    if path.is_file():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            saved = {}
        if isinstance(saved, dict):
            style.update({key: value for key, value in saved.items() if key in style})
    return normalize_style(style)


def normalize_style(style: dict[str, Any]) -> dict[str, Any]:
    merged = dict(REFERENCE_STYLE)
    merged.update({key: value for key, value in (style or {}).items() if key in REFERENCE_STYLE})
    return {
        "font_name": str(merged["font_name"]).strip() or REFERENCE_STYLE["font_name"],
        "font_size": max(10, min(160, int(round(float(merged["font_size"]))))),
        "bold": bool(merged["bold"]),
        "outline": max(0.0, min(10.0, round(float(merged["outline"]), 2))),
        "shadow": max(0.0, min(10.0, round(float(merged["shadow"]), 2))),
        "spacing": max(0.0, min(6.0, round(float(merged["spacing"]), 2))),
        "margin_v": max(0, min(400, int(round(float(merged["margin_v"]))))),
        "margin_h": max(0, min(600, int(round(float(merged["margin_h"]))))),
        "max_chars_per_line": max(6, min(48, int(round(float(merged["max_chars_per_line"]))))),
        "max_lines": max(1, min(3, int(round(float(merged["max_lines"]))))),
        "fade_in_ms": max(0, min(1000, int(round(float(merged["fade_in_ms"]))))),
        "fade_out_ms": max(0, min(1000, int(round(float(merged["fade_out_ms"]))))),
        "primary_colour": str(merged["primary_colour"]),
        "outline_colour": str(merged["outline_colour"]),
        "back_colour": str(merged["back_colour"]),
        "alignment": int(merged["alignment"]) if int(merged["alignment"]) in range(1, 10) else 2,
    }


def save_style(run_dir: Path, style: dict[str, Any]) -> dict[str, Any]:
    cleaned = normalize_style(style)
    write_json_atomic(style_path(run_dir), {"schema_version": "1.0", "updated_at": utc_now(), **cleaned})
    return cleaned


def reset_style(run_dir: Path) -> None:
    style_path(run_dir).unlink(missing_ok=True)


# ------------------------------------------------------------------------------- ASS


def ass_time(seconds: float) -> str:
    total = max(0.0, float(seconds))
    hours, remainder = divmod(int(total), 3600)
    minutes, secs = divmod(remainder, 60)
    centiseconds = int(round((total - int(total)) * 100))
    if centiseconds == 100:
        centiseconds = 99
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def cue_window(cue: dict[str, Any]) -> tuple[float, float]:
    """Absolute position on the finished film, whatever the cue was authored against."""
    if cue.get("film_start_s") is not None and cue.get("film_end_s") is not None:
        return float(cue["film_start_s"]), float(cue["film_end_s"])
    offset = float(cue.get("planned_start_s") or 0)
    return offset + float(cue.get("start_s") or 0), offset + float(cue.get("end_s") or 0)


def wrap_subtitle_text(text: str, *, max_chars: int, max_lines: int) -> str:
    """Break Chinese captions at punctuation so libass never wraps mid-phrase."""
    body = re.sub(r"\s+", "", str(text or "")).replace("\\N", "")
    if len(body) <= max_chars or max_lines <= 1:
        return body
    lines: list[str] = []
    remaining = body
    while remaining and len(lines) < max_lines - 1:
        window = remaining[: max_chars + 1]
        cut = max((window.rfind(mark) for mark in LINE_BREAK_HINTS), default=-1)
        if cut < max_chars // 2:
            cut = max_chars - 1
        lines.append(remaining[: cut + 1])
        remaining = remaining[cut + 1 :]
    if remaining:
        lines.append(remaining)
    return "\\N".join(line for line in lines if line)


def build_ass(
    cues: list[dict[str, Any]],
    *,
    style: dict[str, Any],
    width: int,
    height: int,
    title: str = "SceneFlow",
) -> str:
    resolved = normalize_style(style)
    fade = ""
    if resolved["fade_in_ms"] or resolved["fade_out_ms"]:
        fade = f"{{\\fad({resolved['fade_in_ms']},{resolved['fade_out_ms']})}}"
    header = [
        "[Script Info]",
        f"Title: {title}",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        f"PlayResX: {int(width or REFERENCE_HEIGHT * 16 // 9)}",
        f"PlayResY: {int(height or REFERENCE_HEIGHT)}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,"
        " Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline,"
        " Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        ",".join((
            "Style: Default",
            resolved["font_name"],
            str(resolved["font_size"]),
            resolved["primary_colour"],
            "&H000000FF",
            resolved["outline_colour"],
            resolved["back_colour"],
            "-1" if resolved["bold"] else "0",
            "0", "0", "0", "100", "100",
            f"{resolved['spacing']:g}",
            "0", "1",
            f"{resolved['outline']:g}",
            f"{resolved['shadow']:g}",
            str(resolved["alignment"]),
            str(resolved["margin_h"]),
            str(resolved["margin_h"]),
            str(resolved["margin_v"]),
            "1",
        )),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = []
    for cue in cues:
        text = wrap_subtitle_text(
            cue.get("text") or "",
            max_chars=resolved["max_chars_per_line"],
            max_lines=resolved["max_lines"],
        )
        if not text:
            continue
        start, end = cue_window(cue)
        if end <= start:
            continue
        events.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,"
            f"{cue.get('speaker_id') or ''},0,0,0,,{fade}{text}"
        )
    return "\n".join(header + events) + "\n"


# ------------------------------------------------------------------------- alignment


def _normalize_for_match(text: str) -> str:
    return PUNCTUATION_RE.sub("", str(text or ""))


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def voiced_window(segment: dict[str, Any]) -> tuple[float, float]:
    """VAD brackets speech generously; the RMS envelope says where it really starts."""
    start, end = float(segment["start"]), float(segment["end"])
    energy = segment.get("energy") or []
    frame = float(segment.get("frame_s") or 0.02)
    if len(energy) < 5:
        return start, end
    peak = sorted(energy)[int(len(energy) * 0.95)]
    threshold = max(peak * ASR_VOICED_RATIO, 200)
    voiced = [index for index, value in enumerate(energy) if value >= threshold]
    if not voiced:
        return start, end
    lead = max(start, start + voiced[0] * frame - ASR_EDGE_PAD_S)
    tail = min(end, start + (voiced[-1] + 1) * frame + ASR_EDGE_PAD_S)
    return (lead, tail) if tail - lead >= ASR_MIN_CUE_S else (start, end)


def _quietest_time(segment: dict[str, Any], target: float, radius: float) -> float:
    """Snap a line boundary to the nearest pause instead of cutting mid-word."""
    energy = segment.get("energy") or []
    frame = float(segment.get("frame_s") or 0.02)
    origin = float(segment["start"])
    if len(energy) < 5:
        return target
    low = max(0, int((target - radius - origin) / frame))
    high = min(len(energy) - 1, int((target + radius - origin) / frame))
    if high <= low:
        return target
    index = min(range(low, high + 1), key=lambda position: energy[position])
    return origin + (index + 0.5) * frame


def _spread(window: tuple[float, float], cues: list[dict[str, Any]], segment: dict[str, Any] | None = None) -> None:
    """Split one speech window across the lines it contains, proportional to length."""
    start, end = window
    weights = [max(1, len(_normalize_for_match(cue.get("text") or ""))) for cue in cues]
    total = float(sum(weights))
    boundaries = [start]
    cursor = 0.0
    for weight in weights[:-1]:
        cursor += weight / total
        ideal = start + (end - start) * cursor
        candidate = _quietest_time(segment, ideal, ASR_CUT_RADIUS_S) if segment else ideal
        floor = boundaries[-1] + ASR_MIN_CUE_S
        boundaries.append(min(max(candidate, floor), end - ASR_MIN_CUE_S * (len(weights) - len(boundaries))))
    boundaries.append(end)
    for index, cue in enumerate(cues):
        cue_start = boundaries[index]
        cue_end = max(cue_start + ASR_MIN_CUE_S, boundaries[index + 1] - (ASR_CUE_GAP_S if index + 2 < len(boundaries) else 0.0))
        cue["film_start_s"] = round(cue_start, 3)
        cue["film_end_s"] = round(cue_end, 3)


def _partition(segments: list[dict[str, Any]], cues: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Hand each merged VAD window the consecutive lines that sound most like it.

    Without this, a group spanning three windows would spread its lines evenly across
    the silence between them.
    """
    if len(segments) == 1:
        return [(segments[0], cues)]
    heard = [_normalize_for_match(segment.get("text") or "") for segment in segments]
    scripted = [_normalize_for_match(cue.get("text") or "") for cue in cues]
    best = [[(-1.0, 0)] * (len(cues) + 1) for _ in range(len(segments) + 1)]
    best[0][0] = (0.0, 0)
    for row in range(1, len(segments) + 1):
        for taken in range(len(cues) + 1):
            for previous in range(taken + 1):
                if best[row - 1][previous][0] < 0:
                    continue
                score = best[row - 1][previous][0] + _similarity(heard[row - 1], "".join(scripted[previous:taken]))
                if score > best[row][taken][0]:
                    best[row][taken] = (score, previous)
    groups: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    taken = len(cues)
    for row in range(len(segments), 0, -1):
        previous = best[row][taken][1]
        if taken > previous:
            groups.append((segments[row - 1], cues[previous:taken]))
        taken = previous
    return list(reversed(groups))


def align_cues_to_speech(
    cues: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    *,
    video_duration_s: float = 0.0,
) -> dict[str, Any]:
    """Move planned cues onto recognized speech windows, keeping the scripted wording.

    VAD windows regularly merge several lines, and one line can straddle two windows, so
    each step scores every ``m`` windows against ``k`` upcoming lines and takes the pair
    with the best textual overlap.
    """
    pending = [cue for cue in cues if str(cue.get("text") or "").strip()]
    normalized = [_normalize_for_match(segment.get("text") or "") for segment in segments]
    matched = 0
    cue_index = 0
    segment_index = 0
    while cue_index < len(pending) and segment_index < len(segments):
        best: tuple[float, int, int] | None = None
        for merge in range(1, min(ASR_MAX_MERGED_WINDOWS, len(segments) - segment_index) + 1):
            heard = "".join(normalized[segment_index : segment_index + merge])
            if not heard:
                continue
            for count in range(1, min(ASR_MAX_LINES_PER_STEP, len(pending) - cue_index) + 1):
                scripted = "".join(
                    _normalize_for_match(pending[cue_index + offset].get("text") or "")
                    for offset in range(count)
                )
                score = _similarity(heard, scripted)
                if best is None or score > best[0]:
                    best = (score, merge, count)
        if best is None:
            segment_index += 1
            continue
        score, merge, count = best
        if score < ASR_MATCH_THRESHOLD:
            segment_index += 1
            continue
        group = pending[cue_index : cue_index + count]
        for segment, lines in _partition(segments[segment_index : segment_index + merge], group):
            _spread(voiced_window(segment), lines, segment)
        for cue in group:
            cue["timing_source"] = "asr_sensevoice_vad"
            cue["asr_match_score"] = round(score, 3)
        matched += count
        cue_index += count
        segment_index += merge

    # Lines the recognizer never reached keep a readable window right after the last hit.
    cursor = 0.0
    for cue in pending:
        if cue.get("film_start_s") is None:
            span = max(ASR_MIN_CUE_S, len(_normalize_for_match(cue.get("text") or "")) * 0.22)
            cue["film_start_s"] = round(cursor + ASR_CUE_GAP_S, 3)
            cue["film_end_s"] = round(cursor + ASR_CUE_GAP_S + span, 3)
            cue["timing_source"] = "asr_unmatched_interpolated"
        cursor = float(cue["film_end_s"])

    enforce_monotonic(pending, video_duration_s=video_duration_s)
    return {"matched_cue_count": matched, "cue_count": len(pending), "segment_count": len(segments)}


def enforce_monotonic(cues: list[dict[str, Any]], *, video_duration_s: float = 0.0) -> None:
    """No overlaps, no sub-readable durations, nothing past the end of the film."""
    limit = float(video_duration_s or 0)
    previous_end = 0.0
    for cue in cues:
        start, end = cue_window(cue)
        start = max(start, previous_end + (ASR_CUE_GAP_S if previous_end else 0.0), 0.0)
        end = max(end, start + ASR_MIN_CUE_S)
        if limit:
            end = min(end, limit)
            start = min(start, max(0.0, end - ASR_MIN_CUE_S))
        cue["film_start_s"] = round(start, 3)
        cue["film_end_s"] = round(end, 3)
        previous_end = end


def cues_from_speech(segments: list[dict[str, Any]], shots: list[dict[str, Any]], *, video_duration_s: float = 0.0) -> list[dict[str, Any]]:
    """Last resort when a run has no scripted dialogue: subtitle what was actually said."""
    cues = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start, end = float(segment["start"]), float(segment["end"])
        cues.append({
            "shot_id": shot_at_film_time(shots, (start + end) / 2, video_duration_s=video_duration_s),
            "speaker_id": "",
            "text": text,
            "film_start_s": round(start, 3),
            "film_end_s": round(end, 3),
            "timing_source": "asr_sensevoice_vad",
        })
    enforce_monotonic(cues, video_duration_s=video_duration_s)
    return cues


def shot_at_film_time(shots: list[dict[str, Any]], film_time: float, *, video_duration_s: float = 0.0) -> str:
    """Planned shot windows rarely match the delivered runtime, so scale before lookup."""
    if not shots:
        return ""
    planned_total = max(float(shot.get("planned_end_s") or 0) for shot in shots)
    scale = (planned_total / video_duration_s) if planned_total and video_duration_s else 1.0
    target = film_time * scale
    for shot in shots:
        if float(shot.get("planned_start_s") or 0) <= target < float(shot.get("planned_end_s") or 0):
            return str(shot.get("shot_id") or "")
    return str(shots[-1].get("shot_id") or "")


# ------------------------------------------------------------------------------- ASR


def asr_settings(config: ProjectConfig | None) -> dict[str, Any]:
    values = ((config.data.get("subtitles") if config else None) or {}).get("asr") or {}
    return {
        "python": str(values.get("python") or ""),
        "sensevoice_dir": str(values.get("sensevoice_dir") or ""),
        "vad_dir": str(values.get("vad_dir") or ""),
        "device": str(values.get("device") or "cpu"),
        "max_segment_ms": int(values.get("max_segment_ms") or 15000),
    }


def transcribe(audio: Path, *, config: ProjectConfig | None, log_path: Path | None = None) -> dict[str, Any]:
    settings = asr_settings(config)
    interpreter = Path(settings["python"])
    sensevoice = Path(settings["sensevoice_dir"])
    vad = Path(settings["vad_dir"])
    if not interpreter.is_file():
        raise FileNotFoundError(
            "未配置可用的语音识别环境。请在项目配置 subtitles.asr.python 指向装有 funasr 的 Python。"
        )
    for label, path in (("SenseVoice", sensevoice), ("FSMN VAD", vad)):
        if not path.is_dir():
            raise FileNotFoundError(f"{label} 模型目录不存在：{path}")

    output = audio.parent / "asr_segments.json"
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
        json.dump({
            "audio": str(audio),
            "output": str(output),
            "sensevoice_dir": str(sensevoice),
            "vad_dir": str(vad),
            "device": settings["device"],
            "max_segment_ms": settings["max_segment_ms"],
        }, handle)
        request = Path(handle.name)
    try:
        completed = subprocess.run(
            [str(interpreter), str(Path(__file__).with_name("asr_worker.py")), str(request)],
            capture_output=True,
            text=True,
        )
    finally:
        request.unlink(missing_ok=True)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"# {utc_now()}\n$ {interpreter} asr_worker.py\n\n{completed.stdout}\n{completed.stderr}\n",
            encoding="utf-8",
        )
    if completed.returncode != 0 or not output.is_file():
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError("语音识别失败：" + (" / ".join(tail) or "无输出"))
    return json.loads(output.read_text(encoding="utf-8"))


# -------------------------------------------------------------------------- burn-in


def burn_ass(video: Path, subtitles: Path, target: Path, *, crf: int = 17, preset: str = "medium") -> Path:
    """Re-encode video only; the delivered audio track is copied through untouched."""
    ffmpeg, _ = ffmpeg_binaries()
    target.parent.mkdir(parents=True, exist_ok=True)
    filter_arg = f"ass={subtitles.as_posix()}"
    if FONT_DIR.is_dir():
        filter_arg += f":fontsdir={FONT_DIR.as_posix()}"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
         "-vf", filter_arg,
         "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", str(target)],
        check=True,
    )
    return target
