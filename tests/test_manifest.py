"""Tests for comfy_runner.manifest — block extraction, validation,
GitHub PR body fetch, workflow URL fetch, dedup resolver."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the comfy-runner repo root is importable so ``safe_file`` resolves
# the same way it does when manifest.py imports it locally.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from comfy_runner.manifest import (  # noqa: E402
    DEFAULT_URL_ALLOWLIST,
    MAX_PR_BODY_BYTES,
    MAX_WORKFLOW_BYTES,
    Manifest,
    ModelEntry,
    ResolvedManifest,
    _looks_like_workflow,
    fetch_pr_body,
    fetch_workflow,
    is_url_allowed,
    parse_manifest_block,
    resolve,
    validate_manifest,
)


FIXTURES = Path(__file__).parent / "fixtures" / "manifest"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _streamed_resp(payload: bytes, status: int = 200, chunk_size: int = 64 * 1024):
    """Build a MagicMock that imitates ``requests.get(..., stream=True)``."""
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    # iter_content returns the payload split into chunks of chunk_size bytes
    def _iter(_chunk_size=64 * 1024):
        for i in range(0, len(payload), _chunk_size):
            yield payload[i:i + _chunk_size]
    resp.iter_content.side_effect = lambda chunk_size=64 * 1024: _iter(chunk_size)
    return resp


def _json_resp(payload: dict, status: int = 200, headers: dict | None = None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    resp.headers = headers or {}
    return resp


# ---------------------------------------------------------------------------
# parse_manifest_block
# ---------------------------------------------------------------------------

class TestParseManifestBlock:
    def test_returns_none_on_empty_string(self):
        assert parse_manifest_block("") is None

    def test_returns_none_when_no_block(self):
        assert parse_manifest_block("# Title\n\nSome prose.\n") is None

    def test_returns_none_for_other_language_tag(self):
        body = "```json\n{\"models\": []}\n```\n"
        assert parse_manifest_block(body) is None

    def test_parses_minimal_block(self):
        body = (FIXTURES / "pr_body_minimal.md").read_text(encoding="utf-8")
        m = parse_manifest_block(body)
        assert isinstance(m, Manifest)
        assert len(m.models) == 1
        assert m.models[0].name == "model-a.safetensors"
        assert len(m.workflows) == 1

    def test_parses_realistic_pr_body(self):
        body = (FIXTURES / "pr_body_realistic.md").read_text(encoding="utf-8")
        m = parse_manifest_block(body)
        assert isinstance(m, Manifest)
        assert m.models[0].name == "explicit.safetensors"
        assert m.workflows[0].endswith("/wf-realistic.json")

    def test_parses_hyphenated_language_tag(self):
        body = (FIXTURES / "pr_body_hyphenated.md").read_text(encoding="utf-8")
        m = parse_manifest_block(body)
        assert isinstance(m, Manifest)
        assert m.workflows[0].startswith("https://raw.githubusercontent.com/")

    def test_first_block_wins_when_multiple(self):
        body = (
            "```comfyrunner\n"
            "{\"workflows\": [\"https://huggingface.co/first.json\"]}\n"
            "```\n\n"
            "```comfyrunner\n"
            "{\"workflows\": [\"https://huggingface.co/second.json\"]}\n"
            "```\n"
        )
        m = parse_manifest_block(body)
        assert m.workflows == ["https://huggingface.co/first.json"]

    def test_indented_fence_not_matched(self):
        # Regex is anchored to start-of-line; a four-space indent should
        # prevent it from matching (markdown would render it as code anyway).
        body = "    ```comfyrunner\n    {\"workflows\": []}\n    ```\n"
        assert parse_manifest_block(body) is None

    def test_empty_block_raises(self):
        body = "```comfyrunner\n\n```\n"
        with pytest.raises(ValueError, match="empty"):
            parse_manifest_block(body)

    def test_malformed_json_raises(self):
        body = "```comfyrunner\n{not json}\n```\n"
        with pytest.raises(ValueError, match="JSON"):
            parse_manifest_block(body)

    def test_non_dict_root_raises(self):
        body = "```comfyrunner\n[1, 2, 3]\n```\n"
        with pytest.raises(ValueError, match="object"):
            parse_manifest_block(body)

    def test_oversized_body_rejected(self):
        body = "x" * (MAX_PR_BODY_BYTES + 1)
        with pytest.raises(ValueError, match="suspiciously large"):
            parse_manifest_block(body)


# ---------------------------------------------------------------------------
# validate_manifest
# ---------------------------------------------------------------------------

class TestValidateManifest:
    def test_empty_dict_is_valid(self):
        m = validate_manifest({})
        assert m.models == []
        assert m.workflows == []
        assert m.is_empty()

    def test_explicit_empty_lists(self):
        m = validate_manifest({"models": [], "workflows": []})
        assert m.is_empty()

    def test_full_model_entry(self):
        data = {
            "models": [
                {"name": "a.safetensors", "url": "https://h/x", "directory": "checkpoints"},
            ],
        }
        m = validate_manifest(data)
        assert m.models[0] == ModelEntry("a.safetensors", "https://h/x", "checkpoints")

    @pytest.mark.parametrize("missing", ["name", "url", "directory"])
    def test_model_missing_field(self, missing: str):
        entry = {"name": "a", "url": "https://h", "directory": "c"}
        del entry[missing]
        with pytest.raises(ValueError, match=missing):
            validate_manifest({"models": [entry]})

    @pytest.mark.parametrize("field_name", ["name", "url", "directory"])
    def test_model_empty_field(self, field_name: str):
        entry = {"name": "a", "url": "https://h", "directory": "c"}
        entry[field_name] = ""
        with pytest.raises(ValueError, match=field_name):
            validate_manifest({"models": [entry]})

    def test_model_entry_not_dict(self):
        with pytest.raises(ValueError, match="must be an object"):
            validate_manifest({"models": ["not-a-dict"]})

    def test_models_not_a_list(self):
        with pytest.raises(ValueError, match="must be a list"):
            validate_manifest({"models": "nope"})

    def test_workflow_not_string(self):
        with pytest.raises(ValueError, match="non-empty URL"):
            validate_manifest({"workflows": [42]})

    def test_workflow_empty_string(self):
        with pytest.raises(ValueError, match="non-empty URL"):
            validate_manifest({"workflows": [""]})

    def test_workflows_not_a_list(self):
        with pytest.raises(ValueError, match="must be a list"):
            validate_manifest({"workflows": "url"})

    def test_non_dict_root(self):
        with pytest.raises(ValueError, match="JSON object"):
            validate_manifest([1, 2, 3])

    def test_unknown_top_level_keys_ignored(self):
        # Forward-compat: extra keys we don't know about should be silently
        # ignored so old clients don't break when authors add new sections.
        m = validate_manifest({"models": [], "workflows": [], "future": "thing"})
        assert m.is_empty()


# ---------------------------------------------------------------------------
# is_url_allowed
# ---------------------------------------------------------------------------

class TestIsUrlAllowed:
    def test_exact_host(self):
        assert is_url_allowed("https://huggingface.co/path") is True

    def test_subdomain_match(self):
        assert is_url_allowed("https://cdn.huggingface.co/path") is True

    def test_substring_not_match(self):
        # "evil-huggingface.co" must NOT match "huggingface.co".
        assert is_url_allowed("https://evil-huggingface.co/path") is False

    def test_http_rejected(self):
        assert is_url_allowed("http://huggingface.co/x") is False

    def test_no_scheme_rejected(self):
        assert is_url_allowed("huggingface.co/x") is False

    def test_empty_string_rejected(self):
        assert is_url_allowed("") is False

    def test_malformed_url_rejected(self):
        assert is_url_allowed("not a url at all") is False

    def test_empty_allowlist_rejects_all(self):
        assert is_url_allowed("https://huggingface.co/x", allowlist=()) is False

    def test_default_allowlist_includes_expected_hosts(self):
        for host in DEFAULT_URL_ALLOWLIST:
            assert is_url_allowed(f"https://{host}/x") is True


# ---------------------------------------------------------------------------
# fetch_pr_body
# ---------------------------------------------------------------------------

class TestFetchPrBody:
    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _json_resp({"body": "hello"})
            body = fetch_pr_body("octocat", "Hello-World", 1)
        assert body == "hello"
        # Verify URL composition + User-Agent header.
        args, kwargs = mock_get.call_args
        assert args[0] == "https://api.github.com/repos/octocat/Hello-World/pulls/1"
        assert kwargs["headers"]["User-Agent"] == "comfy-runner"
        assert kwargs["headers"]["Accept"] == "application/vnd.github+json"
        assert "Authorization" not in kwargs["headers"]

    def test_null_body_returns_empty_string(self):
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _json_resp({"body": None})
            assert fetch_pr_body("o", "r", 1) == ""

    def test_explicit_token_used(self):
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _json_resp({"body": ""})
            fetch_pr_body("o", "r", 1, github_token="explicit-tok")
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer explicit-tok"

    def test_explicit_token_wins_over_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env-tok")
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _json_resp({"body": ""})
            fetch_pr_body("o", "r", 1, github_token="explicit-tok")
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer explicit-tok"

    def test_env_token_fallback(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env-tok")
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _json_resp({"body": ""})
            fetch_pr_body("o", "r", 1)
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer env-tok"

    def test_404_distinct_message(self):
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _json_resp({}, status=404)
            with pytest.raises(RuntimeError, match="not found"):
                fetch_pr_body("o", "r", 1)

    def test_401_auth_message(self):
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _json_resp({}, status=401)
            with pytest.raises(RuntimeError, match="authentication failed"):
                fetch_pr_body("o", "r", 1)

    def test_403_auth_message(self):
        # 403 without rate-limit headers → auth bucket.
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _json_resp(
                {}, status=403, headers={"X-RateLimit-Remaining": "30"},
            )
            with pytest.raises(RuntimeError, match="authentication failed"):
                fetch_pr_body("o", "r", 1)

    def test_403_rate_limit_distinct_message(self):
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _json_resp(
                {}, status=403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999"},
            )
            with pytest.raises(RuntimeError, match="rate limit"):
                fetch_pr_body("o", "r", 1)

    def test_500_generic_error(self):
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            resp = _json_resp({"message": "boom"}, status=500)
            resp.text = "internal server error"
            mock_get.return_value = resp
            with pytest.raises(RuntimeError, match="500"):
                fetch_pr_body("o", "r", 1)

    def test_network_error_wrapped(self):
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.side_effect = requests.ConnectionError("DNS fail")
            with pytest.raises(RuntimeError, match="failed to fetch"):
                fetch_pr_body("o", "r", 1)

    def test_non_string_body_raises(self):
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _json_resp({"body": 42})
            with pytest.raises(RuntimeError, match="non-string body"):
                fetch_pr_body("o", "r", 1)

    @pytest.mark.parametrize("owner,repo,pr", [
        ("", "r", 1),
        ("o", "", 1),
        ("o", "r", 0),
        ("o", "r", -5),
    ])
    def test_invalid_args(self, owner: str, repo: str, pr: int):
        with pytest.raises(ValueError):
            fetch_pr_body(owner, repo, pr)

    def test_pr_must_be_int(self):
        with pytest.raises(ValueError):
            fetch_pr_body("o", "r", "1")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# fetch_workflow
# ---------------------------------------------------------------------------

class TestFetchWorkflow:
    def _editor_payload(self) -> bytes:
        return (FIXTURES / "workflow_editor.json").read_bytes()

    def _api_payload(self) -> bytes:
        return (FIXTURES / "workflow_api.json").read_bytes()

    def test_happy_editor_format(self, tmp_path: Path):
        payload = self._editor_payload()
        url = "https://huggingface.co/u/r/raw/wf.json"
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(payload)
            path, data = fetch_workflow(url, tmp_path)
        assert path.name == "wf.json"
        assert path.exists()
        assert data["nodes"][0]["type"] == "CheckpointLoaderSimple"

    def test_happy_api_format(self, tmp_path: Path):
        payload = self._api_payload()
        url = "https://huggingface.co/u/r/raw/api.json"
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(payload)
            path, data = fetch_workflow(url, tmp_path)
        assert path.name == "api.json"
        assert "1" in data and data["1"]["class_type"] == "CheckpointLoaderSimple"

    def test_query_string_stripped_from_filename(self, tmp_path: Path):
        url = "https://huggingface.co/u/r/raw/wf.json?download=true&v=2"
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(self._editor_payload())
            path, _ = fetch_workflow(url, tmp_path)
        assert path.name == "wf.json"

    def test_json_suffix_appended_when_missing(self, tmp_path: Path):
        url = "https://huggingface.co/u/r/raw/wf"
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(self._editor_payload())
            path, _ = fetch_workflow(url, tmp_path)
        assert path.name == "wf.json"

    def test_filename_override_used(self, tmp_path: Path):
        url = "https://huggingface.co/u/r/raw/wf.json"
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(self._editor_payload())
            path, _ = fetch_workflow(url, tmp_path, filename_override="renamed.json")
        assert path.name == "renamed.json"

    @pytest.mark.parametrize("bad", ["../foo.json", "a/b.json", "/abs.json"])
    def test_unsafe_filename_override_rejected(self, tmp_path: Path, bad: str):
        url = "https://huggingface.co/u/r/raw/wf.json"
        with pytest.raises(ValueError, match="unsafe"):
            fetch_workflow(url, tmp_path, filename_override=bad)

    def test_empty_url_path_falls_back_to_default(self, tmp_path: Path):
        url = "https://huggingface.co/"
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(self._editor_payload())
            path, _ = fetch_workflow(url, tmp_path)
        assert path.name == "workflow.json"

    def test_disallowed_host_rejected(self, tmp_path: Path):
        url = "https://malware.example/wf.json"
        with pytest.raises(ValueError, match="allowlist"):
            fetch_workflow(url, tmp_path)

    def test_disallowed_host_allowed_with_flag(self, tmp_path: Path):
        url = "https://malware.example/wf.json"
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(self._editor_payload())
            path, _ = fetch_workflow(url, tmp_path, allow_arbitrary_urls=True)
        assert path.exists()

    def test_http_error(self, tmp_path: Path):
        url = "https://huggingface.co/u/r/raw/wf.json"
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(b"", status=404)
            with pytest.raises(RuntimeError, match="HTTP 404"):
                fetch_workflow(url, tmp_path)

    def test_size_cap_exceeded(self, tmp_path: Path):
        url = "https://huggingface.co/u/r/raw/big.json"
        oversized = b"x" * (MAX_WORKFLOW_BYTES + 1)
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(oversized)
            with pytest.raises(RuntimeError, match="exceeds"):
                fetch_workflow(url, tmp_path)
        # File must NOT be left behind.
        assert not (tmp_path / "big.json").exists()

    def test_malformed_json(self, tmp_path: Path):
        url = "https://huggingface.co/u/r/raw/wf.json"
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(b"<html>not json</html>")
            with pytest.raises(RuntimeError, match="not valid JSON"):
                fetch_workflow(url, tmp_path)

    def test_non_dict_json(self, tmp_path: Path):
        url = "https://huggingface.co/u/r/raw/wf.json"
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(b"[1, 2, 3]")
            with pytest.raises(RuntimeError, match="JSON object"):
                fetch_workflow(url, tmp_path)

    def test_not_a_workflow_shape(self, tmp_path: Path):
        url = "https://huggingface.co/u/r/raw/wf.json"
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(b'{"hello": "world"}')
            with pytest.raises(RuntimeError, match="ComfyUI workflow"):
                fetch_workflow(url, tmp_path)

    def test_network_error_wrapped(self, tmp_path: Path):
        url = "https://huggingface.co/u/r/raw/wf.json"
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.side_effect = requests.ConnectionError("DNS fail")
            with pytest.raises(RuntimeError, match="failed to fetch"):
                fetch_workflow(url, tmp_path)


# ---------------------------------------------------------------------------
# _looks_like_workflow
# ---------------------------------------------------------------------------

class TestLooksLikeWorkflow:
    def test_editor_format(self):
        assert _looks_like_workflow({"nodes": []}) is True
        assert _looks_like_workflow({"nodes": [{"id": 1}]}) is True

    def test_api_format(self):
        assert _looks_like_workflow({"1": {"class_type": "X"}}) is True
        assert _looks_like_workflow({"1": {"class_type": "A"}, "2": {"class_type": "B"}}) is True

    def test_empty_dict_rejected(self):
        assert _looks_like_workflow({}) is False

    def test_random_dict_rejected(self):
        assert _looks_like_workflow({"hello": "world"}) is False

    def test_nodes_not_a_list(self):
        # No 'nodes' list AND keys are non-numeric → reject.
        assert _looks_like_workflow({"nodes": "not-a-list"}) is False

    def test_api_format_one_bad_entry(self):
        # If any value is missing class_type, the whole thing is rejected.
        assert _looks_like_workflow({"1": {"class_type": "A"}, "2": {}}) is False


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

class TestResolve:
    def test_empty_manifest(self, tmp_path: Path):
        result = resolve(Manifest(), tmp_path)
        assert isinstance(result, ResolvedManifest)
        assert result.models == []
        assert result.workflow_files == []
        assert result.failures == []

    def test_models_only_no_fetch(self, tmp_path: Path):
        m = Manifest(models=[ModelEntry("a", "https://h/a", "checkpoints")])
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            result = resolve(m, tmp_path)
        mock_get.assert_not_called()
        assert len(result.models) == 1

    def test_workflow_extracts_embedded_models(self, tmp_path: Path):
        m = Manifest(workflows=["https://huggingface.co/wf.json"])
        payload = (FIXTURES / "workflow_editor.json").read_bytes()
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(payload)
            result = resolve(m, tmp_path)
        assert len(result.workflow_files) == 1
        assert any(mod.name == "embedded.safetensors" for mod in result.models)

    def test_dedup_explicit_wins_over_embedded(self, tmp_path: Path):
        # Manifest declares the same (directory, name) as the workflow's
        # embedded model but with a different URL — the manifest URL must win.
        m = Manifest(
            models=[
                ModelEntry(
                    "embedded.safetensors",
                    "https://huggingface.co/explicit-override",
                    "checkpoints",
                ),
            ],
            workflows=["https://huggingface.co/wf.json"],
        )
        payload = (FIXTURES / "workflow_editor.json").read_bytes()
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(payload)
            result = resolve(m, tmp_path)
        # Only one entry for that key.
        keys = [(mod.directory, mod.name) for mod in result.models]
        assert keys.count(("checkpoints", "embedded.safetensors")) == 1
        # The explicit URL wins.
        winning = [m for m in result.models if m.name == "embedded.safetensors"][0]
        assert winning.url == "https://huggingface.co/explicit-override"

    def test_per_workflow_failure_collected(self, tmp_path: Path):
        m = Manifest(workflows=[
            "https://huggingface.co/good.json",
            "https://huggingface.co/bad.json",
        ])
        good_payload = (FIXTURES / "workflow_editor.json").read_bytes()

        def _side_effect(url, **kwargs):
            if "bad.json" in url:
                return _streamed_resp(b"", status=500)
            return _streamed_resp(good_payload)

        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.side_effect = _side_effect
            result = resolve(m, tmp_path)
        assert len(result.workflow_files) == 1
        assert len(result.failures) == 1
        assert "bad.json" in result.failures[0]["url"]

    def test_allow_arbitrary_urls_forwarded(self, tmp_path: Path):
        m = Manifest(workflows=["https://example.com/wf.json"])
        payload = (FIXTURES / "workflow_editor.json").read_bytes()
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(payload)
            result = resolve(m, tmp_path, allow_arbitrary_urls=True)
        assert len(result.workflow_files) == 1
        assert result.failures == []

    def test_send_output_called(self, tmp_path: Path):
        m = Manifest(workflows=["https://huggingface.co/wf.json"])
        payload = (FIXTURES / "workflow_editor.json").read_bytes()
        captured: list[str] = []
        with patch("comfy_runner.manifest.requests.get") as mock_get:
            mock_get.return_value = _streamed_resp(payload)
            resolve(m, tmp_path, send_output=captured.append)
        assert any("wf.json" in line for line in captured)
