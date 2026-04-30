"""Tests for comfy_runner.hosted.fanout — parallel POST /self-update
across discovered comfy-runners."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfy_runner.hosted import fanout as fo  # noqa: E402


def _ok_response(payload: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.ok = 200 <= status < 300
    resp.status_code = status
    resp.content = b"x"  # truthy so .json() is attempted
    resp.json.return_value = payload
    resp.text = ""
    return resp


def _error_response(status: int, payload: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.ok = False
    resp.status_code = status
    resp.content = b"x"
    resp.json.return_value = payload or {"ok": False, "error": "boom"}
    resp.text = "boom"
    return resp


class TestFanoutSelfUpdate:
    def test_empty_targets_returns_empty(self):
        assert fo.fanout_self_update([]) == []

    def test_uses_server_url_when_present(self):
        target = {"hostname": "comfy-pr-1", "server_url": "http://1.2.3.4:9189"}
        with patch("requests.post") as post:
            post.return_value = _ok_response({"ok": True, "updated": True, "message": "pulled"})
            results = fo.fanout_self_update([target])
        assert len(results) == 1
        r = results[0]
        assert r["name"] == "comfy-pr-1"
        assert r["host"] == "http://1.2.3.4:9189"
        assert r["ok"] is True
        assert r["updated"] is True
        assert r["message"] == "pulled"
        assert r["error"] is None
        # URL must be server_url + /self-update
        assert post.call_args.args[0] == "http://1.2.3.4:9189/self-update"

    def test_falls_back_to_host_port(self):
        target = {"hostname": "comfy-x", "host": "1.2.3.4"}
        with patch("requests.post") as post:
            post.return_value = _ok_response({"ok": True, "updated": False, "message": "Already up to date"})
            results = fo.fanout_self_update([target])
        assert post.call_args.args[0] == "http://1.2.3.4:9189/self-update"
        assert results[0]["updated"] is False
        assert results[0]["ok"] is True

    def test_force_flag_propagates(self):
        target = {"hostname": "h", "host": "1.1.1.1"}
        with patch("requests.post") as post:
            post.return_value = _ok_response({"ok": True})
            fo.fanout_self_update([target], force=True)
        assert post.call_args.kwargs["json"] == {"force": True}

    def test_force_default_false(self):
        target = {"hostname": "h", "host": "1.1.1.1"}
        with patch("requests.post") as post:
            post.return_value = _ok_response({"ok": True})
            fo.fanout_self_update([target])
        assert post.call_args.kwargs["json"] == {"force": False}

    def test_missing_host_and_server_url_returns_error(self):
        results = fo.fanout_self_update([{"hostname": "lonely"}])
        assert len(results) == 1
        r = results[0]
        assert r["ok"] is False
        assert r["status"] == "EXC"
        assert "missing" in r["error"]

    def test_transport_exception_recorded(self):
        target = {"hostname": "h", "host": "1.1.1.1"}
        with patch("requests.post", side_effect=ConnectionError("nope")):
            results = fo.fanout_self_update([target])
        r = results[0]
        assert r["ok"] is False
        assert r["status"] == "EXC"
        assert "nope" in r["error"]
        assert r["updated"] is False

    def test_http_500_recorded(self):
        target = {"hostname": "h", "host": "1.1.1.1"}
        with patch("requests.post", return_value=_error_response(500)):
            results = fo.fanout_self_update([target])
        r = results[0]
        assert r["ok"] is False
        assert r["status"] == 500
        assert r["error"] == "boom"

    def test_http_200_with_ok_false_is_failure(self):
        # The pod responded but reported ok=False (e.g. "git pull failed").
        target = {"hostname": "h", "host": "1.1.1.1"}
        bad = _ok_response({"ok": False, "error": "git pull failed"}, status=200)
        with patch("requests.post", return_value=bad):
            results = fo.fanout_self_update([target])
        r = results[0]
        assert r["ok"] is False
        assert r["status"] == 200
        assert r["error"] == "git pull failed"
        assert r["updated"] is False

    def test_mixed_success_and_failure(self):
        targets = [
            {"hostname": "ok-pod", "host": "1.1.1.1"},
            {"hostname": "bad-pod", "host": "2.2.2.2"},
            {"hostname": "down-pod", "host": "3.3.3.3"},
        ]

        def _post(url, **kwargs):
            if "1.1.1.1" in url:
                return _ok_response({"ok": True, "updated": True, "message": "pulled"})
            if "2.2.2.2" in url:
                return _error_response(503, {"ok": False, "error": "service down"})
            raise ConnectionError("unreachable")

        with patch("requests.post", side_effect=_post):
            results = fo.fanout_self_update(targets)

        by_name = {r["name"]: r for r in results}
        assert by_name["ok-pod"]["ok"] is True
        assert by_name["ok-pod"]["updated"] is True
        assert by_name["bad-pod"]["ok"] is False
        assert by_name["bad-pod"]["status"] == 503
        assert by_name["down-pod"]["ok"] is False
        assert by_name["down-pod"]["status"] == "EXC"
        assert "unreachable" in by_name["down-pod"]["error"]

    def test_results_preserve_input_order(self):
        # Even though completion order is non-deterministic, the result
        # list must mirror the input order so callers can zip / index.
        targets = [{"hostname": f"h{i}", "host": f"1.1.1.{i}"} for i in range(5)]

        # Make h0 finish last by introducing a small per-host delay.
        import time

        def _post(url, **kwargs):
            if "1.1.1.0" in url:
                time.sleep(0.05)
            return _ok_response({"ok": True, "updated": False, "message": ""})

        with patch("requests.post", side_effect=_post):
            results = fo.fanout_self_update(targets)
        assert [r["name"] for r in results] == [t["hostname"] for t in targets]

    def test_timeout_kwarg_forwarded(self):
        target = {"hostname": "h", "host": "1.1.1.1"}
        with patch("requests.post") as post:
            post.return_value = _ok_response({"ok": True})
            fo.fanout_self_update([target], timeout=5.0)
        assert post.call_args.kwargs["timeout"] == 5.0
