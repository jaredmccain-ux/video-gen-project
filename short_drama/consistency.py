"""Orchestrate consistency prep before H3 prompt rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .character_portraits import prepare_character_portraits
from .config import ProjectConfig
from .image_generator import consistency_config
from .keyframe_prepare import prepare_keyframes
from .reference_assets import identity_config
from .reference_selector import prepare_reference_plan
from .shot_decompose import decompose_shots
from .shots import apply_generation_routing
from .state import utc_now, write_json_atomic
from .validators import load_json


def _invalidate_shots_approval(run_dir: Path) -> bool:
    """Drop stale shots approval after consistency rewrites routing fields."""
    approval = run_dir / "03_shots/shots.approved"
    if not approval.is_file():
        return False
    approval.unlink()
    return True


def prepare_consistency(run_dir: Path, config: ProjectConfig) -> Path:
    """Run portrait registry → shot decompose → ref select → keyframes → routing.

    Idempotent enough for debug re-entry: existing keyframe files are reused.
    Does not require shots approval so the full pipeline can approve after enrichment.
    Rewriting shots invalidates any existing shots.approved marker.
    """
    run_dir = run_dir.resolve()
    shots_path = run_dir / "03_shots/shots.json"
    if not shots_path.is_file():
        raise FileNotFoundError(f"缺少镜头规划：{shots_path}")

    cons = consistency_config(config)
    identity = identity_config(config)
    enabled = bool(cons.get("enabled", identity.get("enabled", False)))
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "enabled": enabled,
        "steps": {},
        "warnings": [],
        "blocking_errors": [],
    }
    if not enabled:
        report["note"] = "consistency_pipeline/identity_consistency 未启用，跳过"
        path = run_dir / "03_shots/consistency_report.json"
        write_json_atomic(path, report)
        return path

    story = load_json(run_dir / "02_story/story.json")

    report["steps"]["character_portraits"] = str(prepare_character_portraits(run_dir, config))
    if cons.get("decompose_shots", True):
        report["steps"]["shot_visuals"] = str(decompose_shots(run_dir, config))
    if cons.get("select_references", True):
        report["steps"]["reference_plan"] = str(prepare_reference_plan(run_dir, config))
    # Always attempt: syncs manual keyframes even when the image generator is off.
    report["steps"]["keyframe_plan"] = str(prepare_keyframes(run_dir, config))

    shots_doc = load_json(shots_path)
    apply_generation_routing(shots_doc, story, config)
    write_json_atomic(shots_path, shots_doc)
    report["steps"]["routing"] = "apply_generation_routing"
    report["mode_counts"] = _mode_counts(shots_doc)
    report["warnings"].extend(_keyframe_gap_warnings(run_dir, shots_doc, cons))
    report["blocking_errors"].extend(_ref2va_coverage_errors(run_dir, shots_doc))
    if _invalidate_shots_approval(run_dir):
        report["shots_approval_invalidated"] = True

    path = run_dir / "03_shots/consistency_report.json"
    write_json_atomic(path, report)
    return path


def _keyframe_gap_warnings(
    run_dir: Path, shots_doc: dict[str, Any], cons: dict[str, Any]
) -> list[str]:
    """Warn when medium/large shots expected FL2VA keyframes but lack them."""
    fl_types = {
        str(item).lower()
        for item in (cons.get("fl2va_for_variation") or ["medium", "large"])
    }
    keyframe_plan_path = run_dir / "03_shots/keyframe_plan.json"
    statuses: dict[str, str] = {}
    if keyframe_plan_path.is_file():
        plan = load_json(keyframe_plan_path)
        statuses = {
            item["shot_id"]: str(item.get("status") or "")
            for item in plan.get("shots", [])
            if item.get("shot_id")
        }
    warnings: list[str] = []
    for shot in shots_doc.get("shots", []):
        variation = str(shot.get("variation_type") or "").lower()
        if variation not in fl_types:
            continue
        if shot.get("generation_mode") == "first_last_frame" and shot.get("source_last_frame"):
            continue
        status = statuses.get(shot["shot_id"], "missing")
        warnings.append(
            f'{shot["shot_id"]}: variation={variation} 期望 FL2VA 关键帧，'
            f"当前 status={status}，已降级为 {shot.get('generation_mode')}"
        )
    return warnings


def _ref2va_coverage_errors(run_dir: Path, shots_doc: dict[str, Any]) -> list[str]:
    """Block approve when Ref2VA shots lack per-character reference coverage."""
    plan_path = run_dir / "03_shots/reference_plan.json"
    missing_by_shot: dict[str, list[str]] = {}
    if plan_path.is_file():
        plan = load_json(plan_path)
        for item in plan.get("shots", []):
            missing = item.get("missing_required_character_ids") or []
            if missing:
                missing_by_shot[item["shot_id"]] = [str(value) for value in missing]
    errors: list[str] = []
    for shot in shots_doc.get("shots", []):
        if shot.get("generation_mode") != "ref2va":
            continue
        shot_id = shot["shot_id"]
        selected = shot.get("selected_references") or []
        required = list(shot.get("reference_character_ids") or [])
        covered = {
            str(character_id)
            for item in selected
            for character_id in (item.get("character_ids") or [])
        }
        missing = [character_id for character_id in required if character_id not in covered]
        if not missing:
            missing = missing_by_shot.get(shot_id, [])
        if missing:
            errors.append(
                f"{shot_id}: Ref2VA 缺少角色参考覆盖 {', '.join(missing)}；"
                "请补充 character_references / 肖像，或重新 prepare-consistency"
            )
        if not selected and not shot.get("selected_reference_paths"):
            errors.append(f"{shot_id}: Ref2VA 缺少 selected_references")
    return errors


def _mode_counts(shots_doc: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for shot in shots_doc.get("shots", []):
        mode = str(shot.get("generation_mode") or "unknown")
        counts[mode] = counts.get(mode, 0) + 1
    return counts
