import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from short_drama.azure_client import _is_retryable_llm_error, _request_extra_body, _safe_log, completion_text
from short_drama.config import ProjectConfig


class AzureClientCompatibilityTests(unittest.TestCase):
    def test_ark_disables_thinking_by_default_for_structured_outputs(self):
        config = ProjectConfig(
            path=Path("config.yaml"),
            data={"llm": {"provider": "ark", "model": "doubao-seed-evolving"}},
            project_root=Path("."),
            run_root=Path("runs"),
            input_images=(),
        )
        self.assertEqual(
            _request_extra_body(config, "https://ark.cn-beijing.volces.com/api/v3"),
            {"thinking": {"type": "disabled"}},
        )

    def test_empty_content_after_reasoning_exhaustion_has_actionable_error(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content="", reasoning_content="仍在思考"),
            )]
        )
        with self.assertRaisesRegex(ValueError, "深度思考耗尽"):
            completion_text(response)

    def test_detached_stdout_cannot_abort_llm_request_logging(self):
        with patch("builtins.print", side_effect=BrokenPipeError(32, "Broken pipe")):
            _safe_log("request is starting")

    def test_broken_transport_errors_are_retryable(self):
        self.assertTrue(_is_retryable_llm_error(BrokenPipeError(32, "Broken pipe")))
        self.assertTrue(_is_retryable_llm_error(RuntimeError("Server disconnected without sending a response")))


if __name__ == "__main__":
    unittest.main()
