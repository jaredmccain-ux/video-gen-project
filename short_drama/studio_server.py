"""Local, dependency-free web studio API for human-directed H3 generation."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import threading
import traceback
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .comfy_client import ComfyClient
from .config import ProjectConfig, load_config
from .azure_client import completion_text, create_multimodal_completion, create_text_completion
from .frame_chain import extract_last_frame
from .h3_jobs import (
    AUDIO_VAE,
    FL2VA_DIFFUSION,
    REF2VA_DIFFUSION,
    VIDEO_VAE,
    build_h3_workflow,
    canvas_size,
    comfy_steps,
    configured_text_encoder,
    context_folder_for_run,
    context_window,
    motion_context_enabled,
    sampled_frames,
    shot_clip_index,
)
from .human_orchestration import (
    AUDIO_SUFFIXES,
    H3_INPUT_LIMITS,
    IMAGE_SUFFIXES,
    LIBRARY_UPLOAD_LIMITS,
    VIDEO_SUFFIXES,
    effective_picture_bindings,
    list_run_assets,
    load_orchestration,
    media_type_for,
    normalize_mode,
    recover_generated_image_bindings,
    save_decision,
    save_uploaded_data_url,
    save_uploaded_stream,
    validate_decision,
)
from .h3_prompt import render_studio_prompt
from .image_generator import generate_images, image_generator_config, image_generator_enabled, save_generated_image
from .inspiration import (
    generate_story_inspiration,
    load_inspiration,
    select_inspiration_images,
)
from .state import default_run_id, initialize_run, read_run, utc_now, write_json_atomic
from .studio_finish import (
    align_subtitles_to_speech,
    assemble_studio_run,
    burn_studio_subtitles,
    collect_shot_videos,
    final_status,
    generate_studio_subtitles,
    load_subtitles,
    prompt_review,
    reset_subtitle_style,
    save_subtitle_cues,
)
from .studio_workflow import approve_stage, generate_stage, revise_story, save_stage_document, workflow_snapshot
from .validators import load_json


MAX_JSON_BYTES = 16 * 1024 * 1024
UI_BUILD = "20260819-10"


def _safe_console(message: str) -> None:
    """Keep a detached Studio's closed stdout from breaking HTTP requests."""
    try:
        print(message, flush=True)
    except (BrokenPipeError, OSError, ValueError):
        pass


def _pick_video_from_history(history: dict[str, Any]) -> tuple[str, str]:
    for node_output in history.get("outputs", {}).values():
        for key in ("videos", "gifs", "images"):
            for item in node_output.get(key) or []:
                filename = str(item.get("filename") or "")
                if filename.lower().endswith(tuple(VIDEO_SUFFIXES)) or (filename and key == "videos"):
                    return filename, str(item.get("subfolder") or "")
    raise RuntimeError("ComfyUI 历史结果中未找到视频文件")


def _available_models(client: ComfyClient, node_type: str, input_name: str) -> set[str]:
    node = client.object_info(node_type).get(node_type, {})
    values = node.get("input", {}).get("required", {}).get(input_name, [[]])[0]
    return {str(value) for value in values}


def _media_probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,sample_rate,channels,width,height,r_frame_rate",
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    info = json.loads(result.stdout or "{}")
    return {
        "duration_s": round(float((info.get("format") or {}).get("duration") or 0), 3),
        "streams": info.get("streams") or [],
    }


def _prepare_prompt_media_previews(
    run_dir: Path,
    shot_id: str,
    video_bindings: list[dict[str, Any]],
    audio_bindings: list[dict[str, Any]],
    video_sample_counts: list[int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Path], list[str]]:
    """Materialize labeled visual summaries for the configured vision LLM.

    The configured chat model consumes images, not raw local video/audio files.
    Temporal samples preserve video continuity context; a waveform plus probe
    metadata makes each audio input visible and unambiguous to the LLM. The
    sampled video frames are durable H3 picture inputs: the original video is
    retained for preview but is deliberately not submitted as a video latent.
    """
    output_dir = run_dir / "inputs/studio_prompt_previews" / shot_id
    output_dir.mkdir(parents=True, exist_ok=True)
    video_inputs: list[dict[str, Any]] = []
    audio_inputs: list[dict[str, Any]] = []
    preview_paths: list[Path] = []
    preview_labels: list[str] = []
    sample_counts = video_sample_counts or [3] * len(video_bindings)
    ratios_by_count = {1: (0.9,), 2: (0.35, 0.9), 3: (0.15, 0.55, 0.9)}
    for index, binding in enumerate(video_bindings, start=1):
        source = Path(str(binding["path"]))
        metadata = _media_probe(source)
        duration = max(float(metadata.get("duration_s") or 0), 0.1)
        frames: list[str] = []
        frame_paths: list[str] = []
        sample_ratios = ratios_by_count[max(1, min(3, int(sample_counts[index - 1])))]
        for sample_index, ratio in enumerate(sample_ratios, start=1):
            output = output_dir / f"video_{index:02d}_sample_{sample_index}.png"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{duration * ratio:.3f}", "-i", str(source),
                    "-frames:v", "1", str(output),
                ],
                check=True,
            )
            frames.append(output.name)
            frame_paths.append(str(output.resolve()))
            preview_paths.append(output)
            preview_labels.append(
                f"这是参考视频 {index} 的第 {sample_index}/{len(sample_ratios)} 张时间采样帧"
                f"（约 {ratio * 100:.0f}% 位置）；人工用途为 {binding.get('usage') or 'motion'}。"
            )
        video_inputs.append(
            {
                "source_video": f"参考视频 {index}",
                "file": source.name,
                "usage": binding.get("usage") or "motion",
                "note": binding.get("note") or "",
                "metadata": metadata,
                "preview_frames": frames,
                "sample_paths": frame_paths,
                "sample_ratios": list(sample_ratios),
            }
        )
    for index, binding in enumerate(audio_bindings, start=1):
        source = Path(str(binding["path"]))
        metadata = _media_probe(source)
        waveform = output_dir / f"audio_{index:02d}_waveform.png"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                "-filter_complex", "showwavespic=s=960x240:colors=2f8f6b", "-frames:v", "1", str(waveform),
            ],
            check=True,
        )
        preview_paths.append(waveform)
        preview_labels.append(
            f"这是 <Audio {index}> 的完整波形概览；人工用途为 {binding.get('usage') or 'soundscape'}，"
            "请结合时长、声道和人工备注规划声音使用，不得凭波形虚构台词。"
        )
        audio_inputs.append(
            {
                "audio": f"<Audio {index}>",
                "file": source.name,
                "usage": binding.get("usage") or "soundscape",
                "note": binding.get("note") or "",
                "metadata": metadata,
                "waveform": waveform.name,
            }
        )
    return video_inputs, audio_inputs, preview_paths, preview_labels


