"""Tests for comfy_runner.hosted.tailnet — Tailscale device listing and
comfy-runner discovery via /system-info probing."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfy_runner.hosted import tailnet as tn  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with a clean device cache."""
    tn._clear_devices_cache()
    yield
    tn._clear_devices_cache()


# ---------------------------------------------------------------------------
# list_devices — Tailscale REST API + cache
# ---------------------------------------------------------------------------

def _ok_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


class TestListDevices:
    def test_no_credentials_returns_empty(self):
        with patch.object(tn, "get_tailscale_api_key", return_value=""), \
             patch.object(tn, "get_tailscale_tailnet", return_value=""):
            assert tn.list_devices() == []

    def test_no_tailnet_returns_empty(self):
        with patch.object(tn, "get_tailscale_api_key", return_value="ts_key"), \
             patch.object(tn, "get_tailscale_tailnet", return_value=""):
            assert tn.list_devices() == []

    def test_happy_path_calls_api_with_bearer(self):
        with patch.object(tn, "get_tailscale_api_key", return_value="ts_key"), \
             patch.object(tn, "get_tailscale_tailnet", return_value="ex.com"), \
             patch("requests.get") as get:
            get.return_value = _ok_response({"devices": [{"hostname": "h1"}]})
            devices = tn.list_devices()
        assert devices == [{"hostname": "h1"}]
        url = get.call_args.args[0]
        assert "api.tailscale.com" in url
        assert "/tailnet/ex.com/devices" in url
        assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer ts_key"

    def test_http_error_returns_empty(self):
        bad = MagicMock()
        bad.ok = False
        bad.status_code = 401
        with patch.object(tn, "get_tailscale_api_key", return_value="k"), \
             patch.object(tn, "get_tailscale_tailnet", return_value="ex"), \
             patch("requests.get", return_value=bad):
            assert tn.list_devices() == []

    def test_transport_exception_returns_empty(self):
        with patch.object(tn, "get_tailscale_api_key", return_value="k"), \
             patch.object(tn, "get_tailscale_tailnet", return_value="ex"), \
             patch("requests.get", side_effect=ConnectionError("boom")):
            assert tn.list_devices() == []

    def test_caches_within_ttl(self):
        with patch.object(tn, "get_tailscale_api_key", return_value="k"), \
             patch.object(tn, "get_tailscale_tailnet", return_value="ex"), \
             patch("requests.get") as get:
            get.return_value = _ok_response({"devices": [{"hostname": "h1"}]})
            tn.list_devices()
            tn.list_devices()
            tn.list_devices()
        assert get.call_count == 1

    def test_force_refresh_bypasses_cache(self):
        with patch.object(tn, "get_tailscale_api_key", return_value="k"), \
             patch.object(tn, "get_tailscale_tailnet", return_value="ex"), \
             patch("requests.get") as get:
            get.return_value = _ok_response({"devices": [{"hostname": "h1"}]})
            tn.list_devices()
            tn.list_devices(force=True)
        assert get.call_count == 2

    def test_negative_caches_http_failure(self):
        # An HTTP failure must update the cache TTL so queued callers
        # don't all serially time out against the same broken endpoint.
        bad = MagicMock()
        bad.ok = False
        bad.status_code = 503
        with patch.object(tn, "get_tailscale_api_key", return_value="k"), \
             patch.object(tn, "get_tailscale_tailnet", return_value="ex"), \
             patch("requests.get", return_value=bad) as get:
            tn.list_devices()
            tn.list_devices()
            tn.list_devices()
        assert get.call_count == 1
        assert tn.get_last_devices_error() == "HTTP 503"

    def test_negative_caches_transport_exception(self):
        with patch.object(tn, "get_tailscale_api_key", return_value="k"), \
             patch.object(tn, "get_tailscale_tailnet", return_value="ex"), \
             patch("requests.get", side_effect=ConnectionError("boom")) as get:
            tn.list_devices()
            tn.list_devices()
        assert get.call_count == 1
        err = tn.get_last_devices_error()
        assert err is not None
        assert "boom" in err

    def test_last_error_cleared_on_success(self):
        # After a failure, a subsequent successful refresh must clear
        # the recorded error so callers don't see a stale failure flag.
        bad = MagicMock()
        bad.ok = False
        bad.status_code = 500
        good = _ok_response({"devices": [{"hostname": "h1"}]})
        with patch.object(tn, "get_tailscale_api_key", return_value="k"), \
             patch.object(tn, "get_tailscale_tailnet", return_value="ex"), \
             patch("requests.get", side_effect=[bad, good]):
            tn.list_devices()
            assert tn.get_last_devices_error() == "HTTP 500"
            tn.list_devices(force=True)
            assert tn.get_last_devices_error() is None


