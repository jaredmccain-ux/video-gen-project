"""JSON Schema and cross-file business validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


SCHEMA_FILES = {
    "descriptions": "image_descriptions.schema.json",
    "story": "story.schema.json",
    "shots": "shots.schema.json",
}


def schema_root() -> Path:
    return Path(__file__).resolve().parents[1] / "schemas"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_document(
    kind: str,
    document: dict[str, Any],
    root: Path | None = None,
    *,
    dialogue_overflow_exceptions: set[str] | None = None,
) -> list[str]:
    if kind not in SCHEMA_FILES:
        raise ValueError(f"未知文档类型：{kind}")
    schema = load_json((root or schema_root()) / SCHEMA_FILES[kind])
    validator = Draft202012Validator(schema)
    errors = [f"{'/'.join(str(p) for p in error.path) or '<root>'}: {error.message}" for error in validator.iter_errors(document)]
    if not errors and kind == "shots":
        errors.extend(validate_shot_timeline(document, dialogue_overflow_exceptions=dialogue_overflow_exceptions))
    if not errors and kind == "descriptions":
        images = document.get("images", [])
        ids = [item.get("image_id") for item in images]
        if ids != ["IMG01", "IMG02", "IMG03"]:
            errors.append("images 必须按 IMG01、IMG02、IMG03 顺序且各出现一次")
    if not errors and kind == "story":
        errors.extend(validate_story(document))
    return sorted(errors)


def validate_story(document: dict[str, Any], *, target: float = 120, tolerance: float = 3) -> list[str]:
    errors: list[str] = []
    if set(document.get("image_order", [])) != {"IMG01", "IMG02", "IMG03"}:
        errors.append("image_order 必须恰好包含 IMG01、IMG02、IMG03")
    beats = document.get("beats", [])
    duration = sum(beat.get("duration_s", 0) for beat in beats)
    if not (target - tolerance <= duration <= target + tolerance):
        errors.append(f"beats 总时长 {duration:g}s 不在 {target-tolerance:g}–{target+tolerance:g}s")
    anchors = {beat.get("anchor_image_id") for beat in beats}
    missing = sorted({"IMG01", "IMG02", "IMG03"} - anchors)
    if missing:
        errors.append("缺少锚点图片：" + ", ".join(missing))
    for field, items, id_field in (
        ("characters", document.get("characters", []), "character_id"),
        ("locations", document.get("locations", []), "location_id"),
        ("beats", beats, "beat_id"),
    ):
        ids = [item.get(id_field) for item in items]
        if len(ids) != len(set(ids)):
            errors.append(f"{field} 中的 {id_field} 必须唯一")
    for character in document.get("characters", []):
        character_id = character.get("character_id", "<unknown>")
        reference_ids = character.get("reference_image_ids")
        if reference_ids is None:
            continue  # Backward compatibility for stories generated before Ref2VA support.
        unknown = set(reference_ids) - {"IMG01", "IMG02", "IMG03"}
        if unknown:
            errors.append(
                f"{character_id}: reference_image_ids 包含未知图片 {', '.join(sorted(unknown))}"
            )
        if reference_ids and not str(character.get("reference_subject_description", "")).strip():
            errors.append(f"{character_id}: 有参考图时必须填写 reference_subject_description")
    return errors


def validate_shot_timeline(
    document: dict[str, Any],
    *,
    target: float = 120,
    tolerance: float = 3,
    dialogue_overflow_exceptions: set[str] | None = None,
    dialogue_char_tolerance: int = 3,
) -> list[str]:
    errors: list[str] = []
    shots = document.get("shots", [])
    ids = [shot.get("shot_id") for shot in shots]
    if len(ids) != len(set(ids)):
        errors.append("shot_id 必须唯一")
    known: set[str] = set()
    expected_start = 0.0
    previous_id: str | None = None
    for shot in shots:
        shot_id = shot.get("shot_id", "<unknown>")
        start, end, duration = shot.get("planned_start_s"), shot.get("planned_end_s"), shot.get("duration_s")
        if all(isinstance(v, (int, float)) for v in (start, end, duration)):
            if abs(start - expected_start) > 1e-6:
                errors.append(f"{shot_id}: planned_start_s 与前一镜头不连续")
            if abs((end - start) - duration) > 1e-6:
                errors.append(f"{shot_id}: planned_end_s - planned_start_s != duration_s")
            expected_start = end
        dependency = shot.get("depends_on")
        if dependency is not None and dependency not in known:
            errors.append(f"{shot_id}: depends_on 必须引用此前镜头")
        known.add(shot_id)
        dialogue_text = "".join(item.get("text", "") for item in shot.get("dialogue", []))
        if shot.get("subtitle_text", "") != dialogue_text:
            errors.append(f"{shot_id}: subtitle_text 必须等于对白顺序拼接")
        errors.extend(validate_shot_contract(shot))
        spoken_chars = sum(1 for char in dialogue_text if not char.isspace())
        exceptions = dialogue_overflow_exceptions or set()
        allowed_chars = duration * 4 + dialogue_char_tolerance if isinstance(duration, (int, float)) else None
        if allowed_chars is not None and spoken_chars > allowed_chars and shot_id not in exceptions:
            errors.append(
                f"{shot_id}: 对白 {spoken_chars} 字超过 {duration:g}s 的允许上限 {allowed_chars:g} 字"
            )
    if shots and not (target - tolerance <= expected_start <= target + tolerance):
        errors.append(f"总时长 {expected_start:g}s 不在 {target-tolerance:g}–{target+tolerance:g}s")
    errors.extend(validate_adjacent_shot_continuity(document))
    return errors


def validate_adjacent_shot_continuity(document: dict[str, Any]) -> list[str]:
    """Compare exact boundary state for shots chained by the previous last frame."""
    errors: list[str] = []
    shots = document.get("shots", [])
    by_id = {shot.get("shot_id"): shot for shot in shots}
    for shot in shots:
        dependency = shot.get("depends_on")
        if dependency is None:
            continue
        previous = by_id.get(dependency)
        if previous is None:
            continue
        shot_id = shot.get("shot_id", "<unknown>")
        previous_blocking = {
            item.get("character_id"): item for item in previous.get("blocking", [])
        }
        current_blocking = {
            item.get("character_id"): item for item in shot.get("blocking", [])
        }
        all_characters = sorted(set(previous_blocking) | set(current_blocking))
        for character_id in all_characters:
            before = previous_blocking.get(character_id)
            after = current_blocking.get(character_id)
            if before is None:
                if after and after.get("start", {}).get("visible"):
                    errors.append(
                        f"{dependency}->{shot_id}: {character_id} 在依赖首帧中无来源却标为可见；"
                        "应在镜头内明确入场，且 start.visible=false"
                    )
                continue
            if after is None:
                if before.get("end", {}).get("visible"):
                    errors.append(
                        f"{dependency}->{shot_id}: {character_id} 在上一末帧可见但下一 blocking 缺失"
                    )
                continue
            end_state = before.get("end", {})
            start_state = after.get("start", {})
            for field in ("horizontal", "depth", "facing", "visible"):
                if end_state.get(field) != start_state.get(field):
                    errors.append(
                        f"{dependency}->{shot_id}: {character_id}.{field} 边界不连续 "
                        f"({end_state.get(field)} -> {start_state.get(field)})"
                    )

        previous_direction = {
            character_id: item.get("movement_direction")
            for character_id, item in previous_blocking.items()
            if item.get("end", {}).get("visible")
        }
        current_direction = {
            character_id: item.get("movement_direction")
            for character_id, item in current_blocking.items()
            if item.get("start", {}).get("visible")
        }
        action = shot.get("action_timeline", "")
        transition_terms = ("停", "减慢", "刹", "转", "回头", "进入", "走入", "跑入", "靠近", "离开", "冲出")
        for character_id in sorted(set(previous_direction) & set(current_direction)):
            old_direction = previous_direction[character_id]
            new_direction = current_direction[character_id]
            if old_direction != new_direction and not any(term in action for term in transition_terms):
                errors.append(
                    f"{dependency}->{shot_id}: {character_id}.movement_direction 从 {old_direction} 变为 "
                    f"{new_direction}，action_timeline 缺少停下、转身或入退场动作"
                )
    return errors


def validate_shot_contract(shot: dict[str, Any]) -> list[str]:
    """Validate the single-speaker audio contract and explicit screen blocking."""
    errors: list[str] = []
    shot_id = shot.get("shot_id", "<unknown>")
    characters = shot.get("characters", [])
    character_set = set(characters)
    if len(characters) != len(character_set):
        errors.append(f"{shot_id}: characters 不得重复")

    dialogue = shot.get("dialogue", [])
    dialogue_speakers = [item.get("speaker_id") for item in dialogue]
    unique_dialogue_speakers = set(dialogue_speakers)
    if len(unique_dialogue_speakers) > 1:
        errors.append(f"{shot_id}: 单镜头最多一个说话人")
    if unique_dialogue_speakers - character_set:
        errors.append(f"{shot_id}: 对白说话人必须列入 characters")

    audio = shot.get("audio_contract", {})
    allowed = audio.get("allowed_speaker_ids", [])
    if set(allowed) != unique_dialogue_speakers or len(allowed) != len(unique_dialogue_speakers):
        errors.append(f"{shot_id}: allowed_speaker_ids 必须与实际对白说话人完全一致")

    mappings = shot.get("speaker_mappings", [])
    mapped_characters = [item.get("character_id") for item in mappings]
    if set(mapped_characters) != unique_dialogue_speakers or len(mapped_characters) != len(unique_dialogue_speakers):
        errors.append(f"{shot_id}: speaker_mappings 必须与实际对白说话人完全一致")
    if any(item.get("prompt_speaker_id") != "S1" for item in mappings):
        errors.append(f"{shot_id}: 当前镜头唯一说话人必须映射为 S1")

    blocking = shot.get("blocking", [])
    blocking_ids = [item.get("character_id") for item in blocking]
    if len(blocking_ids) != len(set(blocking_ids)):
        errors.append(f"{shot_id}: blocking 中角色不得重复")
    if set(blocking_ids) != character_set:
        errors.append(f"{shot_id}: blocking 必须恰好覆盖 characters 中全部角色")
    blocking_by_id = {item.get("character_id"): item for item in blocking}
    for character_id in characters:
        item = blocking_by_id.get(character_id)
        if item is None:
            continue
        is_speaker = character_id in unique_dialogue_speakers
        if item.get("speaks") != is_speaker:
            errors.append(f"{shot_id}: {character_id} 的 blocking.speaks 与对白不一致")
        states = [item.get(boundary, {}) for boundary in ("start", "end")]
        for boundary, state in zip(("start", "end"), states):
            if is_speaker:
                if not state.get("visible"):
                    errors.append(f"{shot_id}: 说话人 {character_id} 在 {boundary} 必须可见")
            elif state.get("mouth_state") != "closed":
                errors.append(f"{shot_id}: 非说话人 {character_id} 在 {boundary} 必须闭口")
        if is_speaker and not any(state.get("mouth_state") == "speaking" for state in states):
            errors.append(f"{shot_id}: 说话人 {character_id} 的 start/end 至少一处必须为 speaking")

    ambiguous_terms = ("前面", "后面", "前方", "后方", "身前", "身后", "前边", "后边")
    for field in ("continuity_in", "composition", "action_timeline", "continuity_out"):
        value = shot.get(field, "")
        found = next((term for term in ambiguous_terms if term in value), None)
        if found:
            errors.append(f"{shot_id}: {field} 含模糊站位词“{found}”，请使用 blocking 的画面左右、景深和面朝方向")
    return errors


def dialogue_pacing_warnings(
    document: dict[str, Any], *, chars_per_second: float = 4, char_tolerance: int = 3
) -> list[str]:
    warnings: list[str] = []
    for shot in document.get("shots", []):
        text = "".join(item.get("text", "") for item in shot.get("dialogue", []))
        count = sum(1 for char in text if not char.isspace())
        duration = shot.get("duration_s")
        if isinstance(duration, (int, float)) and count > duration * chars_per_second + char_tolerance:
            warnings.append(
                f"{shot.get('shot_id', '<unknown>')}: 对白 {count} 字/{duration:g}s，"
                f"超过允许上限 {duration * chars_per_second + char_tolerance:g} 字"
            )
    return warnings


def validate_shots_against_story(document: dict[str, Any], story: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    character_ids = {item["character_id"] for item in story.get("characters", [])}
    story_beats = story.get("beats", [])
    beat_ids = {item["beat_id"] for item in story_beats}
    beat_order = {item["beat_id"]: index for index, item in enumerate(story_beats)}
    beat_durations = {item["beat_id"]: item["duration_s"] for item in story_beats}
    shot_duration_by_beat = {beat_id: 0.0 for beat_id in beat_ids}
    previous_beat_index = -1
    anchor_counts = {image_id: 0 for image_id in ("IMG01", "IMG02", "IMG03")}
    shots = document.get("shots", [])
    for shot in shots:
        shot_id = shot.get("shot_id", "<unknown>")
        if shot.get("beat_id") not in beat_ids:
            errors.append(f"{shot_id}: beat_id 不存在于故事")
        else:
            beat_id = shot["beat_id"]
            current_beat_index = beat_order[beat_id]
            if current_beat_index < previous_beat_index:
                errors.append(f"{shot_id}: Beat 顺序倒退，必须按故事顺序连续细分")
            previous_beat_index = current_beat_index
            shot_duration_by_beat[beat_id] += float(shot.get("duration_s", 0))
        unknown_characters = set(shot.get("characters", [])) - character_ids
        if unknown_characters:
            errors.append(f"{shot_id}: 未知角色 {', '.join(sorted(unknown_characters))}")
        mode, dependency, anchor = shot.get("generation_mode"), shot.get("depends_on"), shot.get("source_anchor_image")
        if anchor in anchor_counts:
            anchor_counts[anchor] += 1
        last_frame = shot.get("source_last_frame")
        reference_ids = shot.get("reference_character_ids") or []
        if set(reference_ids) - set(shot.get("characters", [])):
            errors.append(f"{shot_id}: reference_character_ids 必须是 characters 的子集")
        if mode == "t2va" and (
            dependency is not None or anchor is not None or last_frame or reference_ids
        ):
            errors.append(
                f"{shot_id}: t2va 不得设置依赖、锚点、末帧或角色参考"
            )
        elif mode == "first_frame" and ((dependency is None) == (anchor is None)):
            errors.append(f"{shot_id}: first_frame 必须且只能使用锚点图或上一镜头末帧之一")
        elif mode == "first_frame" and dependency is not None and dependency != previous_id:
            errors.append(f"{shot_id}: 使用末帧续接时 depends_on 必须是紧邻的上一镜头")
        elif mode == "first_frame" and (last_frame or reference_ids):
            errors.append(f"{shot_id}: first_frame 不得设置末帧或角色参考")
        elif mode == "first_last_frame":
            prepared = shot.get("prepared_first_frame")
            if prepared:
                if dependency is not None:
                    errors.append(
                        f"{shot_id}: 已有 prepared_first_frame 时不得再依赖上一镜头末帧"
                    )
            elif (dependency is None) == (anchor is None):
                errors.append(
                    f"{shot_id}: first_last_frame 必须且只能使用一个首帧来源"
                )
            if dependency is not None and dependency != previous_id:
                errors.append(
                    f"{shot_id}: first_last_frame 的首帧依赖必须是紧邻上一镜头"
                )
            if not last_frame:
                errors.append(f"{shot_id}: first_last_frame 必须设置 source_last_frame")
            if reference_ids:
                errors.append(f"{shot_id}: first_last_frame 不得同时设置角色参考")
        elif mode == "ref2va":
            if not reference_ids:
                errors.append(f"{shot_id}: ref2va 必须设置 reference_character_ids")
            if dependency is not None and dependency != previous_id:
                errors.append(f"{shot_id}: ref2va 的连续性参考必须来自紧邻上一镜头")
            if last_frame:
                errors.append(f"{shot_id}: ref2va 不使用 source_last_frame")
        previous_id = shot_id
    for image_id, count in anchor_counts.items():
        if count != 1:
            errors.append(f"{image_id}: 必须且只能作为一次原图首帧，当前为 {count} 次")
    for beat_id, expected_duration in beat_durations.items():
        actual_duration = shot_duration_by_beat[beat_id]
        if abs(actual_duration - expected_duration) > 1e-6:
            errors.append(
                f"{beat_id}: Shot 总时长 {actual_duration:g}s 不等于 Beat 时长 {expected_duration:g}s"
            )
    return errors
