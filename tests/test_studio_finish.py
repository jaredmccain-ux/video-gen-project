import json
import tempfile
import unittest
from pathlib import Path

from short_drama.studio_finish import prompt_review, generate_studio_subtitles
from short_drama.config import ProjectConfig


class StudioFinishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name)
        (self.run_dir / "03_shots").mkdir(parents=True)
        (self.run_dir / "02_story").mkdir()
        shots = {
            "shots": [
                {
                    "shot_id": "S001",
                    "duration_s": 6,
                    "planned_start_s": 0,
                    "planned_end_s": 6,
                    "story_purpose": "开场",
                    "generation_mode": "t2va",
                    "action_timeline": "0-6秒说话",
                    "dialogue": [{"speaker_id": "C01", "text": "你来了"}],
                    "subtitle_text": "你来了",
                }
            ]
        }
        (self.run_dir / "03_shots/shots.json").write_text(json.dumps(shots), encoding="utf-8")
        (self.run_dir / "02_story/story.json").write_text(json.dumps({"title": "测"}), encoding="utf-8")
        self.config = ProjectConfig(
            path=self.run_dir / "c.yaml",
            data={},
            project_root=self.run_dir,
            run_root=self.run_dir,
            input_images=(),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_prompt_review_flags_missing_official_structure(self):
        review = prompt_review(self.run_dir)
        self.assertEqual(review["shot_count"], 1)
        self.assertEqual(review["passed"], 0)
        self.assertTrue(review["shots"][0]["errors"])

    def test_studio_subtitles_do_not_require_videos(self):
        result = generate_studio_subtitles(self.run_dir)
        self.assertTrue(result["exists"])
        self.assertEqual(result["subtitle_cue_count"], 1)
        self.assertIn("你来了", result["srt_text"])


if __name__ == "__main__":
    unittest.main()
