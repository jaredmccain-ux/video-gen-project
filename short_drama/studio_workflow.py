"""Editable, locally persisted Studio workflow for planning stages 02–04."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from .approval import approval_status, create_approval, stage_paths
from .azure_client import completion_text, create_text_completion
from .config import ProjectConfig
from .drama_writer import (
    describe_image_as_scene_card,
    load_outline,
    load_screenplay,
    revise_screenplay,
    save_story_sidecars,
    story_document_from_screenplay,
    write_full_screenplay,
    write_story_outline,
)
from .human_orchestration import resolve_asset_path
from .inspiration import load_inspiration
from .state import read_run, utc_now, write_json_atomic


STUDIO_STAGES = ("descriptions", "story", "shots")
STAGE_STATES = {
    "descriptions": "DESCRIPTIONS_GENERATED",
    "story": "STORY_GENERATED",
    "shots": "SHOTS_GENERATED",
}


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    value = json.loads(cleaned.strip())
    if not isinstance(value, dict):
        raise ValueError("模型响应必须是 JSON 对象")
    return value


def _artifact(run_dir: Path, stage: str) -> Path:
    if stage not in STUDIO_STAGES:
        raise ValueError(f"未知制作阶段：{stage}")
    return stage_paths(run_dir, stage)[0]


def _backup_existing(run_dir: Path, stage: str, artifact: Path) -> None:
    if not artifact.is_file():
        return
    version_dir = artifact.parent / "studio_versions"
    version_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("+", "-")
    target = version_dir / f"{stamp}-{uuid.uuid4().hex[:8]}.json"
    shutil.copy2(artifact, target)


def _reset_human_orchestration(run_dir: Path) -> None:
    """Archive decisions tied to the previous shot plan before replacing it."""
    path = run_dir / "03_shots/human_orchestration.json"
    if not path.is_file():
        return
    version_dir = run_dir / "03_shots/studio_versions"
    version_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("+", "-")
    backup = version_dir / f"{stamp}-{uuid.uuid4().hex[:8]}-human_orchestration.json"
    shutil.copy2(path, backup)
    write_json_atomic(path, {
        "schema_version": "1.0",
        "updated_at": utc_now(),
        "policy": "human_decision_overrides_automatic_routing",
        "invalidated_reason": "shot_plan_replaced",
        "archived_as": str(backup.relative_to(run_dir)),
        "shots": {},
    })


def _validate_document(stage: str, document: dict[str, Any]) -> None:
    if stage == "descriptions":
        images = document.get("images")
        if not isinstance(images, list) or not images:
            raise ValueError("画面理解至少需要 1 条图片描述")
        required = ("image_id", "source_path", "visible_facts", "setting", "mood_or_atmosphere")
        for index, item in enumerate(images, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"第 {index} 条图片描述格式错误")
            missing = [key for key in required if item.get(key) in (None, "", [])]
            if missing:
                raise ValueError(f"{item.get('image_id') or index} 缺少字段：{', '.join(missing)}")
    elif stage == "story":
        missing = [key for key in ("title", "logline", "full_story", "beats") if not document.get(key)]
        if missing:
            raise ValueError("故事规划缺少字段：" + ", ".join(missing))
        if not isinstance(document.get("beats"), list):
            raise ValueError("beats 必须是数组")
        duration = sum(float(item.get("duration_s") or 0) for item in document["beats"] if isinstance(item, dict))
        if not 117 <= duration <= 123:
            raise ValueError(f"剧情段落总时长必须为 117–123 秒，当前为 {duration:g} 秒")
    elif stage == "shots":
        shots = document.get("shots")
        if not isinstance(shots, list) or not shots:
            raise ValueError("分镜拆分至少需要 1 个镜头")
        ids = [item.get("shot_id") for item in shots if isinstance(item, dict)]
        if len(ids) != len(shots) or len(ids) != len(set(ids)):
            raise ValueError("每个镜头必须拥有唯一 shot_id")
        durations = [float(item.get("duration_s") or 0) for item in shots]
        if any(not 4 <= value <= 8 for value in durations):
            raise ValueError("每个镜头时长必须在 4–8 秒之间")
        if not 117 <= sum(durations) <= 123:
            raise ValueError(f"镜头总时长必须为 117–123 秒，当前为 {sum(durations):g} 秒")


def save_stage_document(run_dir: Path, stage: str, document: dict[str, Any]) -> Path:
    _validate_document(stage, document)
    artifact = _artifact(run_dir, stage)
    previous = None
    if artifact.is_file():
        try:
            previous = json.loads(artifact.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            previous = None
    changed = previous != document
    if changed:
        _backup_existing(run_dir, stage, artifact)
    if stage == "shots" and changed:
        _reset_human_orchestration(run_dir)
    write_json_atomic(artifact, document)
    if stage == "story" and document.get("screenplay"):
        save_story_sidecars(
            run_dir,
            outline=str(document.get("outline") or load_outline(run_dir)),
            screenplay=str(document["screenplay"]),
        )
    state = read_run(run_dir)
    state["state"] = STAGE_STATES[stage]
    state["updated_at"] = utc_now()
    state[stage] = str(artifact)
    write_json_atomic(run_dir / "run.json", state)
    return artifact


def load_stage_document(run_dir: Path, stage: str) -> dict[str, Any] | None:
    artifact = _artifact(run_dir, stage)
    if not artifact.is_file():
        return None
    value = json.loads(artifact.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"阶段文件格式错误：{artifact}")
    return value


def workflow_snapshot(run_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for stage in STUDIO_STAGES:
        artifact = _artifact(run_dir, stage)
        result[stage] = {
            "document": load_stage_document(run_dir, stage),
            "status": approval_status(run_dir, stage),
            "path": str(artifact.relative_to(run_dir)),
            "updated_at": utc_now() if artifact.is_file() else None,
        }
    return result


def approve_stage(run_dir: Path, stage: str) -> Path:
    document = load_stage_document(run_dir, stage)
    if document is None:
        raise FileNotFoundError("当前阶段还没有可批准的产物")
    _validate_document(stage, document)
    return create_approval(run_dir, stage, confirmed=True)


def _selected_images(run_dir: Path, config: ProjectConfig) -> list[Path]:
    values = list(load_inspiration(run_dir).get("selected_images") or [])
    paths = [resolve_asset_path(value, run_dir=run_dir, config=config, required=True) for value in values]
    unique = list(dict.fromkeys(path for path in paths if path is not None))
    if not unique:
        raise ValueError("请先在素材库选择至少 1 张图片作为灵感，再生成画面描述")
    return unique


def generate_descriptions(run_dir: Path, config: ProjectConfig) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for index, path in enumerate(_selected_images(run_dir, config), start=1):
        records.append(
            describe_image_as_scene_card(
                config,
                image_path=path,
                image_id=f"IMG{index:02d}",
                index=index,
            )
        )
    document = {"schema_version": "2.0", "generated_at": utc_now(), "images": records}
    save_stage_document(run_dir, "descriptions", document)
    return document


def generate_story(run_dir: Path, config: ProjectConfig) -> dict[str, Any]:
    if approval_status(run_dir, "descriptions") != "approved":
        raise ValueError("请先人工批准画面理解结果")
    descriptions = load_stage_document(run_dir, "descriptions") or {}
    inspiration = load_inspiration(run_dir)
    image_ids = [item.get("image_id") for item in descriptions.get("images", [])]
    proposal = (inspiration.get("current_proposal") or {}).get("proposal")
    outline = load_outline(run_dir)
    screenplay = load_screenplay(run_dir)
    if not outline:
        outline = write_story_outline(
            config,
            descriptions=descriptions,
            proposal=proposal if isinstance(proposal, dict) else None,
            target_duration_s=int(config.data.get("target_duration_s") or 120),
        )
    if not screenplay:
        screenplay = write_full_screenplay(config, outline)
        save_story_sidecars(run_dir, outline=outline, screenplay=screenplay)
    else:
        save_story_sidecars(run_dir, outline=outline, screenplay=screenplay)
    document = story_document_from_screenplay(
        config,
        descriptions=descriptions,
        outline=outline,
        screenplay=screenplay,
        image_ids=[value for value in image_ids if value],
    )
    beats = document.get("beats") if isinstance(document.get("beats"), list) else []
    if not 3 <= len(beats) <= 30:
        raise ValueError("模型返回的剧情段落数量异常，请重新生成")
    base, remainder = divmod(120, len(beats))
    if not 4 <= base <= 40:
        raise ValueError("剧情段落数量无法分配为 120 秒")
    for index, beat in enumerate(beats):
        beat["duration_s"] = base + (1 if index < remainder else 0)
    save_stage_document(run_dir, "story", document)
    return document


def _boundary(character_id: str, *, speaking: bool = False) -> dict[str, Any]:
    return {
        "character_id": character_id,
        "speaks": speaking,
        "movement_direction": "none",
        "start": {"horizontal": "screen-center", "depth": "midground", "facing": "camera", "visible": True, "mouth_state": "closed"},
        "end": {"horizontal": "screen-center", "depth": "midground", "facing": "camera", "visible": True, "mouth_state": "speaking" if speaking else "closed"},
    }


def _extract_raw_shots(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    shots = payload.get("shots")
    if isinstance(shots, list):
        return shots
    for value in payload.values():
        if isinstance(value, dict) and isinstance(value.get("shots"), list):
            return value["shots"]
    return []


def _expand_shots_to_count(raw_shots: list[Any], story: dict[str, Any], minimum: int = 15, maximum: int = 30) -> list[dict[str, Any]]:
    shots = [dict(item) if isinstance(item, dict) else {} for item in raw_shots]
    beats = [item for item in (story.get("beats") or []) if isinstance(item, dict)]
    if not shots:
        for index in range(minimum):
            beat = beats[index % len(beats)] if beats else {}
            shots.append({
                "beat_id": beat.get("beat_id") or "B01",
                "story_purpose": str(beat.get("summary") or "推进剧情"),
                "action_timeline": " ".join(str(event) for event in (beat.get("events") or [])[:3]) or "人物完成当前动作。",
                "generation_mode": "t2va",
            })
    while len(shots) < minimum:
        index = max(range(len(shots)), key=lambda i: len(str(shots[i].get("action_timeline") or shots[i].get("story_purpose") or "")))
        source = shots[index]
        first = dict(source)
        second = dict(source)
        purpose = str(source.get("story_purpose") or "推进剧情")
        first["story_purpose"] = purpose + "（前）"
        second["story_purpose"] = purpose + "（后）"
        second["dialogue"] = []
        shots[index:index + 1] = [first, second]
    return shots[:maximum]


def _normalize_shots(document: dict[str, Any], story: dict[str, Any]) -> dict[str, Any]:
    characters = [item.get("character_id") for item in story.get("characters", []) if item.get("character_id")]
    raw_shots = _expand_shots_to_count(_extract_raw_shots(document), story)
    base_duration, duration_remainder = divmod(120, len(raw_shots))
    if not 4 <= base_duration <= 8:
        raise ValueError(f"无法把 {len(raw_shots)} 个镜头分配为 4–8 秒且总长 120 秒")
    previous_id: str | None = None
    cursor = 0.0
    normalized = []
    for index, raw in enumerate(raw_shots, start=1):
        item = dict(raw) if isinstance(raw, dict) else {}
        shot_id = f"S{index:03d}"
        duration = base_duration + (1 if index <= duration_remainder else 0)
        shot_characters = list(dict.fromkeys(item.get("characters") or characters[:1] or ["C01"]))
        dialogue = item.get("dialogue") if isinstance(item.get("dialogue"), list) else []
        dialogue = [entry for entry in dialogue if isinstance(entry, dict) and entry.get("text")]
        speaker = dialogue[0].get("speaker_id") if dialogue else None
        if speaker and speaker not in shot_characters:
            shot_characters.append(speaker)
        mode = str(item.get("generation_mode") or "t2va")
        if mode not in {"t2va", "first_frame", "first_last_frame", "ref2va"}:
            mode = "t2va"
        item.update({
            "shot_id": shot_id,
            "beat_id": item.get("beat_id") or "B01",
            "scene_id": item.get("scene_id") or "L01",
            "planned_start_s": cursor,
            "planned_end_s": cursor + duration,
            "duration_s": duration,
            "story_purpose": str(item.get("story_purpose") or "推进剧情"),
            "composition": str(item.get("composition") or "电影感中景构图"),
            "camera": str(item.get("camera") or "稳定镜头"),
            "action_timeline": str(item.get("action_timeline") or "人物在画面内完成当前剧情动作。"),
            "continuity_in": str(item.get("continuity_in") or "承接上一镜头状态。"),
            "continuity_out": str(item.get("continuity_out") or "保持人物与道具连续。"),
            "characters": shot_characters,
            "blocking": [_boundary(cid, speaking=cid == speaker) for cid in shot_characters],
            "dialogue": dialogue[:1],
            "subtitle_text": "".join(str(entry.get("text") or "") for entry in dialogue[:1]),
            "speaker_mappings": ([{"character_id": speaker, "prompt_speaker_id": "S1"}] if speaker else []),
            "audio_contract": {
                "allowed_speaker_ids": [speaker] if speaker else [],
                "offscreen_human_voice_allowed": False,
                "non_diegetic_music": False,
                "ambient_sounds": list((item.get("audio_contract") or {}).get("ambient_sounds") or []),
                "action_sounds": list((item.get("audio_contract") or {}).get("action_sounds") or []),
            },
            "generation_mode": mode,
            "depends_on": item.get("depends_on") if item.get("depends_on") else previous_id,
            "source_anchor_image": item.get("source_anchor_image"),
            "status": "planned",
            "attempt": 1,
        })
        normalized.append(item)
        previous_id = shot_id
        cursor += duration
    return {"schema_version": "2.0", "generated_at": utc_now(), "shots": normalized}


def generate_shots(run_dir: Path, config: ProjectConfig) -> dict[str, Any]:
    if approval_status(run_dir, "story") != "approved":
        raise ValueError("请先人工批准故事规划")
    story = load_stage_document(run_dir, "story") or {}
    screenplay = load_screenplay(run_dir) or str(story.get("screenplay") or "")
    hint = {
        "shots": [{
            "beat_id": "B01", "scene_id": "L01", "duration_s": 6,
            "story_purpose": "镜头叙事作用", "composition": "构图与可见主体", "camera": "机位与运动",
            "action_timeline": "按秒动作", "continuity_in": "入镜状态", "continuity_out": "出镜状态",
            "characters": ["C01"], "dialogue": [{"speaker_id": "C01", "text": "简短对白"}],
            "generation_mode": "t2va|first_frame|first_last_frame|ref2va", "source_anchor_image": None,
        }]
    }
    last_error: Exception | None = None
    document = None
    for attempt in range(2):
        extra = "" if attempt == 0 else "上次镜头数量不够。这次必须返回 20 个 shot 对象，不要少，也不要输出剧本原文。"
        try:
            response = create_text_completion(
                config,
                system_prompt="你是短剧分镜导演。只返回 JSON 对象，shots 数组必须有 20 个元素。",
                user_text=(
                    extra
                    + "不要修改剧情。必须保留重要对白。每个镜头 4–8 秒、只安排一个主要动作，单镜头最多一个说话人。\n"
                    "生成模式只能是 t2va、first_frame、first_last_frame、ref2va。\n"
                    "已批准故事摘要：\n"
                    + json.dumps({
                        "title": story.get("title"),
                        "logline": story.get("logline"),
                        "characters": story.get("characters"),
                        "beats": story.get("beats"),
                    }, ensure_ascii=False)
                    + ("\n正式剧本节选：\n" + screenplay[:5000] if screenplay else "")
                    + "\n返回结构：\n" + json.dumps(hint, ensure_ascii=False)
                ),
                max_completion_tokens=8192,
            )
            document = _normalize_shots(_parse_json_object(completion_text(response)), story)
            break
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            document = None
    if document is None:
        document = _normalize_shots({"shots": []}, story)
        document["structure_error"] = str(last_error) if last_error else ""
    save_stage_document(run_dir, "shots", document)
    return document


def revise_story(run_dir: Path, config: ProjectConfig, instruction: str = "") -> dict[str, Any]:
    if approval_status(run_dir, "descriptions") != "approved":
        raise ValueError("请先人工批准画面理解结果")
    descriptions = load_stage_document(run_dir, "descriptions") or {}
    story = load_stage_document(run_dir, "story") or {}
    outline = load_outline(run_dir) or str(story.get("outline") or "")
    screenplay = load_screenplay(run_dir) or str(story.get("screenplay") or story.get("full_story") or "")
    if not screenplay:
        raise ValueError("还没有可重写的正式剧本，请先生成故事")
    revised = revise_screenplay(
        config,
        screenplay=screenplay,
        outline=outline,
        descriptions=descriptions,
        instruction=instruction,
    )
    save_story_sidecars(run_dir, outline=outline, screenplay=revised)
    image_ids = [item.get("image_id") for item in descriptions.get("images", []) if item.get("image_id")]
    document = story_document_from_screenplay(
        config,
        descriptions=descriptions,
        outline=outline,
        screenplay=revised,
        image_ids=image_ids,
        previous=story,
    )
    beats = document.get("beats") if isinstance(document.get("beats"), list) else []
    if not beats:
        raise ValueError("重写后没有可用的剧情段落，请再试一次")
    base, remainder = divmod(120, max(len(beats), 1))
    for index, beat in enumerate(beats):
        if isinstance(beat, dict):
            beat["duration_s"] = base + (1 if index < remainder else 0)
    save_stage_document(run_dir, "story", document)
    return document


def generate_stage(run_dir: Path, config: ProjectConfig, stage: str) -> dict[str, Any]:
    if stage == "descriptions":
        return generate_descriptions(run_dir, config)
    if stage == "story":
        return generate_story(run_dir, config)
    if stage == "shots":
        return generate_shots(run_dir, config)
    raise ValueError(f"未知制作阶段：{stage}")
