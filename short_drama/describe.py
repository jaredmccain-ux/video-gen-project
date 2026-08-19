"""Generate and persist fact-first descriptions for the prepared input frames."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .azure_client import completion_text, create_multimodal_completion
from .config import ProjectConfig
from .state import read_run, utc_now, write_json_atomic
from .validators import validate_document


def _package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _json_from_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1 :] if first_newline >= 0 else cleaned
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    value = json.loads(cleaned.strip())
    if not isinstance(value, dict):
        raise ValueError("模型响应 JSON 顶层必须是对象")
    return value


def _usage_dict(response: Any) -> dict[str, Any] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    return {
        key: getattr(usage, key)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if getattr(usage, key, None) is not None
    }


def describe_run_images(run_dir: Path, config: ProjectConfig) -> Path:
    output_dir = run_dir / "01_descriptions"
    artifact = output_dir / "image_descriptions.json"
    raw_path = output_dir / "raw_response.txt"
    metadata_path = output_dir / "request_metadata.json"
    error_path = output_dir / "error.json"
    if artifact.exists() or raw_path.exists():
        raise FileExistsError(f"描述产物已存在，拒绝覆盖：{output_dir}")

    manifest_path = run_dir / "inputs/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("images", [])
    if len(records) != 3:
        raise ValueError("输入清单必须恰好包含三张图片")
    image_paths = [Path(item["output_path"]).resolve() for item in records]
    missing = [str(path) for path in image_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("处理后图片不存在：" + ", ".join(missing))

    schema = json.loads((_package_root() / "schemas/image_descriptions.schema.json").read_text(encoding="utf-8"))
    system_prompt = (_package_root() / "prompts/describe_images.system.txt").read_text(encoding="utf-8").strip()
    image_map = [
        {"image_id": item["image_id"], "source_path": item["source_path"], "submitted_frame": str(path)}
        for item, path in zip(records, image_paths)
    ]
    user_text = (
        "请按附件顺序描述三张图片。图片映射如下：\n"
        + json.dumps(image_map, ensure_ascii=False, indent=2)
        + "\n必须只返回一个 JSON 对象，不要 Markdown 代码块或解释。source_path 必须原样复制映射中的 source_path。"
        + "\nJSON Schema：\n"
        + json.dumps(schema, ensure_ascii=False)
    )
    started = time.monotonic()
    started_at = utc_now()
    try:
        response = create_multimodal_completion(
            config, system_prompt=system_prompt, user_text=user_text, image_paths=image_paths
        )
        content = completion_text(response)
        raw_path.write_text(content + "\n", encoding="utf-8")
        document = _json_from_text(content)
        errors = validate_document("descriptions", document)
        if errors:
            raise ValueError("描述 JSON 校验失败：" + "; ".join(errors))
        write_json_atomic(artifact, document)
        elapsed = round(time.monotonic() - started, 3)
        write_json_atomic(metadata_path, {
            "schema_version": "1.0", "started_at": started_at, "completed_at": utc_now(),
            "elapsed_s": elapsed,
            "deployment": (config.data.get("llm") or config.data.get("azure") or {}).get("model")
            or (config.data.get("azure") or {}).get("deployment"),
            "submitted_images": image_map, "usage": _usage_dict(response),
            "response_id": getattr(response, "id", None),
        })
    except Exception as exc:
        write_json_atomic(error_path, {
            "schema_version": "1.0", "failed_at": utc_now(),
            "elapsed_s": round(time.monotonic() - started, 3),
            "error_type": type(exc).__name__, "message": str(exc),
        })
        raise

    state = read_run(run_dir)
    state["state"] = "DESCRIPTIONS_GENERATED"
    state["updated_at"] = utc_now()
    state["descriptions"] = str(artifact)
    write_json_atomic(run_dir / "run.json", state)
    return artifact
