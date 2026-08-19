import base64
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from short_drama.config import ProjectConfig
from short_drama.human_orchestration import (
    H3_INPUT_LIMITS,
    LIBRARY_UPLOAD_LIMITS,
    effective_picture_bindings,
    list_run_assets,
    recover_generated_image_bindings,
    save_decision,
    save_uploaded_data_url,
    save_uploaded_stream,
    validate_decision,
)
from short_drama.studio_server import GenerationManager, StudioApplication


class HumanOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run_dir = self.root / "runs" / "run-1"
        (self.run_dir / "03_shots").mkdir(parents=True)
        (self.run_dir / "02_story").mkdir(parents=True)
        (self.run_dir / "inputs/processed").mkdir(parents=True)
        (self.root / "assets").mkdir()
        (self.run_dir / "02_story/story.json").write_text(
            json.dumps({"characters": [{"character_id": "C01", "name": "甲", "identity": "青年", "appearance": "黑色外套", "reference_subject_description": "IMG01 中的人物；IMG03 也可参考"}]}),
            encoding="utf-8",
        )
        (self.run_dir / "03_shots/shots.json").write_text(
            json.dumps({"shots": [{
                "shot_id": "S001", "duration_s": 6, "generation_mode": "t2va",
                "story_purpose": "建立场景", "composition": "中景构图", "camera": "固定机位",
                "action_timeline": "人物抬头", "characters": ["C01"],
                "blocking": [{
                    "character_id": "C01", "speaks": False, "movement_direction": "none",
                    "start": {"horizontal": "screen-center", "depth": "midground", "facing": "camera", "visible": True, "mouth_state": "closed"},
                    "end": {"horizontal": "screen-center", "depth": "midground", "facing": "camera", "visible": True, "mouth_state": "closed"},
                }],
                "dialogue": [], "speaker_mappings": [],
                "audio_contract": {"allowed_speaker_ids": [], "offscreen_human_voice_allowed": False, "non_diegetic_music": False, "ambient_sounds": [], "action_sounds": []},
            }]}),
            encoding="utf-8",
        )
        self.image = self.run_dir / "inputs/processed/IMG01.png"
        self.image.write_bytes(b"not-a-real-png")
        (self.run_dir / "inputs/manifest.json").write_text(
            json.dumps({"images": [{"image_id": "IMG01", "output_path": str(self.image)}]}),
            encoding="utf-8",
        )
        self.config = ProjectConfig(
            path=self.root / "config.yaml",
            data={"aspect_ratio": "16:9"},
            project_root=self.root,
            run_root=self.root / "runs",
            input_images=(),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_draft_allows_incomplete_required_inputs(self):
        decision = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {"generation_mode": "FL2VA", "prompt": "test", "approved": False},
        )
        self.assertEqual(decision["generation_mode"], "first_last_frame")
        self.assertIsNone(decision["first_frame"])
        self.assertEqual(decision["prompt_skill"], "MiniMax H3 / h3-prompt-writing")
        self.assertIn("integrated_multimodal_description:", decision["prompt"])

    def test_approved_decision_requires_mode_inputs(self):
        with self.assertRaisesRegex(ValueError, "缺少必需输入图片"):
            validate_decision(
                {"generation_mode": "I2VA", "prompt": "test", "approved": True},
                run_dir=self.run_dir,
                config=self.config,
                require_approved=True,
            )

    def test_locked_decision_requires_explicit_unlock(self):
        save_decision(
            self.run_dir,
            self.config,
            "S001",
            {"generation_mode": "T2VA", "prompt": "locked", "approved": True, "locked": True},
        )
        ignored = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {"generation_mode": "T2VA", "prompt": "changed", "approved": False},
        )
        self.assertTrue(ignored["locked"])
        self.assertTrue(ignored["approved"])
        self.assertNotIn("changed", ignored["prompt"])
        unlocked = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {
                "generation_mode": "T2VA",
                "prompt": "changed",
                "approved": False,
                "locked": False,
                "force_unlock": True,
            },
        )
        self.assertFalse(unlocked["locked"])
        self.assertIn("changed", unlocked["prompt"])

    def test_upload_is_listed_as_asset(self):
        from PIL import Image

        raw = io.BytesIO()
        Image.new("RGB", (320, 320), "blue").save(raw, format="PNG")
        payload = "data:image/png;base64," + base64.b64encode(raw.getvalue()).decode("ascii")
        path = save_uploaded_data_url(self.run_dir, filename="my frame.png", data_url=payload)
        self.assertTrue(path.is_file())
        records = list_run_assets(self.run_dir, self.config)
        self.assertIn(path, {Path(item["path"]) for item in records})

    def test_binary_image_upload_is_validated_and_categorized(self):
        from PIL import Image

        raw = io.BytesIO()
        Image.new("RGB", (320, 320), "red").save(raw, format="PNG")
        payload = raw.getvalue()
        path, metadata = save_uploaded_stream(
            self.run_dir,
            filename="reference.png",
            kind="image",
            stream=io.BytesIO(payload),
            content_length=len(payload),
        )
        self.assertTrue(path.is_file())
        self.assertEqual((metadata["width"], metadata["height"]), (320, 320))
        self.assertTrue(metadata["asset_id"])
        uploaded = [item for item in list_run_assets(self.run_dir, self.config) if item["role"] == "upload"]
        self.assertEqual(uploaded[0]["media_kind"], "image")

    def test_image_library_upload_count_is_unlimited(self):
        from PIL import Image

        raw = io.BytesIO()
        Image.new("RGB", (320, 320), "green").save(raw, format="PNG")
        payload = raw.getvalue()
        paths = []
        for index in range(12):
            path, _ = save_uploaded_stream(
                self.run_dir,
                filename=f"reference-{index}.png",
                kind="image",
                stream=io.BytesIO(payload),
                content_length=len(payload),
            )
            paths.append(path)
        self.assertEqual(len(set(paths)), 12)
        manifest = json.loads((self.run_dir / "inputs/studio_uploads/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["assets"]), 12)
        self.assertTrue(all(not Path(item["local_path"]).is_absolute() for item in manifest["assets"]))

    def test_library_is_unlimited_but_each_h3_shot_keeps_input_limits(self):
        self.assertEqual(
            LIBRARY_UPLOAD_LIMITS,
            {"image": None, "video": None, "audio": None},
        )
        self.assertEqual(
            H3_INPUT_LIMITS,
            {"image": 9, "video": 3, "audio": 3},
        )

    def test_asset_library_excludes_legacy_videos_but_keeps_studio_generations(self):
        legacy = self.run_dir / "05_videos/S001.mp4"
        generated = self.run_dir / "05_videos/studio_generations/S001/S001-new.mp4"
        legacy.parent.mkdir(parents=True)
        generated.parent.mkdir(parents=True)
        legacy.write_bytes(b"legacy")
        generated.write_bytes(b"generated")
        records = list_run_assets(self.run_dir, self.config)
        paths = {Path(item["path"]): item for item in records}
        self.assertNotIn(legacy.resolve(), paths)
        self.assertEqual(paths[generated.resolve()]["asset_origin"], "generated_video")
        self.assertEqual(paths[generated.resolve()]["source_shot_id"], "S001")

    def test_generation_job_accepts_persisted_job_id_field(self):
        manager = GenerationManager()
        try:
            job = manager._update_job(
                self.run_dir,
                "S001-testjob",
                job_id="S001-testjob",
                shot_id="S001",
                status="queued",
            )
            self.assertEqual(job["job_id"], "S001-testjob")
            self.assertEqual(job["status"], "queued")
        finally:
            manager.executor.shutdown(wait=False)

    def test_locked_draft_can_be_approved_and_relocked_for_generation(self):
        draft = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {"generation_mode": "T2VA", "prompt": "镜头草稿", "approved": False, "locked": True},
        )
        approved = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {
                **draft,
                "approved": True,
                "locked": True,
                "force_unlock": True,
            },
        )
        self.assertTrue(approved["approved"])
        self.assertTrue(approved["locked"])

    def test_switching_mode_rebuilds_instead_of_nesting_old_official_prompt(self):
        original = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {"generation_mode": "T2VA", "prompt": "镜头草稿", "approved": False, "locked": True},
        )
        switched = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {
                **original,
                "generation_mode": "I2VA",
                "first_frame": str(self.image),
                "prompt": original["prompt"],
                "locked": False,
                "force_unlock": True,
            },
        )
        self.assertTrue(switched["prompt"].startswith("For the target video"))
        self.assertNotIn("导演补充要求", switched["prompt"])

    def test_input_roles_are_persisted_without_forcing_every_image_to_identity(self):
        decision = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {
                "generation_mode": "Ref2VA",
                "first_frame": str(self.image),
                "last_frame": str(self.image),
                "reference_image_bindings": [
                    {"path": str(self.image), "usage": "keyframe", "note": "人物抬头后的构图"},
                ],
                "prompt": "保持关键动作和空间方向",
                "approved": False,
            },
        )
        self.assertEqual(decision["first_frame"], str(self.image))
        self.assertEqual(decision["last_frame"], str(self.image))
        self.assertEqual(decision["reference_image_bindings"][0]["usage"], "keyframe")
        self.assertIn("keyframe - 参考关键动作状态、构图和空间关系", decision["prompt"])
        self.assertNotIn("<Subject 1>", decision["prompt"])

    def test_ref2va_prompt_uses_every_selected_picture_with_exact_combined_roles(self):
        identity = self.run_dir / "inputs/processed/portrait.png"
        last = self.run_dir / "inputs/processed/ending.png"
        identity.write_bytes(b"portrait")
        last.write_bytes(b"ending")
        decision = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {
                "generation_mode": "Ref2VA",
                "first_frame": str(self.image),
                "last_frame": str(last),
                "reference_image_bindings": [
                    {"path": str(identity), "usage": "identity", "character_ids": ["C01"]},
                    {"path": str(self.image), "usage": "scene"},
                ],
                "prompt": "按人工图片用途生成",
                "approved": False,
            },
        )
        pictures = effective_picture_bindings(decision)
        self.assertEqual([item["path"] for item in pictures], [str(self.image), str(last), str(identity)])
        self.assertEqual(pictures[0]["usages"], ["first_frame", "scene"])
        self.assertIn("<Picture 1>: first_frame - 首帧", decision["prompt"])
        self.assertIn("同时作为scene - 场景参考", decision["prompt"])
        self.assertIn("<Picture 2>: last_frame - 尾帧", decision["prompt"])
        self.assertIn("<Picture 3>: identity - 人物参考", decision["prompt"])
        self.assertNotIn("<Picture 4>", decision["prompt"])

    @patch("short_drama.studio_server.completion_text", return_value="保持中景构图，人物连续抬头，<Picture 1> 只约束关键帧动作状态。")
    @patch("short_drama.studio_server.create_multimodal_completion")
    def test_prompt_pipeline_calls_selected_images_before_official_skill(self, create_completion, completion):
        app = StudioApplication(self.config, self.root)
        try:
            result = app.optimize_prompt(
                self.run_dir,
                self.config,
                "S001",
                {
                    "generation_mode": "Ref2VA",
                    "reference_image_bindings": [
                        {"path": str(self.image), "usage": "keyframe", "note": "抬头动作完成态"},
                    ],
                },
            )
        finally:
            app.generations.executor.shutdown(wait=False)
        create_completion.assert_called_once()
        self.assertIn("图片被人工指定", create_completion.call_args.kwargs["system_prompt"])
        self.assertIn("关键帧动作/构图", create_completion.call_args.kwargs["user_text"])
        self.assertEqual(create_completion.call_args.kwargs["image_paths"], [self.image])
        self.assertEqual(result["pipeline"], ["llm_all_selected_media_draft", "minimax_h3_prompt_skill"])
        self.assertIn("retention_analysis:", result["prompt"])
        self.assertIn("导演补充要求", result["prompt"])
        self.assertNotIn("IMG03", result["prompt"])

    @patch("short_drama.studio_server._prepare_prompt_media_previews")
    @patch("short_drama.studio_server.completion_text", return_value="使用 <Picture 1>、<Picture 2>、<Picture 3> 承接源视频动作，并用 <Audio 1> 保持环境声层次。")
    @patch("short_drama.studio_server.create_multimodal_completion")
    def test_prompt_pipeline_uses_video_and_audio_with_stable_h3_tags(self, create_completion, completion, prepare):
        video = self.run_dir / "inputs/processed/reference.mp4"
        audio = self.run_dir / "inputs/processed/reference.wav"
        video.write_bytes(b"video")
        audio.write_bytes(b"audio")
        sampled_frames = []
        for index in range(1, 4):
            frame = self.run_dir / f"inputs/processed/reference_sample_{index}.png"
            frame.write_bytes(b"frame")
            sampled_frames.append(frame)
        prepare.return_value = (
            [{
                "source_video": "参考视频 1",
                "file": video.name,
                "usage": "continuity",
                "metadata": {"duration_s": 6},
                "sample_paths": [str(path) for path in sampled_frames],
                "sample_ratios": [0.15, 0.55, 0.9],
            }],
            [{"audio": "<Audio 1>", "file": audio.name, "usage": "soundscape", "metadata": {"duration_s": 6}}],
            [*sampled_frames, self.image],
            ["Video 1 sample 1", "Video 1 sample 2", "Video 1 sample 3", "Audio 1 waveform"],
        )
        app = StudioApplication(self.config, self.root)
        try:
            result = app.optimize_prompt(
                self.run_dir,
                self.config,
                "S001",
                {
                    "generation_mode": "Ref2VA",
                    "reference_video_bindings": [{"path": str(video), "usage": "continuity", "note": "仅参考前 3 秒的缓慢推镜和人物起身动作"}],
                    "reference_audio_bindings": [{"path": str(audio), "usage": "soundscape"}],
                },
            )
        finally:
            app.generations.executor.shutdown(wait=False)
        prepare.assert_called_once()
        create_completion.assert_called_once()
        self.assertEqual(
            create_completion.call_args.kwargs["image_labels"],
            [
                "这是 <Picture 1>；人工指定用途为 关键帧动作/构图。",
                "这是 <Picture 2>；人工指定用途为 关键帧动作/构图。",
                "这是 <Picture 3>；人工指定用途为 关键帧动作/构图。",
                "Audio 1 waveform",
            ],
        )
        self.assertNotIn("<Video 1>", result["prompt"])
        self.assertIn("<Picture 1>: keyframe -", result["prompt"])
        self.assertIn("人工说明：仅参考前 3 秒的缓慢推镜和人物起身动作", result["prompt"])
        self.assertIn("<Audio 1>: soundscape -", result["prompt"])
        self.assertEqual(result["video_inputs"][0]["sampled_pictures"], ["<Picture 1>", "<Picture 2>", "<Picture 3>"])
        self.assertEqual(len(result["video_frame_bindings"]), 3)
        self.assertEqual(result["reference_video_strategy"], "sampled_frames")
        self.assertEqual(result["audio_inputs"][0]["audio"], "<Audio 1>")

    def test_save_draft_can_preserve_current_prompt_without_optimization(self):
        prompt = "当前已经优化完成的 Prompt，应原样保存。"
        decision = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {
                "generation_mode": "T2VA",
                "prompt": prompt,
                "approved": False,
                "skip_prompt_optimization": True,
            },
        )
        self.assertEqual(decision["prompt"], prompt)

    def test_save_draft_preserves_explicit_skill_metadata(self):
        prompt = "已经由 MiniMax 官方 Skill 优化完成的 Prompt。"
        decision = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {
                "generation_mode": "T2VA",
                "prompt": prompt,
                "prompt_llm_draft": "LLM 初稿",
                "prompt_skill": "MiniMax H3 / h3-prompt-writing",
                "prompt_pipeline": ["llm_all_selected_media_draft", "minimax_h3_prompt_skill"],
                "prompt_optimized_at": "2026-08-12T00:00:00+00:00",
                "approved": False,
                "skip_prompt_optimization": True,
            },
        )
        self.assertEqual(decision["prompt"], prompt)
        self.assertEqual(decision["prompt_skill"], "MiniMax H3 / h3-prompt-writing")
        self.assertEqual(decision["prompt_llm_draft"], "LLM 初稿")
        self.assertEqual(decision["prompt_optimized_at"], "2026-08-12T00:00:00+00:00")

    def test_generation_job_snapshots_exact_final_prompt(self):
        decision = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {
                "generation_mode": "T2VA",
                "prompt": "镜头导演初稿",
                "approved": True,
                "locked": True,
            },
        )
        manager = GenerationManager()
        try:
            with patch.object(manager.executor, "submit"):
                job = manager.submit(self.run_dir, self.config, "S001")
            stored = manager.load_jobs(self.run_dir)["jobs"][job["job_id"]]
        finally:
            manager.executor.shutdown(wait=False)
        self.assertEqual(stored["prompt_snapshot"], decision["prompt"])
        self.assertEqual(
            stored["prompt_sha256"],
            hashlib.sha256(decision["prompt"].encode("utf-8")).hexdigest(),
        )
        self.assertEqual(stored["prompt_skill"], "MiniMax H3 / h3-prompt-writing")

    def test_generation_rejects_a_prompt_different_from_the_reviewed_ui_text(self):
        decision = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {
                "generation_mode": "T2VA",
                "prompt": "镜头导演初稿",
                "approved": True,
                "locked": True,
            },
        )
        manager = GenerationManager()
        try:
            with self.assertRaisesRegex(ValueError, "页面 Prompt 与本地保存版本不一致"):
                manager.submit(
                    self.run_dir,
                    self.config,
                    "S001",
                    expected_prompt=decision["prompt"] + "旧稿",
                )
        finally:
            manager.executor.shutdown(wait=False)

    @patch("short_drama.studio_server.completion_text", return_value="让 IMG03 中的人物进入当前镜头。")
    @patch("short_drama.studio_server.create_multimodal_completion")
    def test_prompt_pipeline_rejects_unselected_image_ids(self, create_completion, completion):
        app = StudioApplication(self.config, self.root)
        try:
            with self.assertRaisesRegex(ValueError, "未选中的图片：IMG03"):
                app.optimize_prompt(
                    self.run_dir,
                    self.config,
                    "S001",
                    {
                        "generation_mode": "Ref2VA",
                        "reference_image_bindings": [
                            {"path": str(self.image), "usage": "identity"},
                        ],
                    },
                )
        finally:
            app.generations.executor.shutdown(wait=False)

    def test_ai_manifest_recovers_binding_lost_during_page_refresh(self):
        original = save_decision(
            self.run_dir,
            self.config,
            "S001",
            {
                "generation_mode": "Ref2VA",
                "reference_image_bindings": [
                    {"path": str(self.image), "usage": "identity"},
                ],
                "prompt": "人物保持一致",
                "approved": False,
                "locked": True,
            },
        )
        generated = self.run_dir / "inputs/studio_generated/S001/recovered.png"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b"generated")
        manifest_path = self.run_dir / "inputs/studio_generated/manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "images": [{
                        "asset_id": "AIIMG-recover",
                        "shot_id": "S001",
                        "role": "reference_scene",
                        "created_at": "9999-01-01T00:00:00+00:00",
                        "local_path": "inputs/studio_generated/S001/recovered.png",
                    }],
                }
            ),
            encoding="utf-8",
        )
        self.assertTrue(original["locked"])
        self.assertEqual(recover_generated_image_bindings(self.run_dir, self.config), 1)
        recovered = json.loads((self.run_dir / "03_shots/human_orchestration.json").read_text(encoding="utf-8"))["shots"]["S001"]
        self.assertFalse(recovered["locked"])
        self.assertIn(str(generated), recovered["reference_images"])
        binding = next(item for item in recovered["reference_image_bindings"] if item["path"] == str(generated))
        self.assertEqual(binding["usage"], "scene")
        self.assertIn("<Picture 2>: scene", recovered["prompt"])
        self.assertTrue(json.loads(manifest_path.read_text(encoding="utf-8"))["images"][0]["binding_recovered_at"])

    @patch("short_drama.studio_server.generate_images", return_value=[b"generated-image"])
    def test_ai_image_generation_is_saved_and_returned_as_local_asset(self, generate):
        config = ProjectConfig(
            path=self.config.path,
            data={
                **self.config.data,
                "image_generator": {"enabled": True, "model": "seedream-test", "size": "1536x1024"},
            },
            project_root=self.config.project_root,
            run_root=self.config.run_root,
            input_images=(),
        )
        app = StudioApplication(config, self.root)
        try:
            result = app.generate_shot_image(
                self.run_dir,
                config,
                "S001",
                {"prompt": "夕阳下的人物电影特写", "role": "first"},
            )
        finally:
            app.generations.executor.shutdown(wait=False)
        path = Path(result["asset"]["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(result["binding_role"], "first")
        self.assertEqual(result["asset"]["role"], "ai_generated")
        self.assertEqual(result["asset"]["source_shot_id"], "S001")
        self.assertEqual(result["asset"]["asset_origin"], "ai_still")
        manifest = json.loads((self.run_dir / "inputs/studio_generated/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["images"][0]["model"], "seedream-test")
        self.assertFalse(Path(manifest["images"][0]["local_path"]).is_absolute())
        self.assertIn("镜头叙事目的", generate.call_args.kwargs["prompt"])


if __name__ == "__main__":
    unittest.main()
