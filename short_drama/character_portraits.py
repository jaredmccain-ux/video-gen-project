"""Build a front/side/back character portrait registry for Ref2VA."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .best_image_selector import select_best_image
from .config import ProjectConfig
from .image_generator import (
    consistency_config,
    edit_image,
    generate_images,
    image_generator_enabled,
    save_generated_image,
)
from .reference_assets import declared_character_references, identity_config
from .state import utc_now, write_json_atomic
from .validators import load_json


PORTRAIT_VIEWS = ("front", "side", "back")


def _resolve_project_path(config: ProjectConfig, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (config.project_root / path).resolve()


def _anchor_paths(manifest: dict[str, Any]) -> dict[str, Path]:
    return {
        item["image_id"]: Path(item["output_path"]).resolve()
        for item in manifest.get("images", [])
    }


def _existing_sources(
    character: dict[str, Any],
    *,
    config: ProjectConfig,
    anchors: dict[str, Path],
) -> list[dict[str, Any]]:
    cfg = identity_config(config)
    manual = cfg.get("character_references") or {}
    values = manual.get(character["character_id"], []) if isinstance(manual, dict) else []
    if isinstance(values, str):
        values = [values]
    sources: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        path = _resolve_project_path(config, str(value))
        if path.is_file():
            view = PORTRAIT_VIEWS[min(index, len(PORTRAIT_VIEWS) - 1)]
            sources.append(
                {
                    "path": str(path),
                    "view": view,
                    "origin": "manual",
                    "source_image_id": None,
                }
            )
    for image_id in character.get("reference_image_ids") or []:
        path = anchors.get(image_id)
        if path is not None and path.is_file():
            sources.append(
                {
                    "path": str(path),
                    "view": "front" if not sources else "scene",
                    "origin": "story_anchor",
                    "source_image_id": image_id,
                }
            )
    return sources


def _portrait_prompt(character: dict[str, Any], view: str) -> str:
    subject = str(character.get("reference_subject_description") or character.get("visual_description") or "").strip()
    name = character.get("name") or character["character_id"]
    view_text = {
        "front": "front-facing portrait, looking toward camera",
        "side": "clear side-profile portrait facing screen-right",
        "back": "rear three-quarter view showing hair and clothing silhouette from behind",
    }[view]
    return (
        f"Cinematic still of character {name} only, {view_text}. "
        f"Identity lock: {subject}. "
        "Same person, same age, same clothing, same hairstyle, same body proportions. "
        "No text, no watermark, no collage, single subject, full or three-quarter body visible, "
        "neutral background, film still quality."
    )


def _generate_missing_views(
    character: dict[str, Any],
    *,
    config: ProjectConfig,
    existing: list[dict[str, Any]],
    out_dir: Path,
) -> list[dict[str, Any]]:
    have = {item["view"] for item in existing if item["view"] in PORTRAIT_VIEWS}
    needed = [view for view in PORTRAIT_VIEWS if view not in have]
    if not needed or not image_generator_enabled(config):
        return existing
    cons = consistency_config(config)
    if not cons.get("generate_character_portraits", True):
        return existing
    reference = Path(existing[0]["path"]) if existing else None
    results = list(existing)
    candidates_n = max(1, int(cons.get("candidates_per_keyframe") or 3))
    for view in needed:
        prompt = _portrait_prompt(character, view)
        target = out_dir / f"{character['character_id']}.{view}.png"
        try:
            candidate_paths: list[Path] = []
            used_edit = False
            for index in range(candidates_n):
                variant = prompt if index == 0 else f"{prompt} Slight framing variation {index + 1}."
                cand = out_dir / f"{character['character_id']}.{view}.cand{index}.png"
                if reference is not None and reference.is_file():
                    try:
                        data = edit_image(config, prompt=variant, reference_image=reference)
                        used_edit = True
                    except Exception:
                        data = generate_images(config, prompt=variant, count=1)[0]
                else:
                    data = generate_images(config, prompt=variant, count=1)[0]
                save_generated_image(data, cand)
                candidate_paths.append(cand)
            best, reason = select_best_image(
                config,
                candidates=candidate_paths,
                target_description=prompt,
                identity_references=[reference] if reference and reference.is_file() else [],
            )
            save_generated_image(best.read_bytes(), target)
            results.append(
                {
                    "path": str(target.resolve()),
                    "view": view,
                    "origin": "generated_edit" if used_edit else "generated_text",
                    "selection_reason": reason,
                    "source_image_id": None,
                }
            )
            reference = target
        except Exception as exc:  # noqa: BLE001 — keep registry usable if one view fails
            # Do not invent a fake view by reusing another path; that breaks coverage checks.
            results.append(
                {
                    "path": "",
                    "view": view,
                    "origin": "failed",
                    "error": str(exc),
                    "source_image_id": None,
                }
            )
    return [
        item
        for item in results
        if item.get("path") and Path(str(item["path"])).is_file() and item.get("origin") != "failed"
    ]


def prepare_character_portraits(run_dir: Path, config: ProjectConfig) -> Path:
    """Materialize a per-character portrait registry under 03_shots/portraits/."""
    run_dir = run_dir.resolve()
    story = load_json(run_dir / "02_story/story.json")
    manifest = load_json(run_dir / "inputs/manifest.json")
    anchors = _anchor_paths(manifest)
    portraits_dir = run_dir / "03_shots/portraits"
    portraits_dir.mkdir(parents=True, exist_ok=True)

    characters_out: list[dict[str, Any]] = []
    for character in story.get("characters", []):
        if not declared_character_references(character, config):
            continue
        sources = _existing_sources(character, config=config, anchors=anchors)
        # Copy durable copies into the run so later stages do not depend on mutable assets.
        copied: list[dict[str, Any]] = []
        for index, item in enumerate(sources):
            src = Path(item["path"])
            if not src.is_file():
                continue
            dest = portraits_dir / f"{character['character_id']}.src{index}{src.suffix.lower() or '.png'}"
            if not dest.exists() or dest.stat().st_size != src.stat().st_size:
                shutil.copy2(src, dest)
            copied.append({**item, "path": str(dest.resolve())})
        enriched = _generate_missing_views(
            character, config=config, existing=copied, out_dir=portraits_dir
        )
        characters_out.append(
            {
                "character_id": character["character_id"],
                "name": character.get("name"),
                "reference_subject_description": character.get("reference_subject_description"),
                "portraits": enriched,
            }
        )

    artifact = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "characters": characters_out,
        "note": "Manual refs and story anchors are preferred; generated views fill missing front/side/back when image_generator is enabled.",
    }
    path = run_dir / "03_shots/character_portraits.json"
    write_json_atomic(path, artifact)
    return path


def load_portrait_registry(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "03_shots/character_portraits.json"
    if not path.is_file():
        return None
    return load_json(path)


def portrait_bindings_for_characters(
    registry: dict[str, Any] | None,
    character_ids: list[str],
) -> list[tuple[Path, str, str]]:
    """Return (path, character_id, view) for requested characters."""
    if not registry:
        return []
    by_id = {item["character_id"]: item for item in registry.get("characters", [])}
    bindings: list[tuple[Path, str, str]] = []
    seen: set[Path] = set()
    for character_id in character_ids:
        item = by_id.get(character_id)
        if not item:
            continue
        for portrait in item.get("portraits", []):
            path = Path(portrait["path"])
            if path.is_file() and path not in seen:
                seen.add(path)
                bindings.append((path, character_id, str(portrait.get("view") or "portrait")))
    return bindings


def portrait_paths_for_characters(
    registry: dict[str, Any] | None,
    character_ids: list[str],
) -> list[Path]:
    return [path for path, _, _ in portrait_bindings_for_characters(registry, character_ids)]
