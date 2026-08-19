"""Decompose approved shots into first/last frame briefs and variation types."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .azure_client import create_text_completion
from .config import ProjectConfig
from .state import utc_now, write_json_atomic
from .validators import load_json


SYSTEM_PROMPT = """You decompose short-drama shots into first-frame, last-frame, and motion briefs for image/video generation.
Return ONLY valid JSON matching the schema. No markdown fences.
variation_type rules:
- small: camera or subject moves slightly; start and end look almost like the same framing
- medium: clear pose/position/camera change within the same scene, still recognizable continuity
- large: major framing, location within scene, entrance/exit, or composition change
Prefer Chinese for descriptive text fields when the shot content is Chinese.
"""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _heuristic_variation(shot: dict[str, Any]) -> str:
    action = str(shot.get("action_timeline") or "")
    large_terms = ("冲", "跑", "进入", "离开", "转场", "切换", "爆炸", "坠落", "扑")
    medium_terms = ("走", "转", "回头", "靠近", "后退", "坐下", "站起", "伸手", "递")
    if any(term in action for term in large_terms):
        return "large"
    if any(term in action for term in medium_terms):
        return "medium"
    start_states = {
        (b["character_id"], tuple(sorted((b.get("start") or {}).items())))
        for b in shot.get("blocking", [])
    }
    end_states = {
        (b["character_id"], tuple(sorted((b.get("end") or {}).items())))
        for b in shot.get("blocking", [])
    }
    if start_states != end_states:
        return "medium"
    return "small"


def _heuristic_document(shots_doc: dict[str, Any]) -> dict[str, Any]:
    visuals = []
    for shot in shots_doc.get("shots", []):
        variation = _heuristic_variation(shot)
        visuals.append(
            {
                "shot_id": shot["shot_id"],
                "variation_type": variation,
                "first_frame_desc": (
                    f"开场构图：{shot.get('camera', '')}；动作起点：{shot.get('action_timeline', '')[:120]}"
                ),
                "last_frame_desc": (
                    f"收束构图：延续同场，动作终点停在可衔接下一镜的姿态；"
                    f"对白后画面停稳。原动作：{shot.get('action_timeline', '')[:120]}"
                ),
                "motion_desc": shot.get("action_timeline") or shot.get("visual_description") or "",
            }
        )
    return {"shots": visuals}


def decompose_shots(run_dir: Path, config: ProjectConfig) -> Path:
    """Create 03_shots/shot_visuals.json with ff/lf/motion and variation_type."""
    run_dir = run_dir.resolve()
    shots_doc = load_json(run_dir / "03_shots/shots.json")
    story = load_json(run_dir / "02_story/story.json")
    compact_shots = [
        {
            "shot_id": shot["shot_id"],
            "scene_id": shot.get("scene_id"),
            "beat_id": shot.get("beat_id"),
            "duration_s": shot.get("duration_s"),
            "camera": shot.get("camera"),
            "characters": shot.get("characters"),
            "action_timeline": shot.get("action_timeline"),
            "visual_description": shot.get("visual_description"),
            "dialogue": shot.get("dialogue"),
            "blocking": shot.get("blocking"),
        }
        for shot in shots_doc.get("shots", [])
    ]
    user_text = (
        "Story characters:\n"
        + json.dumps(story.get("characters", []), ensure_ascii=False)
        + "\n\nShots to decompose:\n"
        + json.dumps(compact_shots, ensure_ascii=False)
        + "\n\nReturn JSON: {\"shots\":[{\"shot_id\":\"S001\",\"variation_type\":\"small|medium|large\","
        "\"first_frame_desc\":\"...\",\"last_frame_desc\":\"...\",\"motion_desc\":\"...\"}]}"
        " covering EVERY shot_id exactly once."
    )
    try:
        response = create_text_completion(
            config,
            system_prompt=SYSTEM_PROMPT,
            user_text=user_text,
            max_completion_tokens=16384,
        )
        raw = response.choices[0].message.content or ""
        parsed = _extract_json(raw)
    except Exception:
        parsed = _heuristic_document(shots_doc)
        raw = ""

    by_id = {item.get("shot_id"): item for item in parsed.get("shots", []) if item.get("shot_id")}
    visuals: list[dict[str, Any]] = []
    for shot in shots_doc.get("shots", []):
        item = by_id.get(shot["shot_id"]) or {}
        variation = str(item.get("variation_type") or _heuristic_variation(shot)).lower()
        if variation not in {"small", "medium", "large"}:
            variation = _heuristic_variation(shot)
        visuals.append(
            {
                "shot_id": shot["shot_id"],
                "variation_type": variation,
                "first_frame_desc": str(item.get("first_frame_desc") or "").strip()
                or _heuristic_document({"shots": [shot]})["shots"][0]["first_frame_desc"],
                "last_frame_desc": str(item.get("last_frame_desc") or "").strip()
                or _heuristic_document({"shots": [shot]})["shots"][0]["last_frame_desc"],
                "motion_desc": str(item.get("motion_desc") or shot.get("action_timeline") or "").strip(),
            }
        )

    artifact = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "shots": visuals,
        "raw_response_present": bool(raw),
    }
    path = run_dir / "03_shots/shot_visuals.json"
    write_json_atomic(path, artifact)
    # Also stamp variation onto shots for routing / prompts without a second lookup.
    for shot, visual in zip(shots_doc["shots"], visuals, strict=True):
        shot["variation_type"] = visual["variation_type"]
        shot["first_frame_desc"] = visual["first_frame_desc"]
        shot["last_frame_desc"] = visual["last_frame_desc"]
        shot["motion_desc"] = visual["motion_desc"]
    write_json_atomic(run_dir / "03_shots/shots.json", shots_doc)
    if raw:
        (run_dir / "03_shots/shot_visuals.raw.txt").write_text(raw, encoding="utf-8")
    return path


def load_shot_visuals(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "03_shots/shot_visuals.json"
    if not path.is_file():
        return None
    return load_json(path)
