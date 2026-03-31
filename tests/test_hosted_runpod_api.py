"""Tests for comfy_runner.hosted.runpod_api — _request error handling."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from comfy_runner.hosted.runpod_api import RunPodAPI


@pytest.fixture()
def api():
    return RunPodAPI(api_key="test-key")


class TestRequest:
    """Test the _request method's error-handling paths."""

    @patch("comfy_runner.hosted.runpod_api.requests.request")
    def test_timeout_raises_runtime_error(self, mock_req, api):
        mock_req.side_effect = requests.ConnectionError("timed out")
        with pytest.raises(RuntimeError, match="Failed to connect"):
            api._request("GET", "/pods")

    @patch("comfy_runner.hosted.runpod_api.requests.request")
    def test_request_exception_raises_runtime_error(self, mock_req, api):
        mock_req.side_effect = requests.Timeout("timeout")
        with pytest.raises(RuntimeError, match="Failed to connect"):
            api._request("GET", "/pods")

    @patch("comfy_runner.hosted.runpod_api.requests.request")
    def test_4xx_raises_with_status(self, mock_req, api):
        resp = MagicMock()
        resp.status_code = 401
        resp.ok = False
        resp.text = "Unauthorized"
        mock_req.return_value = resp
        with pytest.raises(RuntimeError, match="401"):
            api._request("GET", "/pods")

    @patch("comfy_runner.hosted.runpod_api.requests.request")
    def test_5xx_raises_with_status(self, mock_req, api):
        resp = MagicMock()
        resp.status_code = 500
        resp.ok = False
        resp.text = "Internal Server Error"
        mock_req.return_value = resp
        with pytest.raises(RuntimeError, match="500"):
            api._request("POST", "/pods")

    @patch("comfy_runner.hosted.runpod_api.requests.request")
    def test_204_returns_none(self, mock_req, api):
        resp = MagicMock()
        resp.status_code = 204
        resp.ok = True
        mock_req.return_value = resp
        assert api._request("DELETE", "/pods/abc") is None

    @patch("comfy_runner.hosted.runpod_api.requests.request")
    def test_json_parse_error_raises(self, mock_req, api):
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.json.side_effect = requests.JSONDecodeError("err", "doc", 0)
        resp.text = "<html>not json</html>"
        mock_req.return_value = resp
        with pytest.raises(RuntimeError, match="invalid JSON"):
            api._request("GET", "/pods")

    @patch("comfy_runner.hosted.runpod_api.requests.request")
    def test_success_returns_parsed_dict(self, mock_req, api):
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.json.return_value = {"id": "pod_123", "name": "test"}
        mock_req.return_value = resp
        result = api._request("GET", "/pods/pod_123")
        assert result == {"id": "pod_123", "name": "test"}


class TestHeaders:
    def test_auth_header_contains_api_key(self, api):
        headers = api._headers()
        assert headers["Authorization"] == "Bearer test-key"
        assert headers["Content-Type"] == "application/json"


class TestHighLevelMethods:
    """Verify high-level methods delegate to _request correctly."""

    @patch.object(RunPodAPI, "_request")
    def test_create_pod(self, mock_req, api):
        mock_req.return_value = {"id": "p1"}
        result = api.create_pod(name="test", gpuTypeIds=["A100"])
        mock_req.assert_called_once_with(
            "POST", "/pods", json={"name": "test", "gpuTypeIds": ["A100"]},
        )
        assert result == {"id": "p1"}

    @patch.object(RunPodAPI, "_request")
    def test_list_pods_empty(self, mock_req, api):
        mock_req.return_value = None
        assert api.list_pods() == []

    @patch.object(RunPodAPI, "_request")
    def test_create_volume(self, mock_req, api):
        mock_req.return_value = {"id": "v1"}
        result = api.create_volume("ws", 50, "US-KS-2")
        mock_req.assert_called_once_with(
            "POST", "/networkvolumes",
            json={"name": "ws", "size": 50, "dataCenterId": "US-KS-2"},
        )
        assert result == {"id": "v1"}

    @patch.object(RunPodAPI, "_request")
    def test_delete_volume(self, mock_req, api):
        mock_req.return_value = None
        api.delete_volume("vol_x")
        mock_req.assert_called_once_with("DELETE", "/networkvolumes/vol_x")
