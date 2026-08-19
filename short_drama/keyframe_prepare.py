"""Generate first/last keyframes for medium/large variation shots."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .best_image_selector import select_best_image
from .character_portraits import load_portrait_registry, portrait_paths_for_characters
from .config import ProjectConfig
from .image_generator import (
    consistency_config,
    edit_image,
    generate_images,
    image_generator_enabled,
    save_generated_image,
)
from .reference_assets import identity_config
from .state import utc_now, write_json_atomic
from .validators import load_json


def _visual_map(run_dir: Path, shots_doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    path = run_dir / "03_shots/shot_visuals.json"
    if path.is_file():
        document = load_json(path)
        return {item["shot_id"]: item for item in document.get("shots", [])}
    return {
        shot["shot_id"]: {
            "variation_type": shot.get("variation_type") or "small",
            "first_frame_desc": shot.get("first_frame_desc") or "",
            "last_frame_desc": shot.get("last_frame_desc") or "",
            "motion_desc": shot.get("motion_desc") or "",
        }
        for shot in shots_doc.get("shots", [])
    }


def _identity_refs_for_shot(
    shot: dict[str, Any],
    registry: dict[str, Any] | None,
) -> list[Path]:
    character_ids = list(shot.get("characters") or [])
    paths = portrait_paths_for_characters(registry, character_ids)
    for item in shot.get("selected_references") or []:
        path = Path(str(item.get("path") or ""))
        if path.is_file() and path not in paths:
            paths.append(path)
    for value in shot.get("selected_reference_paths") or []:
        path = Path(value)
        if path.is_file() and path not in paths:
            paths.append(path)
    return paths


def _compose_prompt(desc: str, shot: dict[str, Any], story: dict[str, Any]) -> str:
    characters = {
        item["character_id"]: item for item in story.get("characters", [])
    }
    identity_bits = []
    for character_id in shot.get("characters") or []:
        character = characters.get(character_id) or {}
        bit = character.get("reference_subject_description") or character.get("visual_description")
        if bit:
            identity_bits.append(f"{character.get('name') or character_id}: {bit}")
    identity = " | ".join(identity_bits) if identity_bits else "match provided character references"
    camera = shot.get("camera") or ""
    motion = str(shot.get("motion_desc") or "").strip()
    motion_bit = f" Motion continuity: {motion}." if motion else ""
    return (
        f"Cinematic still keyframe for a short drama. {desc}. "
        f"Camera: {camera}. Identity lock: {identity}.{motion_bit} "
        "Photoreal film still, no text, no watermark, no collage, no split screen."
    )


def _generate_candidates(
    config: ProjectConfig,
    *,
    prompt: str,
    references: list[Path],
    count: int,
    out_dir: Path,
    stem: str,
) -> list[Path]:
    paths: list[Path] = []
    primary = next((path for path in references if path.is_file()), None)
    if primary is not None:
        # edits API typically returns one image; repeat with prompt variants if needed.
        # Rotate among available identity references so multi-character shots are not
        # locked to a single face crop.
        usable_refs = [path for path in references if path.is_file()] or [primary]
        for index in range(count):
            reference = usable_refs[index % len(usable_refs)]
            variant = prompt if index == 0 else f"{prompt} Slightly different framing variation {index + 1}."
            try:
                data = edit_image(config, prompt=variant, reference_image=reference)
            except Exception:
                data = generate_images(config, prompt=variant, count=1)[0]
            path = out_dir / f"{stem}.cand{index}.png"
            save_generated_image(data, path)
            paths.append(path)
        return paths
    blobs = generate_images(config, prompt=prompt, count=count)
    for index, data in enumerate(blobs):
        path = out_dir / f"{stem}.cand{index}.png"
        save_generated_image(data, path)
        paths.append(path)
    return paths


def prepare_keyframes(run_dir: Path, config: ProjectConfig) -> Path:
    """Create first/last stills for medium/large shots and sync FL2VA assets."""
    run_dir = run_dir.resolve()
    shots_doc = load_json(run_dir / "03_shots/shots.json")
    story = load_json(run_dir / "02_story/story.json")
    manifest = load_json(run_dir / "inputs/manifest.json")
    registry = load_portrait_registry(run_dir)
    visuals = _visual_map(run_dir, shots_doc)
    cons = consistency_config(config)
    identity = identity_config(config)
    fl_types = {
        str(item).lower()
        for item in (cons.get("fl2va_for_variation") or ["medium", "large"])
    }
    candidates_n = max(1, int(cons.get("candidates_per_keyframe") or identity.get("candidates_per_keyframe") or 3))
    keyframe_dir = run_dir / "03_shots/keyframes"
    keyframe_dir.mkdir(parents=True, exist_ok=True)
    project_key_dir = None
    if identity.get("shot_keyframes_dir"):
        root = Path(str(identity["shot_keyframes_dir"]))
        project_key_dir = root if root.is_absolute() else (config.project_root / root)
        project_key_dir.mkdir(parents=True, exist_ok=True)

    anchors = {
        item["image_id"]: Path(item["output_path"]).resolve()
        for item in manifest.get("images", [])
    }
    can_generate = image_generator_enabled(config) and bool(cons.get("generate_keyframes", True))

    results: list[dict[str, Any]] = []
    for shot in shots_doc.get("shots", []):
        shot_id = shot["shot_id"]
        visual = visuals.get(shot_id) or {}
        variation = str(visual.get("variation_type") or shot.get("variation_type") or "small").lower()
        entry: dict[str, Any] = {
            "shot_id": shot_id,
            "variation_type": variation,
            "first_frame": None,
            "last_frame": None,
            "status": "skipped_small",
        }
        if variation not in fl_types:
            results.append(entry)
            continue

        # Reuse manually provided last keyframe if present.
        manual_last = None
        if project_key_dir is not None:
            for suffix in (".last.png", ".last.jpg", ".last.jpeg", ".last.webp"):
                candidate = project_key_dir / f"{shot_id}{suffix}"
                if candidate.is_file():
                    manual_last = candidate
                    break

        first_path = keyframe_dir / f"{shot_id}.first.png"
        last_path = keyframe_dir / f"{shot_id}.last.png"
        identity_refs = _identity_refs_for_shot(shot, registry)
        anchor = anchors.get(shot.get("source_anchor_image")) if shot.get("source_anchor_image") else None

        if not can_generate and manual_last is None and not first_path.is_file():
            entry["status"] = "skipped_no_image_generator"
            shot.pop("source_last_frame", None)
            shot.pop("prepared_first_frame", None)
            results.append(entry)
            continue

        try:
            if first_path.is_file():
                pass
            elif anchor is not None:
                shutil.copy2(anchor, first_path)
            elif can_generate:
                prompt = _compose_prompt(
                    str(visual.get("first_frame_desc") or shot.get("first_frame_desc") or ""),
                    shot,
                    story,
                )
                candidates = _generate_candidates(
                    config,
                    prompt=prompt,
                    references=identity_refs,
                    count=candidates_n,
                    out_dir=keyframe_dir,
                    stem=f"{shot_id}.first",
                )
                best, reason = select_best_image(
                    config,
                    candidates=candidates,
                    target_description=prompt,
                    identity_references=identity_refs[:4],
                )
                shutil.copy2(best, first_path)
                entry["first_reason"] = reason
            else:
                entry["status"] = "skipped_missing_first"
                shot.pop("source_last_frame", None)
                shot.pop("prepared_first_frame", None)
                results.append(entry)
                continue

            if manual_last is not None:
                shutil.copy2(manual_last, last_path)
                entry["last_origin"] = "manual"
            elif last_path.is_file():
                entry["last_origin"] = "existing"
            elif can_generate:
                prompt = _compose_prompt(
                    str(visual.get("last_frame_desc") or shot.get("last_frame_desc") or ""),
                    shot,
                    story,
                )
                last_refs = identity_refs[:]
                if first_path.is_file():
                    last_refs = [first_path, *last_refs]
                candidates = _generate_candidates(
                    config,
                    prompt=prompt,
                    references=last_refs,
                    count=candidates_n,
                    out_dir=keyframe_dir,
                    stem=f"{shot_id}.last",
                )
                best, reason = select_best_image(
                    config,
                    candidates=candidates,
                    target_description=prompt,
                    identity_references=identity_refs[:3] + ([first_path] if first_path.is_file() else []),
                )
                shutil.copy2(best, last_path)
                entry["last_reason"] = reason
                entry["last_origin"] = "generated"
            else:
                entry["status"] = "skipped_missing_last"
                shot.pop("source_last_frame", None)
                shot.pop("prepared_first_frame", None)
                results.append(entry)
                continue

            if project_key_dir is not None and last_path.is_file():
                shutil.copy2(last_path, project_key_dir / f"{shot_id}.last.png")

            shot["source_last_frame"] = str(last_path.resolve())
            shot["prepared_first_frame"] = str(first_path.resolve())
            entry.update(
                {
                    "first_frame": str(first_path.resolve()),
                    "last_frame": str(last_path.resolve()),
                    "status": "ready",
                }
            )
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "failed"
            entry["error"] = str(exc)
            # Do not keep stale FL2VA fields after a failed refresh.
            shot.pop("source_last_frame", None)
            shot.pop("prepared_first_frame", None)
        results.append(entry)

    # Clear FL2VA fields for medium/large shots that did not become ready this pass.
    ready_ids = {item["shot_id"] for item in results if item.get("status") == "ready"}
    for shot in shots_doc.get("shots", []):
        variation = str(
            (visuals.get(shot["shot_id"]) or {}).get("variation_type")
            or shot.get("variation_type")
            or "small"
        ).lower()
        if variation in fl_types and shot["shot_id"] not in ready_ids:
            shot.pop("source_last_frame", None)
            shot.pop("prepared_first_frame", None)

    write_json_atomic(run_dir / "03_shots/shots.json", shots_doc)
    path = run_dir / "03_shots/keyframe_plan.json"
    write_json_atomic(
        path,
        {
            "schema_version": "1.0",
            "generated_at": utc_now(),
            "image_generator_enabled": can_generate,
            "fl2va_for_variation": sorted(fl_types),
            "shots": results,
        },
    )
    return path