# ---------------------------------------------------------------------------
# Hostname / online helpers
# ---------------------------------------------------------------------------

class TestDeviceHelpers:
    def test_probe_host_prefers_ipv4(self):
        d = {
            "addresses": ["fd7a:115c:a1e0::1", "100.64.0.5"],
            "name": "box.ts.net",
        }
        assert tn._device_probe_host(d) == "100.64.0.5"

    def test_probe_host_falls_back_to_fqdn(self):
        d = {"addresses": [], "name": "box.ts.net"}
        assert tn._device_probe_host(d) == "box.ts.net"

    def test_probe_host_returns_none_when_no_address(self):
        assert tn._device_probe_host({"addresses": [], "name": ""}) is None

    def test_short_hostname_from_hostname_field(self):
        assert tn._device_short_hostname({"hostname": "box"}) == "box"

    def test_short_hostname_falls_back_to_name(self):
        assert tn._device_short_hostname({"name": "box.ts.net"}) == "box"

    def test_is_online_bool_true(self):
        assert tn._is_device_online({"online": True}) is True

    def test_is_online_string_true(self):
        assert tn._is_device_online({"online": "true"}) is True

    def test_is_online_missing_false(self):
        assert tn._is_device_online({}) is False


# ---------------------------------------------------------------------------
# probe_system_info
# ---------------------------------------------------------------------------

class TestProbeSystemInfo:
    def test_happy_path_returns_inner_dict(self):
        resp = _ok_response({"ok": True, "system_info": {"gpu_label": "NVIDIA"}})
        with patch("requests.get", return_value=resp) as get:
            info = tn.probe_system_info("100.64.0.5")
        assert info == {"gpu_label": "NVIDIA"}
        assert get.call_args.args[0] == "http://100.64.0.5:9189/system-info"

    def test_non_ok_response_returns_none(self):
        resp = MagicMock()
        resp.ok = False
        with patch("requests.get", return_value=resp):
            assert tn.probe_system_info("100.64.0.5") is None

    def test_payload_ok_false_returns_none(self):
        resp = _ok_response({"ok": False, "error": "not a comfy-runner"})
        with patch("requests.get", return_value=resp):
            assert tn.probe_system_info("100.64.0.5") is None

    def test_missing_system_info_returns_none(self):
        resp = _ok_response({"ok": True})
        with patch("requests.get", return_value=resp):
            assert tn.probe_system_info("100.64.0.5") is None

    def test_transport_exception_returns_none(self):
        with patch("requests.get", side_effect=ConnectionError("nope")):
            assert tn.probe_system_info("100.64.0.5") is None

    def test_custom_port_and_scheme(self):
        resp = _ok_response({"ok": True, "system_info": {}})
        with patch("requests.get", return_value=resp) as get:
            tn.probe_system_info(
                "h.ts.net", port=8000, scheme="https", timeout=5,
            )
        assert get.call_args.args[0] == "https://h.ts.net:8000/system-info"
        assert get.call_args.kwargs["timeout"] == 5


# ---------------------------------------------------------------------------
# _summarise_gpu / _ram_gb
# ---------------------------------------------------------------------------

