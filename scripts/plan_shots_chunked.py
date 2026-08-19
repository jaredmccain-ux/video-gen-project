#!/usr/bin/env python3
"""Plan shots in beat chunks to avoid long upstream timeouts."""

from __future__ import annotations

import argparse
import json
import math
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from short_drama.approval import approval_status, stage_paths
from short_drama.azure_client import create_text_completion
from short_drama.config import load_config
from short_drama.shots import (
    _apply_recovery_overrides,
    _dialogue_exception_ids,
    _json_from_text,
    _normalize_known_enums,
    _normalize_workflow_fields,
    _package_root,
    _planning_schema,
    _timeline_markdown,
    _usage_dict,
)
from short_drama.state import read_run, utc_now, write_json_atomic
from short_drama.validators import dialogue_pacing_warnings, validate_document, validate_shots_against_story

MIN_SHOT_S = 4
MAX_SHOT_S = 8


def _chunk_beats(beats: list[dict[str, Any]], chunk_size: int) -> list[list[dict[str, Any]]]:
    return [beats[i : i + chunk_size] for i in range(0, len(beats), chunk_size)]


def _merge_two_shots(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(a)
    merged["story_purpose"] = "；".join(
        part for part in [a.get("story_purpose", "").strip(), b.get("story_purpose", "").strip()] if part
    )
    a_action = str(a.get("action_timeline", "")).strip()
    b_action = str(b.get("action_timeline", "")).strip()
    merged["action_timeline"] = f"{a_action} 接：{b_action}".strip() if a_action and b_action else (a_action or b_action)
    merged["continuity_out"] = b.get("continuity_out", a.get("continuity_out"))
    chars: list[str] = []
    for cid in list(a.get("characters", [])) + list(b.get("characters", [])):
        if cid not in chars:
            chars.append(cid)
    merged["characters"] = chars

    blocking_by: dict[str, dict[str, Any]] = {}
    for item in a.get("blocking", []):
        blocking_by[item["character_id"]] = deepcopy(item)
    for item in b.get("blocking", []):
        cid = item["character_id"]
        if cid in blocking_by:
            blocking_by[cid]["end"] = deepcopy(item.get("end", blocking_by[cid].get("end")))
            blocking_by[cid]["speaks"] = bool(blocking_by[cid].get("speaks") or item.get("speaks"))
            if item.get("movement_direction") and item.get("movement_direction") != "none":
                blocking_by[cid]["movement_direction"] = item["movement_direction"]
        else:
            blocking_by[cid] = deepcopy(item)
    merged["blocking"] = [blocking_by[cid] for cid in chars if cid in blocking_by]

    merged["dialogue"] = list(a.get("dialogue", [])) + list(b.get("dialogue", []))
    merged["subtitle_text"] = "".join(item.get("text", "") for item in merged["dialogue"])
    mappings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(a.get("speaker_mappings", [])) + list(b.get("speaker_mappings", [])):
        cid = item.get("character_id")
        if cid in seen:
            continue
        seen.add(cid)
        mappings.append(deepcopy(item))
    merged["speaker_mappings"] = mappings

    audio_a = a.get("audio_contract") or {}
    audio_b = b.get("audio_contract") or {}
    allowed: list[str] = []
    for cid in list(audio_a.get("allowed_speaker_ids", [])) + list(audio_b.get("allowed_speaker_ids", [])):
        if cid not in allowed:
            allowed.append(cid)
    def _uniq(values: list[Any]) -> list[Any]:
        out: list[Any] = []
        for value in values:
            if value not in out:
                out.append(value)
        return out
    merged["audio_contract"] = {
        "allowed_speaker_ids": allowed,
        "offscreen_human_voice_allowed": bool(
            audio_a.get("offscreen_human_voice_allowed") or audio_b.get("offscreen_human_voice_allowed")
        ),
        "non_diegetic_music": bool(audio_a.get("non_diegetic_music") or audio_b.get("non_diegetic_music")),
        "ambient_sounds": _uniq(list(audio_a.get("ambient_sounds", [])) + list(audio_b.get("ambient_sounds", []))),
        "action_sounds": _uniq(list(audio_a.get("action_sounds", [])) + list(audio_b.get("action_sounds", []))),
    }
    return merged


def _split_shot(shot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    first = deepcopy(shot)
    second = deepcopy(shot)
    action = str(shot.get("action_timeline", "")).strip()
    if "。" in action:
        parts = [p.strip() for p in action.split("。") if p.strip()]
        mid = max(1, len(parts) // 2)
        first["action_timeline"] = "。".join(parts[:mid]) + "。"
        second["action_timeline"] = "。".join(parts[mid:]) + ("。" if parts[mid:] else "")
    else:
        first["action_timeline"] = action
        second["action_timeline"] = action

    bridge = "动作衔接，时空连续。"
    first["continuity_out"] = bridge
    second["continuity_in"] = bridge
    first["story_purpose"] = (shot.get("story_purpose") or "") + "（前半）"
    second["story_purpose"] = (shot.get("story_purpose") or "") + "（后半）"

    dialogue = list(shot.get("dialogue", []))
    if len(dialogue) >= 2:
        mid = len(dialogue) // 2
        first["dialogue"] = deepcopy(dialogue[:mid])
        second["dialogue"] = deepcopy(dialogue[mid:])
    else:
        first["dialogue"] = []
        second["dialogue"] = deepcopy(dialogue)
    first["subtitle_text"] = "".join(item.get("text", "") for item in first["dialogue"])
    second["subtitle_text"] = "".join(item.get("text", "") for item in second["dialogue"])

    for item in first.get("blocking", []):
        item["speaks"] = False
        if "end" in item and "start" in item:
            item["end"] = deepcopy(item["start"])
            if isinstance(item["end"], dict):
                item["end"]["mouth_state"] = "closed"
    speakers = {d.get("speaker_id") for d in second.get("dialogue", [])}
    for item in second.get("blocking", []):
        item["speaks"] = item.get("character_id") in speakers
    return first, second


def _allocate_durations(count: int, target: int) -> list[int]:
    if count * MIN_SHOT_S > target or count * MAX_SHOT_S < target:
        raise ValueError(f"无法把 {target}s 分配到 {count} 个镜头（每镜 {MIN_SHOT_S}-{MAX_SHOT_S}s）")
    durs = [MIN_SHOT_S] * count
    rem = target - MIN_SHOT_S * count
    idx = count - 1
    while rem > 0:
        add = min(MAX_SHOT_S - durs[idx], rem)
        durs[idx] += add
        rem -= add
        idx -= 1
        if idx < 0:
            idx = count - 1
    return durs


def _fit_shots_to_beat_durations(shots: list[dict[str, Any]], story: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge/split and retime shots so each beat sums exactly to its duration_s."""
    targets = {beat["beat_id"]: int(round(float(beat["duration_s"]))) for beat in story.get("beats", [])}
    order = [beat["beat_id"] for beat in story.get("beats", [])]
    grouped: dict[str, list[dict[str, Any]]] = {beat_id: [] for beat_id in order}
    for shot in shots:
        beat_id = shot["beat_id"]
        if beat_id not in grouped:
            raise ValueError(f"未知 beat_id: {beat_id}")
        grouped[beat_id].append(deepcopy(shot))

    fitted: list[dict[str, Any]] = []
    for beat_id in order:
        group = grouped[beat_id]
        target = targets[beat_id]
        min_n = math.ceil(target / MAX_SHOT_S)
        max_n = target // MIN_SHOT_S
        if not group:
            raise ValueError(f"{beat_id}: 无镜头可拟合")
        # Prefer fewest valid shots so global count stays within schema maxItems (24).
        target_n = min_n
        while len(group) > target_n:
            # Merge the adjacent pair with the smallest combined duration proxy (prefer tail).
            best_i = len(group) - 2
            best_score = None
            for i in range(len(group) - 1):
                score = float(group[i].get("duration_s", MIN_SHOT_S)) + float(group[i + 1].get("duration_s", MIN_SHOT_S))
                if best_score is None or score <= best_score:
                    best_score = score
                    best_i = i
            group = group[:best_i] + [_merge_two_shots(group[best_i], group[best_i + 1])] + group[best_i + 2 :]
        while len(group) < target_n:
            idx = max(range(len(group)), key=lambda i: float(group[i].get("duration_s", MIN_SHOT_S)))
            left, right = _split_shot(group[idx])
            group = group[:idx] + [left, right] + group[idx + 1 :]
        durs = _allocate_durations(len(group), target)
        for shot, duration in zip(group, durs):
            shot["duration_s"] = duration
            fitted.append(shot)
    return fitted



def _replace_ambiguous_terms(text: str) -> str:
    replacements = (
        ("身前", "近处"),
        ("身后", "远处"),
        ("前面", "近处"),
        ("后面", "远处"),
        ("前方", "近处"),
        ("后方", "远处"),
        ("前边", "近处"),
        ("后边", "远处"),
    )
    for src, dst in replacements:
        text = text.replace(src, dst)
    return text


def _sync_speech_contract(shot: dict[str, Any]) -> None:
    dialogue = list(shot.get("dialogue") or [])
    speakers = []
    for item in dialogue:
        sid = item.get("speaker_id")
        if sid and sid not in speakers:
            speakers.append(sid)
    # Enforce single speaker: keep first speaker only; fold others into action notes.
    if len(speakers) > 1:
        keep = speakers[0]
        kept = [item for item in dialogue if item.get("speaker_id") == keep]
        dropped = [item for item in dialogue if item.get("speaker_id") != keep]
        if dropped:
            note = "；".join(f'{item.get("speaker_id")}说“{item.get("text","")}”' for item in dropped)
            shot["action_timeline"] = (str(shot.get("action_timeline", "")).rstrip("。") + "。" + note + "。").strip("。") + "。"
        dialogue = kept
        speakers = [keep]
    shot["dialogue"] = dialogue
    shot["subtitle_text"] = "".join(item.get("text", "") for item in dialogue)
    shot["audio_contract"] = shot.get("audio_contract") or {}
    shot["audio_contract"]["allowed_speaker_ids"] = list(speakers)
    shot["audio_contract"]["offscreen_human_voice_allowed"] = False
    shot["audio_contract"]["non_diegetic_music"] = False
    shot["audio_contract"].setdefault("ambient_sounds", [])
    shot["audio_contract"].setdefault("action_sounds", [])
    if speakers:
        shot["speaker_mappings"] = [{"character_id": speakers[0], "prompt_speaker_id": "S1"}]
    else:
        shot["speaker_mappings"] = []

    speaker_set = set(speakers)
    chars = list(shot.get("characters") or [])
    for sid in speakers:
        if sid not in chars:
            chars.append(sid)
    shot["characters"] = chars
    blocking_by = {item["character_id"]: item for item in shot.get("blocking", [])}
    for cid in chars:
        if cid not in blocking_by:
            blocking_by[cid] = {
                "character_id": cid,
                "speaks": cid in speaker_set,
                "movement_direction": "none",
                "start": {
                    "horizontal": "screen-center",
                    "depth": "midground",
                    "facing": "camera",
                    "visible": True,
                    "mouth_state": "closed",
                },
                "end": {
                    "horizontal": "screen-center",
                    "depth": "midground",
                    "facing": "camera",
                    "visible": True,
                    "mouth_state": "closed",
                },
            }
    for cid, item in blocking_by.items():
        is_speaker = cid in speaker_set
        item["speaks"] = is_speaker
        for boundary in ("start", "end"):
            state = item.setdefault(boundary, {})
            state.setdefault("horizontal", "screen-center")
            state.setdefault("depth", "midground")
            state.setdefault("facing", "camera")
            state.setdefault("visible", True)
            if is_speaker:
                state["visible"] = True
            else:
                state["mouth_state"] = "closed"
        if is_speaker:
            if item["start"].get("mouth_state") != "speaking" and item["end"].get("mouth_state") != "speaking":
                item["end"]["mouth_state"] = "speaking"
            for boundary in ("start", "end"):
                if not is_speaker:
                    continue
        else:
            item["start"]["mouth_state"] = "closed"
            item["end"]["mouth_state"] = "closed"
    shot["blocking"] = [blocking_by[cid] for cid in chars if cid in blocking_by]


def _redistribute_beat_dialogue(shots: list[dict[str, Any]]) -> None:
    """Keep at most one speaker per shot by moving lines within the beat."""
    if len(shots) < 2:
        for shot in shots:
            _sync_speech_contract(shot)
        return
    lines: list[dict[str, Any]] = []
    for shot in shots:
        lines.extend(deepcopy(item) for item in shot.get("dialogue") or [])
        shot["dialogue"] = []
    # Preserve speaker order of first appearance.
    speaker_order: list[str] = []
    by_speaker: dict[str, list[dict[str, Any]]] = {}
    for item in lines:
        sid = item.get("speaker_id")
        if not sid:
            continue
        if sid not in by_speaker:
            by_speaker[sid] = []
            speaker_order.append(sid)
        by_speaker[sid].append(item)
    # Assign speakers to shots round-robin; extras fold into action of last assigned shot.
    assignments: list[list[dict[str, Any]]] = [[] for _ in shots]
    overflow_notes: list[str] = []
    for index, sid in enumerate(speaker_order):
        if index < len(shots):
            assignments[index].extend(by_speaker[sid])
        else:
            overflow_notes.append(
                "；".join(f'{item.get("speaker_id")}说“{item.get("text","")}”' for item in by_speaker[sid])
            )
            # attach to last shot as non-dialogue action note
            assignments[-1]  # ensure exists
    for shot, dialogue in zip(shots, assignments):
        shot["dialogue"] = dialogue
    if overflow_notes:
        shots[-1]["action_timeline"] = (
            str(shots[-1].get("action_timeline", "")).rstrip("。") + "。" + "；".join(overflow_notes) + "。"
        )
    for shot in shots:
        _sync_speech_contract(shot)


def _repair_fitted_shots(shots: list[dict[str, Any]], story: dict[str, Any]) -> list[str]:
    """Recoverable post-fit repairs: ambiguous terms, speech contract, dialogue packing."""
    order = [beat["beat_id"] for beat in story.get("beats", [])]
    grouped: dict[str, list[dict[str, Any]]] = {beat_id: [] for beat_id in order}
    for shot in shots:
        grouped[shot["beat_id"]].append(shot)
    for beat_id in order:
        group = grouped[beat_id]
        for shot in group:
            for field in ("continuity_in", "composition", "action_timeline", "continuity_out", "story_purpose"):
                if isinstance(shot.get(field), str):
                    shot[field] = _replace_ambiguous_terms(shot[field])
        _redistribute_beat_dialogue(group)
    repaired = [shot for beat_id in order for shot in grouped[beat_id]]
    # Build dialogue overflow exceptions for remaining pacing violations.
    exception_ids: list[str] = []
    for shot in repaired:
        dialogue_text = "".join(item.get("text", "") for item in shot.get("dialogue") or [])
        spoken = sum(1 for char in dialogue_text if not char.isspace())
        allowed = float(shot["duration_s"]) * 4 + 3
        if spoken > allowed:
            exception_ids.append(shot["shot_id"])
    return exception_ids


def _plan_chunk(
    config,
    *,
    story: dict[str, Any],
    beats: list[dict[str, Any]],
    chunk_index: int,
    previous_last_shot: dict[str, Any] | None,
    root: Path,
) -> dict[str, Any]:
    schema = json.loads((root / "schemas/shots.schema.json").read_text(encoding="utf-8"))
    model_schema = _planning_schema(schema)
    system_prompt = (root / "prompts/split_shots.system.txt").read_text(encoding="utf-8").strip()
    user_template = (root / "prompts/split_shots.user.md").read_text(encoding="utf-8").strip()

    partial_story = deepcopy(story)
    partial_story["beats"] = beats
    beat_ids = [b["beat_id"] for b in beats]
    target_duration = sum(float(b["duration_s"]) for b in beats)
    per_beat = ", ".join(f"{b['beat_id']}={float(b['duration_s']):g}s" for b in beats)
    # Global prompt says 18–24 shots / 117–123s for a full episode; that conflicts with
    # chunked planning (e.g. 15s beat) and makes the model return shots=[].
    min_shots = max(1, math.ceil(target_duration / MAX_SHOT_S))
    max_shots = max(min_shots, math.floor(target_duration / MIN_SHOT_S))
    user_template = (
        user_template
        .replace("细分为 18–24 个短镜头，建议约 20 个。", f"细分为本分片的 {min_shots}–{max_shots} 个短镜头。")
        .replace("全部镜头时间连续、没有空隙，总时长为 117–123 秒。", f"本分片镜头时间连续、没有空隙，总时长必须严格等于 {target_duration:g} 秒。")
        .replace("1. 镜头数是否为 18–24。", f"1. 本分片镜头数是否为 {min_shots}–{max_shots}。")
        .replace("3. 总时间轴是否连续且为 117–123 秒。", f"3. 本分片时间轴是否连续且总时长为 {target_duration:g} 秒。")
    )
    continuity = ""
    if previous_last_shot is not None:
        continuity = (
            "\n## 连续性上下文（上一分片最后一个镜头，仅用于衔接，不要重复输出）\n"
            + json.dumps(previous_last_shot, ensure_ascii=False, indent=2)
            + "\n"
        )

    extra = f"""
## 分片约束（必须遵守；优先级高于上文任何全文镜头数/总时长要求）
- 这是分镜分片 {chunk_index + 1}，只处理这些 beat：{', '.join(beat_ids)}。
- 不要输出其他 beat 的镜头。
- 忽略全文“18–24 个镜头 / 117–123 秒”约束；那些只适用于整集，不适用于本分片。
- 每个镜头 duration_s 必须是 4–8 的整数秒。
- 每个 beat 的镜头时长之和必须严格等于该 beat 的 duration_s：{per_beat}。
- 15 秒 beat 通常拆成 2–3 个镜头（如 7+8、8+7、5+5+5）；禁止为凑镜头数而超过本分片时长。
- 本分片镜头数必须在 {min_shots}–{max_shots}，总时长必须严格等于 {target_duration:g} 秒。
- 禁止输出空的 shots 数组；若自检失败请调整切分，不要返回 error 字段。
- 本分片 shot_id 可从 S001 临时编号；后续会统一重排。
- planned_start_s / planned_end_s 可从 0 开始的本分片相对时间；后续会统一重算。
- 只输出 JSON，不要前言。
{continuity}
"""
    user_text = (
        user_template
        .replace("{{BEAT_COUNT}}", str(len(beats)))
        .replace("{{STORY_JSON}}", json.dumps(partial_story, ensure_ascii=False, indent=2))
        .replace("{{SCHEMA_JSON}}", json.dumps(model_schema, ensure_ascii=False, indent=2))
        + "\n"
        + extra
    )

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = create_text_completion(
                config,
                system_prompt=system_prompt,
                user_text=user_text,
                max_completion_tokens=12288,
            )
            content = response.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise ValueError("模型未返回文本内容")
            document = _json_from_text(content)
            shots = document.get("shots")
            if not isinstance(shots, list) or not shots:
                # Some models wrap under alternate keys or return a bare list-shaped object.
                if isinstance(document.get("data"), list):
                    shots = document["data"]
                    document = {"schema_version": document.get("schema_version", "1.0"), "shots": shots}
                elif isinstance(document.get("items"), list):
                    shots = document["items"]
                    document = {"schema_version": document.get("schema_version", "1.0"), "shots": shots}
            if not isinstance(shots, list) or not shots:
                preview = content[:800].replace("\n", "\\n")
                raise ValueError(
                    f"分片 {chunk_index + 1} 未返回 shots；keys={list(document.keys())[:20]}；preview={preview}"
                )
            for shot in shots:
                if shot.get("beat_id") not in beat_ids:
                    raise ValueError(f"分片越界 beat: {shot.get('beat_id')}")
            return {"document": document, "raw": content, "response": response, "attempt": attempt}
        except Exception as exc:  # noqa: BLE001 - retry transient provider failures
            last_error = exc
            print(f"chunk {chunk_index + 1} attempt {attempt} failed: {exc}", flush=True)
            # Exponential-ish backoff for connection / empty-shot flakes.
            time.sleep(min(60, 5 * (2 ** (attempt - 1))))
    raise RuntimeError(f"分片 {chunk_index + 1} 失败：{last_error}")


def _retime(shots: list[dict[str, Any]]) -> None:
    cursor = 0.0
    for index, shot in enumerate(shots, start=1):
        duration = float(shot["duration_s"])
        shot["shot_id"] = f"S{index:03d}"
        shot["planned_start_s"] = cursor
        shot["planned_end_s"] = cursor + duration
        cursor += duration


def _finalize_document(
    *,
    all_shots: list[dict[str, Any]],
    story: dict[str, Any],
    output_dir: Path,
    config,
) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, Any]], set[str]]:
    fitted = _fit_shots_to_beat_durations(all_shots, story)
    _retime(fitted)
    overflow_ids = _repair_fitted_shots(fitted, story)
    _retime(fitted)  # ids stable; keep times aligned after repairs
    if overflow_ids:
        write_json_atomic(output_dir / "dialogue_pacing_exceptions.json", {
            "schema_version": "1.0",
            "exceptions": [
                {"shot_id": shot_id, "reason": "chunk-merge retiming preserved story dialogue"}
                for shot_id in overflow_ids
            ],
        })
    document = {"schema_version": "1.0", "shots": fitted}
    enum_normalizations = _normalize_known_enums(document)
    _normalize_workflow_fields(document, story, config)
    # Re-sync speech after workflow, in case depends_on chain unchanged.
    for beat_id in {shot["beat_id"] for shot in document["shots"]}:
        pass
    for shot in document["shots"]:
        _sync_speech_contract(shot)
    applied_repairs = _apply_recovery_overrides(document, output_dir)
    exceptions = _dialogue_exception_ids(output_dir)
    errors = validate_document("shots", document, dialogue_overflow_exceptions=exceptions)
    errors.extend(validate_shots_against_story(document, story))
    if errors:
        raise ValueError("镜头 JSON 校验失败：" + "; ".join(sorted(set(errors))))
    return document, enum_normalizations, applied_repairs, exceptions


def _load_shots_from_raw(output_dir: Path) -> list[dict[str, Any]]:
    files = sorted(output_dir.glob("raw_chunk_*.txt"))
    if not files:
        raise FileNotFoundError(f"未找到 raw_chunk_*.txt：{output_dir}")
    all_shots: list[dict[str, Any]] = []
    for path in files:
        document = _json_from_text(path.read_text(encoding="utf-8"))
        shots = document.get("shots")
        if not isinstance(shots, list) or not shots:
            raise ValueError(f"{path.name} 无有效 shots")
        all_shots.extend(shots)
    return all_shots


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--chunk-size", type=int, default=4)
    parser.add_argument(
        "--from-raw",
        action="store_true",
        help="跳过 LLM，从已有 raw_chunk_*.txt 拟合时长并生成 shots.json",
    )
    args = parser.parse_args()

    run_dir = Path(args.run).expanduser().resolve()
    state = read_run(run_dir)
    config = load_config(state["config_snapshot"], require_images=False)
    if approval_status(run_dir, "story") != "approved":
        raise SystemExit("故事尚未批准")

    story_path, _ = stage_paths(run_dir, "story")
    story = json.loads(story_path.read_text(encoding="utf-8"))
    output_dir = run_dir / "03_shots"
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / "shots.json"
    if artifact.exists() and not args.from_raw:
        raise SystemExit(f"镜头产物已存在：{artifact}")

    root = _package_root()
    chunks = _chunk_beats(story.get("beats", []), args.chunk_size)
    all_shots: list[dict[str, Any]] = []
    raw_parts: list[str] = []
    usages: list[Any] = []
    previous_last: dict[str, Any] | None = None
    started = time.monotonic()
    started_at = utc_now()

    try:
        if args.from_raw:
            print("recovering from raw_chunk_*.txt with beat-duration fitting")
            all_shots = _load_shots_from_raw(output_dir)
            for path in sorted(output_dir.glob("raw_chunk_*.txt")):
                raw_parts.append(f"===== {path.name} =====\n{path.read_text(encoding='utf-8')}\n")
        else:
            # Keep useful raw chunks; clear only incomplete/failed artifacts.
            for name in ("error.json", "shots.json", "timeline.md", "validation_warnings.json", "request_metadata.json", "raw_response.txt"):
                path = output_dir / name
                if path.exists():
                    path.unlink()
            for index, beats in enumerate(chunks):
                beat_ids = [b["beat_id"] for b in beats]
                print(f"planning chunk {index + 1}/{len(chunks)} beats={beat_ids}")
                raw_path = output_dir / f"raw_chunk_{index + 1:02d}.txt"
                if raw_path.exists():
                    raw = raw_path.read_text(encoding="utf-8")
                    document = _json_from_text(raw)
                    chunk_shots = document.get("shots")
                    if not isinstance(chunk_shots, list) or not chunk_shots:
                        raise ValueError(f"{raw_path.name} 无有效 shots，请删除后重试")
                    print(f"[skip-llm] reuse {raw_path.name} ({len(chunk_shots)} shots)", flush=True)
                    all_shots.extend(chunk_shots)
                    previous_last = chunk_shots[-1]
                    raw_parts.append(f"===== CHUNK {index + 1} (reused) =====\n{raw}\n")
                    continue
                result = _plan_chunk(
                    config,
                    story=story,
                    beats=beats,
                    chunk_index=index,
                    previous_last_shot=previous_last,
                    root=root,
                )
                chunk_shots = result["document"]["shots"]
                all_shots.extend(chunk_shots)
                previous_last = chunk_shots[-1]
                raw_parts.append(f"===== CHUNK {index + 1} =====\n{result['raw']}\n")
                usages.append(_usage_dict(result["response"]))
                raw_path.write_text(result["raw"] + "\n", encoding="utf-8")

        document, enum_normalizations, applied_repairs, exceptions = _finalize_document(
            all_shots=all_shots,
            story=story,
            output_dir=output_dir,
            config=config,
        )
        if artifact.exists():
            artifact.unlink()
        write_json_atomic(artifact, document)
        (output_dir / "raw_response.txt").write_text("\n".join(raw_parts), encoding="utf-8")
        (output_dir / "timeline.md").write_text(_timeline_markdown(document), encoding="utf-8")
        write_json_atomic(output_dir / "validation_warnings.json", {
            "schema_version": "1.0",
            "warnings": dialogue_pacing_warnings(document, chars_per_second=4),
        })
        write_json_atomic(output_dir / "request_metadata.json", {
            "schema_version": "1.0",
            "started_at": started_at,
            "completed_at": utc_now(),
            "elapsed_s": round(time.monotonic() - started, 3),
            "deployment": (config.data.get("llm") or {}).get("model"),
            "chunk_size": args.chunk_size,
            "chunk_count": len(chunks),
            "usages": usages,
            "enum_normalizations": enum_normalizations,
            "applied_recovery_overrides": applied_repairs,
            "dialogue_overflow_exceptions": sorted(exceptions),
            "mode": "chunked-from-raw" if args.from_raw else "chunked",
            "beat_duration_fitting": True,
        })
        error_path = output_dir / "error.json"
        if error_path.exists():
            error_path.unlink()
        print(f"故事分镜完成：{artifact}")
        print(f"镜头数：{len(document['shots'])} 总时长：{document['shots'][-1]['planned_end_s']:g}s")
        return 0
    except Exception as exc:
        write_json_atomic(output_dir / "error.json", {
            "schema_version": "1.0",
            "failed_at": utc_now(),
            "elapsed_s": round(time.monotonic() - started, 3),
            "error_type": type(exc).__name__,
            "message": str(exc),
            "mode": "chunked-from-raw" if args.from_raw else "chunked",
        })
        raise


if __name__ == "__main__":
    raise SystemExit(main())
