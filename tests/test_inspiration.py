import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from short_drama.config import ProjectConfig
from short_drama.inspiration import generate_story_inspiration, select_inspiration_images


class InspirationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run_dir = self.root / "runs/run-1"
        (self.run_dir / "inputs").mkdir(parents=True)
        (self.root / "assets").mkdir()
        self.config = ProjectConfig(
            path=self.root / "config.yaml",
            data={"llm": {"model": "test-model"}},
            project_root=self.root,
            run_root=self.root / "runs",
            input_images=(),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_image_origin_accepts_any_positive_number_of_images(self):
        paths = []
        for index in range(12):
            path = self.run_dir / "inputs" / f"idea-{index}.png"
            path.write_bytes(b"image")
            paths.append(str(path))
        document = select_inspiration_images(self.run_dir, self.config, paths)
        self.assertEqual(document["active_source"], "images")
        self.assertEqual(len(document["selected_images"]), 12)
        with self.assertRaisesRegex(ValueError, "至少选择 1 张"):
            select_inspiration_images(self.run_dir, self.config, [])

    @patch("short_drama.inspiration.create_text_completion")
    def test_llm_proposal_is_saved_with_history(self, completion):
        proposal = {
            "title": "桥上的蓝布包",
            "logline": "兄弟二人在旧桥发现母亲留下的秘密。",
            "genre": "现实情感",
            "tone": "电影感",
            "hook": "包里传出倒计时声。",
            "characters": [],
            "story_outline": "兄弟相遇、争执并共同揭开秘密。",
            "ending": "两人把蓝布包迎风展开。",
            "visual_motifs": ["蓝布包"],
        }
        completion.return_value = SimpleNamespace(
            id="response-1",
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(proposal, ensure_ascii=False)))],
        )
        document = generate_story_inspiration(
            self.run_dir,
            self.config,
            mode="polish",
            idea_text="两个兄弟在桥上发现母亲留下的包",
        )
        self.assertEqual(document["active_source"], "polish")
        self.assertEqual(document["current_proposal"]["proposal"]["title"], "桥上的蓝布包")
        self.assertEqual(len(document["history"]), 1)
        local_files = document["current_proposal"]["local_files"]
        self.assertTrue((self.run_dir / local_files["json"]).is_file())
        self.assertTrue((self.run_dir / local_files["markdown"]).is_file())
        self.assertFalse(Path(local_files["json"]).is_absolute())
        self.assertTrue((self.run_dir / "02_story/studio_drafts/latest.json").is_file())
        self.assertTrue((self.run_dir / "02_story/studio_drafts/latest.md").is_file())


if __name__ == "__main__":
    unittest.main()
