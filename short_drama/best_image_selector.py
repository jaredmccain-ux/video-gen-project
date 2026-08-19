"""Pick the best still among generated keyframe/portrait candidates."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .azure_client import create_multimodal_completion
from .config import ProjectConfig


SYSTEM_PROMPT = """You rank still-image candidates for short-drama keyframes.
Choose the single best image that:
1) matches the target description,
2) preserves character identity from any identity reference images,
3) is usable as a video keyframe (clear subject, no text/watermark, no collage).
Return ONLY JSON: {"best_index": 0, "reason": "..."} with a 0-based index into candidates.
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


def select_best_image(
    config: ProjectConfig,
    *,
    candidates: list[Path],
    target_description: str,
    identity_references: list[Path] | None = None,
) -> tuple[Path, str]:
    if not candidates:
        raise ValueError("没有候选图片可供择优")
    if len(candidates) == 1:
        return candidates[0], "single candidate"

    identity_references = [path for path in (identity_references or []) if path.is_file()]
    labels = [f"candidate[{i}]={path.name}" for i, path in enumerate(candidates)]
    if identity_references:
        labels.extend(
            f"identity_ref[{i}]={path.name}" for i, path in enumerate(identity_references)
        )
    user_text = (
        f"Target description:\n{target_description}\n\n"
        f"Images follow in order:\n" + "\n".join(labels) + "\n"
        "First choose among candidate[*] only; identity_ref[*] are identity locks, not candidates."
    )
    image_paths = list(candidates) + identity_references
    try:
        response = create_multimodal_completion(
            config,
            system_prompt=SYSTEM_PROMPT,
            user_text=user_text,
            image_paths=image_paths,
            max_completion_tokens=1024,
        )
        parsed = _extract_json(response.choices[0].message.content or "")
        index = int(parsed.get("best_index", 0))
        if not 0 <= index < len(candidates):
            index = 0
        return candidates[index], str(parsed.get("reason") or "llm selection")
    except Exception as exc:  # noqa: BLE001
        return candidates[0], f"fallback first candidate: {exc}"
