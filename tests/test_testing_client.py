"""Tests for comfy_runner.testing.client — ComfyTestClient."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from comfy_runner.testing.client import ComfyTestClient, OutputFile, PromptResult


# ---------------------------------------------------------------------------
# queue_prompt
# ---------------------------------------------------------------------------

class TestQueuePrompt:
    @patch("comfy_runner.testing.client.requests.post")
    def test_success(self, mock_post):
        resp = MagicMock()
        resp.json.return_value = {"prompt_id": "abc-123"}
        resp.raise_for_status = MagicMock()
        mock_post.return_value = resp
        client = ComfyTestClient("http://localhost:8188")
        pid = client.queue_prompt({"1": {"class_type": "KSampler", "inputs": {}}})
        assert pid == "abc-123"
        call_kwargs = mock_post.call_args[1]
        assert "prompt" in call_kwargs["json"]

    @patch("comfy_runner.testing.client.requests.post")
    def test_wraps_workflow_in_prompt(self, mock_post):
        resp = MagicMock()
        resp.json.return_value = {"prompt_id": "xyz"}
        resp.raise_for_status = MagicMock()
        mock_post.return_value = resp
        client = ComfyTestClient("http://localhost:8188")
        workflow = {"1": {"class_type": "EmptyLatentImage", "inputs": {}}}
        client.queue_prompt(workflow)
        body = mock_post.call_args[1]["json"]
        assert body["prompt"] == workflow

    @patch("comfy_runner.testing.client.requests.post")
    def test_passes_through_prompt_key(self, mock_post):
        resp = MagicMock()
        resp.json.return_value = {"prompt_id": "xyz"}
        resp.raise_for_status = MagicMock()
        mock_post.return_value = resp
        client = ComfyTestClient("http://localhost:8188")
        body = {"prompt": {"1": {}}, "extra_data": {}}
        client.queue_prompt(body)
        sent = mock_post.call_args[1]["json"]
        assert sent == body

    @patch("comfy_runner.testing.client.requests.post")
    def test_connection_error(self, mock_post):
        mock_post.side_effect = requests.ConnectionError("refused")
        client = ComfyTestClient("http://localhost:8188")
        with pytest.raises(RuntimeError, match="Failed to queue"):
            client.queue_prompt({"1": {}})

    @patch("comfy_runner.testing.client.requests.post")
    def test_rejected_prompt(self, mock_post):
        resp = MagicMock()
        resp.json.return_value = {"error": "Invalid workflow"}
        resp.raise_for_status = MagicMock()
        mock_post.return_value = resp
        client = ComfyTestClient("http://localhost:8188")
        with pytest.raises(RuntimeError, match="rejected"):
            client.queue_prompt({"1": {}})


# ---------------------------------------------------------------------------
# wait_for_completion
# ---------------------------------------------------------------------------

class TestWaitForCompletion:
    @patch("comfy_runner.testing.client.time.sleep")
    @patch("comfy_runner.testing.client.requests.get")
    def test_immediate_completion(self, mock_get, mock_sleep):
        resp = MagicMock()
        resp.json.return_value = {
            "abc-123": {
                "status": {"status_str": "success"},
                "outputs": {},
            }
        }
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        client = ComfyTestClient("http://localhost:8188")
        entry = client.wait_for_completion("abc-123")
        assert entry["status"]["status_str"] == "success"

    @patch("comfy_runner.testing.client.time.sleep")
    @patch("comfy_runner.testing.client.requests.get")
    def test_polls_until_ready(self, mock_get, mock_sleep):
        # First call: not in history yet; second call: completed
        resp_empty = MagicMock()
        resp_empty.json.return_value = {}
        resp_empty.raise_for_status = MagicMock()
        resp_done = MagicMock()
        resp_done.json.return_value = {
            "abc-123": {
                "status": {"status_str": "success"},
                "outputs": {},
            }
        }
        resp_done.raise_for_status = MagicMock()
        mock_get.side_effect = [resp_empty, resp_done]
        client = ComfyTestClient("http://localhost:8188")
        entry = client.wait_for_completion("abc-123")
        assert entry is not None
        assert mock_sleep.call_count == 1

    @patch("comfy_runner.testing.client.time.monotonic")
    @patch("comfy_runner.testing.client.time.sleep")
    @patch("comfy_runner.testing.client.requests.get")
    def test_timeout(self, mock_get, mock_sleep, mock_time):
        resp = MagicMock()
        resp.json.return_value = {}
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        mock_time.side_effect = [0, 999]
        client = ComfyTestClient("http://localhost:8188")
        with pytest.raises(RuntimeError, match="timed out"):
            client.wait_for_completion("abc-123", timeout=10)

    @patch("comfy_runner.testing.client.time.sleep")
    @patch("comfy_runner.testing.client.requests.get")
    def test_execution_error(self, mock_get, mock_sleep):
        resp = MagicMock()
        resp.json.return_value = {
            "abc-123": {
                "status": {"status_str": "error", "messages": ["node 5 failed"]},
                "outputs": {},
            }
        }
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        client = ComfyTestClient("http://localhost:8188")
        with pytest.raises(RuntimeError, match="execution error"):
            client.wait_for_completion("abc-123")

    @patch("comfy_runner.testing.client.time.sleep")
    @patch("comfy_runner.testing.client.requests.get")
    def test_retries_on_connection_error(self, mock_get, mock_sleep):
        resp_done = MagicMock()
        resp_done.json.return_value = {
            "abc-123": {
                "status": {"status_str": "success"},
                "outputs": {},
            }
        }
        resp_done.raise_for_status = MagicMock()
        mock_get.side_effect = [requests.ConnectionError("refused"), resp_done]
        client = ComfyTestClient("http://localhost:8188")
        entry = client.wait_for_completion("abc-123")
        assert entry is not None


# ---------------------------------------------------------------------------
# get_outputs
# ---------------------------------------------------------------------------

class TestGetOutputs:
    def test_extracts_images(self):
        client = ComfyTestClient("http://localhost:8188")
        history = {
            "outputs": {
                "9": {
                    "images": [
                        {"filename": "out_00001_.png", "subfolder": "", "type": "output"},
                    ]
                }
            }
        }
        outputs = client.get_outputs(history)
        assert "9" in outputs
        assert len(outputs["9"]) == 1
        assert outputs["9"][0].filename == "out_00001_.png"

    def test_multiple_output_types(self):
        client = ComfyTestClient("http://localhost:8188")
        history = {
            "outputs": {
                "5": {
                    "images": [{"filename": "a.png", "subfolder": "", "type": "output"}],
                    "gifs": [{"filename": "b.gif", "subfolder": "", "type": "output"}],
                }
            }
        }
        outputs = client.get_outputs(history)
        assert len(outputs["5"]) == 2

    def test_empty_outputs(self):
        client = ComfyTestClient("http://localhost:8188")
        outputs = client.get_outputs({"outputs": {}})
        assert outputs == {}


# ---------------------------------------------------------------------------
# download_output
# ---------------------------------------------------------------------------

class TestDownloadOutput:
    @patch("comfy_runner.testing.client.requests.get")
    def test_downloads_file(self, mock_get, tmp_path):
        resp = MagicMock()
        resp.iter_content.return_value = [b"fake image data"]
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        client = ComfyTestClient("http://localhost:8188")
        of = OutputFile(node_id="9", filename="test.png", subfolder="", type="output")
        path = client.download_output(of, tmp_path)
        assert path.exists()
        assert path.read_bytes() == b"fake image data"
        assert of.local_path == path

    @patch("comfy_runner.testing.client.requests.get")
    def test_path_traversal_sanitized(self, mock_get, tmp_path):
        resp = MagicMock()
        resp.iter_content.return_value = [b"malicious"]
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        client = ComfyTestClient("http://localhost:8188")
        of = OutputFile(node_id="9", filename="../../../etc/passwd", subfolder="", type="output")
        path = client.download_output(of, tmp_path)
        # Should be saved as just "passwd" inside tmp_path, not escaping
        assert path.parent == tmp_path
        assert path.name == "passwd"

    @patch("comfy_runner.testing.client.requests.get")
    def test_download_error(self, mock_get, tmp_path):
        mock_get.side_effect = requests.ConnectionError("refused")
        client = ComfyTestClient("http://localhost:8188")
        of = OutputFile(node_id="9", filename="test.png", subfolder="", type="output")
        with pytest.raises(RuntimeError, match="Failed to download"):
            client.download_output(of, tmp_path)


# ---------------------------------------------------------------------------
# run_workflow (end-to-end convenience method)
# ---------------------------------------------------------------------------

class TestRunWorkflow:
    @patch("comfy_runner.testing.client.requests.get")
    @patch("comfy_runner.testing.client.requests.post")
    @patch("comfy_runner.testing.client.time.sleep")
    def test_end_to_end(self, mock_sleep, mock_post, mock_get, tmp_path):
        # queue_prompt response
        post_resp = MagicMock()
        post_resp.json.return_value = {"prompt_id": "p1"}
        post_resp.raise_for_status = MagicMock()
        mock_post.return_value = post_resp

        # history response (immediate completion, no outputs)
        history_resp = MagicMock()
        history_resp.json.return_value = {
            "p1": {
                "status": {"status_str": "success"},
                "outputs": {},
            }
        }
        history_resp.raise_for_status = MagicMock()
        mock_get.return_value = history_resp

        client = ComfyTestClient("http://localhost:8188")
        workflow = {"1": {"class_type": "EmptyLatentImage", "inputs": {}}}
        result = client.run_workflow(workflow, tmp_path, timeout=30)

        assert isinstance(result, PromptResult)
        assert result.prompt_id == "p1"
        assert result.status == "success"
        assert result.execution_time is not None
