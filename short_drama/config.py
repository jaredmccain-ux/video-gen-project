"""Project configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when project configuration is invalid."""


REQUIRED_TOP_LEVEL = {
    "project_name", "project_root", "run_root", "input_images",
    "target_duration_s", "duration_tolerance_s", "aspect_ratio", "short_edge",
    "dialogue_language", "narration", "non_diegetic_music",
    "default_shot_duration_s", "min_shot_duration_s", "max_shot_duration_s",
    "sglang",
}

# Either legacy azure.* or openai-compatible llm.* (Rivo, etc.)
REQUIRED_LLM_SECTIONS = ("llm", "azure")


@dataclass(frozen=True)
class ProjectConfig:
    path: Path
    data: dict[str, Any]
    project_root: Path
    run_root: Path
    input_images: tuple[Path, ...]


def load_config(path: str | Path, *, require_images: bool = True) -> ProjectConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在：{config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("配置顶层必须是映射")
    missing = sorted(REQUIRED_TOP_LEVEL - raw.keys())
    if missing:
        raise ConfigError(f"配置缺少字段：{', '.join(missing)}")
    if not any(isinstance(raw.get(key), dict) for key in REQUIRED_LLM_SECTIONS):
        raise ConfigError("配置必须包含 llm 或 azure 段")

    project_root = (config_path.parent / str(raw["project_root"])).resolve()
    run_root_value = Path(str(raw["run_root"]))
    run_root = run_root_value if run_root_value.is_absolute() else project_root / run_root_value
    images = tuple(
        p if p.is_absolute() else project_root / p
        for p in (Path(str(value)) for value in raw["input_images"])
    )
    _validate_values(raw, images, require_images=require_images)
    return ProjectConfig(config_path, raw, project_root, run_root.resolve(), images)


def _validate_values(data: dict[str, Any], images: tuple[Path, ...], *, require_images: bool) -> None:
    if len(images) != 3 or len(set(images)) != 3:
        raise ConfigError("input_images 必须恰好包含 3 个不同路径")
    if require_images:
        missing = [str(path) for path in images if not path.is_file()]
        if missing:
            raise ConfigError("输入图片不存在：" + ", ".join(missing))
    if data["aspect_ratio"] not in ("auto", "16:9", "9:16"):
        raise ConfigError("aspect_ratio 必须为 auto、16:9 或 9:16")
    if data["dialogue_language"] != "zh-CN" or data["narration"] is not False:
        raise ConfigError("首期只允许简体中文对白且 narration=false")
    if data["non_diegetic_music"] is not False:
        raise ConfigError("首期 non_diegetic_music 必须为 false")
    low, default, high = (
        data["min_shot_duration_s"], data["default_shot_duration_s"], data["max_shot_duration_s"]
    )
    if not (4 <= low <= default <= high <= 8):
        raise ConfigError("镜头时长必须满足 4 <= min <= default <= max <= 8")
    llm = data.get("llm") if isinstance(data.get("llm"), dict) else data.get("azure")
    if not isinstance(llm, dict):
        raise ConfigError("llm / azure 必须是映射")
    if data.get("llm"):
        for key in ("endpoint", "model", "api_key_env"):
            if not llm.get(key) and not (key == "endpoint" and llm.get("base_url")):
                raise ConfigError(f"llm.{key} 不能为空")
    else:
        for key in ("endpoint", "api_version", "deployment", "api_key_env"):
            if not llm.get(key):
                raise ConfigError(f"azure.{key} 不能为空")
    identity = data.get("identity_consistency")
    if identity is not None:
        if not isinstance(identity, dict):
            raise ConfigError("identity_consistency 必须是映射")
        for key in (
            "enabled",
            "require_character_references",
            "include_previous_last_frame_reference",
        ):
            if key in identity and not isinstance(identity[key], bool):
                raise ConfigError(f"identity_consistency.{key} 必须是布尔值")
        interval = identity.get("reanchor_interval_shots", 2)
        if not isinstance(interval, int) or interval < 1:
            raise ConfigError("identity_consistency.reanchor_interval_shots 必须是正整数")
        max_refs = identity.get("max_reference_images", 9)
        if not isinstance(max_refs, int) or not 1 <= max_refs <= 9:
            raise ConfigError("identity_consistency.max_reference_images 必须为 1–9")
        if identity.get("ref_image_size", "match") not in {"match", "max"}:
            raise ConfigError("identity_consistency.ref_image_size 必须为 match 或 max")
        references = identity.get("character_references", {})
        if not isinstance(references, dict):
            raise ConfigError("identity_consistency.character_references 必须是映射")
        for character_id, paths in references.items():
            if not isinstance(character_id, str) or not isinstance(paths, (str, list)):
                raise ConfigError("character_references 必须使用角色 ID 映射到路径或路径列表")
    image_gen = data.get("image_generator")
    if image_gen is not None:
        if not isinstance(image_gen, dict):
            raise ConfigError("image_generator 必须是映射")
        if "enabled" in image_gen and not isinstance(image_gen["enabled"], bool):
            raise ConfigError("image_generator.enabled 必须是布尔值")
        if image_gen.get("enabled"):
            if not image_gen.get("model"):
                raise ConfigError("启用 image_generator 时必须设置 model")
    consistency = data.get("consistency_pipeline")
    if consistency is not None:
        if not isinstance(consistency, dict):
            raise ConfigError("consistency_pipeline 必须是映射")
        for key in (
            "enabled",
            "decompose_shots",
            "select_references",
            "generate_character_portraits",
            "generate_keyframes",
        ):
            if key in consistency and not isinstance(consistency[key], bool):
                raise ConfigError(f"consistency_pipeline.{key} 必须是布尔值")
        max_refs = consistency.get("max_refs_per_shot")
        if max_refs is not None and (not isinstance(max_refs, int) or not 1 <= max_refs <= 9):
            raise ConfigError("consistency_pipeline.max_refs_per_shot 必须为 1–9")
        candidates = consistency.get("candidates_per_keyframe")
        if candidates is not None and (not isinstance(candidates, int) or candidates < 1):
            raise ConfigError("consistency_pipeline.candidates_per_keyframe 必须是正整数")
        fl_types = consistency.get("fl2va_for_variation")
        if fl_types is not None:
            if not isinstance(fl_types, list) or not fl_types:
                raise ConfigError("consistency_pipeline.fl2va_for_variation 必须是非空列表")
            unknown = [item for item in fl_types if str(item).lower() not in {"small", "medium", "large"}]
            if unknown:
                raise ConfigError(
                    "consistency_pipeline.fl2va_for_variation 只能包含 small/medium/large："
                    + ", ".join(map(str, unknown))
                )
