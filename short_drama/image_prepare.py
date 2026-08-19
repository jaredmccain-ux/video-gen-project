"""Non-destructive image inspection and conservative 16:9/9:16 preparation."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import ProjectConfig
from .state import read_run, utc_now, write_json_atomic


TARGETS = {
    "16:9": (1376, 768),
    "9:16": (768, 1376),
}


def prepare_run_images(run_dir: Path, config: ProjectConfig) -> Path:
    """Inspect configured images and create derivatives matching common orientation."""
    manifest_path = run_dir / "inputs/manifest.json"
    if manifest_path.exists():
        raise FileExistsError(f"输入清单已存在，拒绝覆盖：{manifest_path}")
    output_dir = run_dir / "inputs/processed"
    output_dir.mkdir(parents=True, exist_ok=True)

    orientations = [_orientation(path) for path in config.input_images]
    if len(set(orientations)) != 1:
        raise ValueError(f"三张输入图方向不一致：{orientations}")
    orientation = orientations[0]
    inferred_ratio = "16:9" if orientation == "landscape" else "9:16"
    configured_ratio = config.data["aspect_ratio"]
    if configured_ratio != "auto" and configured_ratio != inferred_ratio:
        raise ValueError(f"配置画幅 {configured_ratio} 与输入方向推断的 {inferred_ratio} 不一致")
    target_width, target_height = TARGETS[inferred_ratio]

    records: list[dict[str, Any]] = []
    preview_items: list[tuple[str, Image.Image]] = []
    for index, source in enumerate(config.input_images, start=1):
        image_id = f"IMG{index:02d}"
        relative_key = str(source.relative_to(config.project_root))
        focus = config.data.get("crop_focus", {}).get(relative_key, {"x": 0.5, "y": 0.5})
        record, output = prepare_one(source, output_dir, image_id, target_width, target_height, focus)
        records.append(record)
        with Image.open(output) as prepared:
            preview_items.append((f"{image_id}  {source.name}  {record['strategy']}", prepared.convert("RGB").copy()))

    preview_path = output_dir / "contact_sheet.jpg"
    create_contact_sheet(preview_items, preview_path)
    manifest = {
        "schema_version": "1.0",
        "created_at": utc_now(),
        "input_orientation": orientation,
        "target": {"aspect_ratio": inferred_ratio, "width": target_width, "height": target_height},
        "policy": {
            "strategy": "focal_crop_only",
            "description": "Crop to the inferred target ratio around a normalized focal point. Never add borders or blurred padding to an H3 first frame.",
        },
        "images": records,
        "preview": str(preview_path.relative_to(run_dir)),
        "requires_human_review": True,
    }
    write_json_atomic(manifest_path, manifest)

    state = read_run(run_dir)
    state["state"] = "INPUTS_PREPARED"
    state["updated_at"] = utc_now()
    state["input_manifest"] = str(manifest_path)
    write_json_atomic(run_dir / "run.json", state)
    return manifest_path


def _orientation(source: Path) -> str:
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        width, height = image.size
    if width == height:
        raise ValueError(f"正方形图片无法判断横竖方向：{source}")
    return "landscape" if width > height else "portrait"


def prepare_one(
    source: Path,
    output_dir: Path,
    image_id: str,
    target_width: int,
    target_height: int,
    focus: dict[str, float],
) -> tuple[dict[str, Any], Path]:
    with Image.open(source) as opened:
        exif_orientation = opened.getexif().get(274)
        original_mode = opened.mode
        original_size = opened.size
        image = ImageOps.exif_transpose(opened).convert("RGB")

    oriented_width, oriented_height = image.size
    focus_x = float(focus.get("x", 0.5))
    focus_y = float(focus.get("y", 0.5))
    if not (0 <= focus_x <= 1 and 0 <= focus_y <= 1):
        raise ValueError(f"crop_focus 必须在 0–1 范围：{source}")
    prepared, crop_box, retention_ratio = _focal_crop(
        image, target_width, target_height, focus_x, focus_y
    )
    strategy = "focal_crop"
    rationale = f"Borderless target-ratio crop retains {retention_ratio:.1%} on the cropped axis."

    output = output_dir / f"{image_id}_{source.stem}_processed.png"
    if output.exists():
        raise FileExistsError(f"画幅处理输出已存在，拒绝覆盖：{output}")
    prepared.save(output, format="PNG", optimize=True)
    mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    record = {
        "image_id": image_id,
        "source_path": str(source),
        "mime_type": mime,
        "original_width": original_size[0],
        "original_height": original_size[1],
        "original_mode": original_mode,
        "exif_orientation": exif_orientation,
        "oriented_width": oriented_width,
        "oriented_height": oriented_height,
        "crop_retention_ratio": round(retention_ratio, 6),
        "crop_focus": {"x": focus_x, "y": focus_y},
        "crop_box": list(crop_box),
        "strategy": strategy,
        "strategy_rationale": rationale,
        "output_path": str(output),
        "output_width": target_width,
        "output_height": target_height,
    }
    return record, output


def _focal_crop(
    image: Image.Image,
    target_width: int,
    target_height: int,
    focus_x: float,
    focus_y: float,
) -> tuple[Image.Image, tuple[int, int, int, int], float]:
    target_ratio = target_width / target_height
    width, height = image.size
    source_ratio = width / height
    if source_ratio >= target_ratio:
        crop_width = int(round(height * target_ratio))
        center_x = round(focus_x * width)
        left = max(0, min(width - crop_width, center_x - crop_width // 2))
        box = (left, 0, left + crop_width, height)
        retention = crop_width / width
    else:
        crop_height = int(round(width / target_ratio))
        center_y = round(focus_y * height)
        top = max(0, min(height - crop_height, center_y - crop_height // 2))
        box = (0, top, width, top + crop_height)
        retention = crop_height / height
    prepared = image.crop(box).resize((target_width, target_height), Image.Resampling.LANCZOS)
    return prepared, box, retention


def create_contact_sheet(items: list[tuple[str, Image.Image]], output: Path) -> None:
    ratio = items[0][1].width / items[0][1].height
    if ratio > 1:
        thumb_width, thumb_height = 416, round(416 / ratio)
    else:
        thumb_width, thumb_height = round(516 * ratio), 516
    gap, label_height = 24, 44
    canvas = Image.new("RGB", (gap + len(items) * (thumb_width + gap), thumb_height + label_height + gap * 2), "#202020")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for index, (label, image) in enumerate(items):
        thumb = image.resize((thumb_width, thumb_height), Image.Resampling.LANCZOS)
        x, y = gap + index * (thumb_width + gap), gap
        canvas.paste(thumb, (x, y))
        draw.text((x, y + thumb_height + 10), label, fill="white", font=font)
    canvas.save(output, format="JPEG", quality=92, optimize=True)
