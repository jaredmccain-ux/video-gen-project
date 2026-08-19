"""Select up to N reference images per shot using a multimodal LLM."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .azure_client import create_multimodal_completion
from .character_portraits import load_portrait_registry, portrait_bindings_for_characters
from .config import ProjectConfig
from .image_generator import consistency_config
from .reference_assets import identity_config
from .state import utc_now, write_json_atomic
from .validators import load_json


SYSTEM_PROMPT = """You are a reference-image selector for identity-consistent video generation.
Given candidate stills and a shot brief, choose the smallest useful set of images.
Hard rules:
- Every required_character_id MUST keep at least one image in selected_indices.
- Prefer clear face/identity portraits for every visible character.
- Prefer complementary views (front + side) over near-duplicates.
- Include at most one scene/environment still if it helps location identity.
- Never exceed the provided max_images.
- Do not drop a character just to keep a nicer duplicate of another character.
Return ONLY JSON: {"selected_indices":[0,2,...],"reasons":["..."]}
Indices are 0-based into the candidate list shown to you.
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


def _visible_character_ids(shot: dict[str, Any]) -> list[str]:
    """Prefer characters marked visible in blocking; fall back to shot.characters."""
    visible: list[str] = []
    seen: set[str] = set()
    for item in shot.get("blocking") or []:
        character_id = item.get("character_id")
        start = item.get("start") or {}
        end = item.get("end") or {}
        if character_id and (start.get("visible") or end.get("visible")):
            if character_id not in seen:
                seen.add(character_id)
                visible.append(character_id)
    if visible:
        return visible
    return list(shot.get("characters") or [])


