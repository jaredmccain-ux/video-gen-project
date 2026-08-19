"""Human-authored shot orchestration overlays for the web studio.

The overlay deliberately lives beside ``shots.json`` instead of rewriting it.
This keeps the LLM-authored plan and its approval hash intact while allowing a
human to choose the concrete H3 task, image inputs, and final prompt.
"""

from __future__ import annotations

import base64
import binascii
import io
import mimetypes
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from PIL import Image

from .config import ProjectConfig
from .h3_prompt import render_studio_prompt
from .state import utc_now, write_json_atomic
from .validators import load_json


MODE_ALIASES = {
    "T2VA": "t2va",
    "I2VA": "first_frame",
    "FL2VA": "first_last_frame",
    "REF2VA": "ref2va",
    "t2va": "t2va",
    "first_frame": "first_frame",
    "first_last_frame": "first_last_frame",
    "ref2va": "ref2va",
}
MODE_LABELS = {
    "t2va": "T2VA",
    "first_frame": "I2VA",
    "first_last_frame": "FL2VA",
    "ref2va": "Ref2VA",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac"}
MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES | AUDIO_SUFFIXES
H3_INPUT_LIMITS = {"image": 9, "video": 3, "audio": 3}
REFERENCE_IMAGE_USAGES = {"identity", "scene", "style", "keyframe"}
PROMPT_IMAGE_USAGES = {"first_frame", "last_frame", *REFERENCE_IMAGE_USAGES}
REFERENCE_VIDEO_USAGES = {"motion", "camera", "style", "continuity"}
REFERENCE_AUDIO_USAGES = {"voice", "soundscape", "action_sound", "rhythm"}
LIBRARY_UPLOAD_LIMITS: dict[str, int | None] = {"image": None, "video": None, "audio": None}
UPLOAD_SIZE_LIMITS = {
    "image": 20 * 1024 * 1024,
    "video": 512 * 1024 * 1024,
    "audio": 100 * 1024 * 1024,
}
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_UPLOAD_MANIFEST_LOCK = threading.Lock()


def orchestration_path(run_dir: Path) -> Path:
    return run_dir / "03_shots/human_orchestration.json"


def load_orchestration(run_dir: Path) -> dict[str, Any]:
    path = orchestration_path(run_dir)
    if not path.is_file():
        return {
            "schema_version": "1.0",
            "updated_at": None,
            "policy": "human_decision_overrides_automatic_routing",
            "shots": {},
        }
    document = load_json(path)
    if not isinstance(document.get("shots"), dict):
        raise ValueError(f"人工编排文件格式错误：{path}")
    return document


def normalize_mode(value: Any) -> str:
    key = str(value or "")
    mode = MODE_ALIASES.get(key) or MODE_ALIASES.get(key.upper())
    if mode is None:
        raise ValueError(f"未知生成模式：{value}")
    return mode


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _allowed_asset_roots(run_dir: Path, config: ProjectConfig) -> tuple[Path, ...]:
    roots = [run_dir.resolve(), (config.project_root / "assets").resolve()]
    return tuple(root for root in roots if root.exists())


def resolve_asset_path(
    value: Any,
    *,
    run_dir: Path,
    config: ProjectConfig,
    required: bool = False,
) -> Path | None:
    if value in (None, ""):
        if required:
            raise ValueError("缺少必需输入图片")
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (run_dir / path).resolve()
    else:
        path = path.resolve()
    if not any(_is_within(path, root) for root in _allowed_asset_roots(run_dir, config)):
        raise ValueError(f"输入文件不在允许的 run/assets 目录：{path}")
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(f"输入文件不是支持的图片格式：{path}")
    if not path.is_file():
        raise FileNotFoundError(f"输入图片不存在：{path}")
    return path


def resolve_reference_path(
    value: Any,
    *,
    run_dir: Path,
    config: ProjectConfig,
    kind: str,
) -> Path:
    suffixes = VIDEO_SUFFIXES if kind == "video" else AUDIO_SUFFIXES
    path = Path(str(value or "")).expanduser()
    path = (run_dir / path).resolve() if not path.is_absolute() else path.resolve()
    if not any(_is_within(path, root) for root in _allowed_asset_roots(run_dir, config)):
        raise ValueError(f"输入文件不在允许的 run/assets 目录：{path}")
    if path.suffix.lower() not in suffixes or not path.is_file():
        raise ValueError(f"{kind} 参考素材不存在或格式不支持：{path}")
    return path


def _normalize_media_bindings(
    decision: dict[str, Any],
    *,
    run_dir: Path,
    config: ProjectConfig,
    kind: str,
) -> list[dict[str, Any]]:
    binding_key = f"reference_{kind}_bindings"
    legacy_key = f"reference_{kind}s"
    default_usage = "motion" if kind == "video" else "soundscape"
    allowed_usages = REFERENCE_VIDEO_USAGES if kind == "video" else REFERENCE_AUDIO_USAGES
    raw = decision.get(binding_key)
    if not isinstance(raw, list):
        raw = [
            {"path": value, "usage": default_usage, "note": ""}
            for value in (decision.get(legacy_key) or [])
        ]
    bindings: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for item in raw:
        if isinstance(item, str):
            item = {"path": item, "usage": default_usage, "note": ""}
        if not isinstance(item, dict) or not item.get("path"):
            continue
        path = resolve_reference_path(item["path"], run_dir=run_dir, config=config, kind=kind)
        if path in seen:
            continue
        usage = str(item.get("usage") or default_usage).strip().lower()
        if usage not in allowed_usages:
            raise ValueError(f"未知{kind}参考用途：{usage}")
        bindings.append(
            {
                "path": str(path),
                "usage": usage,
                "note": str(item.get("note") or "").strip()[:500],
            }
        )
        seen.add(path)
    return bindings


def validate_decision(
    decision: dict[str, Any],
    *,
    run_dir: Path,
    config: ProjectConfig,
    require_approved: bool = False,
) -> dict[str, Any]:
    mode = normalize_mode(decision.get("generation_mode"))
    prompt = str(decision.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("最终 Prompt 不能为空")
    strict_inputs = require_approved or bool(decision.get("approved"))
    first = resolve_asset_path(
        decision.get("first_frame"),
        run_dir=run_dir,
        config=config,
        required=strict_inputs and mode in {"first_frame", "first_last_frame"},
    )
    last = resolve_asset_path(
        decision.get("last_frame"),
        run_dir=run_dir,
        config=config,
        required=strict_inputs and mode == "first_last_frame",
    )
    raw_bindings = decision.get("reference_image_bindings")
    if not isinstance(raw_bindings, list):
        # Backward compatibility: old Studio revisions treated every selected
        # reference as an identity reference. New revisions always persist an
        # explicit usage so scene/style/keyframe references stay independent.
        raw_bindings = [
            {"path": value, "usage": "identity", "character_ids": []}
            for value in (decision.get("reference_images") or [])
        ]
    bindings: list[dict[str, Any]] = []
    seen_reference_paths: set[Path] = set()
    for item in raw_bindings:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        path = resolve_asset_path(item["path"], run_dir=run_dir, config=config, required=True)
        if path is None or path in seen_reference_paths:
            continue
        usage = str(item.get("usage") or "scene").strip().lower()
        if usage not in REFERENCE_IMAGE_USAGES:
            raise ValueError(f"未知参考图用途：{usage}")
        character_ids = item.get("character_ids") or []
        if isinstance(character_ids, str):
            character_ids = [character_ids]
        bindings.append(
            {
                "path": str(path),
                "usage": usage,
                "character_ids": list(dict.fromkeys(str(value) for value in character_ids if value)),
                "note": str(item.get("note") or "").strip()[:500],
            }
        )
        seen_reference_paths.add(path)
    references = [Path(item["path"]) for item in bindings]
    video_frame_bindings: list[dict[str, Any]] = []
    for item in decision.get("video_frame_bindings") or []:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        path = resolve_asset_path(item["path"], run_dir=run_dir, config=config, required=True)
        if path is None or path in seen_reference_paths:
            continue
        video_frame_bindings.append(
            {
                "path": str(path),
                "usage": "keyframe",
                "character_ids": [],
                "note": str(item.get("note") or "参考视频抽帧连续性参考").strip()[:500],
                "source_video": str(item.get("source_video") or ""),
                "source_video_index": int(item.get("source_video_index") or 0),
                "sample_ratio": float(item.get("sample_ratio") or 0),
            }
        )
        seen_reference_paths.add(path)
    video_bindings = _normalize_media_bindings(
        decision, run_dir=run_dir, config=config, kind="video"
    )
    audio_bindings = _normalize_media_bindings(
        decision, run_dir=run_dir, config=config, kind="audio"
    )
    reference_videos = [Path(item["path"]) for item in video_bindings]
    reference_audios = [Path(item["path"]) for item in audio_bindings]
    if mode == "ref2va":
        if strict_inputs and not (first or last or references or reference_videos or reference_audios):
            raise ValueError("Ref2VA 至少需要一项图片、视频或音频参考")
    picture_count = len(
        {
            str(path)
            for path in (
                ([first] if mode == "ref2va" and first else [])
                + ([last] if mode == "ref2va" and last else [])
                + references
                + [Path(item["path"]) for item in video_frame_bindings]
            )
        }
    )
    if (picture_count if mode == "ref2va" else len(references)) > 9:
        raise ValueError("单个 H3 镜头最多允许 9 张图片输入（首帧、尾帧与参考图合计）")
    if len(reference_videos) > 3:
        raise ValueError("单个 H3 镜头最多允许 3 个参考视频")
    if len(reference_audios) > 3:
        raise ValueError("单个 H3 镜头最多允许 3 个独立参考音频")
    if require_approved and not decision.get("approved"):
        raise ValueError("镜头尚未人工批准")
    return {
        **decision,
        "generation_mode": mode,
        "mode_label": MODE_LABELS[mode],
        "first_frame": str(first) if first else None,
        "last_frame": str(last) if last else None,
        "reference_images": [str(path) for path in references],
        "reference_image_bindings": bindings,
        "video_frame_bindings": video_frame_bindings,
        "reference_video_strategy": str(decision.get("reference_video_strategy") or "sampled_frames"),
        "reference_videos": [str(path) for path in reference_videos],
        "reference_audios": [str(path) for path in reference_audios],
        "reference_video_bindings": video_bindings,
        "reference_audio_bindings": audio_bindings,
        "prompt": prompt,
        "approved": bool(decision.get("approved", False)),
        "locked": bool(decision.get("locked", False)),
        "seed": int(decision.get("seed") or 2101),
    }


def effective_picture_bindings(decision: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the exact ordered picture contract used by Prompt and H3.

    Ref2VA can use every human-selected picture.  First/last-frame selections
    therefore become explicit temporal reference roles in the Ref2VA picture
    list, followed by the ordinary identity/scene/style/keyframe references.
    When one file has several roles, it is submitted only once and its roles
    are merged so that ``<Picture i>`` stays stable across the whole pipeline.

    I2VA/FL2VA use their dedicated frame inputs, so their picture contract is
    exactly Picture 1 = first frame and (for FL2VA) Picture 2 = last frame.
    """
    mode = normalize_mode(decision.get("generation_mode"))
    ordered: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}

    def add(
        path_value: Any,
        usage: str,
        *,
        character_ids: list[str] | tuple[str, ...] | None = None,
        note: str = "",
    ) -> None:
        if not path_value:
            return
        path = str(path_value)
        item = by_path.get(path)
        if item is None:
            item = {
                "path": path,
                "usage": usage,
                "usages": [usage],
                "character_ids": [],
                "note": "",
            }
            by_path[path] = item
            ordered.append(item)
        elif usage not in item["usages"]:
            item["usages"].append(usage)
        for character_id in character_ids or []:
            value = str(character_id)
            if value and value not in item["character_ids"]:
                item["character_ids"].append(value)
        clean_note = str(note or "").strip()
        if clean_note and clean_note not in item["note"]:
            item["note"] = "；".join(value for value in (item["note"], clean_note) if value)

    if mode in {"first_frame", "first_last_frame", "ref2va"}:
        add(decision.get("first_frame"), "first_frame")
    if mode in {"first_last_frame", "ref2va"}:
        add(decision.get("last_frame"), "last_frame")
    if mode == "ref2va":
        for raw in decision.get("reference_image_bindings") or []:
            if not isinstance(raw, dict):
                continue
            usage = str(raw.get("usage") or "scene").lower()
            if usage not in REFERENCE_IMAGE_USAGES:
                continue
            add(
                raw.get("path"),
                usage,
                character_ids=raw.get("character_ids") or [],
                note=str(raw.get("note") or ""),
            )
        for raw in decision.get("video_frame_bindings") or []:
            if not isinstance(raw, dict):
                continue
            add(
                raw.get("path"),
                "keyframe",
                note=str(raw.get("note") or "参考视频抽帧连续性参考"),
            )
    for index, item in enumerate(ordered, start=1):
        item["picture"] = f"<Picture {index}>"
    return ordered


def save_decision(
    run_dir: Path,
    config: ProjectConfig,
    shot_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    shots_doc = load_json(run_dir / "03_shots/shots.json")
    known = {shot["shot_id"] for shot in shots_doc.get("shots", [])}
    if shot_id not in known:
        raise ValueError(f"未知镜头：{shot_id}")
    document = load_orchestration(run_dir)
    previous = document["shots"].get(shot_id) or {}
    if previous.get("locked") and not payload.get("force_unlock"):
        # Re-saving an already locked shot (e.g. "批准并进入下一镜") must not
        # require an unlock. Only reject when the client asked to change it.
        return validate_decision(
            previous,
            run_dir=run_dir,
            config=config,
            require_approved=False,
        )
    merged = {**previous, **payload}
    if payload.get("force_unlock"):
        # ``force_unlock`` authorizes editing a previously locked revision;
        # the submitted lock checkbox still decides the new revision's state.
        merged["locked"] = bool(payload.get("locked", False))
    normalized = validate_decision(
        merged,
        run_dir=run_dir,
        config=config,
        require_approved=False,
    )
    shot = next(item for item in shots_doc.get("shots", []) if item["shot_id"] == shot_id)
    story = load_json(run_dir / "02_story/story.json")
    prompt_source = normalized["prompt"]
    normalized["prompt_source"] = prompt_source
    if not payload.get("skip_prompt_optimization"):
        picture_bindings = effective_picture_bindings(normalized)
        optimized_prompt, prompt_errors = render_studio_prompt(
            shot,
            story,
            generation_mode=normalized["generation_mode"],
            user_prompt=prompt_source,
            reference_paths=[item["path"] for item in picture_bindings],
            reference_bindings=picture_bindings,
            reference_video_bindings=normalized.get("reference_video_bindings") or [],
            reference_audio_bindings=normalized.get("reference_audio_bindings") or [],
        )
        if prompt_errors:
            raise ValueError("MiniMax H3 官方 Prompt Skill 校验失败：" + "; ".join(prompt_errors))
        normalized["prompt"] = optimized_prompt
        normalized["prompt_skill"] = "MiniMax H3 / h3-prompt-writing"
        normalized["prompt_skill_source"] = "https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing"
        normalized["prompt_optimized_at"] = utc_now()
    normalized["revision"] = int(previous.get("revision") or 0) + 1
    normalized["updated_at"] = utc_now()
    normalized.pop("force_unlock", None)
    normalized.pop("skip_prompt_optimization", None)
    document["shots"][shot_id] = normalized
    document["updated_at"] = utc_now()
    write_json_atomic(orchestration_path(run_dir), document)
    return normalized


def recover_generated_image_bindings(run_dir: Path, config: ProjectConfig) -> int:
    """Recover AI-image bindings created before the browser could persist them.

    Image bytes and their intent are written to the generation manifest before
    the client receives a response. If the page reloads in that small window,
    the manifest is therefore the durable recovery log. A newer saved decision
    always wins, so a picture explicitly removed later is never re-attached.
    """
    manifest_path = run_dir / "inputs/studio_generated/manifest.json"
    if not manifest_path.is_file():
        return 0
    manifest = load_json(manifest_path)
    orchestration = load_orchestration(run_dir)
    recovered = 0
    usage_by_role = {
        "reference": "scene",
        "reference_identity": "identity",
        "reference_scene": "scene",
        "reference_style": "style",
        "keyframe": "keyframe",
    }
    for record in manifest.get("images") or []:
        if not isinstance(record, dict):
            continue
        shot_id = str(record.get("shot_id") or "")
        role = str(record.get("role") or "library")
        decision = orchestration.get("shots", {}).get(shot_id)
        if not isinstance(decision, dict) or role == "library":
            continue
        created_at = str(record.get("created_at") or "")
        if created_at and str(decision.get("updated_at") or "") >= created_at:
            continue
        local_path = str(record.get("local_path") or "")
        path = (run_dir / local_path).resolve()
        if not path.is_file():
            continue
        payload = {**decision, "approved": False, "locked": False, "force_unlock": True}
        changed = False
        if role == "first" and decision.get("first_frame") != str(path):
            payload["first_frame"] = str(path)
            changed = True
        elif role == "last" and decision.get("last_frame") != str(path):
            payload["last_frame"] = str(path)
            changed = True
        elif role in usage_by_role:
            bindings = list(decision.get("reference_image_bindings") or [])
            if not bindings:
                bindings = [
                    {"path": value, "usage": "identity", "character_ids": [], "note": ""}
                    for value in (decision.get("reference_images") or [])
                ]
            if not any(str(item.get("path")) == str(path) for item in bindings if isinstance(item, dict)):
                bindings.append(
                    {
                        "path": str(path),
                        "usage": usage_by_role[role],
                        "character_ids": [],
                        "note": "从 AI 生图本地清单恢复",
                    }
                )
                payload["reference_image_bindings"] = bindings
                payload["reference_images"] = [str(item.get("path")) for item in bindings]
                changed = True
        if not changed:
            continue
        # Force a fresh canonical Skill render so Picture indices and usage
        # contracts match the recovered binding instead of preserving an older
        # structurally-valid prompt that predates this image.
        payload["prompt"] = str(
            decision.get("prompt_llm_draft")
            or "按已恢复的本地 AI 图片绑定重新生成当前镜头。"
        )
        save_decision(run_dir, config, shot_id, payload)
        orchestration = load_orchestration(run_dir)
        record["binding_recovered_at"] = utc_now()
        recovered += 1
    if recovered:
        manifest["updated_at"] = utc_now()
        write_json_atomic(manifest_path, manifest)
    return recovered


def _asset_record(path: Path, *, role: str, label: str, run_dir: Path) -> dict[str, Any]:
    try:
        relative = str(path.resolve().relative_to(run_dir.resolve()))
    except ValueError:
        relative = path.name
    suffix = path.suffix.lower()
    media_kind = "image" if suffix in IMAGE_SUFFIXES else "video" if suffix in VIDEO_SUFFIXES else "audio"
    shot_match = re.search(r"(?:^|[/_.-])(S\d{3})(?:[/_.-]|$)", relative, flags=re.IGNORECASE)
    source_shot_id = shot_match.group(1).upper() if shot_match else None
    if "inputs/studio_generated/" in relative:
        asset_origin = "ai_still"
    elif relative.endswith(".last_frame.png") or relative.endswith(".last.png"):
        asset_origin = "video_last_frame"
    elif "05_videos/studio_generations/" in relative and media_kind == "video":
        asset_origin = "generated_video"
    elif relative.startswith("05_videos/") and media_kind == "video":
        asset_origin = "legacy_pipeline_video"
    else:
        asset_origin = role
    return {
        "path": str(path.resolve()),
        "name": path.name,
        "label": label,
        "role": role,
        "relative_path": relative,
        "size_bytes": path.stat().st_size,
        "media_kind": media_kind,
        "source_shot_id": source_shot_id,
        "asset_origin": asset_origin,
        "created_at": path.stat().st_mtime,
    }


def list_run_assets(run_dir: Path, config: ProjectConfig) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    manifest_path = run_dir / "inputs/manifest.json"
    if manifest_path.is_file():
        for item in load_json(manifest_path).get("images", []):
            path = Path(str(item.get("output_path") or ""))
            if path.is_file():
                records.append(
                    _asset_record(
                        path,
                        role="anchor",
                        label=f'{item.get("image_id")} · 原始锚点',
                        run_dir=run_dir,
                    )
                )
    registry_path = run_dir / "03_shots/character_portraits.json"
    if registry_path.is_file():
        for character in load_json(registry_path).get("characters", []):
            for portrait in character.get("portraits", []):
                path = Path(str(portrait.get("path") or ""))
                if path.is_file():
                    records.append(
                        _asset_record(
                            path,
                            role="portrait",
                            label=f'{character.get("name") or character.get("character_id")} · {portrait.get("view")}',
                            run_dir=run_dir,
                        )
                    )
    patterns = (
        (run_dir / "03_shots/keyframes", "keyframe", "关键帧"),
        (run_dir / "05_videos", "last_frame", "生成末帧"),
        (run_dir / "inputs/studio_uploads", "upload", "人工上传"),
        (run_dir / "inputs/studio_generated", "ai_generated", "AI 生成图"),
        (run_dir / "05_videos/studio_generations", "generation", "Studio 生成"),
    )
    for root, role, label in patterns:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES:
                if role == "last_frame" and _is_within(
                    path, run_dir / "05_videos/studio_generations"
                ):
                    # The dedicated studio_generations pattern below supplies
                    # the correct source-shot label and avoids the parent
                    # 05_videos scan mislabelling new videos as legacy output.
                    continue
                if (
                    role == "last_frame"
                    and path.parent.resolve() == (run_dir / "05_videos").resolve()
                    and path.suffix.lower() in VIDEO_SUFFIXES
                ):
                    # Original pipeline videos are historical outputs, not
                    # user-selectable Ref2VA material. Only Studio generations
                    # and user uploads belong in the current material library.
                    continue
                item_role = role
                item_label = label
                if role == "last_frame" and path.suffix.lower() in VIDEO_SUFFIXES:
                    item_role, item_label = "generation", "原流水线生成视频"
                records.append(_asset_record(path, role=item_role, label=item_label, run_dir=run_dir))
    unique: dict[Path, dict[str, Any]] = {}
    for record in records:
        unique.setdefault(Path(record["path"]), record)
    return list(unique.values())


def save_uploaded_data_url(
    run_dir: Path,
    *,
    filename: str,
    data_url: str,
) -> Path:
    if not data_url.startswith("data:image/") or ";base64," not in data_url:
        raise ValueError("上传内容必须是 base64 图片 data URL")
    header, encoded = data_url.split(",", 1)
    mime = header[5:].split(";", 1)[0].lower()
    suffix = mimetypes.guess_extension(mime) or ".png"
    if suffix == ".jpe":
        suffix = ".jpg"
    safe = SAFE_NAME_RE.sub("-", Path(filename).stem).strip(".-") or "upload"
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("上传图片 base64 无效") from exc
    path, _ = save_uploaded_stream(
        run_dir,
        filename=f"{safe}{suffix}",
        kind="image",
        stream=io.BytesIO(raw),
        content_length=len(raw),
    )
    return path


def _probe_media(path: Path, kind: str) -> dict[str, Any]:
    if kind == "image":
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return {"width": image.width, "height": image.height}
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type", "-of", "json", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError("未找到 ffprobe，无法校验视频/音频素材") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"无法解析上传的{kind}文件") from exc
    import json

    info = json.loads(result.stdout)
    stream_types = {item.get("codec_type") for item in info.get("streams", [])}
    if kind not in stream_types:
        raise ValueError(f"上传文件不包含有效的{kind}轨道")
    duration = float((info.get("format") or {}).get("duration") or 0)
    if kind == "video" and not 2 <= duration <= 15:
        raise ValueError("H3 参考视频时长必须为 2–15 秒")
    return {"duration_s": round(duration, 3)}


def _record_uploaded_asset(
    run_dir: Path,
    *,
    path: Path,
    original_filename: str,
    kind: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    manifest_path = run_dir / "inputs/studio_uploads/manifest.json"
    with _UPLOAD_MANIFEST_LOCK:
        document = (
            load_json(manifest_path)
            if manifest_path.is_file()
            else {"schema_version": "1.0", "updated_at": None, "assets": []}
        )
        asset_id = uuid.uuid4().hex
        record = {
            "asset_id": asset_id,
            "media_kind": kind,
            "original_filename": Path(original_filename).name,
            "local_path": str(path.resolve().relative_to(run_dir.resolve())),
            "size_bytes": path.stat().st_size,
            "metadata": metadata,
            "created_at": utc_now(),
        }
        document["assets"].append(record)
        document["updated_at"] = utc_now()
        write_json_atomic(manifest_path, document)
    return {**metadata, "asset_id": asset_id}


def save_uploaded_stream(
    run_dir: Path,
    *,
    filename: str,
    kind: str,
    stream: BinaryIO,
    content_length: int,
) -> tuple[Path, dict[str, Any]]:
    if kind not in LIBRARY_UPLOAD_LIMITS:
        raise ValueError(f"未知素材类型：{kind}")
    suffixes = IMAGE_SUFFIXES if kind == "image" else VIDEO_SUFFIXES if kind == "video" else AUDIO_SUFFIXES
    suffix = Path(filename).suffix.lower()
    if suffix not in suffixes:
        raise ValueError(f"{kind} 文件格式不支持：{suffix or '无扩展名'}")
    if content_length <= 0 or content_length > UPLOAD_SIZE_LIMITS[kind]:
        raise ValueError(f"{kind} 文件大小超出限制")
    upload_root = run_dir / "inputs/studio_uploads"
    output_dir = upload_root / f"{kind}s"
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = [path for path in upload_root.rglob("*") if path.is_file() and path.suffix.lower() in suffixes]
    library_limit = LIBRARY_UPLOAD_LIMITS[kind]
    if library_limit is not None and len(existing) >= library_limit:
        raise ValueError(f"每个 run 最多上传 {library_limit} 个{kind}参考素材")
    safe = SAFE_NAME_RE.sub("-", Path(filename).stem).strip(".-") or kind
    stamp = utc_now().replace(":", "").replace("+", "-")
    target = output_dir / f"{safe}-{stamp}-{uuid.uuid4().hex[:8]}{suffix}"
    temporary = output_dir / f".upload-{uuid.uuid4().hex}{suffix}"
    remaining = content_length
    try:
        with temporary.open("wb") as output:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("上传内容提前结束")
                output.write(chunk)
                remaining -= len(chunk)
        metadata = _probe_media(temporary, kind)
        temporary.replace(target)
        metadata = _record_uploaded_asset(
            run_dir,
            path=target,
            original_filename=filename,
            kind=kind,
            metadata=metadata,
        )
        return target.resolve(), metadata
    finally:
        temporary.unlink(missing_ok=True)


def media_type_for(path: Path) -> str:
    if path.suffix.lower() in VIDEO_SUFFIXES | AUDIO_SUFFIXES:
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
