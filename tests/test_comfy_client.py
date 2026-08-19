import unittest
from unittest.mock import patch

from short_drama.comfy_client import ComfyClient
from short_drama.studio_server import GenerationManager


class ComfyClientTests(unittest.TestCase):
    def test_wait_history_summarizes_oom_without_tensor_dump(self):
        client = ComfyClient("http://127.0.0.1:6006")
        history = {
            "status": {
                "status_str": "error",
                "messages": [[
                    "execution_error",
                    {
                        "node_type": "SamplerCustomAdvanced",
                        "exception_type": "torch.OutOfMemoryError",
                        "exception_message": "Allocation on device\nThis error means you ran out of memory on your GPU.",
                        "current_inputs": {"latent_image": "very-large-tensor-dump"},
                    },
                ]],
            }
        }
        with patch.object(client, "get_history", return_value=history):
            with self.assertRaisesRegex(RuntimeError, "GPU 显存不足") as caught:
                client.wait_history("prompt-id", poll_s=0, timeout_s=1)
        self.assertNotIn("very-large-tensor-dump", str(caught.exception))

    def test_public_job_summarizes_legacy_oom_and_hides_traceback(self):
        result = GenerationManager.public_job({
            "job_id": "S002-old",
            "status": "failed",
            "error": "ComfyUI failed: torch.OutOfMemoryError Allocation on device " + "x" * 5000,
            "traceback": "large traceback",
        })
        self.assertEqual(result["error"], "GPU 显存不足；已调整 ComfyUI 的模型卸载策略，请重新生成本镜头")
        self.assertNotIn("traceback", result)


if __name__ == "__main__":
    unittest.main()