class GenerationManager:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-h3")
        self.lock = threading.Lock()

    def _jobs_path(self, run_dir: Path) -> Path:
        return run_dir / "05_videos/studio_jobs.json"

    def load_jobs(self, run_dir: Path) -> dict[str, Any]:
        path = self._jobs_path(run_dir)
        if path.is_file():
            return load_json(path)
        return {"schema_version": "1.0", "updated_at": None, "jobs": {}}

    @staticmethod
    def public_job(job: dict[str, Any]) -> dict[str, Any]:
        """Return UI-safe job data, including concise legacy error messages."""
        result = dict(job)
        error = str(result.get("error") or "")
        if "torch.OutOfMemoryError" in error or "Allocation on device" in error or "out of memory on your GPU" in error:
            result["error"] = "GPU 显存不足；已调整 ComfyUI 的模型卸载策略，请重新生成本镜头"
        elif len(error) > 1200:
            result["error"] = error[:1200].rstrip() + "…"
        result.pop("traceback", None)
        return result

    def _update_job(self, run_dir: Path, _job_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            document = self.load_jobs(run_dir)
            job = document["jobs"].setdefault(_job_id, {})
            job.update(changes)
            job["updated_at"] = utc_now()
            document["updated_at"] = utc_now()
            write_json_atomic(self._jobs_path(run_dir), document)
            return dict(job)

    def submit(
        self,
        run_dir: Path,
        config: ProjectConfig,
        shot_id: str,
        *,
        expected_prompt: str | None = None,
    ) -> dict[str, Any]:
        decision = load_orchestration(run_dir).get("shots", {}).get(shot_id)
        if not isinstance(decision, dict):
            raise ValueError(f"{shot_id} 尚未保存人工编排")
        decision = validate_decision(
            decision,
            run_dir=run_dir,
            config=config,
            require_approved=True,
        )
        saved_prompt = str(decision.get("prompt") or "").strip()
        if expected_prompt is not None and str(expected_prompt).strip() != saved_prompt:
            raise ValueError(
                f"{shot_id} 页面 Prompt 与本地保存版本不一致；已阻止使用旧 Prompt 生成，请刷新后重试"
            )
        shots_doc = load_json(run_dir / "03_shots/shots.json")
        shot = next((item for item in shots_doc.get("shots", []) if item.get("shot_id") == shot_id), None)
        if shot is None:
            raise ValueError(f"未知镜头：{shot_id}")
        story = load_json(run_dir / "02_story/story.json")
        if decision.get("reference_video_bindings") and not decision.get("video_frame_bindings"):
            raise ValueError(
                f"{shot_id} 的参考视频尚未转换为连续性抽帧；请先点击“重新生成并优化”，"
                "确认新的 Picture 输入后再生成镜头"
            )
        picture_bindings = effective_picture_bindings(decision)
        optimized_prompt, prompt_errors = render_studio_prompt(
            shot,
            story,
            generation_mode=decision["generation_mode"],
            user_prompt=decision["prompt"],
            reference_paths=[item["path"] for item in picture_bindings],
            reference_bindings=picture_bindings,
            # Source videos are never sent as H3 video latents.  Their durable
            # sampled frames are already included in picture_bindings.
            reference_video_bindings=[],
            reference_audio_bindings=decision.get("reference_audio_bindings") or [],
        )
        if prompt_errors:
            raise ValueError("MiniMax H3 官方 Prompt Skill 校验失败：" + "; ".join(prompt_errors))
        if optimized_prompt.strip() != saved_prompt:
            raise ValueError(
                f"{shot_id} 当前 Prompt 不符合所选模式的最终 Skill 结构；"
                "生成阶段不会自动改写，请先点击“重新生成并优化”"
            )
        decision["prompt"] = saved_prompt
        decision["prompt_skill"] = "MiniMax H3 / h3-prompt-writing"
        decision["effective_picture_bindings"] = picture_bindings
        job_id = f"{shot_id}-{uuid.uuid4().hex[:10]}"
        job = {
            "job_id": job_id,
            "shot_id": shot_id,
            "status": "queued",
            "generation_mode": decision["generation_mode"],
            "created_at": utc_now(),
            "decision_revision": decision.get("revision"),
            # Persist the exact text submitted to ComfyUI.  This makes every
            # generated asset auditable even if the shot is edited later.
            "prompt_snapshot": decision["prompt"],
            "prompt_sha256": hashlib.sha256(decision["prompt"].encode("utf-8")).hexdigest(),
            "prompt_skill": decision.get("prompt_skill") or "MiniMax H3 / h3-prompt-writing",
            "prompt_optimized_at": decision.get("prompt_optimized_at"),
            "reference_video_strategy": "sampled_frames",
            "sampled_video_frame_count": len(decision.get("video_frame_bindings") or []),
        }
        self._update_job(run_dir, job_id, **job)
        self.executor.submit(self._run, run_dir, config, job_id, decision)
        return job

    def submit_approved(self, run_dir: Path, config: ProjectConfig) -> list[dict[str, Any]]:
        orchestration = load_orchestration(run_dir)
        approved = [
            shot_id
            for shot_id, item in orchestration.get("shots", {}).items()
            if item.get("approved")
        ]
        if not approved:
            raise ValueError("没有已批准的人工编排镜头")
        return [self.submit(run_dir, config, shot_id) for shot_id in sorted(approved)]

    def _run(
        self,
        run_dir: Path,
        config: ProjectConfig,
        job_id: str,
        decision: dict[str, Any],
    ) -> None:
        shot_id = job_id.split("-", 1)[0]
        try:
            shots_doc = load_json(run_dir / "03_shots/shots.json")
            shots = {shot["shot_id"]: shot for shot in shots_doc.get("shots", [])}
            shot = shots[shot_id]
            comfy_cfg = config.data.get("comfyui") or {}
            client = ComfyClient(str(comfy_cfg.get("base_url") or "http://127.0.0.1:6006"))
            self._update_job(run_dir, job_id, status="preflight", started_at=utc_now())
            client.system_stats()
            fl_model = str(comfy_cfg.get("fl2va_checkpoint") or FL2VA_DIFFUSION)
            ref_model = str(comfy_cfg.get("ref2va_checkpoint") or REF2VA_DIFFUSION)
            text_encoder = configured_text_encoder(config)
            required_model = ref_model if decision["generation_mode"] == "ref2va" else fl_model
            if required_model not in _available_models(client, "UNETLoader", "unet_name"):
                raise RuntimeError(f"ComfyUI 缺少 checkpoint：{required_model}")
            if text_encoder not in _available_models(client, "CLIPLoader", "clip_name"):
                raise RuntimeError(f"ComfyUI 缺少文本编码器：{text_encoder}")
            available_vaes = _available_models(client, "VAELoader", "vae_name")
            missing_vaes = {VIDEO_VAE, AUDIO_VAE} - available_vaes
            if missing_vaes:
                raise RuntimeError(f"ComfyUI 缺少 VAE：{', '.join(sorted(missing_vaes))}")
            if decision["generation_mode"] == "ref2va":
                info = client.object_info("MiniMaxH3ReferenceToVideo")
                if "MiniMaxH3ReferenceToVideo" not in info:
                    raise RuntimeError("当前 ComfyUI 不支持 MiniMaxH3ReferenceToVideo")
                required_nodes = []
                if decision.get("reference_audios"):
                    required_nodes.append("LoadAudio")
                for node_type in required_nodes:
                    if node_type not in client.object_info(node_type):
                        raise RuntimeError(f"当前 ComfyUI 缺少节点：{node_type}")
            use_motion = motion_context_enabled(config)
            if use_motion and "MiniMaxH3MotionContext" not in client.object_info("MiniMaxH3MotionContext"):
                raise RuntimeError(
                    "当前 ComfyUI 不包含 MiniMaxH3MotionContext，请安装 "
                    "custom_nodes/ComfyUI-H3-Motion-Context 后重启"
                )

            def upload(path_value: str | None, role: str, index: int = 0) -> str | None:
                if not path_value:
                    return None
                path = Path(path_value)
                remote = f"studio_{run_dir.name}_{job_id}_{role}_{index}{path.suffix.lower()}"
                return client.upload_image(path, remote_name=remote)

            self._update_job(run_dir, job_id, status="uploading_inputs")
            mode = decision["generation_mode"]
            first_name = upload(decision.get("first_frame"), "first") if mode in {"first_frame", "first_last_frame"} else None
            last_name = upload(decision.get("last_frame"), "last") if mode == "first_last_frame" else None
            picture_bindings = decision.get("effective_picture_bindings") or effective_picture_bindings(decision)
            ref_names = [
                upload(str(item["path"]), "ref", index)
                for index, item in enumerate(picture_bindings, 1)
            ] if mode == "ref2va" else []
            ref_names = [value for value in ref_names if value]

            # Deliberately empty: source videos were sampled while optimizing
            # the prompt, and those PNGs are present in ref_names.  Loading the
            # whole video here would recreate the large temporal latent that
            # caused 24 GB GPUs to run out of memory.
            ref_video_names: list[str] = []
            ref_audio_names = [
                upload(value, "audio", index)
                for index, value in enumerate(decision.get("reference_audios") or [], 1)
            ] if mode == "ref2va" else []
            prev_id = str(shot.get("depends_on") or "") or None
            context_clip = None
            if use_motion and prev_id:
                for job in self.load_jobs(run_dir).get("jobs", {}).values():
                    if job.get("shot_id") == prev_id and job.get("status") == "completed":
                        context_clip = shot_clip_index(prev_id)
                        break
            if context_clip and first_name and decision.get("first_frame"):
                prev_jobs = [
                    job for job in self.load_jobs(run_dir).get("jobs", {}).values()
                    if job.get("shot_id") == prev_id and job.get("status") == "completed" and job.get("last_frame")
                ]
                if prev_jobs and Path(str(decision.get("first_frame"))).resolve() == Path(prev_jobs[-1]["last_frame"]).resolve():
                    first_name = None
                    if mode == "first_frame":
                        mode = "t2va"
            context_len, audio_context_len = context_window(config)
            context_frames = int(context_len) if context_clip else 0
            length = sampled_frames(float(shot["duration_s"]), context_frames=context_frames)
            width, height = canvas_size(config, aspect_ratio=str(config.data.get("aspect_ratio") or "16:9"))
            workflow = build_h3_workflow(
                prompt=decision["prompt"],
                width=width,
                height=height,
                length=length,
                seed=int(decision.get("seed") or 2101),
                steps=comfy_steps(config),
                filename_prefix=f"short_drama/studio/{run_dir.name}/{job_id}",
                generation_mode=mode,
                first_frame_name=first_name,
                last_frame_name=last_name,
                ref_image_names=ref_names,
                ref_video_names=[value for value in ref_video_names if value],
                ref_audio_names=[value for value in ref_audio_names if value],
                ref_image_size=str((config.data.get("identity_consistency") or {}).get("ref_image_size") or "match"),
                fl2va_diffusion=fl_model,
                ref2va_diffusion=ref_model,
                text_encoder=text_encoder,
                context_folder=context_folder_for_run(run_dir) if use_motion else None,
                context_clip_index=context_clip,
                save_clip_index=shot_clip_index(shot_id) if use_motion else None,
                context_length=context_len,
                audio_context_length=audio_context_len,
            )
            prompt_id = client.queue_prompt(workflow)
            self._update_job(
                run_dir,
                job_id,
                status="running",
                prompt_id=prompt_id,
                length=length,
                width=width,
                height=height,
                context_clip_index=context_clip,
                save_clip_index=shot_clip_index(shot_id) if use_motion else None,
            )
            history = client.wait_history(prompt_id)
            filename, subfolder = _pick_video_from_history(history)
            output_dir = run_dir / "05_videos/studio_generations" / shot_id
            output_dir.mkdir(parents=True, exist_ok=True)
            video = output_dir / f"{job_id}.mp4"
            frame = output_dir / f"{job_id}.last_frame.png"
            video.write_bytes(
                client.download_view(filename=filename, subfolder=subfolder, folder_type="output")
            )
            extract_last_frame(video, frame)
            self._update_job(
                run_dir,
                job_id,
                status="completed",
                video=str(video.resolve()),
                last_frame=str(frame.resolve()),
                remote_filename=filename,
                completed_at=utc_now(),
            )
        except Exception as exc:  # noqa: BLE001
            self._update_job(
                run_dir,
                job_id,
                status="failed",
                error_type=type(exc).__name__,
                error=str(exc),
                traceback=traceback.format_exc(limit=8),
            )


class StudioApplication:
    def __init__(self, config: ProjectConfig, studio_dir: Path):
        self.config = config
        self.studio_dir = studio_dir.resolve()
        self.generations = GenerationManager()
        self.inspiration_lock = threading.Lock()
        self.workflow_lock = threading.Lock()
        self.subtitle_lock = threading.Lock()
        self.image_generation_lock = threading.Lock()
        self.prompt_generation_lock = threading.Lock()
        self.project_lock = threading.Lock()

    def resolve_run(self, run_id: str) -> Path:
        if not run_id or Path(run_id).name != run_id:
            raise ValueError("run_id 非法")
        run_dir = (self.config.run_root / run_id).resolve()
        if run_dir.parent != self.config.run_root.resolve() or not (run_dir / "run.json").is_file():
            raise FileNotFoundError(f"run 不存在：{run_id}")
        return run_dir

    def run_config(self, run_dir: Path) -> ProjectConfig:
        """Load project settings while binding to this machine's services.

        Creative settings remain reproducible in the run snapshot.  LLM,
        ComfyUI and image-generation endpoints are runtime infrastructure:
        keeping an old endpoint there makes a migrated or long-lived project
        unusable after providers change.  Those sections therefore come from
        the Studio server's active config without modifying the snapshot.
        """
        snapshot = run_dir / "project.config.yaml"
        config = load_config(snapshot if snapshot.is_file() else self.config.path, require_images=False)
        data = copy.deepcopy(config.data)
        # Do not leave the alternate legacy provider section behind: config
        # selection prefers llm over azure, so both must be replaced as a pair.
        data.pop("llm", None)
        data.pop("azure", None)
        for key in ("llm", "azure", "sglang", "comfyui", "image_generator"):
            if key in self.config.data:
                data[key] = copy.deepcopy(self.config.data[key])
        # Speech-recognition paths are machine infrastructure like the endpoints above,
        # while subtitle styling is a creative choice that stays in the run snapshot.
        live_asr = (self.config.data.get("subtitles") or {}).get("asr")
        if live_asr:
            subtitles = dict(data.get("subtitles") or {})
            subtitles["asr"] = copy.deepcopy(live_asr)
            data["subtitles"] = subtitles

        project_root = config.project_root
        input_images = config.input_images
        if not project_root.exists():
            # Copied projects may retain paths from their source machine.
            project_root = self.config.project_root
            input_images = self.config.input_images
        data["project_root"] = str(project_root)
        data["run_root"] = str(self.config.run_root)
        data["input_images"] = [str(path) for path in input_images]
        return ProjectConfig(
            path=config.path,
            data=data,
            project_root=project_root,
            run_root=self.config.run_root,
            input_images=input_images,
        )

    def runs(self) -> list[dict[str, Any]]:
        result = []
        if not self.config.run_root.is_dir():
            return result
        for run_dir in sorted(self.config.run_root.iterdir(), reverse=True):
            state_path = run_dir / "run.json"
            shots_path = run_dir / "03_shots/shots.json"
            if not state_path.is_file():
                continue
            state = read_run(run_dir)
            result.append(
                {
                    "run_id": run_dir.name,
                    "project_name": state.get("project_name") or run_dir.name,
                    "state": state.get("state"),
                    "created_at": state.get("created_at"),
                    "updated_at": state.get("updated_at"),
                    "shot_count": len(load_json(shots_path).get("shots", [])) if shots_path.is_file() else 0,
                }
            )
        return result

    def create_project(self, project_name: str) -> dict[str, Any]:
        """Create an isolated, durable local SceneFlow project."""
        name = " ".join(str(project_name or "").split())
        if not name:
            raise ValueError("请输入项目名称")
        if len(name) > 80:
            raise ValueError("项目名称不能超过 80 个字符")
        if any(ord(char) < 32 for char in name):
            raise ValueError("项目名称不能包含控制字符")

        data = copy.deepcopy(self.config.data)
        data["project_name"] = name
        project_config = ProjectConfig(
            path=self.config.path,
            data=data,
            project_root=self.config.project_root,
            run_root=self.config.run_root,
            input_images=self.config.input_images,
        )
        with self.project_lock:
            # The timestamp-based default is human-readable. Add a short suffix
            # only for the rare case of two creations in the same second.
            try:
                run_dir = initialize_run(project_config)
            except FileExistsError:
                run_dir = initialize_run(project_config, run_id=f"{default_run_id(name)}-{uuid.uuid4().hex[:6]}")
        return next(item for item in self.runs() if item["run_id"] == run_dir.name)

    def workspace(self, run_dir: Path) -> dict[str, Any]:
        # Reconcile the durable AI generation log before returning browser
        # state. This heals bindings lost to refreshes or interrupted requests.
        recover_generated_image_bindings(run_dir, self.run_config(run_dir))
        shots_path = run_dir / "03_shots/shots.json"
        shots_doc = load_json(shots_path) if shots_path.is_file() else {"shots": []}
        story_path = run_dir / "02_story/story.json"
        story = load_json(story_path) if story_path.is_file() else {}
        orchestration = load_orchestration(run_dir)
        locations = {item.get("location_id"): item for item in story.get("locations", [])}
        output = []
        for shot in shots_doc.get("shots", []):
            shot_id = shot["shot_id"]
            decision = orchestration.get("shots", {}).get(shot_id)
            # 04_prompts may belong to an older shot plan with the same Sxxx IDs.
            # Stage V must always begin from the current shots.json content.
            fallback_prompt = str(
                shot.get("visual_description")
                or shot.get("motion_desc")
                or "；".join(
                    value for value in (
                        str(shot.get("composition") or "").strip(),
                        str(shot.get("action_timeline") or "").strip(),
                    )
                    if value
                )
            )
            try:
                suggested_prompt, prompt_errors = render_studio_prompt(
                    shot,
                    story,
                    generation_mode=str(shot.get("generation_mode") or "t2va"),
                )
                if prompt_errors:
                    raise ValueError("; ".join(prompt_errors))
            except (KeyError, TypeError, ValueError):
                suggested_prompt = fallback_prompt
            scene_id = shot.get("scene_id")
            location = locations.get(scene_id) or {}
            output.append(
                {
                    "shot_id": shot_id,
                    "title": shot.get("story_purpose") or shot.get("visual_description") or shot_id,
                    "scene": location.get("name") or scene_id or "未命名场景",
                    "duration_s": shot.get("duration_s"),
                    "beat_id": shot.get("beat_id"),
                    "suggested_mode": shot.get("generation_mode"),
                    "suggested_prompt": suggested_prompt,
                    "official_prompt": suggested_prompt,
                    "prompt_skill": "MiniMax H3 / h3-prompt-writing",
                    "decision": decision,
                    "characters": shot.get("characters") or [],
                    "composition": shot.get("composition") or "",
                    "camera": shot.get("camera") or "",
                    "action_timeline": shot.get("action_timeline") or "",
                    "continuity_in": shot.get("continuity_in") or "",
                    "continuity_out": shot.get("continuity_out") or "",
                    "dialogue": shot.get("dialogue") or [],
                    "subtitle_text": shot.get("subtitle_text") or "",
                    "audio_contract": shot.get("audio_contract") or {},
                    "source_anchor_image": shot.get("source_anchor_image"),
                    "depends_on": shot.get("depends_on"),
                    "first_frame_desc": shot.get("first_frame_desc") or "",
                    "last_frame_desc": shot.get("last_frame_desc") or "",
                }
            )
        jobs = self.generations.load_jobs(run_dir)
        return {
            "run": read_run(run_dir),
            "story": {"title": story.get("title") or story.get("story_title") or "未命名短剧"},
            "shots": output,
            "orchestration_updated_at": orchestration.get("updated_at"),
            "jobs": [self.generations.public_job(job) for job in jobs.get("jobs", {}).values()],
        }

    def optimize_prompt(
        self,
        run_dir: Path,
        config: ProjectConfig,
        shot_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        shots_doc = load_json(run_dir / "03_shots/shots.json")
        shot = next((item for item in shots_doc.get("shots", []) if item.get("shot_id") == shot_id), None)
        if shot is None:
            raise ValueError(f"未知镜头：{shot_id}")
        story = load_json(run_dir / "02_story/story.json")
        mode = normalize_mode(payload.get("generation_mode"))
        normalized = validate_decision(
            {
                **payload,
                "generation_mode": mode,
                "prompt": str(payload.get("user_prompt") or "待生成"),
                "approved": False,
            },
            run_dir=run_dir,
            config=config,
            require_approved=False,
        )
        usage_labels = {
            "first_frame": "首帧（0.00 秒起始画面）",
            "last_frame": "尾帧（结束画面）",
            "identity": "人物身份",
            "scene": "场景空间",
            "style": "视觉风格",
            "keyframe": "关键帧动作/构图",
        }
        # Source videos are preview-only.  Every optimization pass rebuilds a
        # durable set of sampled stills and those stills become the real H3
        # picture contract.  Never let stale samples from a previous video
        # selection leak into the new prompt.
        normalized["video_frame_bindings"] = []
        manual_bindings = effective_picture_bindings(normalized)
        video_bindings = list(normalized.get("reference_video_bindings") or []) if mode == "ref2va" else []
        audio_bindings = list(normalized.get("reference_audio_bindings") or []) if mode == "ref2va" else []
        available_video_frame_slots = 9 - len(manual_bindings)
        if video_bindings and available_video_frame_slots < len(video_bindings):
            raise ValueError(
                "当前图片已占满 H3 的 9 张输入额度；每个参考视频至少需要 1 个抽帧位置，"
                "请减少图片或参考视频后重试"
            )
        video_sample_counts = [1] * len(video_bindings)
        remaining_slots = available_video_frame_slots - len(video_bindings)
        while remaining_slots > 0 and any(value < 3 for value in video_sample_counts):
            for index in range(len(video_sample_counts)):
                if remaining_slots <= 0:
                    break
                if video_sample_counts[index] < 3:
                    video_sample_counts[index] += 1
                    remaining_slots -= 1
        video_inputs, audio_inputs, media_preview_paths, media_preview_labels = _prepare_prompt_media_previews(
            run_dir,
            shot_id,
            video_bindings,
            audio_bindings,
            video_sample_counts=video_sample_counts,
        )
        video_frame_bindings: list[dict[str, Any]] = []
        for video_index, (binding, video_input) in enumerate(zip(video_bindings, video_inputs), start=1):
            sampled_pictures: list[str] = []
            for sample_index, (sample_path, sample_ratio) in enumerate(
                zip(video_input.get("sample_paths") or [], video_input.get("sample_ratios") or []),
                start=1,
            ):
                picture_number = len(manual_bindings) + len(video_frame_bindings) + 1
                sampled_pictures.append(f"<Picture {picture_number}>")
                video_frame_bindings.append(
                    {
                        "path": str(sample_path),
                        "usage": "keyframe",
                        "character_ids": [],
                        "note": (
                            f"参考视频 {video_index} 的第 {sample_index} 张连续性抽帧（约 {float(sample_ratio) * 100:.0f}% 位置）；"
                            f"源视频人工用途为 {binding.get('usage') or 'continuity'}"
                            + (f"；人工说明：{binding.get('note')}" if binding.get("note") else "")
                        ),
                        "source_video": str(binding["path"]),
                        "source_video_index": video_index,
                        "sample_ratio": float(sample_ratio),
                    }
                )
            video_input["sampled_pictures"] = sampled_pictures

        normalized["video_frame_bindings"] = video_frame_bindings
        normalized["reference_video_strategy"] = "sampled_frames"
        active_bindings = effective_picture_bindings(normalized)
        image_inputs: list[dict[str, Any]] = []
        image_paths: list[Path] = []
        image_labels: list[str] = []
        for index, item in enumerate(active_bindings, start=1):
            usages = item.get("usages") or [item.get("usage") or "scene"]
            usage_names = [usage_labels.get(str(value), str(value)) for value in usages]
            image_paths.append(Path(str(item["path"])))
            image_labels.append(
                f"这是 <Picture {index}>；人工指定用途为 {' + '.join(usage_names)}。"
            )
            image_inputs.append(
                {
                    "picture": f"<Picture {index}>",
                    "file": Path(str(item["path"])).name,
                    "type": " + ".join(usage_names),
                    "usages": list(usages),
                    "purpose": item.get("note") or "；".join(usage_names),
                    "character_ids": item.get("character_ids") or [],
                }
            )
        sampled_video_frame_count = sum(video_sample_counts)
        audio_preview_paths = media_preview_paths[sampled_video_frame_count:]
        audio_preview_labels = media_preview_labels[sampled_video_frame_count:]
        director_note = str(payload.get("user_prompt") or "").strip()
        if any(field in director_note for field in ("integrated_multimodal_description:", "subject_definitions:", "detailed_description:")):
            director_note = ""
        system_prompt = (
            "你是短剧视频生成的镜头提示词导演。请先根据分镜剧本、当前生成模式，以及每张图片被人工指定的类型和作用、"
            "每个源视频和参考音频被人工指定的用途，写一份简体中文的中间导演稿。必须明确开场、连续动作、末帧、机位、人物连续性，"
            "并逐项合理使用所有已提供的 <Picture i>、<Audio j>；场景图、风格图和关键帧图不得擅自当成人物身份图。不得改变原对白，不得增加"
            "旁白、画外人声、字幕、BGM、切镜或额外人物。只返回连续的导演说明正文，不要 JSON、Markdown、"
            "字段标题、分析过程，也不要输出 MiniMax 官方字段名。只能使用 human_image_inputs、source_video_inputs、"
            "human_audio_inputs 中列出的素材，严格按照编号和人工用途应用；不得提及或推断未列出的素材。源视频本身不会提交给 H3，"
            "source_video_inputs.sampled_pictures 已把源视频转换为实际提交的静态帧；必须按其用途引用这些 <Picture i>，"
            "严禁输出任何 <Video k>。音频波形只用于理解结构；不得根据波形虚构台词或声音内容。"
        )
        user_text = (
            "请生成后续 MiniMax H3 官方 Skill 的中间导演稿。\n"
            + json.dumps(
                {
                    "generation_mode": mode,
                    "shot_script": {
                        "shot_id": shot.get("shot_id"),
                        "duration_s": shot.get("duration_s"),
                        "story_purpose": shot.get("story_purpose"),
                        "composition": shot.get("composition"),
                        "camera": shot.get("camera"),
                        "action_timeline": shot.get("action_timeline"),
                        "first_frame_desc": shot.get("first_frame_desc"),
                        "last_frame_desc": shot.get("last_frame_desc"),
                        "dialogue": shot.get("dialogue") or [],
                        "characters": [
                            {
                                "character_id": character.get("character_id"),
                                "name": character.get("name"),
                                "identity": character.get("identity"),
                                "appearance": character.get("appearance"),
                            }
                            for character in (story.get("characters") or [])
                            if character.get("character_id") in (shot.get("characters") or [])
                        ],
                        "continuity_in": shot.get("continuity_in"),
                        "continuity_out": shot.get("continuity_out"),
                    },
                    "story_context": {
                        "title": story.get("title") or story.get("story_title"),
                        "style_bible": story.get("style_bible"),
                    },
                    "human_image_inputs": image_inputs,
                    "source_video_inputs": video_inputs,
                    "human_audio_inputs": audio_inputs,
                    "human_director_note": director_note,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        with self.prompt_generation_lock:
            # Sampled video frames are already part of image_paths.  Do not
            # send the same pixels to the vision LLM twice.
            visual_paths = [*image_paths, *audio_preview_paths]
            visual_labels = [*image_labels, *audio_preview_labels]
            if visual_paths:
                response = create_multimodal_completion(
                    config,
                    system_prompt=system_prompt,
                    user_text=user_text,
                    image_paths=visual_paths,
                    image_labels=visual_labels,
                    max_completion_tokens=4096,
                )
            else:
                response = create_text_completion(
                    config,
                    system_prompt=system_prompt,
                    user_text=user_text,
                    max_completion_tokens=4096,
                )
        llm_draft = completion_text(response).strip()
        if not llm_draft:
            raise ValueError("LLM 没有返回可用于 Skill 优化的镜头稿")
        allowed_image_ids = {
            token.upper()
            for item in image_inputs
            for token in re.findall(r"\bIMG\d+\b", str(item.get("file") or ""), flags=re.IGNORECASE)
        }
        mentioned_image_ids = {
            token.upper()
            for token in re.findall(r"\bIMG\d+\b", llm_draft, flags=re.IGNORECASE)
        }
        unexpected_image_ids = sorted(mentioned_image_ids - allowed_image_ids)
        if unexpected_image_ids:
            raise ValueError(
                "LLM 引用了当前镜头未选中的图片："
                + "、".join(unexpected_image_ids)
                + "；已拒绝生成错误 Prompt，请重新尝试"
            )
        mentioned_videos = sorted({int(value) for value in re.findall(r"<Video\s+(\d+)>", llm_draft)})
        if mentioned_videos:
            raise ValueError(
                "参考视频已采用抽帧模式，LLM 不应输出 Video 标签："
                + "、".join(f"<Video {index}>" for index in mentioned_videos)
            )
        for kind, count in (("Audio", len(audio_inputs)),):
            mentioned = {int(value) for value in re.findall(fr"<{kind}\s+(\d+)>", llm_draft)}
            unexpected = sorted(index for index in mentioned if index < 1 or index > count)
            if unexpected:
                raise ValueError(
                    f"LLM 引用了当前镜头未提交的 {kind}："
                    + "、".join(f"<{kind} {index}>" for index in unexpected)
                )
        prompt, errors = render_studio_prompt(
            shot,
            story,
            generation_mode=mode,
            user_prompt=llm_draft,
            reference_paths=[str(item["path"]) for item in active_bindings],
            reference_bindings=active_bindings,
            reference_video_bindings=[],
            reference_audio_bindings=audio_bindings,
        )
        if errors:
            raise ValueError("MiniMax H3 官方 Prompt Skill 校验失败：" + "; ".join(errors))
        return {
            "prompt": prompt,
            "llm_draft": llm_draft,
            "generation_mode": mode,
            "prompt_skill": "MiniMax H3 / h3-prompt-writing",
            "prompt_optimized_at": utc_now(),
            "pipeline": ["llm_all_selected_media_draft", "minimax_h3_prompt_skill"],
            "image_inputs": image_inputs,
            "video_inputs": video_inputs,
            "audio_inputs": audio_inputs,
            "video_frame_bindings": video_frame_bindings,
            "reference_video_strategy": "sampled_frames",
        }

    def generate_shot_image(
        self,
        run_dir: Path,
        config: ProjectConfig,
        shot_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not image_generator_enabled(config):
            raise ValueError("当前项目尚未启用 AI 生图配置")
        prompt = str(payload.get("prompt") or "").strip()
        if len(prompt) < 4:
            raise ValueError("请至少输入 4 个字符的生图 Prompt")
        if len(prompt) > 8000:
            raise ValueError("生图 Prompt 不得超过 8000 个字符")
        role = str(payload.get("role") or "library")
        if role not in {"first", "last", "reference", "reference_identity", "reference_scene", "reference_style", "keyframe", "library"}:
            raise ValueError("生图用途必须是首帧、末帧、人物/场景/风格/关键帧参考，或仅保存到素材库")
        shots_doc = load_json(run_dir / "03_shots/shots.json")
        shot = next((item for item in shots_doc.get("shots", []) if item.get("shot_id") == shot_id), None)
        if shot is None:
            raise ValueError(f"未知镜头：{shot_id}")
        aspect = str(config.data.get("aspect_ratio") or "16:9")
        final_prompt = (
            f"电影感短剧单帧画面，画幅比例 {aspect}。"
            f"镜头叙事目的：{shot.get('story_purpose') or '推进剧情'}。"
            f"构图与场景：{shot.get('composition') or '自然电影构图'}。"
            f"用户画面要求：{prompt}。"
            "生成单张连贯、写实、可作为视频生成输入的静态画面；不要字幕、文字、标志、水印或拼图。"
        )
        with self.image_generation_lock:
            data = generate_images(config, prompt=final_prompt, count=1)[0]
            stamp = utc_now().replace(":", "").replace("+", "-")
            filename = f"{stamp}-{uuid.uuid4().hex[:8]}.png"
            path = save_generated_image(data, run_dir / "inputs/studio_generated" / shot_id / filename).resolve()
            manifest_path = run_dir / "inputs/studio_generated/manifest.json"
            manifest = load_json(manifest_path) if manifest_path.is_file() else {"schema_version": "1.0", "images": []}
            record = {
                "asset_id": f"AIIMG-{uuid.uuid4().hex[:12]}",
                "shot_id": shot_id,
                "role": role,
                "prompt": prompt,
                "expanded_prompt": final_prompt,
                "model": image_generator_config(config).get("model"),
                "created_at": utc_now(),
                "local_path": str(path.relative_to(run_dir)),
            }
            manifest.setdefault("images", []).append(record)
            manifest["updated_at"] = utc_now()
            write_json_atomic(manifest_path, manifest)
        return {
            "asset": {
                "path": str(path),
                "name": path.name,
                "label": f"{shot_id} · AI 生成图",
                "role": "ai_generated",
                "media_kind": "image",
                "source_shot_id": shot_id,
                "asset_origin": "ai_still",
                "created_at": path.stat().st_mtime,
                "relative_path": str(path.relative_to(run_dir)),
                "size_bytes": path.stat().st_size,
                "url": "/api/media?" + urllib.parse.urlencode({"run": run_dir.name, "path": str(path)}),
                "generation": record,
            },
            "binding_role": role,
        }

    def comfy_status(self) -> dict[str, Any]:
        comfy_cfg = self.config.data.get("comfyui") or {}
        base_url = str(comfy_cfg.get("base_url") or "http://127.0.0.1:6006")
        try:
            client = ComfyClient(base_url, timeout_s=4)
            stats = client.system_stats()
            unets = _available_models(client, "UNETLoader", "unet_name")
            encoders = _available_models(client, "CLIPLoader", "clip_name")
            vaes = _available_models(client, "VAELoader", "vae_name")
            return {
                "online": True,
                "base_url": base_url,
                "devices": len(stats.get("devices") or []),
                "fl2va_ready": str(comfy_cfg.get("fl2va_checkpoint") or FL2VA_DIFFUSION) in unets,
                "ref2va_ready": str(comfy_cfg.get("ref2va_checkpoint") or REF2VA_DIFFUSION) in unets,
                "ref_node_ready": "MiniMaxH3ReferenceToVideo" in client.object_info("MiniMaxH3ReferenceToVideo"),
                "motion_context_ready": "MiniMaxH3MotionContext" in client.object_info("MiniMaxH3MotionContext"),
                "text_encoder_ready": configured_text_encoder(self.config) in encoders,
                "video_vae_ready": VIDEO_VAE in vaes,
                "audio_vae_ready": AUDIO_VAE in vaes,
            }
        except Exception as exc:  # noqa: BLE001
            return {"online": False, "base_url": base_url, "error": str(exc)}

    def llm_status(self, run_dir: Path) -> dict[str, Any]:
        config = self.run_config(run_dir)
        llm = config.data.get("llm") or config.data.get("azure") or {}
        env_name = str(llm.get("api_key_env") or "RIVO_API_KEY")
        return {
            "configured": bool(llm.get("model") or llm.get("deployment")),
            "credential_ready": bool(os.environ.get(env_name)),
            "model": llm.get("model") or llm.get("deployment"),
            "provider": llm.get("provider") or "azure",
        }

    def image_generator_status(self, config: ProjectConfig | None = None) -> dict[str, Any]:
        active_config = config or self.config
        cfg = image_generator_config(active_config)
        llm = active_config.data.get("llm") or active_config.data.get("azure") or {}
        env_name = str(cfg.get("api_key_env") or llm.get("api_key_env") or "ARK_API_KEY")
        return {
            "enabled": image_generator_enabled(active_config),
            "credential_ready": bool(os.environ.get(env_name)),
            "model": cfg.get("model"),
            "provider": cfg.get("provider") or "openai-compatible",
            "size": cfg.get("size") or "按项目画幅自动选择",
        }

    def media_path(self, run_dir: Path, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        roots = (run_dir.resolve(), (self.run_config(run_dir).project_root / "assets").resolve())
        if not any(_is_relative_to(path, root) for root in roots):
            raise ValueError("媒体路径超出允许目录")
        if path.suffix.lower() not in IMAGE_SUFFIXES | VIDEO_SUFFIXES | AUDIO_SUFFIXES or not path.is_file():
            raise FileNotFoundError("媒体文件不存在或格式不受支持")
        return path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def make_handler(app: StudioApplication) -> type[SimpleHTTPRequestHandler]:
    class Handler(SimpleHTTPRequestHandler):
        server_version = "SceneFlowStudio/1.0"

        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, directory=str(app.studio_dir), **kwargs)

        def log_message(self, fmt: str, *args: Any) -> None:
            _safe_console(f"[studio] {self.address_string()} {fmt % args}")

        def end_headers(self) -> None:
            # The Studio is frequently updated while a tab remains open.  Do
            # not let an intermediary or browser reuse an old HTML/JS bundle
            # after the backend has moved to a newer prompt contract.
            if not self.path.startswith("/api/media"):
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
            super().end_headers()

        def _json(self, value: Any, status: int = 200) -> None:
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                return
            status = HTTPStatus.NOT_FOUND if isinstance(exc, FileNotFoundError) else HTTPStatus.BAD_REQUEST
            self._json({"error": type(exc).__name__, "message": str(exc)}, int(status))

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_JSON_BYTES:
                raise ValueError("JSON 请求体为空或超过 16MB")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("JSON 请求体顶层必须是对象")
            return value

        def _require_current_ui(self) -> bool:
            client_build = str(self.headers.get("X-SceneFlow-UI-Build") or "").strip()
            if client_build == UI_BUILD:
                return True
            self._json(
                {
                    "error": "StaleUIBuild",
                    "message": "页面版本已更新，旧页面已被阻止覆盖最终 Skill Prompt；正在重新载入最新项目数据",
                    "reload_required": True,
                    "required_ui_build": UI_BUILD,
                    "client_ui_build": client_build or None,
                },
                int(HTTPStatus.CONFLICT),
            )
            return False

        def _route(self) -> tuple[list[str], dict[str, list[str]]]:
            parsed = urllib.parse.urlparse(self.path)
            return [urllib.parse.unquote(x) for x in parsed.path.split("/") if x], urllib.parse.parse_qs(parsed.query)

        def do_GET(self) -> None:  # noqa: N802
            parts, query = self._route()
            try:
                # "/" is the terminal gate, the workbench itself lives at
                # "/studio".  Relative asset URLs only resolve correctly
                # without a trailing slash, so normalise that first.
                if parts == []:
                    self.path = "/gate.html"
                elif parts == ["studio"]:
                    if self.path.startswith("/studio/"):
                        self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                        self.send_header("Location", "/studio")
                        self.end_headers()
                        return
                    self.path = "/index.html"
                if parts == ["api", "health"]:
                    self._json({"ok": True, "time": utc_now(), "ui_build": UI_BUILD, "comfyui": app.comfy_status(), "image_generator": app.image_generator_status()})
                    return
                if parts == ["api", "version"]:
                    self._json({"ui_build": UI_BUILD})
                    return
                if parts == ["api", "bootstrap"]:
                    runs = app.runs()
                    default_run = next((item for item in runs if int(item.get("shot_count") or 0) > 0), runs[0] if runs else None)
                    self._json({"runs": runs, "default_run_id": default_run["run_id"] if default_run else None})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "workspace":
                    self._json(app.workspace(app.resolve_run(parts[2])))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "assets":
                    run_dir = app.resolve_run(parts[2])
                    assets = list_run_assets(run_dir, app.run_config(run_dir))
                    for item in assets:
                        item["url"] = "/api/media?" + urllib.parse.urlencode({"run": parts[2], "path": item["path"]})
                    upload_counts = {
                        kind: sum(1 for item in assets if item.get("role") == "upload" and item.get("media_kind") == kind)
                        for kind in LIBRARY_UPLOAD_LIMITS
                    }
                    self._json(
                        {
                            "assets": assets,
                            "upload_limits": LIBRARY_UPLOAD_LIMITS,
                            "h3_input_limits": H3_INPUT_LIMITS,
                            "upload_counts": upload_counts,
                        }
                    )
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "jobs":
                    run_dir = app.resolve_run(parts[2])
                    jobs = app.generations.load_jobs(run_dir)
                    self._json({"jobs": [app.generations.public_job(job) for job in jobs.get("jobs", {}).values()]})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "inspiration":
                    run_dir = app.resolve_run(parts[2])
                    self._json(
                        {
                            "inspiration": load_inspiration(run_dir),
                            "llm": app.llm_status(run_dir),
                        }
                    )
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "workflow":
                    run_dir = app.resolve_run(parts[2])
                    self._json({"workflow": workflow_snapshot(run_dir)})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "prompt-review":
                    self._json(prompt_review(app.resolve_run(parts[2])))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "videos":
                    run_dir = app.resolve_run(parts[2])
                    videos = collect_shot_videos(run_dir)
                    for item in videos:
                        if item.get("video"):
                            item["url"] = "/api/media?" + urllib.parse.urlencode({"run": parts[2], "path": item["video"]})
                    self._json({"videos": videos, "jobs": [app.generations.public_job(job) for job in app.generations.load_jobs(run_dir).get("jobs", {}).values()]})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "subtitles":
                    self._json(load_subtitles(app.resolve_run(parts[2])))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "final":
                    run_dir = app.resolve_run(parts[2])
                    payload = final_status(run_dir)
                    if payload.get("video"):
                        payload["url"] = "/api/media?" + urllib.parse.urlencode({"run": parts[2], "path": payload["video"]})
                    for item in payload.get("videos") or []:
                        if item.get("video"):
                            item["url"] = "/api/media?" + urllib.parse.urlencode({"run": parts[2], "path": item["video"]})
                    self._json(payload)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "settings":
                    run_dir = app.resolve_run(parts[2])
                    state = read_run(run_dir)
                    config = app.run_config(run_dir)
                    llm = config.data.get("llm") or {}
                    comfy = config.data.get("comfyui") or {}
                    self._json({
                        "run": state,
                        "run_dir": str(run_dir),
                        "llm": {"provider": llm.get("provider"), "model": llm.get("model"), "endpoint": llm.get("endpoint")},
                        "comfyui": comfy,
                        "health": app.comfy_status(),
                        "image_generator": app.image_generator_status(config),
                    })
                    return
                if parts == ["api", "media"]:
                    run_id = (query.get("run") or [""])[0]
                    value = (query.get("path") or [""])[0]
                    path = app.media_path(app.resolve_run(run_id), value)
                    size = path.stat().st_size
                    start, end = 0, size - 1
                    range_header = self.headers.get("Range")
                    partial = False
                    if range_header and range_header.startswith("bytes="):
                        requested = range_header[6:].split(",", 1)[0]
                        left, _, right = requested.partition("-")
                        if left:
                            start = int(left)
                            end = min(int(right), end) if right else end
                        elif right:
                            start = max(0, size - int(right))
                        if not 0 <= start <= end < size:
                            raise ValueError("媒体 Range 请求非法")
                        partial = True
                    self.send_response(206 if partial else 200)
                    self.send_header("Content-Type", media_type_for(path))
                    self.send_header("Content-Length", str(end - start + 1))
                    self.send_header("Accept-Ranges", "bytes")
                    if partial:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    with path.open("rb") as source:
                        source.seek(start)
                        remaining = end - start + 1
                        while remaining:
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    return
            except Exception as exc:  # noqa: BLE001
                self._handle_error(exc)
                return
            super().do_GET()

        def do_DELETE(self) -> None:  # noqa: N802
            parts, _ = self._route()
            try:
                if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3:] == ["subtitles", "style"]:
                    run_dir = app.resolve_run(parts[2])
                    with app.subtitle_lock:
                        self._json(reset_subtitle_style(run_dir))
                    return
                self.send_error(404)
            except Exception as exc:  # noqa: BLE001
                self._handle_error(exc)

        def do_PUT(self) -> None:  # noqa: N802
            parts, query = self._route()
            try:
                if len(parts) == 6 and parts[:2] == ["api", "runs"] and parts[3] == "shots" and parts[5] == "decision":
                    if not self._require_current_ui():
                        return
                    run_dir = app.resolve_run(parts[2])
                    decision = save_decision(
                        run_dir,
                        app.run_config(run_dir),
                        parts[4],
                        self._body(),
                    )
                    self._json({"decision": decision})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "inspiration":
                    run_dir = app.resolve_run(parts[2])
                    body = self._body()
                    if body.get("action") != "select_images":
                        raise ValueError("未知灵感保存动作")
                    with app.inspiration_lock:
                        document = select_inspiration_images(
                            run_dir,
                            app.run_config(run_dir),
                            list(body.get("image_paths") or []),
                        )
                    self._json({"inspiration": document})
                    return
                if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3] == "workflow":
                    run_dir = app.resolve_run(parts[2])
                    body = self._body()
                    document = body.get("document")
                    if not isinstance(document, dict):
                        raise ValueError("document 必须是 JSON 对象")
                    with app.workflow_lock:
                        save_stage_document(run_dir, parts[4], document)
                    self._json({"workflow": workflow_snapshot(run_dir)})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "subtitles":
                    run_dir = app.resolve_run(parts[2])
                    body = self._body()
                    cues = body.get("cues")
                    if not isinstance(cues, list):
                        raise ValueError("cues 必须是数组")
                    style = body.get("style") if isinstance(body.get("style"), dict) else None
                    self._json(save_subtitle_cues(run_dir, cues, style))
                    return
                self.send_error(404)
            except Exception as exc:  # noqa: BLE001
                self._handle_error(exc)

        def do_POST(self) -> None:  # noqa: N802
            parts, query = self._route()
            try:
                if parts == ["api", "runs"]:
                    project = app.create_project(str(self._body().get("project_name") or ""))
                    self._json({"project": project}, 201)
                    return
                if len(parts) == 7 and parts[:2] == ["api", "runs"] and parts[3] == "shots" and parts[5:] == ["images", "generate"]:
                    run_dir = app.resolve_run(parts[2])
                    self._json(
                        app.generate_shot_image(run_dir, app.run_config(run_dir), parts[4], self._body()),
                        201,
                    )
                    return
                if len(parts) == 7 and parts[:2] == ["api", "runs"] and parts[3] == "shots" and parts[5:] == ["prompt", "optimize"]:
                    if not self._require_current_ui():
                        return
                    run_dir = app.resolve_run(parts[2])
                    self._json(app.optimize_prompt(run_dir, app.run_config(run_dir), parts[4], self._body()))
                    return
                if len(parts) == 6 and parts[:2] == ["api", "runs"] and parts[3] == "shots" and parts[5] == "generate":
                    if not self._require_current_ui():
                        return
                    run_dir = app.resolve_run(parts[2])
                    body = self._body()
                    self._json(
                        {
                            "job": app.generations.submit(
                                run_dir,
                                app.run_config(run_dir),
                                parts[4],
                                expected_prompt=body.get("expected_prompt"),
                            )
                        },
                        202,
                    )
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "generate-approved":
                    run_dir = app.resolve_run(parts[2])
                    self._json(
                        {"jobs": app.generations.submit_approved(run_dir, app.run_config(run_dir))},
                        202,
                    )
                    return
                if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3:] == ["inspiration", "generate"]:
                    run_dir = app.resolve_run(parts[2])
                    body = self._body()
                    with app.inspiration_lock:
                        document = generate_story_inspiration(
                            run_dir,
                            app.run_config(run_dir),
                            mode=str(body.get("mode") or ""),
                            idea_text=str(body.get("idea_text") or ""),
                            genre=str(body.get("genre") or "现实情感"),
                            tone=str(body.get("tone") or "电影感"),
                        )
                    self._json({"inspiration": document})
                    return
                if len(parts) == 6 and parts[:2] == ["api", "runs"] and parts[3] == "workflow" and parts[5] == "generate":
                    run_dir = app.resolve_run(parts[2])
                    with app.workflow_lock:
                        document = generate_stage(run_dir, app.run_config(run_dir), parts[4])
                    self._json({"document": document, "workflow": workflow_snapshot(run_dir)})
                    return
                if len(parts) == 6 and parts[:2] == ["api", "runs"] and parts[3] == "workflow" and parts[5] == "revise":
                    run_dir = app.resolve_run(parts[2])
                    body = self._body()
                    with app.workflow_lock:
                        document = revise_story(
                            run_dir,
                            app.run_config(run_dir),
                            instruction=str(body.get("instruction") or ""),
                        )
                    self._json({"document": document, "workflow": workflow_snapshot(run_dir)})
                    return
                if len(parts) == 6 and parts[:2] == ["api", "runs"] and parts[3] == "workflow" and parts[5] == "approve":
                    run_dir = app.resolve_run(parts[2])
                    with app.workflow_lock:
                        approve_stage(run_dir, parts[4])
                    self._json({"workflow": workflow_snapshot(run_dir)})
                    return
                if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3:] == ["subtitles", "generate"]:
                    self._json(generate_studio_subtitles(app.resolve_run(parts[2])))
                    return
                if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3:] == ["subtitles", "align"]:
                    run_dir = app.resolve_run(parts[2])
                    with app.subtitle_lock:
                        self._json(align_subtitles_to_speech(run_dir, app.run_config(run_dir)))
                    return
                if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3:] == ["subtitles", "burn"]:
                    run_dir = app.resolve_run(parts[2])
                    with app.subtitle_lock:
                        report = burn_studio_subtitles(run_dir)
                    report["url"] = "/api/media?" + urllib.parse.urlencode({"run": parts[2], "path": report["video"]})
                    self._json(report, 201)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "assemble":
                    run_dir = app.resolve_run(parts[2])
                    body = self._body() if (self.headers.get("Content-Length") or "0") != "0" else {}
                    report = assemble_studio_run(run_dir, burn_subtitles=bool(body.get("burn_subtitles", True)))
                    if report.get("final_url_path"):
                        report["url"] = "/api/media?" + urllib.parse.urlencode({"run": parts[2], "path": report["final_url_path"]})
                    self._json(report, 201)
                    return
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "assets":
                    run_dir = app.resolve_run(parts[2])
                    content_type = self.headers.get("Content-Type") or ""
                    if not content_type.startswith("application/json"):
                        filename = (query.get("filename") or [""])[0]
                        kind = (query.get("kind") or [""])[0]
                        content_length = int(self.headers.get("Content-Length") or 0)
                        path, metadata = save_uploaded_stream(
                            run_dir,
                            filename=filename,
                            kind=kind,
                            stream=self.rfile,
                            content_length=content_length,
                        )
                        self._json(
                            {
                                "asset": {
                                    "path": str(path),
                                    "name": path.name,
                                    "label": "人工上传",
                                    "role": "upload",
                                    "media_kind": kind,
                                    "size_bytes": path.stat().st_size,
                                    "metadata": metadata,
                                    "url": "/api/media?" + urllib.parse.urlencode({"run": parts[2], "path": str(path)}),
                                }
                            },
                            201,
                        )
                        return
                    body = self._body()
                    path = save_uploaded_data_url(
                        run_dir,
                        filename=str(body.get("filename") or "upload.png"),
                        data_url=str(body.get("data_url") or ""),
                    )
                    self._json(
                        {
                            "asset": {
                                "path": str(path),
                                "name": path.name,
                                "label": "人工上传",
                                "role": "upload",
                                "media_kind": "image",
                                "url": "/api/media?" + urllib.parse.urlencode({"run": parts[2], "path": str(path)}),
                            }
                        },
                        201,
                    )
                    return
                self.send_error(404)
            except Exception as exc:  # noqa: BLE001
                self._handle_error(exc)

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SceneFlow 人工镜头编排与 H3 生成服务")
    parser.add_argument("--config", default="configs/project.local.yaml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4173)
    return parser


def _load_local_debug_env(project_root: Path) -> None:
    """Load the project's existing local debug credentials without logging values."""
    path = project_root / ".vscode/debug.env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.removeprefix("export ").strip()
        value = value.strip().strip("\"'")
        if name and value:
            os.environ.setdefault(name, value)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    _load_local_debug_env(config.project_root)
    studio_dir = Path(__file__).resolve().parents[1] / "studio"
    app = StudioApplication(config, studio_dir)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    _safe_console(f"SceneFlow 入口: http://{args.host}:{args.port}/")
    _safe_console(f"SceneFlow 工作台: http://{args.host}:{args.port}/studio")
    _safe_console(f"run root: {config.run_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _safe_console("\nSceneFlow Studio stopped")
    finally:
        server.server_close()
        app.generations.executor.shutdown(wait=False, cancel_futures=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
