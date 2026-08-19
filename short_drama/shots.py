"""Split an approved story into short, continuity-aware generation units."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

from .approval import approval_status, stage_paths
from .azure_client import completion_text, create_text_completion
from .config import ProjectConfig
from .reference_assets import (
    declared_character_references,
    identity_config,
    shot_last_keyframe,
)
from .state import read_run, utc_now, write_json_atomic
from .validators import dialogue_pacing_warnings, validate_document, validate_shots_against_story


def _package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _json_from_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1 :] if first_newline >= 0 else cleaned
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型响应 JSON 顶层必须是对象")
    return value


def _usage_dict(response: Any) -> dict[str, Any] | None:
    usage = getattr(response, "usage", None)
    return usage.model_dump() if usage is not None and hasattr(usage, "model_dump") else None


def _timeline_markdown(document: dict[str, Any]) -> str:
    lines = [
        "# 镜头时间线", "",
        "| 镜头 | 时间 | 时长 | Beat | 模式 | 锚点/依赖 | 剧情作用 | 对白 |", "|---|---:|---:|---|---|---|---|---|",
    ]
    for shot in document["shots"]:
        link = shot["source_anchor_image"] or shot["depends_on"] or "—"
        dialogue = " / ".join(item["text"] for item in shot["dialogue"]) or "—"
        lines.append(
            f"| {shot['shot_id']} | {shot['planned_start_s']:g}–{shot['planned_end_s']:g}s | "
            f"{shot['duration_s']:g}s | {shot['beat_id']} | {shot['generation_mode']} | {link} | "
            f"{shot['story_purpose'].replace('|', '｜')} | {dialogue.replace('|', '｜')} |"
        )
    lines.extend(["", f"总时长：{document['shots'][-1]['planned_end_s']:g} 秒；镜头数：{len(document['shots'])}。", ""])
    return "\n".join(lines)


WORKFLOW_FIELDS = {
    "generation_mode", "depends_on", "source_anchor_image", "source_last_frame",
    "reference_character_ids", "status", "attempt",
    "variation_type", "first_frame_desc", "last_frame_desc", "motion_desc",
    "selected_reference_paths", "selected_references", "prepared_first_frame",
}


def _planning_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the model-facing schema without local orchestration fields."""
    result = copy.deepcopy(schema)
    result["title"] = "Short-drama shot plan"
    shot_schema = result["properties"]["shots"]["items"]
    shot_schema["required"] = [
        field for field in shot_schema["required"] if field not in WORKFLOW_FIELDS
    ]
    for field in WORKFLOW_FIELDS:
        shot_schema["properties"].pop(field, None)
    return result


