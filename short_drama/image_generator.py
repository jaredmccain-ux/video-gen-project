"""OpenAI-compatible image generation helpers for portraits and keyframes.

Supports Volcengine Ark Seedream (`doubao-seedream-*`) and other OpenAI-style
Images APIs on the same base URL used by the planning LLM.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from openai import OpenAI

from .azure_client import (
    _direct_http_client,
    _llm_config,
    _normalize_openai_base,
    create_client,
    llm_base_candidates,
)
from .config import ProjectConfig


def image_generator_config(config: ProjectConfig) -> dict[str, Any]:
    value = config.data.get("image_generator")
    return value if isinstance(value, dict) else {}


def image_generator_enabled(config: ProjectConfig) -> bool:
    cfg = image_generator_config(config)
    return bool(cfg.get("enabled", False))


def consistency_config(config: ProjectConfig) -> dict[str, Any]:
    value = config.data.get("consistency_pipeline")
    return value if isinstance(value, dict) else {}


def _image_client(config: ProjectConfig, *, base_url_override: str | None = None) -> OpenAI:
    cfg = image_generator_config(config)
    llm = _llm_config(config)
    env_name = str(cfg.get("api_key_env") or llm.get("api_key_env") or "ARK_API_KEY")
    api_key = os.environ.get(env_name)
    if not api_key:
        raise ValueError(f"环境变量 {env_name} 未设置；图片生成需要 API Key")
    endpoint = str(
        base_url_override
        or cfg.get("endpoint")
        or cfg.get("base_url")
        or llm.get("endpoint")
        or llm.get("base_url")
        or ""
    ).rstrip("/")
    if not endpoint:
        raise ValueError("image_generator.endpoint 或 llm.endpoint 不能为空")
    base_url = _normalize_openai_base(endpoint)
    return OpenAI(api_key=api_key, base_url=base_url, http_client=_direct_http_client(timeout_s=300.0))


def _image_client_with_fallback(config: ProjectConfig) -> OpenAI:
    """Prefer configured image endpoint, then shared LLM candidates."""
    cfg = image_generator_config(config)
    primary = str(cfg.get("endpoint") or cfg.get("base_url") or "").rstrip("/")
    candidates: list[str] = []
    if primary:
        candidates.append(_normalize_openai_base(primary))
    for item in llm_base_candidates(config):
        if item not in candidates:
            candidates.append(item)
    return _image_client(config, base_url_override=candidates[0])


def _model_name(config: ProjectConfig) -> str:
    cfg = image_generator_config(config)
    model = str(cfg.get("model") or "").strip()
    if not model:
        raise ValueError("image_generator.model 不能为空")
    return model


def _is_seedream(model: str) -> bool:
    return "seedream" in model.lower()


def target_image_size(config: ProjectConfig) -> str:
    cfg = image_generator_config(config)
    if cfg.get("size"):
        return str(cfg["size"])
    # Seedream accepts 1K/2K/4K or WxH; prefer explicit pixels for drama frames.
    aspect = str(config.data.get("aspect_ratio") or "16:9")
    if aspect == "9:16":
        return "1024x1536"
    return "1536x1024"


def _decode_image_payload(item: Any) -> bytes:
    b64 = getattr(item, "b64_json", None) or (item.get("b64_json") if isinstance(item, dict) else None)
    if b64:
        return base64.b64decode(b64)
    url = getattr(item, "url", None) or (item.get("url") if isinstance(item, dict) else None)
    if url:
        with urlopen(url) as response:  # noqa: S310 — trusted API response URL
            return response.read()
    raise ValueError("图片 API 未返回 b64_json 或 url")


def save_generated_image(data: bytes, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _reference_data_url(path: Path) -> str:
    raw = path.read_bytes()
    suffix = path.suffix.lower().lstrip(".") or "png"
    if suffix == "jpg":
        suffix = "jpeg"
    return f"data:image/{suffix};base64,{base64.b64encode(raw).decode('ascii')}"


def generate_images(
    config: ProjectConfig,
    *,
    prompt: str,
    count: int = 1,
    size: str | None = None,
) -> list[bytes]:
    """Generate one or more still images from text."""
    if not image_generator_enabled(config):
        raise ValueError("image_generator.enabled 未开启")
    client = _image_client(config)
    cfg = image_generator_config(config)
    model = _model_name(config)
    n = max(1, min(count, int(cfg.get("max_candidates") or 4)))
    size_value = size or target_image_size(config)
    kwargs: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": n,
        "size": size_value,
    }
    extra: dict[str, Any] = {}
    quality = cfg.get("quality")
    if quality and not _is_seedream(model):
        kwargs["quality"] = quality
    if _is_seedream(model):
        # Ark Seedream: prefer no watermark; response may be url or b64.
        extra["watermark"] = False
        if cfg.get("response_format"):
            kwargs["response_format"] = str(cfg["response_format"])
        else:
            kwargs["response_format"] = "b64_json"
    else:
        kwargs["response_format"] = "b64_json"

    try:
        if extra:
            response = client.images.generate(**kwargs, extra_body=extra)
        else:
            response = client.images.generate(**kwargs)
    except TypeError:
        kwargs.pop("response_format", None)
        response = client.images.generate(**kwargs, extra_body=extra) if extra else client.images.generate(**kwargs)
    except Exception:
        # Some gateways reject response_format / n / quality; retry minimal.
        minimal = {"model": model, "prompt": prompt, "size": size_value, "n": 1}
        if extra:
            response = client.images.generate(**minimal, extra_body=extra)
        else:
            response = client.images.generate(**minimal)
    return [_decode_image_payload(item) for item in response.data]


def edit_image(
    config: ProjectConfig,
    *,
    prompt: str,
    reference_image: Path,
    size: str | None = None,
) -> bytes:
    """Generate an image conditioned on one reference still when the API supports edits."""
    if not image_generator_enabled(config):
        raise ValueError("image_generator.enabled 未开启")
    client = _image_client(config)
    cfg = image_generator_config(config)
    model = _model_name(config)
    size_value = size or target_image_size(config)

    # Seedream: same /images/generations endpoint with `image` reference (not /edits).
    if _is_seedream(model):
        kwargs: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size_value,
            "response_format": "b64_json",
        }
        extra = {
            "watermark": False,
            "image": _reference_data_url(reference_image),
        }
        try:
            response = client.images.generate(**kwargs, extra_body=extra)
        except Exception:
            kwargs.pop("response_format", None)
            response = client.images.generate(**kwargs, extra_body=extra)
        return _decode_image_payload(response.data[0])

    kwargs = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size_value,
    }
    quality = cfg.get("quality")
    if quality:
        kwargs["quality"] = quality
    with reference_image.open("rb") as handle:
        try:
            response = client.images.edit(**kwargs, image=handle, response_format="b64_json")
        except TypeError:
            handle.seek(0)
            response = client.images.edit(**kwargs, image=handle)
        except Exception:
            handle.seek(0)
            response = client.images.edit(**kwargs, image=handle)
    return _decode_image_payload(response.data[0])


def chat_client_for_vision(config: ProjectConfig) -> Any:
    """Reuse the planning LLM client for multimodal ranking / selection."""
    return create_client(config)
