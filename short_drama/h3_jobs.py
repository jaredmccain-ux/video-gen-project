"""Submit MiniMax H3 video jobs to a running ComfyUI server (stage VII)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .comfy_client import ComfyClient
from .config import ProjectConfig
from .frame_chain import extract_last_frame, last_frame_path
from .state import read_run, utc_now, write_json_atomic
from .validators import load_json


FL2VA_DIFFUSION = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
REF2VA_DIFFUSION = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
TEXT_ENCODER = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"

# Aligned with "MiniMax H3 with Motion Context.json"
DEFAULT_WIDTH = 864
DEFAULT_HEIGHT = 480
DEFAULT_STEPS = 8
DEFAULT_CONTEXT_LENGTH = "22"
DEFAULT_AUDIO_CONTEXT_LENGTH = 24


def frames_for_duration(duration_s: float, fps: int = 24) -> int:
    """Snap duration UP to MiniMax H3's 17n+5 frame grid at 24fps.

    Matches the Motion Context reference graph:
    max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17
    """
    target = max(5, int(round(float(duration_s) * fps)))
    return int(target + (5 - (target % 17)) % 17)


def sampled_frames(duration_s: float, *, context_frames: int = 0, fps: int = 24) -> int:
    """Sample length so the delivered clip stays near duration_s after a head trim."""
    target = max(5, int(round(float(duration_s) * fps)) + max(0, int(context_frames)))
    return int(target + (5 - (target % 17)) % 17)


def shot_clip_index(shot_id: str) -> int:
    digits = "".join(char for char in str(shot_id) if char.isdigit())
    return int(digits) if digits else 0


def context_folder_for_run(run_dir: Path) -> str:
    return f"short_drama/{run_dir.name}"


def context_prefix_for_run(run_dir: Path) -> str:
    return f"{context_folder_for_run(run_dir)}/h3_context"


def canvas_size(config: ProjectConfig, *, aspect_ratio: str | None = None) -> tuple[int, int]:
    cfg = config.data.get("comfyui") or {}
    if cfg.get("width") and cfg.get("height"):
        return int(cfg["width"]), int(cfg["height"])
    ratio = aspect_ratio or str(config.data.get("aspect_ratio") or "16:9")
    if ratio == "9:16":
        return DEFAULT_HEIGHT, DEFAULT_WIDTH
    return DEFAULT_WIDTH, DEFAULT_HEIGHT


def comfy_steps(config: ProjectConfig) -> int:
    cfg = config.data.get("comfyui") or {}
    return int(cfg.get("steps") or DEFAULT_STEPS)


def motion_context_enabled(config: ProjectConfig) -> bool:
    cfg = config.data.get("comfyui") or {}
    return bool(cfg.get("motion_context", True))


def context_window(config: ProjectConfig) -> tuple[str, int]:
    cfg = config.data.get("comfyui") or {}
    return (
        str(cfg.get("context_length") or DEFAULT_CONTEXT_LENGTH),
        int(cfg.get("audio_context_length") or DEFAULT_AUDIO_CONTEXT_LENGTH),
    )


def configured_text_encoder(config: ProjectConfig) -> str:
    cfg = config.data.get("comfyui") or {}
    return str(cfg.get("text_encoder") or TEXT_ENCODER)


def build_h3_workflow(
    *,
    prompt: str,
    width: int,
    height: int,
    length: int,
    seed: int,
    steps: int = DEFAULT_STEPS,
    filename_prefix: str,
    generation_mode: str | None = None,
    first_frame_name: str | None = None,
    last_frame_name: str | None = None,
    ref_image_names: list[str] | None = None,
    ref_video_names: list[str] | None = None,
    ref_audio_names: list[str] | None = None,
    ref_image_size: str = "match",
    fl2va_diffusion: str = FL2VA_DIFFUSION,
    ref2va_diffusion: str = REF2VA_DIFFUSION,
    text_encoder: str = TEXT_ENCODER,
    shift_video: float = 12.0,
    shift_audio: float = 3.0,
    context_folder: str | None = None,
    context_clip_index: int | None = None,
    save_clip_index: int | None = None,
    context_length: str = DEFAULT_CONTEXT_LENGTH,
    audio_context_length: int = DEFAULT_AUDIO_CONTEXT_LENGTH,
) -> dict[str, Any]:
    """Build an API-format ComfyUI graph aligned with Motion Context chaining.

    Clip 1: stock H3 condition → sample → save latent → decode → video.
    Later clips: LoadLatent(prev) → Motion Context → sample → save → trim → video.
    """
    ref_image_names = ref_image_names or []
    ref_video_names = ref_video_names or []
    ref_audio_names = ref_audio_names or []
    mode = generation_mode or (
        "ref2va"
        if ref_image_names or ref_video_names or ref_audio_names
        else "first_last_frame"
        if first_frame_name and last_frame_name
        else "first_frame"
        if first_frame_name
        else "t2va"
    )
    if mode == "ref2va" and not (ref_image_names or ref_video_names or ref_audio_names):
        raise ValueError("ref2va 至少需要一项图片、视频或音频参考")
    if mode == "first_last_frame" and not (first_frame_name and last_frame_name):
        raise ValueError("first_last_frame 同时需要 first_frame 和 last_frame")
    if mode == "first_frame" and not first_frame_name:
        mode = "t2va"
    if mode not in {"t2va", "first_frame", "first_last_frame", "ref2va"}:
        raise ValueError(f"未知 H3 generation_mode：{mode}")

    use_context = bool(context_folder and context_clip_index and int(context_clip_index) > 0)
    diffusion = ref2va_diffusion if mode == "ref2va" else fl2va_diffusion
    condition_node: dict[str, Any]
    if mode == "ref2va":
        condition_node = {
            "class_type": "MiniMaxH3ReferenceToVideo",
            "inputs": {
                "clip": ["2", 0],
                "vae": ["3", 0],
                "audio_vae": ["4", 0],
                "prompt": prompt,
                "width": width,
                "height": height,
                "length": length,
                "ref_image_size": ref_image_size,
            },
        }
    else:
        condition_node = {
            "class_type": "MiniMaxH3ImageToVideo",
            "inputs": {
                "clip": ["2", 0],
                "vae": ["3", 0],
                "prompt": prompt,
                "width": width,
                "height": height,
                "length": length,
            },
        }

    cond_source = ["71", 0] if use_context else ["10", 0]
    image_source = ["72", 0] if use_context else ["40", 0]
    audio_source = ["72", 1] if use_context else ["41", 0]
    workflow: dict[str, Any] = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": diffusion, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": text_encoder, "type": "minimax"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VIDEO_VAE}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": AUDIO_VAE}},
        "5": {
            "class_type": "MiniMaxH3SigmaShift",
            "inputs": {"model": ["1", 0], "shift_video": shift_video, "shift_audio": shift_audio},
        },
        "10": condition_node,
        "20": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "21": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "22": {
            "class_type": "BasicScheduler",
            "inputs": {"model": ["5", 0], "scheduler": "simple", "steps": int(steps), "denoise": 1.0},
        },
        "23": {"class_type": "BasicGuider", "inputs": {"model": ["5", 0], "conditioning": cond_source}},
        "30": {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["20", 0],
                "guider": ["23", 0],
                "sampler": ["21", 0],
                "sigmas": ["22", 0],
                "latent_image": ["10", 1],
            },
        },
        "40": {"class_type": "VAEDecode", "inputs": {"samples": ["30", 0], "vae": ["3", 0]}},
        "41": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["30", 0], "vae": ["4", 0]}},
        "50": {"class_type": "CreateVideo", "inputs": {"images": image_source, "fps": 24.0, "audio": audio_source}},
        "60": {
            "class_type": "SaveVideo",
            "inputs": {
                "video": ["50", 0],
                "filename_prefix": filename_prefix,
                "format": "mp4",
                "codec": "h264",
            },
        },
    }
    if first_frame_name:
        workflow["11"] = {"class_type": "LoadImage", "inputs": {"image": first_frame_name}}
        workflow["10"]["inputs"]["first_frame"] = ["11", 0]
    if last_frame_name:
        workflow["12"] = {"class_type": "LoadImage", "inputs": {"image": last_frame_name}}
        workflow["10"]["inputs"]["last_frame"] = ["12", 0]
    for index, image_name in enumerate(ref_image_names):
        node_id = str(110 + index)
        workflow[node_id] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        workflow["10"]["inputs"][f"ref_images.ref_image_{index}"] = [node_id, 0]
    for index, video_name in enumerate(ref_video_names):
        load_id = str(210 + index * 2)
        components_id = str(211 + index * 2)
        workflow[load_id] = {"class_type": "LoadVideo", "inputs": {"file": video_name}}
        workflow[components_id] = {"class_type": "GetVideoComponents", "inputs": {"video": [load_id, 0]}}
        workflow["10"]["inputs"][f"ref_videos.ref_video_{index}"] = [components_id, 0]
    for index, audio_name in enumerate(ref_audio_names):
        node_id = str(310 + index)
        workflow[node_id] = {"class_type": "LoadAudio", "inputs": {"audio": audio_name}}
        workflow["10"]["inputs"][f"ref_audios.ref_audio_{index}"] = [node_id, 0]
    if use_context:
        workflow["70"] = {
            "class_type": "MiniMaxH3MotionContextLoadLatent",
            "inputs": {
                "latent_path": context_folder,
                "clip_index": int(context_clip_index),
            },
        }
        workflow["71"] = {
            "class_type": "MiniMaxH3MotionContext",
            "inputs": {
                "conditioning": ["10", 0],
                "vae": ["3", 0],
                "latent": ["10", 1],
                "context_length": str(context_length),
                "audio_context_length": int(audio_context_length),
                "context_latent": ["70", 0],
            },
        }
        workflow["72"] = {
            "class_type": "MiniMaxH3MotionContextTrim",
            "inputs": {
                "images": ["40", 0],
                "audio": ["41", 0],
                "trim_frames": ["71", 1],
                "fps": 24.0,
                "match_tail": True,
            },
        }
    if save_clip_index:
        workflow["73"] = {
            "class_type": "MiniMaxH3MotionContextSaveLatent",
            "inputs": {
                "latent": ["30", 0],
                "filename_prefix": f"{context_folder or 'h3_context'}/h3_context",
                "clip_index": int(save_clip_index),
            },
        }
    return workflow


def _pick_video_from_history(history: dict[str, Any]) -> tuple[str, str]:
    outputs = history.get("outputs", {})
    for node_output in outputs.values():
        for key in ("videos", "gifs", "images"):
            items = node_output.get(key) or []
            for item in items:
                filename = item.get("filename")
                if filename and str(filename).lower().endswith((".mp4", ".webm", ".mkv")):
                    return str(filename), str(item.get("subfolder") or "")
                if filename and key == "videos":
                    return str(filename), str(item.get("subfolder") or "")
    raise RuntimeError(f"历史结果中未找到视频文件：{json.dumps(outputs)[:1000]}")


def generate_run_videos(run_dir: Path, config: ProjectConfig) -> Path:
    """Generate videos for ready H3 prompts with last-frame chaining."""
    run_dir = run_dir.resolve()
    report_path = run_dir / "04_prompts/validation_report.json"
    if not report_path.is_file():
        raise FileNotFoundError("请先执行 render-prompts")
    prompt_report = load_json(report_path)
    shots_doc = load_json(run_dir / "03_shots/shots.json")
    shots = {shot["shot_id"]: shot for shot in shots_doc["shots"]}

    comfy_cfg = config.data.get("comfyui") or {}
    base_url = str(comfy_cfg.get("base_url") or "http://127.0.0.1:6006")
    steps = comfy_steps(config)
    fl2va_diffusion = str(
        comfy_cfg.get("fl2va_checkpoint") or FL2VA_DIFFUSION
    )
    ref2va_diffusion = str(
        comfy_cfg.get("ref2va_checkpoint") or REF2VA_DIFFUSION
    )
    text_encoder = configured_text_encoder(config)
    context_len, audio_context_len = context_window(config)
    use_motion = motion_context_enabled(config)
    client = ComfyClient(base_url)
    client.system_stats()  # fail fast if server is down
    modes = {
        item.get("generation_mode") for item in prompt_report.get("shots", [])
    }
    if "ref2va" in modes:
        ref_node = client.object_info("MiniMaxH3ReferenceToVideo")
        if "MiniMaxH3ReferenceToVideo" not in ref_node:
            raise RuntimeError(
                "当前 ComfyUI 不包含 MiniMaxH3ReferenceToVideo，请更新 ComfyUI。"
            )
    if use_motion:
        motion_info = client.object_info("MiniMaxH3MotionContext")
        if "MiniMaxH3MotionContext" not in motion_info:
            raise RuntimeError(
                "当前 ComfyUI 不包含 MiniMaxH3MotionContext，请安装 "
                "custom_nodes/ComfyUI-H3-Motion-Context 后重启。"
            )
    loader_info = client.object_info("UNETLoader").get("UNETLoader", {})
    available = (
        loader_info.get("input", {})
        .get("required", {})
        .get("unet_name", [[]])[0]
    )
    required_models: set[str] = set()
    if modes & {"t2va", "first_frame", "first_last_frame"}:
        required_models.add(fl2va_diffusion)
    if "ref2va" in modes:
        required_models.add(ref2va_diffusion)
    missing_models = sorted(required_models - set(available))
    if missing_models:
        raise RuntimeError(
            "ComfyUI 缺少 diffusion checkpoint："
            + ", ".join(missing_models)
            + "。请下载到 ComfyUI/models/diffusion_models 后重启 ComfyUI。"
        )
    clip_info = client.object_info("CLIPLoader").get("CLIPLoader", {})
    available_clips = (
        clip_info.get("input", {})
        .get("required", {})
        .get("clip_name", [[]])[0]
    )
    if text_encoder not in set(available_clips):
        raise RuntimeError(f"ComfyUI 缺少文本编码器：{text_encoder}")

    aspect = str((prompt_report.get("target") or {}).get("aspect_ratio") or config.data.get("aspect_ratio") or "16:9")
    width, height = canvas_size(config, aspect_ratio=aspect)

    video_dir = run_dir / "05_videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    jobs_path = video_dir / "jobs.json"
    jobs: dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "comfyui": base_url,
        "shots": {},
    }
    if jobs_path.is_file():
        jobs = load_json(jobs_path)

    for item in prompt_report["shots"]:
        shot_id = item["shot_id"]
        shot = shots[shot_id]
        out_video = video_dir / f"{shot_id}.mp4"
        out_frame = last_frame_path(run_dir, shot_id)
        existing = jobs.get("shots", {}).get(shot_id, {})
        if out_video.is_file() and out_video.stat().st_size > 0 and existing.get("status") == "completed":
            continue

        first_frame = item.get("first_frame_path") or item.get("condition_path")
        last_frame = item.get("last_frame_path")
        reference_images = list(item.get("reference_images") or [])
        required_images = [
            value for value in [first_frame, last_frame, *reference_images] if value
        ]
        missing_images = [
            value for value in required_images if not Path(value).is_file()
        ]

        # Never continue a chain from a failed/incomplete upstream shot even if a
        # stale last_frame.png remains on disk from a previous successful run.
        depends_on = item.get("depends_on") or shot.get("depends_on")
        upstream_block_reason = None
        if depends_on:
            upstream = jobs.get("shots", {}).get(depends_on) or {}
            upstream_status = upstream.get("status")
            upstream_frame = last_frame_path(run_dir, depends_on)
            uses_upstream_frame = False
            for path_value in [first_frame, *reference_images]:
                if path_value and Path(path_value).resolve() == upstream_frame.resolve():
                    uses_upstream_frame = True
                    break
            if uses_upstream_frame or shot.get("generation_mode") in {
                "first_frame",
                "first_last_frame",
            }:
                if upstream_status != "completed":
                    upstream_block_reason = (
                        f"依赖镜头 {depends_on} 尚未成功完成"
                        f"（status={upstream_status or 'missing'}），拒绝使用可能陈旧的末帧"
                    )
                elif not upstream_frame.is_file():
                    upstream_block_reason = f"依赖镜头 {depends_on} 缺少 last_frame.png"

        if missing_images or upstream_block_reason:
            jobs.setdefault("shots", {})[shot_id] = {
                "status": "blocked_dependency",
                "reason": upstream_block_reason
                or item.get("reason")
                or f"缺少条件图：{', '.join(missing_images)}",
                "updated_at": utc_now(),
            }
            write_json_atomic(jobs_path, jobs)
            continue

        try:
            prompt_text = (run_dir / item["prompt"]).read_text(encoding="utf-8").strip()
            request = load_json(run_dir / item["request"])
            seed = int(request.get("seed") or 2101)
            prev_id = str(item.get("depends_on") or shot.get("depends_on") or "") or None
            context_clip = None
            if use_motion and prev_id:
                prev_job = jobs.get("shots", {}).get(prev_id) or {}
                if prev_job.get("status") == "completed":
                    context_clip = shot_clip_index(prev_id)
            context_frames = int(context_len) if context_clip else 0
            length = sampled_frames(shot["duration_s"], context_frames=context_frames)
            mode = shot["generation_mode"]
            if context_clip and first_frame:
                prev_frame = last_frame_path(run_dir, prev_id)
                if Path(first_frame).resolve() == prev_frame.resolve():
                    first_frame = None
                    if mode == "first_frame":
                        mode = "t2va"

            def upload(path_value: str, role: str, index: int = 0) -> str:
                path = Path(path_value)
                remote_name = (
                    f"{run_dir.name}_{shot_id}_{role}_{index}{path.suffix.lower() or '.png'}"
                )
                return client.upload_image(path, remote_name=remote_name)

            first_frame_name = upload(first_frame, "first") if first_frame else None
            last_frame_name = upload(last_frame, "last") if last_frame else None
            ref_image_names = [
                upload(path, "ref", ref_index)
                for ref_index, path in enumerate(reference_images, start=1)
            ]

            workflow = build_h3_workflow(
                prompt=prompt_text,
                width=width,
                height=height,
                length=length,
                seed=seed,
                steps=steps,
                filename_prefix=f"short_drama/{run_dir.name}/{shot_id}",
                generation_mode=mode,
                first_frame_name=first_frame_name,
                last_frame_name=last_frame_name,
                ref_image_names=ref_image_names,
                ref_image_size=str(item.get("ref_image_size") or "match"),
                fl2va_diffusion=fl2va_diffusion,
                ref2va_diffusion=ref2va_diffusion,
                text_encoder=text_encoder,
                shift_video=float(request.get("flow_shift") or 12.0),
                shift_audio=float(request.get("audio_flow_shift") or 3.0),
                context_folder=context_folder_for_run(run_dir) if use_motion else None,
                context_clip_index=context_clip,
                save_clip_index=shot_clip_index(shot_id) if use_motion else None,
                context_length=context_len,
                audio_context_length=audio_context_len,
            )
            prompt_id = client.queue_prompt(workflow)
            jobs.setdefault("shots", {})[shot_id] = {
                "status": "running",
                "prompt_id": prompt_id,
                "length": length,
                "seed": seed,
                "generation_mode": shot["generation_mode"],
                "first_frame": first_frame,
                "last_frame": last_frame,
                "reference_images": reference_images,
                "started_at": utc_now(),
            }
            write_json_atomic(jobs_path, jobs)

            history = client.wait_history(prompt_id)
            filename, subfolder = _pick_video_from_history(history)
            video_bytes = client.download_view(filename=filename, subfolder=subfolder, folder_type="output")
            out_video.write_bytes(video_bytes)
            extract_last_frame(out_video, out_frame)

            jobs["shots"][shot_id] = {
                "status": "completed",
                "prompt_id": prompt_id,
                "length": length,
                "seed": seed,
                "video": str(out_video),
                "last_frame": str(out_frame),
                "remote_filename": filename,
                "completed_at": utc_now(),
            }
            write_json_atomic(jobs_path, jobs)

            for dependent in prompt_report["shots"]:
                if dependent.get("depends_on") == shot_id:
                    if dependent.get("generation_mode") in {
                        "first_frame",
                        "first_last_frame",
                    }:
                        dependent["condition_path"] = str(out_frame)
                        dependent["first_frame_path"] = str(out_frame)
                    pending = [
                        path
                        for path in [
                            dependent.get("first_frame_path"),
                            dependent.get("last_frame_path"),
                            *(dependent.get("reference_images") or []),
                        ]
                        if path and not Path(path).is_file()
                    ]
                    dependent["status"] = "ready" if not pending else "blocked_dependency"
                    dependent["reason"] = (
                        None
                        if not pending
                        else "仍缺少条件图：" + ", ".join(pending)
                    )
                    if not pending and dependent["shot_id"] in prompt_report.get("blocked", []):
                        prompt_report["blocked"] = [
                            x for x in prompt_report["blocked"] if x != dependent["shot_id"]
                        ]
                        prompt_report.setdefault("ready", []).append(dependent["shot_id"])
            write_json_atomic(report_path, prompt_report)
        except Exception as exc:  # noqa: BLE001 — isolate per-shot Comfy failures
            jobs.setdefault("shots", {})[shot_id] = {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "generation_mode": shot.get("generation_mode"),
                "first_frame": first_frame,
                "last_frame": last_frame,
                "reference_images": reference_images,
                "updated_at": utc_now(),
            }
            write_json_atomic(jobs_path, jobs)
            # Prevent dependents from consuming a stale last frame from a prior success.
            if out_frame.is_file():
                stale = out_frame.with_suffix(out_frame.suffix + ".stale")
                out_frame.replace(stale)
            for dependent in prompt_report["shots"]:
                if dependent.get("depends_on") == shot_id:
                    dependent["status"] = "blocked_dependency"
                    dependent["reason"] = f"依赖镜头 {shot_id} 生成失败"
                    if dependent["shot_id"] in prompt_report.get("ready", []):
                        prompt_report["ready"] = [
                            x for x in prompt_report["ready"] if x != dependent["shot_id"]
                        ]
                        prompt_report.setdefault("blocked", []).append(dependent["shot_id"])
            write_json_atomic(report_path, prompt_report)
            continue

    statuses = [item.get("status") for item in jobs.get("shots", {}).values()]
    if any(status == "failed" for status in statuses):
        run_state = "VIDEOS_PARTIAL"
    elif any(status == "blocked_dependency" for status in statuses):
        run_state = "VIDEOS_BLOCKED"
    else:
        run_state = "VIDEOS_GENERATED"
    state = read_run(run_dir)
    state.update({"state": run_state, "updated_at": utc_now(), "video_jobs": str(jobs_path)})
    write_json_atomic(run_dir / "run.json", state)
    return jobs_path