def _boundary_matches(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_states = {
        item["character_id"]: item["end"] for item in previous.get("blocking", [])
    }
    current_states = {
        item["character_id"]: item["start"] for item in current.get("blocking", [])
    }
    if set(previous_states) != set(current_states):
        return False
    fields = ("horizontal", "depth", "facing", "visible")
    states_match = all(
        all(previous_states[character_id].get(field) == current_states[character_id].get(field) for field in fields)
        for character_id in previous_states
    )
    if not states_match:
        return False
    transition_terms = ("停", "减慢", "刹", "转", "回头", "进入", "走入", "跑入", "靠近", "离开", "冲出")
    action = current.get("action_timeline", "")
    for character_id in previous_states:
        before = next(item for item in previous["blocking"] if item["character_id"] == character_id)
        after = next(item for item in current["blocking"] if item["character_id"] == character_id)
        if before.get("movement_direction") != after.get("movement_direction"):
            if not any(term in action for term in transition_terms):
                return False
    return True


def _normalize_known_enums(document: dict[str, Any]) -> list[dict[str, str]]:
    """Canonicalize only aliases with an exact meaning in the target enum."""
    changes: list[dict[str, str]] = []
    for shot in document.get("shots", []):
        for blocking in shot.get("blocking", []):
            for boundary in ("start", "end"):
                state = blocking.get(boundary, {})
                if state.get("facing") == "toward-camera":
                    state["facing"] = "camera"
                    changes.append({
                        "shot_id": shot["shot_id"],
                        "character_id": blocking["character_id"],
                        "boundary": boundary,
                        "field": "facing",
                        "from": "toward-camera",
                        "to": "camera",
                    })
    return changes


def _prepared_last_frame(shot: dict[str, Any], config: ProjectConfig | None) -> Path | None:
    """Resolve a still last keyframe from shot fields or project assets."""
    for key in ("source_last_frame",):
        value = shot.get(key)
        if value:
            path = Path(str(value))
            if path.is_file():
                return path.resolve()
    prepared = shot.get("prepared_first_frame")
    # Prefer explicit last beside prepared first inside the run keyframe dir.
    if prepared:
        sibling = Path(str(prepared)).with_name(
            Path(str(prepared)).name.replace(".first.", ".last.")
        )
        if sibling.is_file():
            return sibling.resolve()
    if config is None:
        return None
    return shot_last_keyframe(config, shot["shot_id"])


def _prepared_first_frame(shot: dict[str, Any]) -> Path | None:
    value = shot.get("prepared_first_frame")
    if not value:
        return None
    path = Path(str(value))
    return path.resolve() if path.is_file() else None


def apply_generation_routing(
    document: dict[str, Any], story: dict[str, Any], config: ProjectConfig | None = None
) -> None:
    """Public entry used by plan-shots and prepare-consistency."""
    _normalize_workflow_fields(document, story, config)


def _normalize_workflow_fields(
    document: dict[str, Any], story: dict[str, Any], config: ProjectConfig | None = None
) -> None:
    """Derive H3 generation modes after visual planning / consistency prep.

    Priority:
    1. True FL2VA when first+last still keyframes exist (manual or generated)
    2. Periodic Ref2VA re-anchor when character references exist
    3. I2VA last-frame chain for small/adjacent continuity
    4. T2VA fallback
    """
    anchor_by_beat = {
        beat["beat_id"]: beat.get("anchor_image_id") for beat in story.get("beats", [])
    }
    characters = {
        item["character_id"]: item for item in story.get("characters", [])
    }
    identity = identity_config(config) if config is not None else {}
    identity_enabled = bool(identity.get("enabled", False))
    reanchor_interval = max(1, int(identity.get("reanchor_interval_shots") or 2))
    include_previous = bool(
        identity.get("include_previous_last_frame_reference", True)
    )
    used_anchors: set[str] = set()
    previous: dict[str, Any] | None = None
    shots_since_reanchor = reanchor_interval
    if (
        identity_enabled
        and identity.get("require_character_references", True)
        and not any(
            declared_character_references(character, config)
            for character in characters.values()
        )
    ):
        raise ValueError(
            "已启用人物一致性，但故事和配置中没有任何角色参考图。"
            "请重新执行 plan-story，或配置 identity_consistency.character_references。"
        )
    for shot in document.get("shots", []):
        shot["status"] = shot.get("status") or "planned"
        shot["attempt"] = int(shot.get("attempt") or 1)
        anchor = anchor_by_beat.get(shot.get("beat_id"))
        anchor = anchor if anchor and anchor not in used_anchors else None
        same_scene_boundary = (
            previous is not None
            and shot.get("scene_id") == previous.get("scene_id")
            and _boundary_matches(previous, shot)
        )
        scene_changed = previous is None or shot.get("scene_id") != previous.get("scene_id")
        if scene_changed:
            shots_since_reanchor = reanchor_interval

        reference_character_ids = [
            character_id
            for character_id in shot.get("characters", [])
            if character_id in characters
            and config is not None
            and declared_character_references(characters[character_id], config)
        ]
        last_keyframe = (
            _prepared_last_frame(shot, config)
            if identity_enabled or shot.get("source_last_frame")
            else None
        )
        first_keyframe = _prepared_first_frame(shot)
        has_fl_pair = last_keyframe is not None and (
            first_keyframe is not None or anchor is not None or same_scene_boundary
        )

        due_for_reanchor = (
            identity_enabled
            and bool(reference_character_ids)
            and not has_fl_pair
            and (scene_changed or shots_since_reanchor >= reanchor_interval)
        )
        if has_fl_pair:
            shot["generation_mode"] = "first_last_frame"
            if first_keyframe is not None:
                shot["prepared_first_frame"] = str(first_keyframe)
                # Keep beat-anchor attribution for the unique IMG01/02/03 rule even
                # when the concrete first still is a prepared/copied keyframe file.
                shot["source_anchor_image"] = anchor
                shot["depends_on"] = None
            elif anchor:
                shot["source_anchor_image"] = anchor
                shot["depends_on"] = None
                shot.pop("prepared_first_frame", None)
            else:
                shot["source_anchor_image"] = None
                shot["depends_on"] = previous["shot_id"] if previous is not None else None
                shot.pop("prepared_first_frame", None)
            shot["source_last_frame"] = str(last_keyframe)
            shot["reference_character_ids"] = []
            shots_since_reanchor += 1
        elif due_for_reanchor:
            shot["generation_mode"] = "ref2va"
            shot["source_anchor_image"] = anchor
            shot["depends_on"] = (
                previous["shot_id"]
                if same_scene_boundary and include_previous and previous is not None
                else None
            )
            shot["reference_character_ids"] = reference_character_ids
            shot["source_last_frame"] = None
            shots_since_reanchor = 1
        elif anchor:
            shot["generation_mode"] = "first_frame"
            shot["source_anchor_image"] = anchor
            shot["depends_on"] = None
            shot["source_last_frame"] = None
            shot["reference_character_ids"] = []
            shots_since_reanchor += 1
        elif same_scene_boundary and previous is not None:
            shot["generation_mode"] = "first_frame"
            shot["source_anchor_image"] = None
            shot["depends_on"] = previous["shot_id"]
            shot["source_last_frame"] = None
            shot["reference_character_ids"] = []
            shots_since_reanchor += 1
        elif identity_enabled and reference_character_ids:
            # A scene cut cannot use the previous frame as an exact keyframe,
            # but canonical references can still start the new scene.
            shot["generation_mode"] = "ref2va"
            shot["source_anchor_image"] = None
            shot["depends_on"] = None
            shot["source_last_frame"] = None
            shot["reference_character_ids"] = reference_character_ids
            shots_since_reanchor = 1
        else:
            shot["generation_mode"] = "t2va"
            shot["source_anchor_image"] = None
            shot["depends_on"] = None
            shot["source_last_frame"] = None
            shot["reference_character_ids"] = []
            shots_since_reanchor += 1
        if anchor:
            used_anchors.add(anchor)
        previous = shot


def _apply_recovery_overrides(document: dict[str, Any], output_dir: Path) -> list[dict[str, Any]]:
    """Apply explicit, reviewable repairs without modifying the preserved raw response."""
    path = output_dir / "recovery_overrides.json"
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    shots = {shot["shot_id"]: shot for shot in document.get("shots", [])}
    applied: list[dict[str, Any]] = []
    for repair in payload.get("repairs", []):
        shot_id = repair["shot_id"]
        if shot_id not in shots:
            raise ValueError(f"修正清单引用未知镜头：{shot_id}")
        if repair.get("target") == "dialogue":
            dialogue_index = int(repair["dialogue_index"])
            dialogue = shots[shot_id]["dialogue"]
            if dialogue_index >= len(dialogue) or dialogue[dialogue_index].get("text") != repair["from"]:
                raise ValueError(f"修正清单对白原值不匹配：{shot_id}/{dialogue_index}")
            dialogue[dialogue_index]["text"] = repair["to"]
            shots[shot_id]["subtitle_text"] = "".join(item["text"] for item in dialogue)
            applied.append(repair)
            continue
        if repair.get("target") == "shot_field":
            field = repair["field"]
            if shots[shot_id].get(field) != repair["from"]:
                raise ValueError(f"修正清单镜头字段原值不匹配：{shot_id}/{field}")
            shots[shot_id][field] = repair["to"]
            applied.append(repair)
            continue
        character_id = repair["character_id"]
        boundary = repair.get("boundary")
        field = repair["field"]
        blocking = next(
            (item for item in shots[shot_id]["blocking"] if item["character_id"] == character_id),
            None,
        )
        if blocking is None:
            raise ValueError(f"修正清单引用未知角色：{shot_id}/{character_id}")
        target = blocking[boundary] if boundary else blocking
        expected = repair["from"]
        if target.get(field) != expected:
            raise ValueError(
                f"修正清单原值不匹配：{shot_id}/{character_id}/{boundary or 'blocking'}/{field}"
            )
        target[field] = repair["to"]
        applied.append(repair)
    return applied


def _dialogue_exception_ids(output_dir: Path) -> set[str]:
    path = output_dir / "dialogue_pacing_exceptions.json"
    if not path.is_file():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {item["shot_id"] for item in payload.get("exceptions", [])}


def plan_shots(run_dir: Path, config: ProjectConfig) -> Path:
    if approval_status(run_dir, "story") != "approved":
        raise ValueError("故事尚未批准，或批准标记已失效")
    story_path, _ = stage_paths(run_dir, "story")
    story = json.loads(story_path.read_text(encoding="utf-8"))

    output_dir = run_dir / "03_shots"
    artifact = output_dir / "shots.json"
    raw_path = output_dir / "raw_response.txt"
    metadata_path = output_dir / "request_metadata.json"
    timeline_path = output_dir / "timeline.md"
    error_path = output_dir / "error.json"
    if artifact.exists():
        raise FileExistsError(f"镜头产物已存在，拒绝覆盖：{artifact}")

    root = _package_root()
    schema = json.loads((root / "schemas/shots.schema.json").read_text(encoding="utf-8"))
    model_schema = _planning_schema(schema)
    system_prompt = (root / "prompts/split_shots.system.txt").read_text(encoding="utf-8").strip()
    user_template = (root / "prompts/split_shots.user.md").read_text(encoding="utf-8").strip()
    user_text = (
        user_template
        .replace("{{BEAT_COUNT}}", str(len(story.get("beats", []))))
        .replace("{{STORY_JSON}}", json.dumps(story, ensure_ascii=False, indent=2))
        .replace("{{SCHEMA_JSON}}", json.dumps(model_schema, ensure_ascii=False, indent=2))
    )
    started = time.monotonic()
    started_at = utc_now()
    response: Any | None = None
    recovered_from_raw = raw_path.is_file()
    try:
        if recovered_from_raw:
            content = raw_path.read_text(encoding="utf-8")
        else:
            response = create_text_completion(config, system_prompt=system_prompt, user_text=user_text)
            content = completion_text(response)
            raw_path.write_text(content + "\n", encoding="utf-8")
        document = _json_from_text(content)
        enum_normalizations = _normalize_known_enums(document)
        _normalize_workflow_fields(document, story, config)
        applied_repairs = _apply_recovery_overrides(document, output_dir)
        exceptions = _dialogue_exception_ids(output_dir)
        errors = validate_document("shots", document, dialogue_overflow_exceptions=exceptions)
        errors.extend(validate_shots_against_story(document, story))
        if errors:
            raise ValueError("镜头 JSON 校验失败：" + "; ".join(sorted(set(errors))))
        write_json_atomic(artifact, document)
        timeline_path.write_text(_timeline_markdown(document), encoding="utf-8")
        write_json_atomic(output_dir / "validation_warnings.json", {
            "schema_version": "1.0", "warnings": dialogue_pacing_warnings(document, chars_per_second=4)
        })
        write_json_atomic(metadata_path, {
            "schema_version": "1.0", "started_at": started_at, "completed_at": utc_now(),
            "elapsed_s": round(time.monotonic() - started, 3),
            "deployment": (config.data.get("llm") or config.data.get("azure") or {}).get("model")
            or (config.data.get("azure") or {}).get("deployment"),
            "usage": _usage_dict(response) if response is not None else None,
            "response_id": getattr(response, "id", None),
            "recovered_from_existing_raw_response": recovered_from_raw,
            "enum_normalizations": enum_normalizations,
            "applied_recovery_overrides": applied_repairs,
            "dialogue_overflow_exceptions": sorted(exceptions),
        })
    except Exception as exc:
        write_json_atomic(error_path, {
            "schema_version": "1.0", "failed_at": utc_now(),
            "elapsed_s": round(time.monotonic() - started, 3),
            "error_type": type(exc).__name__, "message": str(exc),
        })
        raise

    state = read_run(run_dir)
    state["state"] = "SHOTS_GENERATED"
    state["updated_at"] = utc_now()
    state["shots"] = str(artifact)
    write_json_atomic(run_dir / "run.json", state)
    return artifact