class TestSummariseGpu:
    def test_first_gpu_with_model_and_vram(self):
        info = {
            "gpu_label": "NVIDIA",
            "gpus": [{"vendor": "nvidia", "model": "RTX 4090", "vram_mb": 24576}],
        }
        assert tn._summarise_gpu(info) == "RTX 4090 (24576 MB)"

    def test_first_gpu_model_only(self):
        info = {"gpus": [{"model": "Apple M2 Max", "vram_mb": None}]}
        assert tn._summarise_gpu(info) == "Apple M2 Max"

    def test_falls_back_to_label_when_no_gpus(self):
        info = {"gpu_label": "Apple Silicon", "gpus": []}
        assert tn._summarise_gpu(info) == "Apple Silicon"

    def test_empty_when_nothing_known(self):
        assert tn._summarise_gpu({}) == ""

    def test_handles_malformed_gpus_list(self):
        info = {"gpus": "not-a-list", "gpu_label": "AMD"}
        assert tn._summarise_gpu(info) == "AMD"


class TestRamGb:
    def test_int_value(self):
        assert tn._ram_gb({"total_memory_gb": 64}) == 64

    def test_string_int(self):
        assert tn._ram_gb({"total_memory_gb": "32"}) == 32

    def test_missing_returns_none(self):
        assert tn._ram_gb({}) is None

    def test_invalid_returns_none(self):
        assert tn._ram_gb({"total_memory_gb": "huge"}) is None


# ---------------------------------------------------------------------------
# _match_pod_record — runpod join logic
# ---------------------------------------------------------------------------

class TestMatchPodRecord:
    _RECORDS = {
        "pr-1234": {"id": "p1", "purpose": "pr", "pr_number": 1234},
        "dev-box": {"id": "p2", "purpose": "persistent"},
    }

    def test_exact_match_after_comfy_prefix(self):
        match = tn._match_pod_record("comfy-pr-1234", self._RECORDS)
        assert match is not None
        name, rec = match
        assert name == "pr-1234"
        assert rec["purpose"] == "pr"

    def test_match_with_drift_suffix(self):
        # comfy-dev-box-1 → strip suffix → dev-box
        match = tn._match_pod_record("comfy-dev-box-1", self._RECORDS)
        assert match is not None
        name, rec = match
        assert name == "dev-box"
        assert rec["purpose"] == "persistent"

    def test_no_comfy_prefix_returns_none(self):
        assert tn._match_pod_record("my-laptop", self._RECORDS) is None

    def test_unknown_pod_returns_none(self):
        assert tn._match_pod_record("comfy-mystery", self._RECORDS) is None

    def test_literal_n_suffix_pod_matches_itself(self):
        # When a pod's literal name ends in -N (e.g. PR pods are
        # "pr-1234"), the exact-match path must win — even if the same
        # registry also has a record with the suffix-stripped form.
        records = {
            "dev": {"id": "p1", "purpose": "persistent"},
            "dev-1": {"id": "p2", "purpose": "test"},
        }
        match = tn._match_pod_record("comfy-dev-1", records)
        assert match is not None
        name, rec = match
        # Must be the literal "dev-1" pod, NOT the suffix-stripped "dev".
        assert name == "dev-1"
        assert rec["id"] == "p2"

    def test_drift_suffix_only_used_when_exact_fails(self):
        # No literal record for "pr-1234-1" — fall back to "pr-1234".
        match = tn._match_pod_record("comfy-pr-1234-1", self._RECORDS)
        assert match is not None
        name, rec = match
        assert name == "pr-1234"
        assert rec["pr_number"] == 1234


# ---------------------------------------------------------------------------
# discover_comfy_runners — end-to-end
# ---------------------------------------------------------------------------

