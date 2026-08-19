import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from short_drama.config import ProjectConfig, load_config
from short_drama.state import default_run_id, read_run
from short_drama.studio_server import StudioApplication


class StudioProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ProjectConfig(
            path=self.root / "project.yaml",
            data={"project_name": "base-project", "input_images": []},
            project_root=self.root,
            run_root=self.root / "runs",
            input_images=(),
        )
        self.app = StudioApplication(self.config, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_chinese_project_name_gets_portable_run_id(self):
        run_id = default_run_id("雨夜来信")
        self.assertTrue(run_id.endswith("-sceneflow-project"))
        self.assertTrue(run_id.isascii())

    def test_create_project_persists_isolated_local_workspace(self):
        project = self.app.create_project("雨夜来信")
        run_dir = self.config.run_root / project["run_id"]

        self.assertEqual(project["project_name"], "雨夜来信")
        self.assertEqual(project["shot_count"], 0)
        self.assertEqual(read_run(run_dir)["project_name"], "雨夜来信")
        self.assertTrue((run_dir / "inputs/studio_uploads").parent.is_dir())
        self.assertTrue((run_dir / "project.config.yaml").is_file())

        second = self.app.create_project("雨夜来信")
        self.assertNotEqual(second["run_id"], project["run_id"])
        self.assertEqual(len(self.app.runs()), 2)

    def test_create_project_rejects_empty_name(self):
        with self.assertRaisesRegex(ValueError, "项目名称"):
            self.app.create_project("   ")

    def test_existing_project_uses_current_machine_service_bindings(self):
        base = yaml.safe_load((Path(__file__).parents[1] / "configs/project.local.yaml").read_text(encoding="utf-8"))
        base["project_root"] = str(self.root)
        base["run_root"] = str(self.root / "runs")
        base["input_images"] = ["old-a.png", "old-b.png", "old-c.png"]
        base["llm"] = {
            "provider": "ark",
            "endpoint": "https://current.example/api/v3",
            "model": "current-model",
            "api_key_env": "CURRENT_KEY",
        }
        service_path = self.root / "service.yaml"
        service_path.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
        app = StudioApplication(load_config(service_path, require_images=False), self.root)

        run_dir = self.root / "runs/old-project"
        run_dir.mkdir(parents=True)
        snapshot = copy.deepcopy(base)
        snapshot["llm"] = {
            "provider": "rivo",
            "endpoint": "https://retired.example/v1",
            "model": "retired-model",
            "api_key_env": "OLD_KEY",
        }
        (run_dir / "project.config.yaml").write_text(
            yaml.safe_dump(snapshot, allow_unicode=True), encoding="utf-8"
        )

        effective = app.run_config(run_dir)
        self.assertEqual(effective.data["llm"]["model"], "current-model")
        self.assertEqual(effective.data["llm"]["endpoint"], "https://current.example/api/v3")
        self.assertEqual(effective.data["project_name"], base["project_name"])


if __name__ == "__main__":
    unittest.main()
