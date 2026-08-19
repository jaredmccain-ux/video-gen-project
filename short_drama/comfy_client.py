"""Minimal ComfyUI HTTP client for MiniMax H3 I2V / T2V jobs."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, parse, request


class ComfyClient:
    def __init__(self, base_url: str, timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _request(self, method: str, path: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> bytes:
        req = request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers or {},
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.read()
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ComfyUI HTTP {exc.code} {path}: {body}") from exc

    def system_stats(self) -> dict[str, Any]:
        return json.loads(self._request("GET", "/system_stats"))

    def object_info(self, node_type: str) -> dict[str, Any]:
        return json.loads(self._request("GET", f"/object_info/{parse.quote(node_type)}"))

    def upload_image(
        self, path: Path, *, overwrite: bool = True, remote_name: str | None = None
    ) -> str:
        boundary = f"----ComfyBoundary{uuid.uuid4().hex}"
        filename = remote_name or path.name
        file_bytes = path.read_bytes()
        parts = [
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"),
            file_bytes,
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="overwrite"\r\n\r\n'
            f"{'true' if overwrite else 'false'}\r\n"
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
        body = b"".join(parts)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        payload = json.loads(self._request("POST", "/upload/image", data=body, headers=headers))
        name = payload.get("name")
        if not name:
            raise RuntimeError(f"上传失败：{payload}")
        return str(name)

    def queue_prompt(self, workflow: dict[str, Any], client_id: str | None = None) -> str:
        payload = {"prompt": workflow, "client_id": client_id or uuid.uuid4().hex}
        raw = self._request(
            "POST",
            "/prompt",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(raw)
        if "error" in data:
            raise RuntimeError(f"提交工作流失败：{data}")
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"响应缺少 prompt_id：{data}")
        return str(prompt_id)

    def get_history(self, prompt_id: str) -> dict[str, Any] | None:
        data = json.loads(self._request("GET", f"/history/{prompt_id}"))
        return data.get(prompt_id)

    def wait_history(self, prompt_id: str, *, poll_s: float = 5.0, timeout_s: float = 7200.0) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            item = self.get_history(prompt_id)
            if item and item.get("outputs"):
                return item
            if item and item.get("status", {}).get("status_str") == "error":
                status = item.get("status") or {}
                execution_error = next(
                    (
                        payload
                        for message_type, payload in reversed(status.get("messages") or [])
                        if message_type == "execution_error" and isinstance(payload, dict)
                    ),
                    {},
                )
                error_type = str(execution_error.get("exception_type") or "RuntimeError")
                message = str(execution_error.get("exception_message") or "未知执行错误").strip()
                if error_type == "torch.OutOfMemoryError" or "out of memory" in message.lower() or "Allocation on device" in message:
                    message = "GPU 显存不足；请降低输出分辨率/时长或让 ComfyUI 预留更多采样显存"
                node = str(execution_error.get("node_type") or execution_error.get("node_id") or "未知节点")
                raise RuntimeError(f"ComfyUI 任务失败（{error_type}，节点 {node}）：{message}")
            time.sleep(poll_s)
        raise TimeoutError(f"等待 ComfyUI 任务超时：{prompt_id}")

    def download_view(self, *, filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
        query = (
            "/view?"
            + parse.urlencode(
                {"filename": filename, "subfolder": subfolder, "type": folder_type}
            )
        )
        return self._request("GET", query)
