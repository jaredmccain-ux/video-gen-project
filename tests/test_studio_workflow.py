import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from short_drama.approval import approval_status
from short_drama.config import ProjectConfig
from short_drama.studio_workflow import (
    _normalize_shots,
    approve_stage,
    generate_descriptions,
    save_stage_document,
    workflow_snapshot,
)


class StudioWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run_dir = self.root / "runs/run-1"
        (self.run_dir / "inputs").mkdir(parents=True)
        (self.root / "assets").mkdir()
        (self.run_dir / "run.json").write_text(
            json.dumps({"run_id": "run-1", "state": "CREATED"}), encoding="utf-8"
        )
        self.config = ProjectConfig(
            path=self.root / "config.yaml",
            data={"llm": {"model": "test"}},
            project_root=self.root,
            run_root=self.root / "runs",
            input_images=(),
        )

    def tearDown(self):
        self.temp.cleanup()

    def description_document(self, count=4):
        return {
            "schema_version": "2.0",
            "images": [
                {
                    "image_id": f"IMG{index:02d}",
                    "source_path": f"image-{index}.png",
                    "visible_facts": ["事实"],
                    "setting": "场景",
                    "people": [],
                    "objects": [],
                    "mood_or_atmosphere": "自然光",
                    "uncertainties": [],
                }
                for index in range(1, count + 1)
            ],
        }

    def test_variable_image_descriptions_can_be_saved_and_approved(self):
        save_stage_document(self.run_dir, "descriptions", self.description_document())
        approve_stage(self.run_dir, "descriptions")
        self.assertEqual(approval_status(self.run_dir, "descriptions"), "approved")
        snapshot = workflow_snapshot(self.run_dir)
        self.assertEqual(len(snapshot["descriptions"]["document"]["images"]), 4)

    def test_editing_approved_stage_invalidates_hash_and_keeps_version(self):
        document = self.description_document()
        save_stage_document(self.run_dir, "descriptions", document)
        approve_stage(self.run_dir, "descriptions")
        document["images"][0]["setting"] = "修改后的场景"
        save_stage_document(self.run_dir, "descriptions", document)
        self.assertEqual(approval_status(self.run_dir, "descriptions"), "stale_artifact_changed")
        versions = list((self.run_dir / "01_descriptions/studio_versions").glob("*.json"))
        self.assertEqual(len(versions), 1)

    @patch("short_drama.drama_writer.create_multimodal_completion")
    def test_description_generation_processes_every_selected_image(self, completion):
        paths = []
        for index in range(5):
            path = self.run_dir / "inputs" / f"selected-{index}.png"
            path.write_bytes(b"image")
            paths.append(str(path))
        (self.run_dir / "inputs/inspiration.json").write_text(
            json.dumps({"selected_images": paths}), encoding="utf-8"
        )
        response = {
            "visible_facts": ["事实"], "setting": "场景", "people": [], "objects": [],
            "mood_or_atmosphere": "自然光", "uncertainties": [], "story_affordances": [],
        }
        completion.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(response, ensure_ascii=False)))]
        )
        document = generate_descriptions(self.run_dir, self.config)
        self.assertEqual(len(document["images"]), 5)
        self.assertEqual(completion.call_count, 5)

    def test_description_generation_ignores_unselected_manifest_images(self):
        from short_drama.studio_workflow import generate_descriptions
        manifest_dir = self.run_dir / "inputs"
        extra = manifest_dir / "processed"
        extra.mkdir(parents=True, exist_ok=True)
        leftover = extra / "IMG01_anchor_processed.png"
        leftover.write_bytes(b"old")
        (manifest_dir / "manifest.json").write_text(
            json.dumps({"images": [{"output_path": str(leftover)}, {"output_path": str(leftover)}]}),
            encoding="utf-8",
        )
        chosen = manifest_dir / "only-this.png"
        chosen.write_bytes(b"new")
        (manifest_dir / "inspiration.json").write_text(
            json.dumps({"selected_images": [str(chosen)]}),
            encoding="utf-8",
        )
        with patch("short_drama.drama_writer.create_multimodal_completion") as completion:
            completion.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                    "visible_facts": ["事实"], "setting": "场景", "people": [], "objects": [],
                    "mood_or_atmosphere": "自然光", "uncertainties": [], "story_affordances": [],
                }, ensure_ascii=False)))]
            )
            document = generate_descriptions(self.run_dir, self.config)
        self.assertEqual(len(document["images"]), 1)
        self.assertEqual(document["images"][0]["source_path"], str(chosen))

    def test_description_generation_requires_explicit_inspiration_selection(self):
        from short_drama.studio_workflow import generate_descriptions
        (self.run_dir / "inputs/manifest.json").write_text(
            json.dumps({"images": [{"output_path": str(self.run_dir / "inputs/x.png")}]}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "选择至少 1 张"):
            generate_descriptions(self.run_dir, self.config)

    def test_shot_normalization_produces_contiguous_timeline(self):
        story = {"characters": [{"character_id": "C01"}]}
        document = _normalize_shots(
            {"shots": [
                {"duration_s": 3},
                {"duration_s": 9, "dialogue": [{"speaker_id": "C01", "text": "你好"}]},
                *({"duration_s": 6} for _ in range(18)),
            ]},
            story,
        )
        first, second = document["shots"][:2]
        self.assertEqual((first["duration_s"], second["duration_s"]), (6, 6))
        self.assertEqual(second["planned_start_s"], first["planned_end_s"])
        self.assertEqual(second["subtitle_text"], "你好")
        self.assertEqual(sum(item["duration_s"] for item in document["shots"]), 120)

    def test_shot_normalization_expands_short_model_output(self):
        story = {"characters": [{"character_id": "C01"}], "beats": [{"beat_id": "B01", "summary": "开场"}]}
        document = _normalize_shots({"shots": [{"story_purpose": "见面"}, {"story_purpose": "追车"}]}, story)
        self.assertGreaterEqual(len(document["shots"]), 15)
        self.assertLessEqual(len(document["shots"]), 30)
        self.assertEqual(sum(item["duration_s"] for item in document["shots"]), 120)
        self.assertTrue(all(4 <= item["duration_s"] <= 8 for item in document["shots"]))

    def test_replacing_shots_archives_and_clears_old_orchestration(self):
        orchestration = self.run_dir / "03_shots/human_orchestration.json"
        orchestration.parent.mkdir(parents=True, exist_ok=True)
        orchestration.write_text(
            json.dumps({"shots": {"S001": {"prompt": "旧镜头提示词"}}}), encoding="utf-8"
        )
        shots = {
            "schema_version": "2.0",
            "shots": [
                {"shot_id": f"S{index:03d}", "duration_s": 6}
                for index in range(1, 21)
            ],
        }
        save_stage_document(self.run_dir, "shots", shots)
        current = json.loads(orchestration.read_text(encoding="utf-8"))
        self.assertEqual(current["shots"], {})
        self.assertEqual(current["invalidated_reason"], "shot_plan_replaced")
        backups = list((self.run_dir / "03_shots/studio_versions").glob("*-human_orchestration.json"))
        self.assertEqual(len(backups), 1)


if __name__ == "__main__":
    unittest.main()
