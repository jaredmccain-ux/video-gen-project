"""Resolve stable character and keyframe references for H3 generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ProjectConfig


@dataclass(frozen=True)
class ReferenceBinding:
    """One image in the exact order presented to H3 Ref2VA."""

    path: Path
    character_ids: tuple[str, ...]
    roles: tuple[str, ...]


def identity_config(config: ProjectConfig) -> dict[str, Any]:
    value = config.data.get("identity_consistency")
    return value if isinstance(value, dict) else {}


def _resolve_project_path(config: ProjectConfig, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (config.project_root / path).resolve()


def declared_character_references(
    character: dict[str, Any], config: ProjectConfig
) -> bool:
    """Return whether a character has manual or story-derived references."""
    cfg = identity_config(config)
    manual = cfg.get("character_references") or {}
    values = manual.get(character.get("character_id")) if isinstance(manual, dict) else None
    return bool(values) or bool(character.get("reference_image_ids"))


def shot_last_keyframe(config: ProjectConfig, shot_id: str) -> Path | None:
    """Resolve an optional manually prepared final keyframe for a shot."""
    cfg = identity_config(config)
    root_value = cfg.get("shot_keyframes_dir")
    if not root_value:
        return None
    root = _resolve_project_path(config, str(root_value))
    for suffix in (".last.png", ".last.jpg", ".last.jpeg", ".last.webp"):
        candidate = root / f"{shot_id}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _selected_reference_entries(shot: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize structured selected refs or legacy path-only lists."""
    structured = shot.get("selected_references")
    if isinstance(structured, list) and structured:
        entries: list[dict[str, Any]] = []
        for item in structured:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            character_ids = item.get("character_ids") or []
            if isinstance(character_ids, str):
                character_ids = [character_ids]
            roles = item.get("roles") or item.get("role") or ("selected",)
            if isinstance(roles, str):
                roles = [roles]
            entries.append(
                {
                    "path": str(item["path"]),
                    "character_ids": [str(value) for value in character_ids],
                    "roles": [str(value) for value in roles],
                }
            )
        if entries:
            return entries

    paths = shot.get("selected_reference_paths") or []
    return [
        {
            "path": str(value),
            "character_ids": [],
            "roles": ["selected"],
        }
        for value in paths
    ]


def resolve_shot_references(
    *,
    run_dir: Path,
    config: ProjectConfig,
    story: dict[str, Any],
    manifest: dict[str, Any],
    shot: dict[str, Any],
) -> list[ReferenceBinding]:
    """Resolve and de-duplicate Ref2VA images in deterministic presentation order.

    Prefer an explicit selected-reference plan with per-image character bindings;
    otherwise fall back to manual character refs, story anchors, scene anchor,
    and the previous shot's last frame.
    """
    cfg = identity_config(config)
    max_images = max(1, min(9, int(cfg.get("max_reference_images") or 9)))
    selected = _selected_reference_entries(shot)
    if selected:
        bindings: list[ReferenceBinding] = []
        seen: set[Path] = set()
        for item in selected:
            path = Path(str(item["path"])).expanduser()
            path = path.resolve() if path.is_absolute() else (config.project_root / path).resolve()
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            character_ids = tuple(item.get("character_ids") or [])
            # Legacy path-only selections fall back to the shot-level ids.
            if not character_ids:
                character_ids = tuple(shot.get("reference_character_ids") or [])
            bindings.append(
                ReferenceBinding(
                    path=path,
                    character_ids=character_ids,
                    roles=tuple(item.get("roles") or ("selected",)),
                )
            )
        if bindings:
            if cfg.get("include_previous_last_frame_reference", True) and shot.get("depends_on"):
                prev = (run_dir / "05_videos" / f'{shot["depends_on"]}.last_frame.png').resolve()
                if prev not in seen:
                    bindings.append(
                        ReferenceBinding(
                            path=prev,
                            character_ids=(),
                            roles=("previous_last_frame",),
                        )
                    )
            return bindings[:max_images]

    # Prefer reference_plan.json when shots only retained path lists.
    plan_path = run_dir / "03_shots/reference_plan.json"
    if plan_path.is_file():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            for entry in plan.get("shots", []):
                if entry.get("shot_id") != shot.get("shot_id"):
                    continue
                restored = []
                for item in entry.get("selected") or []:
                    if not isinstance(item, dict) or not item.get("path"):
                        continue
                    restored.append(
                        {
                            "path": item["path"],
                            "character_ids": list(item.get("character_ids") or []),
                            "roles": [item.get("role") or "selected"],
                        }
                    )
                if restored:
                    shot = {**shot, "selected_references": restored}
                    return resolve_shot_references(
                        run_dir=run_dir,
                        config=config,
                        story=story,
                        manifest=manifest,
                        shot=shot,
                    )
        except (OSError, ValueError, TypeError):
            pass

    manual = cfg.get("character_references") or {}
    characters = {
        item["character_id"]: item for item in story.get("characters", [])
    }
    anchor_paths = {
        item["image_id"]: Path(item["output_path"]).resolve()
        for item in manifest.get("images", [])
    }

    ordered: list[tuple[Path, str | None, str]] = []
    character_extras: list[tuple[Path, str | None, str]] = []
    requested_character_ids = (
        shot.get("reference_character_ids") or shot.get("characters") or []
    )
    for character_id in requested_character_ids:
        character = characters.get(character_id)
        if character is None:
            continue
        values = manual.get(character_id, []) if isinstance(manual, dict) else []
        if isinstance(values, str):
            values = [values]
        sources: list[tuple[Path, str | None, str]] = [
            (_resolve_project_path(config, str(value)), character_id, "character")
            for value in values or []
        ]
        for image_id in character.get("reference_image_ids") or []:
            if image_id in anchor_paths:
                sources.append(
                    (anchor_paths[image_id], character_id, "character_anchor")
                )
        if sources:
            ordered.append(sources[0])
            character_extras.extend(sources[1:])
    ordered.extend(character_extras)

    anchor_id = shot.get("source_anchor_image")
    if anchor_id in anchor_paths:
        ordered.append((anchor_paths[anchor_id], None, "scene_anchor"))

    if cfg.get("include_previous_last_frame_reference", True) and shot.get("depends_on"):
        ordered.append(
            (
                run_dir / "05_videos" / f'{shot["depends_on"]}.last_frame.png',
                None,
                "previous_last_frame",
            )
        )

    merged: dict[Path, dict[str, set[str]]] = {}
    for path, character_id, role in ordered:
        entry = merged.setdefault(path, {"characters": set(), "roles": set()})
        if character_id:
            entry["characters"].add(character_id)
        entry["roles"].add(role)

    bindings = [
        ReferenceBinding(
            path=path,
            character_ids=tuple(sorted(values["characters"])),
            roles=tuple(sorted(values["roles"])),
        )
        for path, values in merged.items()
    ]
    return bindings[:max_images]
