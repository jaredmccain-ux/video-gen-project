"""LLM client for planning stages (Azure OpenAI or OpenAI-compatible gateways).

Supports Rivo and Volcengine Ark (豆包) via the OpenAI SDK.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from openai import AzureOpenAI, OpenAI

from .config import ProjectConfig

# Rivo (legacy / optional)
RIVO_PRIMARY_BASE = "https://api.rivoapi.com/v1"
RIVO_FALLBACK_BASE = "https://rivoapi.com/v1"

# Volcengine Ark OpenAI-compatible Chat API
# Docs: https://www.volcengine.com/docs/82379/1399008
ARK_DEFAULT_BASE = "https://ark.cn-beijing.volces.com/api/v3"

OPENAI_COMPAT_PROVIDERS = {
    "rivo",
    "openai",
    "openai_compatible",
    "ark",
    "volcengine",
    "doubao",
    "huoshan",
}


def _safe_log(message: str) -> None:
    """Best-effort diagnostics must never abort an LLM request.

    Studio commonly runs under nohup/setsid.  If the launcher-owned stdout
    pipe disappears, a plain ``print(..., flush=True)`` raises EPIPE before
    the request even reaches the model.  Logging is observability only, so a
    detached or closed stream is intentionally ignored.
    """
    try:
        print(message, file=sys.stderr, flush=True)
    except (BrokenPipeError, OSError, ValueError):
        pass


def _llm_config(config: ProjectConfig) -> dict[str, Any]:
    data = config.data
    if isinstance(data.get("llm"), dict):
        return data["llm"]
    if isinstance(data.get("azure"), dict):
        return data["azure"]
    raise ValueError("配置缺少 llm 或 azure 段")


def _direct_http_client(*, timeout_s: float = 600.0) -> httpx.Client:
    """Bypass HTTP(S)_PROXY for LLM gateways (avoid Clash hijacking long requests)."""
    return httpx.Client(
        trust_env=False,
        timeout=httpx.Timeout(timeout_s, connect=45.0),
    )


def _normalize_openai_base(endpoint: str) -> str:
    endpoint = endpoint.rstrip("/")
    if not endpoint:
        raise ValueError("llm.endpoint / llm.base_url 不能为空")
    # Ark / coding plan already include /api/v3 or /api/coding/v3
    if (
        endpoint.endswith("/v1")
        or endpoint.endswith("/v3")
        or "/api/v3" in endpoint
        or "/api/coding" in endpoint
    ):
        return endpoint
    return f"{endpoint}/v1"


def create_client(config: ProjectConfig, *, base_url_override: str | None = None) -> OpenAI | AzureOpenAI:
    llm = _llm_config(config)
    env_name = str(llm.get("api_key_env") or "RIVO_API_KEY")
    api_key = os.environ.get(env_name)
    if not api_key:
        raise ValueError(f"环境变量 {env_name} 未设置；请先 export {env_name}=...")

    provider = str(llm.get("provider") or "").lower()
    endpoint = str(
        base_url_override or llm.get("endpoint") or llm.get("base_url") or ""
    ).rstrip("/")
    if not endpoint:
        raise ValueError("llm.endpoint / llm.base_url 不能为空")

    # OpenAI-compatible gateways (Rivo, Ark/Doubao, etc.)
    if (
        provider in OPENAI_COMPAT_PROVIDERS
        or "rivoapi.com" in endpoint
        or "volces.com" in endpoint
        or endpoint.endswith(("/v1", "/v3"))
        or "/api/v3" in endpoint
    ):
        base_url = _normalize_openai_base(endpoint)
        return OpenAI(api_key=api_key, base_url=base_url, http_client=_direct_http_client())

    # Legacy Azure OpenAI
    api_version = str(llm.get("api_version") or "")
    if not api_version:
        raise ValueError("使用 Azure 时必须设置 api_version")
    return AzureOpenAI(
        api_version=api_version,
        azure_endpoint=endpoint,
        api_key=api_key,
    )


def llm_base_candidates(config: ProjectConfig) -> list[str]:
    """Return base URL candidates for the configured provider."""
    llm = _llm_config(config)
    provider = str(llm.get("provider") or "").lower()
    primary = str(llm.get("endpoint") or llm.get("base_url") or "").rstrip("/")
    if not primary:
        if provider in {"ark", "volcengine", "doubao", "huoshan"}:
            primary = ARK_DEFAULT_BASE
        else:
            primary = RIVO_FALLBACK_BASE
    primary = _normalize_openai_base(primary)

    if provider in {"ark", "volcengine", "doubao", "huoshan"} or "volces.com" in primary:
        return [primary]

    # Rivo: config first, then documented alternate hosts
    out = [primary]
    for alt in (RIVO_FALLBACK_BASE, RIVO_PRIMARY_BASE):
        if alt not in out:
            out.append(alt)
    return out


# Back-compat alias
def rivo_base_candidates(config: ProjectConfig) -> list[str]:
    return llm_base_candidates(config)


def _model_name(config: ProjectConfig) -> str:
    llm = _llm_config(config)
    return str(llm.get("model") or llm.get("deployment") or "")


def image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _is_retryable_llm_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "connection error",
            "broken pipe",
            "server disconnected",
            "remoteprotocolerror",
            "connection reset",
            "unexpected eof",
            "timeout",
            "timed out",
            "connect",
            "502",
            "503",
            "504",
            "500",
            "internal server error",
            "upstream",
            "do_request_failed",
            "do request failed",
            "overloaded",
            "service unavailable",
            "rate limit",
            "429",
        )
    )


def _chat_create(
    client: OpenAI | AzureOpenAI,
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_completion_tokens: int,
    extra_body: dict[str, Any] | None = None,
) -> Any:
    """Prefer max_completion_tokens; fall back to max_tokens for older gateways."""
    try:
        return client.chat.completions.create(
            model=model,
            messages=messages,
            max_completion_tokens=max_completion_tokens,
            extra_body=extra_body,
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "max_completion_tokens" in msg or "unexpected keyword" in msg or "max_tokens" in msg:
            return client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_completion_tokens,
                extra_body=extra_body,
            )
        raise


def completion_text(response: Any) -> str:
    """Return assistant text with a useful error for exhausted reasoning responses."""
    choice = response.choices[0]
    message = choice.message
    content = getattr(message, "content", None)
    if isinstance(content, str) and content.strip():
        return content
    reasoning = getattr(message, "reasoning_content", None)
    finish_reason = getattr(choice, "finish_reason", None)
    if reasoning and finish_reason == "length":
        raise ValueError("模型的深度思考耗尽了输出额度，尚未生成正文；请关闭深度思考或提高输出额度")
    raise ValueError("模型响应成功，但没有返回正文内容")


def _request_extra_body(config: ProjectConfig, base_url: str) -> dict[str, Any] | None:
    llm = _llm_config(config)
    provider = str(llm.get("provider") or "").lower()
    if provider in {"ark", "volcengine", "doubao", "huoshan"} or "volces.com" in base_url:
        thinking = str(llm.get("thinking") or "disabled").lower()
        if thinking not in {"enabled", "disabled", "auto"}:
            raise ValueError("llm.thinking 必须是 enabled、disabled 或 auto")
        return {"thinking": {"type": thinking}}
    return None


def create_multimodal_completion(
    config: ProjectConfig,
    *,
    system_prompt: str,
    user_text: str,
    image_paths: list[Path],
    image_labels: list[str] | None = None,
    max_completion_tokens: int = 16384,
) -> Any:
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    labels = image_labels or []
    for index, path in enumerate(image_paths):
        if index < len(labels) and labels[index]:
            content.append({"type": "text", "text": labels[index]})
        content.append({"type": "image_url", "image_url": {"url": image_data_url(path), "detail": "high"}})
    model = _model_name(config)
    if not model:
        raise ValueError("llm.model / azure.deployment 不能为空")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]
    last_error: Exception | None = None
    for base_url in llm_base_candidates(config):
        try:
            _safe_log(f"[llm] multimodal via {base_url} model={model}")
            return _chat_create(
                create_client(config, base_url_override=base_url),
                model=model,
                messages=messages,
                max_completion_tokens=max_completion_tokens,
                extra_body=_request_extra_body(config, base_url),
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            _safe_log(f"[llm] {base_url} failed: {exc}")
            if not _is_retryable_llm_error(exc):
                raise
    assert last_error is not None
    raise last_error


def create_text_completion(
    config: ProjectConfig,
    *,
    system_prompt: str,
    user_text: str,
    max_completion_tokens: int = 16384,
) -> Any:
    model = _model_name(config)
    if not model:
        raise ValueError("llm.model / azure.deployment 不能为空")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]
    last_error: Exception | None = None
    for base_url in llm_base_candidates(config):
        try:
            _safe_log(f"[llm] chat.completions via {base_url} model={model}")
            return _chat_create(
                create_client(config, base_url_override=base_url),
                model=model,
                messages=messages,
                max_completion_tokens=max_completion_tokens,
                extra_body=_request_extra_body(config, base_url),
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            _safe_log(f"[llm] {base_url} failed: {exc}")
            if not _is_retryable_llm_error(exc):
                raise
    assert last_error is not None
    raise last_error
