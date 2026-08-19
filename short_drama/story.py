"""Create a coherent two-minute story from approved image descriptions."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .approval import approval_status, stage_paths
from .azure_client import completion_text, create_text_completion
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
    return usage.model_dump() if usage is not None and hasattr(usage, "model_dump") else None


def plan_story(run_dir: Path, config: ProjectConfig) -> Path:
    if approval_status(run_dir, "descriptions") != "approved":
        raise ValueError("图片描述尚未批准，或批准标记已失效")
    descriptions_path, _ = stage_paths(run_dir, "descriptions")
    descriptions = json.loads(descriptions_path.read_text(encoding="utf-8"))
    run_state = read_run(run_dir)
    baseline_story: dict[str, Any] | None = None
    parent_run_value = run_state.get("parent_run")
    if parent_run_value:
        baseline_path = Path(parent_run_value) / "02_story/story.json"
        if not baseline_path.is_file():
            raise FileNotFoundError(f"父版本故事不存在：{baseline_path}")
        baseline_story = json.loads(baseline_path.read_text(encoding="utf-8"))

    output_dir = run_dir / "02_story"
    artifact = output_dir / "story.json"
    raw_path = output_dir / "raw_response.txt"
    metadata_path = output_dir / "request_metadata.json"
    error_path = output_dir / "error.json"
    if artifact.exists() or raw_path.exists():
        raise FileExistsError(f"故事产物已存在，拒绝覆盖：{output_dir}")

    root = _package_root()
    schema = json.loads((root / "schemas/story.schema.json").read_text(encoding="utf-8"))
    system_prompt = (root / "prompts/plan_story.system.txt").read_text(encoding="utf-8").strip()
    revision_instruction = ""
    if baseline_story is not None:
        revision_instruction = (
            "这是第二轮定向修订，不是重新选题。必须保留第一轮的《落日前的蓝布包》题材、人物关系、"
            "IMG02→IMG03→IMG01 叙事顺序，以及‘蓝布包—录音笔/线索—草帽存储卡—海边反转’因果链。"
            "录音笔仍可作为静音道具，但绝对不能播放沈岚或任何人的声音；把原 B4 的信息改为设备屏幕上"
            "可见且简短的文字线索，或留到沈岚本人在海边可见出镜后当面说出。不要增加电话、广播、电视、"
            "扩音器或其他离屏人声替代方案。\n"
            "第一轮故事修订底稿：\n"
            + json.dumps(baseline_story, ensure_ascii=False, indent=2)
            + "\n"
        )
    user_text = (
        "根据下面已人工批准的三图事实描述，规划一部目标时长 120 秒（容差 117–123 秒）的横屏短剧。\n"
        "你可以创造人物姓名、身份、关系和图外场景，并自主决定 IMG01/IMG02/IMG03 的叙事顺序。"
        "但三张锚点图都必须推动因果链，image_order 必须恰好包含三个 image_id。\n"
        "采用适合当前通用短剧的强钩子、冲突升级、信息反转和结尾回收；不要依赖特定平台梗。"
        "声音硬约束：成片只允许当前画面中真实可见人物说简体中文对白；禁止旁白、内心独白、画外解说、"
        "离屏人物对白，以及录音、电话、广播、电视、扬声器等设备播放的任何可听人声。"
        "允许环境声、非语言人物声和短动作音效，不设计非叙事 BGM。"
        "本阶段的 full_story、events 等字段可以用简体中文说明画面动作，这些说明不是成片旁白。\n"
        "beats 的 duration_s 总和必须在 117–123 秒；每张图至少作为一个 beat 的 anchor_image_id。"
        "人物外观和地点特征应足够稳定，方便后续拆成 4–8 秒镜头。"
        "尽量让主要人物直接对应锚点图中可辨识的人物，不得把明显可见的性别、年龄、服装或体型改成另一种。"
        "每个 character 都必须填写 reference_image_ids 和 reference_subject_description："
        "前者列出该人物真实出现的 IMGxx；没有可靠参考时填空数组；后者用位置、衣着、发型和体型明确指出图中主体。"
        "主要人物应尽可能至少绑定一张锚点图，后续会把这些图作为 Ref2VA 固定身份参考。\n"
        "style_bible 必须明确写出：仅画面内可见人物可说话、无离屏人声、无旁白、无 BGM。"
        "beats 中每条涉及对白的事件都必须同时明确说话人物处于画面内；文字线索必须写明仅供观看、无语音播放。\n"
        "必须只返回一个 JSON 对象，不要 Markdown 代码块或额外解释。\n"
        + revision_instruction
        + "已批准图片描述：\n" + json.dumps(descriptions, ensure_ascii=False, indent=2)
        + "\nJSON Schema：\n" + json.dumps(schema, ensure_ascii=False)
    )
    started = time.monotonic()
    started_at = utc_now()
    try:
        response = create_text_completion(config, system_prompt=system_prompt, user_text=user_text)
        content = completion_text(response)
        raw_path.write_text(content + "\n", encoding="utf-8")
        document = _json_from_text(content)
        errors = validate_document("story", document)
        if errors:
            raise ValueError("故事 JSON 校验失败：" + "; ".join(errors))
        write_json_atomic(artifact, document)
        write_json_atomic(metadata_path, {
            "schema_version": "1.0", "started_at": started_at, "completed_at": utc_now(),
            "elapsed_s": round(time.monotonic() - started, 3),
            "deployment": (config.data.get("llm") or config.data.get("azure") or {}).get("model")
            or (config.data.get("azure") or {}).get("deployment"),
            "usage": _usage_dict(response),
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
    state["state"] = "STORY_GENERATED"
    state["updated_at"] = utc_now()
    state["story"] = str(artifact)
    write_json_atomic(run_dir / "run.json", state)
    return artifact
