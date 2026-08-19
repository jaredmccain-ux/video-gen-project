"""Human approval gates recorded as explicit workflow decisions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .state import utc_now, write_json_atomic


STAGES = {
    "descriptions": (Path("01_descriptions/image_descriptions.json"), Path("01_descriptions/image_descriptions.approved")),
    "story": (Path("02_story/story.json"), Path("02_story/story.approved")),
    "shots": (Path("03_shots/shots.json"), Path("03_shots/shots.approved")),
}


def stage_paths(run_dir: Path, stage: str) -> tuple[Path, Path]:
    if stage not in STAGES:
        raise ValueError(f"未知审核阶段：{stage}")
    artifact_rel, approval_rel = STAGES[stage]
    return run_dir / artifact_rel, run_dir / approval_rel


def artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def create_approval(run_dir: Path, stage: str, *, confirmed: bool) -> Path:
    if not confirmed:
        raise PermissionError("必须显式确认后才能创建批准标记")
    artifact, approval = stage_paths(run_dir, stage)
    if not artifact.is_file():
        raise FileNotFoundError(f"待批准文件不存在：{artifact}")
    # Reject malformed JSON before binding approval to it.
    json.loads(artifact.read_text(encoding="utf-8"))
    payload = {
        "schema_version": "1.1",
        "stage": stage,
        "artifact": str(artifact.relative_to(run_dir)),
        "artifact_sha256": artifact_sha256(artifact),
        "approved_at": utc_now(),
        "decision": "approved",
    }
    write_json_atomic(approval, payload)
    return approval


def approval_status(run_dir: Path, stage: str) -> str:
    artifact, approval = stage_paths(run_dir, stage)
    if not approval.is_file():
        return "not_approved"
    if not artifact.is_file():
        return "stale_missing_artifact"
    try:
        payload = json.loads(approval.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "invalid_approval"
    expected_artifact = str(artifact.relative_to(run_dir))
    if payload.get("stage") != stage or payload.get("artifact") != expected_artifact:
        return "invalid_approval"
    if payload.get("decision", "approved") != "approved":
        return "invalid_approval"
    expected_hash = payload.get("artifact_sha256")
    if expected_hash:
        try:
            if artifact_sha256(artifact) != expected_hash:
                return "stale_artifact_changed"
        except OSError:
            return "stale_missing_artifact"
    return "approved"
