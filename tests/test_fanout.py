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


def _https_required_response() -> MagicMock:
    """Mock 400 response with the Go ``net/http`` HTTPS-on-HTTP signature
    that Tailscale serve emits for plain-HTTP requests to its HTTPS port."""
    resp = MagicMock()
    resp.ok = False
    resp.status_code = 400
    resp.content = b"x"
    resp.text = "Client sent an HTTP request to an HTTPS server.\n\n"
    # .json() will not be called because the retry logic short-circuits
    # before the success path; still, return something safe.
    resp.json.return_value = {}
    return resp


class TestFanoutHttpsRetry:
    """Mirror probe_system_info's HTTPS-fallback behaviour: when the
    runner is fronted by ``tailscale serve`` and discovery somehow gave
    us an HTTP+IP server_url, retry the POST against HTTPS+FQDN so a
    stale URL or a races doesn't sink the whole sweep."""

    def test_retries_over_https_fqdn_on_signature(self):
        target = {
            "hostname": "comfy-x",
            "server_url": "http://1.2.3.4:9189",
            "fqdn": "comfy-x.tail.ts.net",
        }
        good = _ok_response({"ok": True, "updated": True, "message": "pulled"})
        with patch("requests.post", side_effect=[_https_required_response(), good]) as post:
            results = fo.fanout_self_update([target])
        assert post.call_count == 2
        # First: original HTTP+IP URL.
        assert post.call_args_list[0].args[0] == \
            "http://1.2.3.4:9189/self-update"
        # Second: canonical HTTPS+FQDN URL.
        assert post.call_args_list[1].args[0] == \
            "https://comfy-x.tail.ts.net:9189/self-update"
        r = results[0]
        assert r["ok"] is True
        assert r["updated"] is True
        # host_label reflects the URL that actually succeeded.
        assert r["host"] == "https://comfy-x.tail.ts.net:9189/self-update"

    def test_no_retry_without_fqdn(self):
        # Without an fqdn we can't safely retry over HTTPS (cert SAN
        # would not include the IP); surface the 400 as-is.
        target = {"hostname": "h", "server_url": "http://1.2.3.4:9189"}
        with patch("requests.post", return_value=_https_required_response()) as post:
            results = fo.fanout_self_update([target])
        assert post.call_count == 1
        r = results[0]
        assert r["ok"] is False
        assert r["status"] == 400

    def test_no_retry_on_generic_400(self):
        # A non-signature 400 (malformed body, etc.) must NOT trigger
        # an HTTPS retry — only the specific Go HTTP-on-HTTPS body does.
        target = {
            "hostname": "h",
            "server_url": "http://1.2.3.4:9189",
            "fqdn": "h.tail.ts.net",
        }
        bad = MagicMock()
        bad.ok = False
        bad.status_code = 400
        bad.content = b"x"
        bad.text = "bad request"
        bad.json.return_value = {"ok": False, "error": "bad request"}
        with patch("requests.post", return_value=bad) as post:
            results = fo.fanout_self_update([target])
        assert post.call_count == 1
        assert results[0]["ok"] is False

    def test_no_retry_when_already_https(self):
        # If discovery already gave us HTTPS, we trust it; getting the
        # signature back from an HTTPS URL would be very unusual and
        # retrying would just loop.
        target = {
            "hostname": "h",
            "server_url": "https://h.tail.ts.net:9189",
            "fqdn": "h.tail.ts.net",
        }
        with patch("requests.post", return_value=_https_required_response()) as post:
            results = fo.fanout_self_update([target])
        assert post.call_count == 1
        assert results[0]["ok"] is False

    def test_https_retry_transport_failure_recorded(self):
        target = {
            "hostname": "h",
            "server_url": "http://1.2.3.4:9189",
            "fqdn": "h.tail.ts.net",
        }
        seq = [_https_required_response(), ConnectionError("boom")]
        with patch("requests.post", side_effect=seq) as post:
            results = fo.fanout_self_update([target])
        assert post.call_count == 2
        r = results[0]
        assert r["ok"] is False
        assert r["status"] == "EXC"
        assert "boom" in r["error"]
        # host_label now reflects the HTTPS URL we attempted.
        assert r["host"] == "https://h.tail.ts.net:9189/self-update"

    def test_retry_uses_host_port_when_no_server_url(self):
        # Same retry behaviour when caller supplied host+port instead
        # of an explicit server_url.
        target = {
            "hostname": "h",
            "host": "1.2.3.4",
            "fqdn": "h.tail.ts.net",
        }
        good = _ok_response({"ok": True, "updated": False, "message": "noop"})
        with patch("requests.post", side_effect=[_https_required_response(), good]) as post:
            results = fo.fanout_self_update([target])
        assert post.call_count == 2
        assert post.call_args_list[0].args[0] == \
            "http://1.2.3.4:9189/self-update"
        assert post.call_args_list[1].args[0] == \
            "https://h.tail.ts.net:9189/self-update"
        assert results[0]["ok"] is True

    def test_retry_preserves_custom_port_from_server_url(self):
        # If the original server_url uses a non-default port, the
        # HTTPS retry must keep that port — not silently reset to 9189.
        target = {
            "hostname": "h",
            "server_url": "http://1.2.3.4:8000",
            "fqdn": "h.tail.ts.net",
        }
        good = _ok_response({"ok": True, "updated": False, "message": ""})
        with patch("requests.post", side_effect=[_https_required_response(), good]) as post:
            results = fo.fanout_self_update([target])
        assert post.call_count == 2
        assert post.call_args_list[1].args[0] == \
            "https://h.tail.ts.net:8000/self-update"
        assert results[0]["ok"] is True

    def test_retry_5xx_response_recorded(self):
        # HTTPS retry succeeds at the transport layer but the runner
        # itself returns 502 — surface it as a per-target failure with
        # the retry URL as the host label.
        target = {
            "hostname": "h",
            "server_url": "http://1.2.3.4:9189",
            "fqdn": "h.tail.ts.net",
        }
        bad_5xx = _error_response(502, {"ok": False, "error": "bad gateway"})
        with patch("requests.post", side_effect=[_https_required_response(), bad_5xx]) as post:
            results = fo.fanout_self_update([target])
        assert post.call_count == 2
        r = results[0]
        assert r["ok"] is False
        assert r["status"] == 502
        assert r["host"] == "https://h.tail.ts.net:9189/self-update"

    def test_retry_unparseable_body_uses_raw(self):
        # HTTPS retry returns 200 OK but the body isn't valid JSON
        # (e.g. an HTML error page from a misbehaving proxy). The
        # raw text must surface so operators see *something* useful.
        target = {
            "hostname": "h",
            "server_url": "http://1.2.3.4:9189",
            "fqdn": "h.tail.ts.net",
        }
        garbled = MagicMock()
        # 200 OK at the transport layer, but body isn't JSON — the
        # ok-check (`resp.ok and body.get("ok", False)`) still fails
        # because body falls back to {"raw": ...} which has no "ok" key.
        garbled.ok = True
        garbled.status_code = 200
        garbled.content = b"<html>nope</html>"
        garbled.text = "<html>nope</html>"
        garbled.json.side_effect = ValueError("not json")
        with patch("requests.post", side_effect=[_https_required_response(), garbled]) as post:
            results = fo.fanout_self_update([target])
        assert post.call_count == 2
        r = results[0]
        assert r["ok"] is False
        # raw fallback bubbles up via the error-resolution chain.
        assert "<html>" in r["error"]
