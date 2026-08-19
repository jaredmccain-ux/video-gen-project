"""Run directory creation and durable state helpers."""

from __future__ import annotations

import json
import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import ProjectConfig


RUN_SUBDIRS = (
    "inputs/source", "inputs/processed", "01_descriptions", "02_story", "03_shots",
    "04_prompts", "05_videos", "06_subtitles/audio", "07_final", "logs",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_run_id(project_name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Run directory names deliberately stay ASCII so projects with Chinese
    # display names remain portable across local machines and archive tools.
    safe_name = "".join(
        c.lower() if c.isascii() and (c.isalnum() or c in "-_") else "-"
        for c in project_name.strip()
    )
    safe_name = "-".join(part for part in safe_name.split("-") if part).strip("-_") or "sceneflow-project"
    return f"{stamp}-{safe_name}"


def initialize_run(config: ProjectConfig, run_id: str | None = None) -> Path:
    selected_id = run_id or default_run_id(str(config.data["project_name"]))
    if not selected_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in selected_id):
        raise ValueError("run_id 只能包含字母、数字、连字符和下划线")
    run_dir = config.run_root / selected_id
    if run_dir.exists():
        raise FileExistsError(f"run 已存在，拒绝覆盖：{run_dir}")
    for subdir in RUN_SUBDIRS:
        (run_dir / subdir).mkdir(parents=True, exist_ok=True)
    snapshot = run_dir / "project.config.yaml"
    snapshot_data = copy.deepcopy(config.data)
    snapshot_data["project_root"] = str(config.project_root)
    snapshot_data["run_root"] = str(config.run_root)
    snapshot_data["input_images"] = [str(path) for path in config.input_images]
    snapshot.write_text(
        yaml.safe_dump(snapshot_data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    state = {
        "schema_version": "1.0",
        "run_id": selected_id,
        "project_name": config.data["project_name"],
        "state": "CREATED",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "config_source": str(config.path),
        "config_snapshot": str(snapshot),
        "approvals": {},
    }
    write_json_atomic(run_dir / "run.json", state)
    return run_dir


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_run(run_dir: Path) -> dict[str, Any]:
    state_path = run_dir / "run.json"
    if not state_path.is_file():
        raise FileNotFoundError(f"run.json 不存在：{state_path}")
    return json.loads(state_path.read_text(encoding="utf-8"))