def _candidate_pool(
    *,
    run_dir: Path,
    config: ProjectConfig,
    story: dict[str, Any],
    manifest: dict[str, Any],
    shot: dict[str, Any],
    registry: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    character_ids = list(shot.get("characters") or [])
    pool: list[dict[str, Any]] = []

    # Manual identity_consistency overrides first.
    manual = identity_config(config).get("character_references") or {}
    if isinstance(manual, dict):
        for character_id in character_ids:
            values = manual.get(character_id, [])
            if isinstance(values, str):
                values = [values]
            for value in values or []:
                path = Path(value).expanduser()
                path = path.resolve() if path.is_absolute() else (config.project_root / path).resolve()
                if path.is_file() and all(path != item["path"] for item in pool):
                    pool.append({"path": path, "role": "manual", "character_ids": [character_id]})

    for path, character_id, view in portrait_bindings_for_characters(registry, character_ids):
        if all(path != item["path"] for item in pool):
            pool.append(
                {
                    "path": path,
                    "role": "portrait",
                    "view": view,
                    "character_ids": [character_id],
                }
            )

    characters = {item["character_id"]: item for item in story.get("characters", [])}
    anchors = {
        item["image_id"]: Path(item["output_path"]).resolve()
        for item in manifest.get("images", [])
    }
    for character_id in character_ids:
        character = characters.get(character_id) or {}
        for image_id in character.get("reference_image_ids") or []:
            path = anchors.get(image_id)
            if path and path.is_file() and all(path != item["path"] for item in pool):
                pool.append({"path": path, "role": "character_anchor", "character_ids": [character_id]})

    anchor_id = shot.get("source_anchor_image")
    if anchor_id in anchors and all(anchors[anchor_id] != item["path"] for item in pool):
        pool.append({"path": anchors[anchor_id], "role": "scene_anchor", "character_ids": []})

    return pool


def _role_rank(role: str) -> int:
    return {"manual": 0, "portrait": 1, "character_anchor": 2, "scene_anchor": 3}.get(role, 9)


def _covers_character(item: dict[str, Any], character_id: str) -> bool:
    return character_id in (item.get("character_ids") or [])


def _required_cover_indices(pool: list[dict[str, Any]], required: list[str]) -> list[int]:
    """Pick one best image per required character, then stop."""
    chosen: list[int] = []
    used: set[int] = set()
    for character_id in required:
        candidates = [
            index
            for index, item in enumerate(pool)
            if _covers_character(item, character_id) and index not in used
        ]
        if not candidates:
            continue
        best = min(candidates, key=lambda i: (_role_rank(pool[i]["role"]), i))
        used.add(best)
        chosen.append(best)
    return chosen


def _enforce_character_coverage(
    pool: list[dict[str, Any]],
    selected: list[int],
    required: list[str],
    max_images: int,
) -> list[int]:
    """Ensure every required character keeps >=1 image; fill remaining by rank."""
    clean: list[int] = []
    seen: set[int] = set()
    for index in selected:
        if 0 <= index < len(pool) and index not in seen:
            seen.add(index)
            clean.append(index)

    for index in _required_cover_indices(pool, required):
        if index not in seen:
            # Insert mandatory covers at the front.
            clean.insert(0, index)
            seen.add(index)

    # Drop extras from the end while preserving coverage.
    while len(clean) > max_images:
        drop_at = None
        for position in range(len(clean) - 1, -1, -1):
            index = clean[position]
            covered = {
                character_id
                for kept in clean
                if kept != index
                for character_id in (pool[kept].get("character_ids") or [])
            }
            item_chars = set(pool[index].get("character_ids") or [])
            # Never drop the last image covering a required character.
            if item_chars & set(required) and not item_chars.issubset(covered):
                continue
            drop_at = position
            break
        if drop_at is None:
            break
        seen.discard(clean[drop_at])
        clean.pop(drop_at)

    if len(clean) < max_images:
        ranked = sorted(range(len(pool)), key=lambda i: (_role_rank(pool[i]["role"]), i))
        for index in ranked:
            if index in seen:
                continue
            clean.append(index)
            seen.add(index)
            if len(clean) >= max_images:
                break
    return clean[:max_images]


def _heuristic_select(
    pool: list[dict[str, Any]],
    max_images: int,
    required: list[str] | None = None,
) -> list[int]:
    required = required or []
    ranked = sorted(range(len(pool)), key=lambda i: (_role_rank(pool[i]["role"]), i))
    return _enforce_character_coverage(pool, ranked, required, max_images)


def select_references_for_shot(
    *,
    config: ProjectConfig,
    shot: dict[str, Any],
    pool: list[dict[str, Any]],
    max_images: int,
) -> tuple[list[int], list[str], bool]:
    required = _visible_character_ids(shot)
    if not pool:
        return [], [], False
    if len(pool) <= max_images:
        return list(range(len(pool))), ["all candidates within budget"], False

    labels = []
    for index, item in enumerate(pool):
        labels.append(
            f"[{index}] role={item['role']} characters={','.join(item['character_ids']) or '-'} "
            f"path={item['path'].name}"
        )
    user_text = (
        f"Shot {shot['shot_id']}\n"
        f"required_character_ids={required}\n"
        f"Camera: {shot.get('camera')}\n"
        f"Action: {shot.get('action_timeline')}\n"
        f"Visual: {shot.get('visual_description')}\n"
        f"First frame: {shot.get('first_frame_desc')}\n"
        f"Last frame: {shot.get('last_frame_desc')}\n"
        f"max_images={max_images}\nCandidates:\n" + "\n".join(labels)
    )
    try:
        response = create_multimodal_completion(
            config,
            system_prompt=SYSTEM_PROMPT,
            user_text=user_text,
            image_paths=[item["path"] for item in pool],
            max_completion_tokens=2048,
        )
        parsed = _extract_json(response.choices[0].message.content or "")
        indices = [int(i) for i in parsed.get("selected_indices", []) if isinstance(i, int)]
        clean = _enforce_character_coverage(pool, indices, required, max_images)
        if not clean:
            clean = _heuristic_select(pool, max_images, required)
        reasons = [str(r) for r in parsed.get("reasons", [])]
        reasons.append("enforced_character_coverage")
        return clean, reasons, True
    except Exception as exc:  # noqa: BLE001
        return _heuristic_select(pool, max_images, required), [f"fallback: {exc}"], False


def prepare_reference_plan(run_dir: Path, config: ProjectConfig) -> Path:
    """Write 03_shots/reference_plan.json with selected refs per shot."""
    run_dir = run_dir.resolve()
    shots_doc = load_json(run_dir / "03_shots/shots.json")
    story = load_json(run_dir / "02_story/story.json")
    manifest = load_json(run_dir / "inputs/manifest.json")
    registry = load_portrait_registry(run_dir)
    cons = consistency_config(config)
    identity = identity_config(config)
    max_images = int(
        cons.get("max_refs_per_shot")
        or identity.get("max_reference_images")
        or 8
    )
    max_images = max(1, min(9, max_images))

    plan_shots: list[dict[str, Any]] = []
    for shot in shots_doc.get("shots", []):
        pool = _candidate_pool(
            run_dir=run_dir,
            config=config,
            story=story,
            manifest=manifest,
            shot=shot,
            registry=registry,
        )
        required = _visible_character_ids(shot)
        if cons.get("select_references", True) and pool:
            indices, reasons, used_llm = select_references_for_shot(
                config=config, shot=shot, pool=pool, max_images=max_images
            )
        else:
            indices = _heuristic_select(pool, max_images, required)
            reasons = ["heuristic"]
            used_llm = False
        selected = [
            {
                "path": str(pool[i]["path"]),
                "role": pool[i]["role"],
                "character_ids": list(pool[i]["character_ids"]),
            }
            for i in indices
        ]
        missing_required = [
            character_id
            for character_id in required
            if not any(character_id in item["character_ids"] for item in selected)
        ]
        if missing_required:
            reasons = list(reasons) + [
                f"missing_coverage:{','.join(missing_required)}"
            ]
        plan_shots.append(
            {
                "shot_id": shot["shot_id"],
                "required_character_ids": required,
                "selected": selected,
                "missing_required_character_ids": missing_required,
                "candidate_count": len(pool),
                "used_llm": used_llm,
                "reasons": reasons,
            }
        )
        # Preserve per-image character bindings for Ref2VA subject mapping.
        shot["selected_references"] = [
            {
                "path": item["path"],
                "character_ids": item["character_ids"],
                "roles": [item["role"]],
            }
            for item in selected
        ]
        shot["selected_reference_paths"] = [item["path"] for item in selected]

    write_json_atomic(run_dir / "03_shots/shots.json", shots_doc)
    path = run_dir / "03_shots/reference_plan.json"
    write_json_atomic(
        path,
        {
            "schema_version": "1.0",
            "generated_at": utc_now(),
            "max_refs_per_shot": max_images,
            "shots": plan_shots,
        },
    )
    return path


def load_reference_plan(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "03_shots/reference_plan.json"
    if not path.is_file():
        return None
    return load_json(path)