class TestDiscoverComfyRunners:
    """Drive list_devices and probe_system_info in parallel; assert
    the joined result shape."""

    _DEVICES = [
        {
            "hostname": "comfy-pr-1234",
            "name": "comfy-pr-1234.tn.ts.net",
            "addresses": ["100.64.0.10"],
            "online": True,
        },
        {
            "hostname": "my-laptop",
            "name": "my-laptop.tn.ts.net",
            "addresses": ["100.64.0.20"],
            "online": True,
        },
        {
            # Online but doesn't run comfy-runner — probe will return None.
            "hostname": "router",
            "name": "router.tn.ts.net",
            "addresses": ["100.64.0.30"],
            "online": True,
        },
        {
            # Offline — must be skipped without probing.
            "hostname": "comfy-old-box",
            "name": "comfy-old-box.tn.ts.net",
            "addresses": ["100.64.0.40"],
            "online": False,
        },
    ]

    _POD_RECORDS = {
        "pr-1234": {
            "id": "p1", "purpose": "pr", "pr_number": 1234,
            "gpu_type": "RTX 4090",
        },
    }

    _DEFAULT_PROBES = {
        "100.64.0.10": {
            "platform": "linux",
            "os_release": "Ubuntu 22.04",
            "gpu_label": "NVIDIA",
            "gpus": [{
                "vendor": "nvidia", "model": "RTX 4090",
                "vram_mb": 24576,
            }],
            "total_memory_gb": 64,
        },
        "100.64.0.20": {
            "platform": "darwin",
            "os_release": "macOS 14.5",
            "gpu_label": "Apple Silicon",
            "gpus": [{
                "vendor": "mps", "model": "Apple M2 Max",
                "vram_mb": None,
            }],
            "total_memory_gb": 32,
        },
        "100.64.0.30": None,
    }

    def _patches(
        self,
        *,
        devices=None,
        pod_records=None,
        probe_returns=None,
        api_key="k",
        tailnet="ex",
    ):
        """Return a stacked context manager applying all module patches."""
        if devices is None:
            devices = list(self._DEVICES)
        if pod_records is None:
            pod_records = dict(self._POD_RECORDS)
        if probe_returns is None:
            probe_returns = dict(self._DEFAULT_PROBES)

        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(
            tn, "list_devices", return_value=list(devices),
        ))
        stack.enter_context(patch.object(
            tn, "list_pod_records", return_value=dict(pod_records),
        ))
        stack.enter_context(patch.object(
            tn, "probe_system_info",
            side_effect=lambda host, *a, **kw: probe_returns.get(host),
        ))
        stack.enter_context(patch.object(
            tn, "get_tailscale_api_key", return_value=api_key,
        ))
        stack.enter_context(patch.object(
            tn, "get_tailscale_tailnet", return_value=tailnet,
        ))
        return stack

    def test_returns_only_responders(self):
        with self._patches():
            result = tn.discover_comfy_runners()
        assert result["ok"] is True
        # Two responders out of three online devices; the offline one
        # was never probed.
        assert len(result["runners"]) == 2
        names = {r["hostname"] for r in result["runners"]}
        assert names == {"comfy-pr-1234", "my-laptop"}
        assert result["device_count"] == 4
        assert result["online_count"] == 3
        assert result["tailnet_configured"] is True

    def test_runpod_join_attaches_metadata(self):
        with self._patches():
            result = tn.discover_comfy_runners()
        by_name = {r["hostname"]: r for r in result["runners"]}
        runpod = by_name["comfy-pr-1234"]
        assert runpod["provider"] == "runpod"
        assert runpod["pod_name"] == "pr-1234"
        assert runpod["purpose"] == "pr"
        assert runpod["pr_number"] == 1234
        # RunPod gpu_type label wins over /system-info.
        assert runpod["gpu"] == "RTX 4090"

        local = by_name["my-laptop"]
        assert local["provider"] == "local"
        assert local["pod_name"] is None
        assert local["purpose"] is None
        assert local["pr_number"] is None
        # /system-info-derived fields:
        assert local["gpu"] == "Apple M2 Max"
        assert local["ram_gb"] == 32
        assert local["platform"] == "darwin"
        assert local["os"] == "macOS 14.5"

    def test_no_credentials_short_circuits(self):
        with self._patches(devices=[], api_key="", tailnet=""):
            result = tn.discover_comfy_runners()
        assert result["ok"] is True
        assert result["runners"] == []
        assert result["tailnet_configured"] is False
        assert result["device_count"] == 0

    def test_offline_devices_not_probed(self):
        # Offline devices must not be passed to probe_system_info.
        seen: list[str] = []
        def _probe(host, *a, **kw):
            seen.append(host)
            return None  # Nothing responds; we only care which hosts got probed.
        with patch.object(tn, "list_devices", return_value=list(self._DEVICES)), \
             patch.object(tn, "list_pod_records", return_value={}), \
             patch.object(tn, "probe_system_info", side_effect=_probe), \
             patch.object(tn, "get_tailscale_api_key", return_value="k"), \
             patch.object(tn, "get_tailscale_tailnet", return_value="ex"):
            tn.discover_comfy_runners()
        # Three online devices probed; the offline "comfy-old-box" was skipped.
        assert "100.64.0.40" not in seen
        assert sorted(seen) == ["100.64.0.10", "100.64.0.20", "100.64.0.30"]

    def test_probe_failures_dropped_silently(self):
        # All three online devices fail to probe → empty runners list,
        # but the call still succeeds.
        with patch.object(tn, "list_devices", return_value=list(self._DEVICES)), \
             patch.object(tn, "list_pod_records", return_value={}), \
             patch.object(tn, "probe_system_info", return_value=None), \
             patch.object(tn, "get_tailscale_api_key", return_value="k"), \
             patch.object(tn, "get_tailscale_tailnet", return_value="ex"):
            result = tn.discover_comfy_runners()
        assert result["ok"] is True
        assert result["runners"] == []
        assert result["online_count"] == 3

    def test_force_refresh_propagates_to_list_devices(self):
        with patch.object(tn, "list_devices", return_value=[]) as ld, \
             patch.object(tn, "list_pod_records", return_value={}), \
             patch.object(tn, "get_tailscale_api_key", return_value="k"), \
             patch.object(tn, "get_tailscale_tailnet", return_value="ex"):
            tn.discover_comfy_runners(force_refresh=True)
        ld.assert_called_once_with(force=True)

    def test_error_propagated_into_payload_no_devices(self):
        # When list_devices fails, the cached error must surface in the
        # discovery payload's ``error`` field, and ``ok`` flips to False.
        with patch.object(tn, "list_devices", return_value=[]), \
             patch.object(tn, "get_last_devices_error", return_value="HTTP 503"), \
             patch.object(tn, "list_pod_records", return_value={}), \
             patch.object(tn, "get_tailscale_api_key", return_value="k"), \
             patch.object(tn, "get_tailscale_tailnet", return_value="ex"):
            result = tn.discover_comfy_runners()
        assert result["ok"] is False
        assert result["error"] == "HTTP 503"
        assert result["runners"] == []
        assert result["tailnet_configured"] is True

    def test_error_field_none_on_success(self):
        # On a clean run, ``error`` is None (not missing) and ``ok``
        # stays True.
        with self._patches():
            result = tn.discover_comfy_runners()
        assert "error" in result
        assert result["error"] is None
        assert result["ok"] is True

    def test_error_propagated_with_partial_devices(self):
        # If list_devices returns something but the cached error is
        # still set (e.g. a transient failure during a forced refresh),
        # the payload's ``ok`` is False but the runners we *do* know
        # about are still surfaced.
        devices = [self._DEVICES[0]]  # Just the responsive runpod box.
        with patch.object(tn, "list_devices", return_value=devices), \
             patch.object(tn, "get_last_devices_error", return_value="boom"), \
             patch.object(tn, "list_pod_records", return_value=dict(self._POD_RECORDS)), \
             patch.object(
                tn, "probe_system_info",
                side_effect=lambda host, *a, **kw: self._DEFAULT_PROBES.get(host),
             ), \
             patch.object(tn, "get_tailscale_api_key", return_value="k"), \
             patch.object(tn, "get_tailscale_tailnet", return_value="ex"):
            result = tn.discover_comfy_runners()
        assert result["ok"] is False
        assert result["error"] == "boom"
        assert len(result["runners"]) == 1
