"""Tests for comfy_runner.review — orchestration: fetch_and_resolve_manifest,
provision_models_local, prepare_local_review, ReviewResult."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfy_runner import review as review_mod  # noqa: E402
from comfy_runner.manifest import (  # noqa: E402
    Manifest,
    ModelEntry,
    ResolvedManifest,
)
from comfy_runner.review import (  # noqa: E402
    ReviewResult,
    _normalize_repo_url,
    cleanup_runpod_review,
    fetch_and_resolve_manifest,
    prepare_local_review,
    prepare_remote_review,
    prepare_runpod_review,
    prepare_server_review,
    provision_models_local,
    remote_install_at_pr,
    workflows_dest_for,
)


FIXTURES = Path(__file__).parent / "fixtures" / "manifest"


# ---------------------------------------------------------------------------
# ReviewResult
# ---------------------------------------------------------------------------

class TestReviewResult:
    def test_default_shape(self):
        r = ReviewResult(target_name="local:main")
        assert r.target_name == "local:main"
        assert r.deploy == {}
        assert r.downloaded == []
        assert r.failures == []
        assert r.is_partial() is False

    def test_partial_when_failures(self):
        r = ReviewResult(target_name="x", failures=[{"url": "u", "error": "e"}])
        assert r.is_partial() is True

    def test_partial_when_failed(self):
        r = ReviewResult(target_name="x", failed=["model"])
        assert r.is_partial() is True

    def test_to_dict_includes_partial_flag(self):
        r = ReviewResult(target_name="x")
        d = r.to_dict()
        assert d["partial"] is False
        assert d["target_name"] == "x"
        # round-trips through json
        json.dumps(d)


# ---------------------------------------------------------------------------
# workflows_dest_for
# ---------------------------------------------------------------------------

class TestWorkflowsDestFor:
    def test_path_shape(self, tmp_path: Path):
        result = workflows_dest_for(tmp_path)
        # Path equality is OS-agnostic.
        assert result == tmp_path / "ComfyUI" / "user" / "default" / "workflows"

    def test_accepts_string(self, tmp_path: Path):
        result = workflows_dest_for(str(tmp_path))
        assert isinstance(result, Path)
        assert result == tmp_path / "ComfyUI" / "user" / "default" / "workflows"


# ---------------------------------------------------------------------------
# fetch_and_resolve_manifest
# ---------------------------------------------------------------------------

class TestFetchAndResolveManifest:
    def test_no_body_no_extras_returns_none_none(self, tmp_path: Path):
        with patch("comfy_runner.review._manifest.fetch_pr_body", return_value=""):
            parsed, resolved = fetch_and_resolve_manifest(
                "o", "r", 1, tmp_path,
            )
        assert parsed is None
        assert resolved is None

    def test_extras_only_no_body(self, tmp_path: Path):
        with patch("comfy_runner.review._manifest.fetch_pr_body", return_value=""), \
             patch("comfy_runner.review._manifest.resolve") as mock_resolve:
            fake_resolved = ResolvedManifest(
                models=[ModelEntry("a", "https://h/a", "checkpoints")],
                workflow_files=[],
            )
            mock_resolve.return_value = fake_resolved
            parsed, resolved = fetch_and_resolve_manifest(
                "o", "r", 1, tmp_path,
                extra_models=[ModelEntry("a", "https://h/a", "checkpoints")],
            )
        assert parsed is not None
        assert len(parsed.models) == 1
        assert resolved is fake_resolved

    def test_pr_body_with_block_parsed(self, tmp_path: Path):
        body = (FIXTURES / "pr_body_minimal.md").read_text(encoding="utf-8")
        with patch("comfy_runner.review._manifest.fetch_pr_body", return_value=body), \
             patch("comfy_runner.review._manifest.resolve") as mock_resolve:
            mock_resolve.return_value = ResolvedManifest(models=[], workflow_files=[])
            parsed, resolved = fetch_and_resolve_manifest(
                "o", "r", 1, tmp_path,
            )
        assert parsed is not None
        assert parsed.models[0].name == "model-a.safetensors"
        assert len(parsed.workflows) == 1
        assert resolved is not None

    def test_extras_merge_with_pr_body(self, tmp_path: Path):
        body = (FIXTURES / "pr_body_minimal.md").read_text(encoding="utf-8")
        with patch("comfy_runner.review._manifest.fetch_pr_body", return_value=body), \
             patch("comfy_runner.review._manifest.resolve") as mock_resolve:
            mock_resolve.return_value = ResolvedManifest(models=[], workflow_files=[])
            parsed, _ = fetch_and_resolve_manifest(
                "o", "r", 1, tmp_path,
                extra_models=[ModelEntry("extra", "https://h/x", "loras")],
                extra_workflows=["https://huggingface.co/extra.json"],
            )
        # PR body had 1 model + 1 workflow; extras add 1 of each.
        assert len(parsed.models) == 2
        assert len(parsed.workflows) == 2

    def test_malformed_block_warns_and_falls_back(self, tmp_path: Path):
        body = "```comfyrunner\n{not valid json}\n```\n"
        captured: list[str] = []
        with patch("comfy_runner.review._manifest.fetch_pr_body", return_value=body):
            parsed, resolved = fetch_and_resolve_manifest(
                "o", "r", 1, tmp_path, send_output=captured.append,
            )
        assert parsed is None
        assert resolved is None
        assert any("failed to parse" in line for line in captured)

    def test_network_error_degrades_gracefully(self, tmp_path: Path):
        captured: list[str] = []
        with patch(
            "comfy_runner.review._manifest.fetch_pr_body",
            side_effect=RuntimeError("network down"),
        ):
            parsed, resolved = fetch_and_resolve_manifest(
                "o", "r", 1, tmp_path,
                extra_models=[ModelEntry("a", "https://h/a", "loras")],
                send_output=captured.append,
            )
        # Body fetch failed → warning issued, but extras still produce a manifest.
        assert any("Could not fetch PR body" in line for line in captured)
        assert parsed is not None
        assert len(parsed.models) == 1

    def test_github_token_threaded(self, tmp_path: Path):
        with patch("comfy_runner.review._manifest.fetch_pr_body", return_value="") as mock_fetch:
            fetch_and_resolve_manifest(
                "o", "r", 1, tmp_path, github_token="tok",
            )
        assert mock_fetch.call_args.kwargs["github_token"] == "tok"

    def test_empty_parsed_with_no_extras_returns_resolved_none(self, tmp_path: Path):
        # PR body parses to an empty manifest — function returns (parsed, None)
        # because there's nothing to provision.
        body = "```comfyrunner\n{}\n```\n"
        with patch("comfy_runner.review._manifest.fetch_pr_body", return_value=body):
            parsed, resolved = fetch_and_resolve_manifest(
                "o", "r", 1, tmp_path,
            )
        assert parsed is not None
        assert parsed.is_empty()
        assert resolved is None


# ---------------------------------------------------------------------------
# provision_models_local
# ---------------------------------------------------------------------------

class TestProvisionModelsLocal:
    def test_empty_models_early_return(self, tmp_path: Path):
        with patch("comfy_runner.review.download_models") as mock_dl, \
             patch("comfy_runner.review.check_missing_models") as mock_check:
            result = provision_models_local(tmp_path, [])
        mock_dl.assert_not_called()
        mock_check.assert_not_called()
        assert result == {"downloaded": [], "skipped": [], "failed": [], "errors": []}

    def test_all_present_skip_path(self, tmp_path: Path):
        models = [
            ModelEntry("a.safetensors", "https://h/a", "checkpoints"),
            ModelEntry("b.safetensors", "https://h/b", "loras"),
        ]
        existing = [m.to_dict() for m in models]
        with patch(
            "comfy_runner.review.check_missing_models",
            return_value=([], existing),
        ), patch("comfy_runner.review.download_models") as mock_dl, \
           patch(
               "comfy_runner.review.resolve_models_dir",
               return_value=tmp_path / "models",
           ):
            result = provision_models_local(tmp_path, models)
        mock_dl.assert_not_called()
        assert "checkpoints/a.safetensors" in result["skipped"]
        assert "loras/b.safetensors" in result["skipped"]
        assert result["downloaded"] == []

    def test_missing_triggers_download(self, tmp_path: Path):
        models = [
            ModelEntry("a.safetensors", "https://h/a", "checkpoints"),
            ModelEntry("b.safetensors", "https://h/b", "loras"),
        ]
        # 'a' is missing, 'b' already present.
        missing = [models[0].to_dict()]
        existing = [models[1].to_dict()]
        with patch(
            "comfy_runner.review.check_missing_models",
            return_value=(missing, existing),
        ), patch("comfy_runner.review.download_models") as mock_dl, \
           patch(
               "comfy_runner.review.resolve_models_dir",
               return_value=tmp_path / "models",
           ):
            mock_dl.return_value = {
                "downloaded": ["checkpoints/a.safetensors"],
                "skipped": [],
                "failed": [],
                "errors": [],
            }
            result = provision_models_local(tmp_path, models, token="hf_tok")
        # download_models called once, with only the missing subset.
        assert mock_dl.call_count == 1
        called_models = mock_dl.call_args.args[0]
        assert called_models == missing
        assert mock_dl.call_args.kwargs["token"] == "hf_tok"
        # Pre-existing entry merged into final skipped list.
        assert "loras/b.safetensors" in result["skipped"]
        assert "checkpoints/a.safetensors" in result["downloaded"]

    def test_download_failures_propagate(self, tmp_path: Path):
        models = [ModelEntry("x", "https://h/x", "checkpoints")]
        missing = [models[0].to_dict()]
        with patch(
            "comfy_runner.review.check_missing_models",
            return_value=(missing, []),
        ), patch("comfy_runner.review.download_models") as mock_dl, \
           patch(
               "comfy_runner.review.resolve_models_dir",
               return_value=tmp_path / "models",
           ):
            mock_dl.return_value = {
                "downloaded": [],
                "skipped": [],
                "failed": ["checkpoints/x"],
                "errors": ["404 on https://h/x"],
            }
            result = provision_models_local(tmp_path, models)
        assert result["failed"] == ["checkpoints/x"]
        assert result["errors"] == ["404 on https://h/x"]


# ---------------------------------------------------------------------------
# prepare_local_review
# ---------------------------------------------------------------------------

class TestPrepareLocalReview:
    def test_no_manifest_returns_skip_shape(self, tmp_path: Path):
        with patch(
            "comfy_runner.review.fetch_and_resolve_manifest",
            return_value=(None, None),
        ):
            result = prepare_local_review(tmp_path, "o", "r", 1)
        assert result["manifest"] is None
        assert result["resolved"] is None
        assert result["downloaded"] == []
        assert result["failed"] == []
        assert result["failures"] == []
        assert result["workflows_dir"].endswith("workflows") or \
               result["workflows_dir"].replace("\\", "/").endswith("workflows")

    def test_empty_resolved_no_provisioning(self, tmp_path: Path):
        empty_parsed = Manifest()
        with patch(
            "comfy_runner.review.fetch_and_resolve_manifest",
            return_value=(empty_parsed, None),
        ), patch("comfy_runner.review.provision_models_local") as mock_prov:
            result = prepare_local_review(tmp_path, "o", "r", 1)
        mock_prov.assert_not_called()
        assert result["manifest"] == {"models": [], "workflows": []}
        assert result["resolved"] is None

    def test_skip_provisioning_honored(self, tmp_path: Path):
        parsed = Manifest(models=[ModelEntry("a", "https://h/a", "checkpoints")])
        resolved = ResolvedManifest(
            models=parsed.models, workflow_files=[],
        )
        with patch(
            "comfy_runner.review.fetch_and_resolve_manifest",
            return_value=(parsed, resolved),
        ), patch("comfy_runner.review.provision_models_local") as mock_prov:
            result = prepare_local_review(
                tmp_path, "o", "r", 1, skip_provisioning=True,
            )
        mock_prov.assert_not_called()
        assert result["manifest"]["models"][0]["name"] == "a"
        assert result["downloaded"] == []
        assert result["skipped"] == []
        assert result["failed"] == []

    def test_happy_full_path(self, tmp_path: Path):
        parsed = Manifest(
            models=[
                ModelEntry("a.safetensors", "https://h/a", "checkpoints"),
                ModelEntry("b.safetensors", "https://h/b", "loras"),
            ],
            workflows=["https://huggingface.co/wf.json"],
        )
        wf_path = tmp_path / "ComfyUI" / "user" / "default" / "workflows" / "wf.json"
        resolved = ResolvedManifest(
            models=parsed.models, workflow_files=[wf_path],
        )
        with patch(
            "comfy_runner.review.fetch_and_resolve_manifest",
            return_value=(parsed, resolved),
        ), patch("comfy_runner.review.provision_models_local") as mock_prov:
            mock_prov.return_value = {
                "downloaded": ["checkpoints/a.safetensors"],
                "skipped": ["loras/b.safetensors"],
                "failed": [],
                "errors": [],
            }
            result = prepare_local_review(
                tmp_path, "o", "r", 1, download_token="hf_tok",
            )
        assert result["manifest"]["models"][0]["name"] == "a.safetensors"
        assert result["resolved"] is not None
        assert str(wf_path) in result["workflows"]
        assert "checkpoints/a.safetensors" in result["downloaded"]
        assert "loras/b.safetensors" in result["skipped"]
        # Token threaded through.
        assert mock_prov.call_args.kwargs["token"] == "hf_tok"

    def test_workflow_failures_surface(self, tmp_path: Path):
        parsed = Manifest(workflows=["https://huggingface.co/bad.json"])
        resolved = ResolvedManifest(
            models=[],
            workflow_files=[],
            failures=[{"url": "https://huggingface.co/bad.json", "error": "404"}],
        )
        with patch(
            "comfy_runner.review.fetch_and_resolve_manifest",
            return_value=(parsed, resolved),
        ), patch("comfy_runner.review.provision_models_local") as mock_prov:
            mock_prov.return_value = {
                "downloaded": [], "skipped": [], "failed": [], "errors": [],
            }
            result = prepare_local_review(tmp_path, "o", "r", 1)
        assert len(result["failures"]) == 1
        assert result["failures"][0]["url"].endswith("bad.json")

    def test_extras_threaded_through(self, tmp_path: Path):
        with patch(
            "comfy_runner.review.fetch_and_resolve_manifest",
            return_value=(None, None),
        ) as mock_far:
            extras = [ModelEntry("a", "https://h/a", "loras")]
            prepare_local_review(
                tmp_path, "o", "r", 1,
                extra_models=extras,
                extra_workflows=["https://huggingface.co/extra.json"],
                allow_arbitrary_urls=True,
                github_token="ghp_tok",
            )
        kwargs = mock_far.call_args.kwargs
        assert kwargs["extra_models"] == extras
        assert kwargs["extra_workflows"] == ["https://huggingface.co/extra.json"]
        assert kwargs["allow_arbitrary_urls"] is True
        assert kwargs["github_token"] == "ghp_tok"


# ---------------------------------------------------------------------------
# Smoke test — exercise parse + resolve + provision composed together,
# mocking only at the network seams (requests.get + download_models).
# ---------------------------------------------------------------------------

class TestSmokeEndToEnd:
    def test_compose_real_layers(self, tmp_path: Path):
        body = (FIXTURES / "pr_body_minimal.md").read_text(encoding="utf-8")
        wf_payload = (FIXTURES / "workflow_editor.json").read_bytes()

        # Two HTTP calls happen: one to the GitHub API for the PR body,
        # one to fetch the workflow URL declared in the manifest.
        def _get_side_effect(url, *args, **kwargs):
            if url.startswith("https://api.github.com/"):
                resp = MagicMock()
                resp.status_code = 200
                resp.headers = {}
                resp.json.return_value = {"body": body}
                return resp
            # Streamed workflow fetch.
            resp = MagicMock()
            resp.status_code = 200

            def _iter(chunk_size=64 * 1024):
                for i in range(0, len(wf_payload), chunk_size):
                    yield wf_payload[i:i + chunk_size]
            resp.iter_content.side_effect = _iter
            return resp

        # Patch the network seams + check_missing_models / download_models.
        # check_missing_models needs the models_dir to exist; we point it at
        # tmp_path so resolve_models_dir's fallback works without creating a
        # real install.
        with patch("comfy_runner.manifest.requests.get", side_effect=_get_side_effect), \
             patch(
                 "comfy_runner.review.resolve_models_dir",
                 return_value=tmp_path / "models",
             ), \
             patch("comfy_runner.review.check_missing_models") as mock_check, \
             patch("comfy_runner.review.download_models") as mock_dl:
            # Treat the explicit + embedded models as missing so download
            # path is exercised.
            mock_check.return_value = (
                [
                    {
                        "name": "model-a.safetensors",
                        "url": "https://huggingface.co/test/model-a.safetensors",
                        "directory": "checkpoints",
                    },
                    {
                        "name": "embedded.safetensors",
                        "url": "https://huggingface.co/test/embedded.safetensors",
                        "directory": "checkpoints",
                    },
                ],
                [],
            )
            mock_dl.return_value = {
                "downloaded": [
                    "checkpoints/model-a.safetensors",
                    "checkpoints/embedded.safetensors",
                ],
                "skipped": [],
                "failed": [],
                "errors": [],
            }
            result = prepare_local_review(tmp_path, "o", "r", 1)

        assert result["manifest"] is not None
        assert result["manifest"]["models"][0]["name"] == "model-a.safetensors"
        # Workflow file landed under workflows_dest_for(install_path).
        wf_dest = workflows_dest_for(tmp_path)
        assert any(
            Path(p).parent == wf_dest for p in result["workflows"]
        )
        assert "checkpoints/model-a.safetensors" in result["downloaded"]
        assert "checkpoints/embedded.safetensors" in result["downloaded"]
        assert result["failures"] == []


# ---------------------------------------------------------------------------
# prepare_remote_review (item 2)
# ---------------------------------------------------------------------------

class TestPrepareRemoteReview:
    def _make_runner(self, *, request_resp=None, poll_resp=None):
        """Build a fake RemoteRunner whose _request and poll_job are patched."""
        runner = MagicMock()
        runner._request = MagicMock(
            return_value=request_resp or {"ok": True, "job_id": "job-1"}
        )
        runner.poll_job = MagicMock(return_value=poll_resp or {})
        return runner

    def test_posts_to_pods_review_with_minimal_body(self):
        runner = self._make_runner(poll_resp={
            "pod_name": "pod-a",
            "server_url": "https://pod-a.ts.net:9189",
            "deploy_result": {"restarted": True},
            "review_result": {
                "manifest": None,
                "downloaded": [],
                "skipped": [],
                "failed": [],
                "failures": [],
                "workflows": [],
                "workflows_dir": "/x",
            },
        })
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            result = prepare_remote_review(
                "https://station.example", "pod-a", "main",
                "owner", "repo", 42,
            )

        # _request was called with the expected URL/body shape.
        call = runner._request.call_args
        assert call.args == ("POST", "/pods/pod-a/review")
        body = call.kwargs["json"]
        assert body["install"] == "main"
        assert body["owner"] == "owner"
        assert body["repo"] == "repo"
        assert body["pr"] == 42
        assert body["allow_arbitrary_urls"] is False
        assert body["skip_provisioning"] is False
        # No optional fields when not supplied.
        assert "github_token" not in body
        assert "download_token" not in body
        assert "extra_models" not in body
        assert "extra_workflows" not in body

        # Result carries the inner review_result + pod metadata.
        assert result["pod_name"] == "pod-a"
        assert result["server_url"] == "https://pod-a.ts.net:9189"
        assert result["deploy_result"] == {"restarted": True}

    def test_passes_through_extras(self):
        runner = self._make_runner(poll_resp={
            "pod_name": "pod-b",
            "server_url": "https://x",
            "deploy_result": {},
            "review_result": {"workflows": ["w1"]},
        })
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_remote_review(
                "https://station.example", "pod-b", "main",
                "o", "r", 1,
                github_token="ghp_x",
                download_token="hf_y",
                extra_models=[ModelEntry("m.safetensors", "https://h/m", "loras")],
                extra_workflows=["https://h/w.json"],
                allow_arbitrary_urls=True,
                skip_provisioning=True,
            )

        body = runner._request.call_args.kwargs["json"]
        assert body["github_token"] == "ghp_x"
        assert body["download_token"] == "hf_y"
        assert body["extra_models"] == [
            {"name": "m.safetensors", "url": "https://h/m", "directory": "loras"}
        ]
        assert body["extra_workflows"] == ["https://h/w.json"]
        assert body["allow_arbitrary_urls"] is True
        assert body["skip_provisioning"] is True

    def test_force_purpose_omitted_by_default(self):
        runner = self._make_runner(poll_resp={
            "pod_name": "p", "server_url": "", "review_result": {},
            "deploy_result": None,
        })
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_remote_review(
                "https://x", "p", "main", "o", "r", 1,
            )
        body = runner._request.call_args.kwargs["json"]
        assert "force_purpose" not in body

    def test_force_purpose_passed_through(self):
        runner = self._make_runner(poll_resp={
            "pod_name": "p", "server_url": "", "review_result": {},
            "deploy_result": None,
        })
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_remote_review(
                "https://x", "p", "main", "o", "r", 1,
                force_purpose=True,
            )
        body = runner._request.call_args.kwargs["json"]
        assert body["force_purpose"] is True

    def test_pod_purpose_surfaced_in_result(self):
        runner = self._make_runner(poll_resp={
            "pod_name": "p", "pod_purpose": "pr", "server_url": "",
            "review_result": {"workflows": []},
            "deploy_result": {},
        })
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            result = prepare_remote_review(
                "https://x", "p", "main", "o", "r", 1,
            )
        assert result["pod_purpose"] == "pr"

    def test_force_deploy_passed_through(self):
        runner = self._make_runner(poll_resp={
            "pod_name": "p", "server_url": "", "review_result": {},
            "deploy_result": None,
        })
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_remote_review(
                "https://x", "p", "main", "o", "r", 1,
                force_deploy=True,
            )
        body = runner._request.call_args.kwargs["json"]
        assert body["force_deploy"] is True

    def test_force_deploy_omitted_by_default(self):
        runner = self._make_runner(poll_resp={
            "pod_name": "p", "server_url": "", "review_result": {},
            "deploy_result": None,
        })
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_remote_review(
                "https://x", "p", "main", "o", "r", 1,
            )
        body = runner._request.call_args.kwargs["json"]
        assert "force_deploy" not in body

    def test_idle_timeout_passed_through(self):
        runner = self._make_runner(poll_resp={
            "pod_name": "p", "server_url": "", "review_result": {},
            "deploy_result": None,
            "idle_timeout_s": 600,
        })
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            result = prepare_remote_review(
                "https://x", "p", "main", "o", "r", 1,
                idle_timeout_s=600,
            )
        body = runner._request.call_args.kwargs["json"]
        assert body["idle_timeout_s"] == 600
        # And it surfaces back in the merged result.
        assert result["idle_timeout_s"] == 600

    def test_idle_timeout_omitted_by_default(self):
        runner = self._make_runner(poll_resp={
            "pod_name": "p", "server_url": "", "review_result": {},
            "deploy_result": None,
        })
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_remote_review(
                "https://x", "p", "main", "o", "r", 1,
            )
        body = runner._request.call_args.kwargs["json"]
        assert "idle_timeout_s" not in body

    def test_missing_job_id_raises(self):
        runner = self._make_runner(request_resp={"ok": True})  # no job_id
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            with pytest.raises(RuntimeError, match="job_id"):
                prepare_remote_review(
                    "https://station.example", "pod-a", "main",
                    "o", "r", 1,
                )

    def test_poll_failure_propagates(self):
        runner = MagicMock()
        runner._request = MagicMock(return_value={"ok": True, "job_id": "j"})
        runner.poll_job = MagicMock(
            side_effect=RuntimeError("Job j failed: boom")
        )
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                prepare_remote_review(
                    "https://station.example", "pod-a", "main",
                    "o", "r", 1,
                )

    def test_pipes_send_output_to_poll_job(self):
        sink: list[str] = []

        def collector(text: str) -> None:
            sink.append(text)

        runner = self._make_runner(poll_resp={
            "pod_name": "pod-a",
            "server_url": "",
            "review_result": {},
            "deploy_result": None,
        })
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_remote_review(
                "https://station.example", "pod-a", "main",
                "o", "r", 7,
                send_output=collector,
            )
        # poll_job got the same callable for streaming output.
        assert runner.poll_job.call_args.kwargs["on_output"] is collector


# ---------------------------------------------------------------------------
# prepare_runpod_review (item 3)
# ---------------------------------------------------------------------------

class TestPrepareRunpodReview:
    """Two-step launch-pr → review chained against the central station."""

    def _runner_with_two_jobs(
        self, *, launch_result=None, review_result=None,
        launch_resp=None, review_resp=None,
    ):
        runner = MagicMock()
        runner._request = MagicMock(side_effect=[
            launch_resp or {
                "ok": True, "job_id": "launch-1", "name": "pr-foo-99",
            },
            review_resp or {"ok": True, "job_id": "review-1"},
        ])
        runner.poll_job = MagicMock(side_effect=[
            launch_result or {
                "name": "pr-foo-99",
                "pr": 99,
                "created": True,
                "server_url": "https://pod-a.ts.net:9189",
                "comfy_url": "https://pod-a.ts.net:8188",
                "idle_timeout_s": 1800,
                "deploy_result": {"restarted": True},
            },
            review_result or {
                "pod_name": "pr-foo-99",
                "pod_purpose": "pr",
                "server_url": "https://pod-a.ts.net:9189",
                "deploy_result": None,
                "review_result": {
                    "manifest": None, "resolved": None,
                    "downloaded": [], "skipped": [], "failed": [],
                    "errors": [], "workflows": [], "workflows_dir": "/x",
                    "failures": [],
                },
            },
        ])
        return runner

    def test_two_step_orchestration(self):
        runner = self._runner_with_two_jobs()
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            result = prepare_runpod_review(
                "https://station.example",
                "comfy-org", "ComfyUI", 99,
                gpu_type="RTX_4090",
            )

        # Two HTTP calls: launch-pr first, then pods/<name>/review.
        calls = runner._request.call_args_list
        assert len(calls) == 2
        assert calls[0].args == ("POST", "/pods/launch-pr")
        launch_body = calls[0].kwargs["json"]
        assert launch_body["pr"] == 99
        assert launch_body["repo"] == "https://github.com/comfy-org/ComfyUI"
        assert launch_body["install"] == "main"
        assert launch_body["gpu_type"] == "RTX_4090"

        assert calls[1].args == ("POST", "/pods/pr-foo-99/review")
        review_body = calls[1].kwargs["json"]
        assert review_body["skip_deploy"] is True
        assert review_body["install"] == "main"
        assert review_body["pr"] == 99

        # Result merges both layers.
        assert result["pod_name"] == "pr-foo-99"
        assert result["pod_purpose"] == "pr"
        assert result["server_url"] == "https://pod-a.ts.net:9189"
        assert result["created_new"] is True
        assert result["idle_timeout_s"] == 1800
        assert result["deploy_result"] == {"restarted": True}

    def test_no_gpu_type_omitted_from_body(self):
        runner = self._runner_with_two_jobs()
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_runpod_review(
                "https://station.example",
                "o", "r", 1,
            )
        launch_body = runner._request.call_args_list[0].kwargs["json"]
        assert "gpu_type" not in launch_body

    def test_idle_timeout_passed_through(self):
        runner = self._runner_with_two_jobs()
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_runpod_review(
                "https://station.example",
                "o", "r", 1,
                idle_timeout_s=600,
            )
        launch_body = runner._request.call_args_list[0].kwargs["json"]
        assert launch_body["idle_timeout_s"] == 600

    def test_extras_passed_to_review_step(self):
        runner = self._runner_with_two_jobs()
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_runpod_review(
                "https://station.example",
                "o", "r", 1,
                github_token="ghp",
                download_token="hf",
                extra_models=[ModelEntry("m", "https://h/m", "loras")],
                extra_workflows=["https://h/wf.json"],
                allow_arbitrary_urls=True,
                skip_provisioning=True,
            )
        review_body = runner._request.call_args_list[1].kwargs["json"]
        assert review_body["github_token"] == "ghp"
        assert review_body["download_token"] == "hf"
        assert review_body["allow_arbitrary_urls"] is True
        assert review_body["skip_provisioning"] is True
        assert review_body["extra_workflows"] == ["https://h/wf.json"]
        assert review_body["extra_models"] == [
            {"name": "m", "url": "https://h/m", "directory": "loras"}
        ]

    def test_launch_missing_job_id_raises(self):
        runner = self._runner_with_two_jobs(
            launch_resp={"ok": True},  # no job_id
        )
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            with pytest.raises(RuntimeError, match="job_id"):
                prepare_runpod_review(
                    "https://station.example", "o", "r", 1,
                )

    def test_launch_missing_name_raises(self):
        runner = self._runner_with_two_jobs(
            launch_resp={"ok": True, "job_id": "j"},  # no name
            launch_result={"created": True},  # poll returns no name
        )
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            with pytest.raises(RuntimeError, match="pod name"):
                prepare_runpod_review(
                    "https://station.example", "o", "r", 1,
                )


# ---------------------------------------------------------------------------
# cleanup_runpod_review (item 3)
# ---------------------------------------------------------------------------

class TestCleanupRunpodReview:
    def test_posts_to_reviews_cleanup(self):
        runner = MagicMock()
        runner._request = MagicMock(return_value={
            "ok": True, "pr": 42, "dry_run": False,
            "terminated": [{"name": "pr-foo-42", "id": "abc"}],
            "skipped": [], "removed_records": ["pr-foo-42"],
            "total_found": 1, "total_terminated": 1,
        })
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            result = cleanup_runpod_review("https://station", 42)
        runner._request.assert_called_once_with(
            "POST", "/reviews/cleanup",
            json={"pr": 42, "dry_run": False},
        )
        assert result["total_terminated"] == 1

    def test_dry_run_passes_flag(self):
        runner = MagicMock()
        runner._request = MagicMock(return_value={
            "ok": True, "pr": 7, "dry_run": True,
            "terminated": [], "skipped": [{"name": "p"}],
            "total_found": 1, "total_terminated": 0,
        })
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            cleanup_runpod_review("https://station", 7, dry_run=True)
        body = runner._request.call_args.kwargs["json"]
        assert body["dry_run"] is True


# ---------------------------------------------------------------------------
# _normalize_repo_url
# ---------------------------------------------------------------------------

class TestNormalizeRepoUrl:
    def test_empty(self):
        assert _normalize_repo_url("") == ""
        assert _normalize_repo_url("   ") == ""

    def test_owner_name_slug(self):
        assert _normalize_repo_url("Comfy-Org/ComfyUI") == "comfy-org/comfyui"

    def test_full_https_url_with_dot_git(self):
        assert _normalize_repo_url(
            "https://github.com/Comfy-Org/ComfyUI.git"
        ) == "comfy-org/comfyui"

    def test_full_url_no_dot_git(self):
        assert _normalize_repo_url(
            "https://github.com/Kosinkadink/comfy-runner"
        ) == "kosinkadink/comfy-runner"

    def test_strips_trailing_slash(self):
        assert _normalize_repo_url(
            "https://github.com/o/r/"
        ) == "o/r"


# ---------------------------------------------------------------------------
# remote_install_at_pr
# ---------------------------------------------------------------------------

class TestRemoteInstallAtPr:
    def _runner(self, info: dict | None = None, *, raise_exc: Exception | None = None):
        runner = MagicMock()
        if raise_exc is not None:
            runner._request = MagicMock(side_effect=raise_exc)
        else:
            runner._request = MagicMock(return_value=info or {})
        return runner

    def test_match_returns_true(self):
        runner = self._runner({
            "deployed_pr": 42,
            "deployed_repo": "https://github.com/Comfy-Org/ComfyUI",
        })
        assert remote_install_at_pr(
            runner, "main", 42, "comfy-org", "ComfyUI",
        ) is True
        runner._request.assert_called_once_with("GET", "/main/info")

    def test_match_with_dot_git_suffix(self):
        runner = self._runner({
            "deployed_pr": 42,
            "deployed_repo": "https://github.com/Comfy-Org/ComfyUI.git",
        })
        assert remote_install_at_pr(
            runner, "main", 42, "Comfy-Org", "ComfyUI",
        ) is True

    def test_match_with_owner_name_slug(self):
        runner = self._runner({
            "deployed_pr": 42,
            "deployed_repo": "Comfy-Org/ComfyUI",
        })
        assert remote_install_at_pr(
            runner, "main", 42, "comfy-org", "comfyui",
        ) is True

    def test_pr_mismatch_returns_false(self):
        runner = self._runner({
            "deployed_pr": 41,
            "deployed_repo": "https://github.com/Comfy-Org/ComfyUI",
        })
        assert remote_install_at_pr(
            runner, "main", 42, "comfy-org", "ComfyUI",
        ) is False

    def test_repo_mismatch_returns_false(self):
        runner = self._runner({
            "deployed_pr": 42,
            "deployed_repo": "https://github.com/other/ComfyUI",
        })
        assert remote_install_at_pr(
            runner, "main", 42, "comfy-org", "ComfyUI",
        ) is False

    def test_no_deployed_pr_returns_false(self):
        runner = self._runner({"deployed_repo": "owner/repo"})
        assert remote_install_at_pr(
            runner, "main", 42, "owner", "repo",
        ) is False

    def test_invalid_deployed_pr_returns_false(self):
        runner = self._runner({
            "deployed_pr": "not-an-int",
            "deployed_repo": "owner/repo",
        })
        assert remote_install_at_pr(
            runner, "main", 42, "owner", "repo",
        ) is False

    def test_request_exception_returns_false_and_warns(self):
        runner = self._runner(raise_exc=RuntimeError("connection refused"))
        sink: list[str] = []
        result = remote_install_at_pr(
            runner, "main", 42, "owner", "repo",
            send_output=lambda t: sink.append(t),
        )
        assert result is False
        assert any("idempotency check" in line for line in sink)


# ---------------------------------------------------------------------------
# prepare_server_review
# ---------------------------------------------------------------------------

class TestPrepareServerReview:
    """``prepare_server_review`` talks straight to a comfy-runner server.

    No central station, no /pods/{name}/review proxy — just /<install>/info,
    /<install>/deploy, /reviews/local on the target server.
    """

    def _runner(
        self,
        *,
        info: dict | None = None,
        deploy_resp: dict | None = None,
        review_resp: dict | None = None,
        deploy_poll: dict | None = None,
        review_poll: dict | None = None,
    ):
        """Build a fake RemoteRunner with sequential _request side_effect."""
        runner = MagicMock()
        # _request will be called: GET /info (idempotency), POST /deploy,
        # POST /reviews/local. poll_job will be called twice (deploy +
        # review). Default to "not at PR" + simple OK responses so the
        # full happy path runs.
        info_default = info if info is not None else {}
        runner._request = MagicMock(side_effect=[
            info_default,
            deploy_resp or {"ok": True, "job_id": "deploy-job"},
            review_resp or {"ok": True, "job_id": "review-job"},
        ])
        runner.poll_job = MagicMock(side_effect=[
            deploy_poll or {"restarted": True, "port": 8188},
            review_poll or {"workflows": [], "downloaded": [], "failures": []},
        ])
        return runner

    def test_full_happy_path_deploy_then_review(self):
        runner = self._runner()
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            result = prepare_server_review(
                "https://h.ts.net:9189", "main",
                "comfy-org", "ComfyUI", 42,
            )
        # Three requests: info (idempotency), deploy, review.
        assert runner._request.call_count == 3
        info_call = runner._request.call_args_list[0]
        assert info_call.args == ("GET", "/main/info")
        deploy_call = runner._request.call_args_list[1]
        assert deploy_call.args == ("POST", "/main/deploy")
        deploy_body = deploy_call.kwargs["json"]
        assert deploy_body["pr"] == 42
        assert deploy_body["repo"] == "https://github.com/comfy-org/ComfyUI"
        assert deploy_body["start"] is True
        review_call = runner._request.call_args_list[2]
        assert review_call.args == ("POST", "/reviews/local")
        review_body = review_call.kwargs["json"]
        assert review_body["install"] == "main"
        assert review_body["owner"] == "comfy-org"
        assert review_body["repo"] == "ComfyUI"
        assert review_body["pr"] == 42
        # Two poll_job calls: deploy then review.
        assert runner.poll_job.call_count == 2
        # Result has the review payload + server_url + deploy_result.
        assert result["server_url"] == "https://h.ts.net:9189"
        assert result["deploy_result"]["restarted"] is True

    def test_idempotent_skip_when_already_at_pr(self):
        runner = MagicMock()
        # /info responds with matching PR — skip deploy entirely.
        runner._request = MagicMock(side_effect=[
            {
                "deployed_pr": 42,
                "deployed_repo": "https://github.com/Comfy-Org/ComfyUI",
            },
            {"ok": True, "job_id": "review-job"},
        ])
        runner.poll_job = MagicMock(return_value={
            "workflows": [], "downloaded": [], "failures": [],
        })
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            result = prepare_server_review(
                "https://h.ts.net:9189", "main",
                "comfy-org", "ComfyUI", 42,
            )
        # Only two requests: info, then directly /reviews/local. No deploy.
        assert runner._request.call_count == 2
        assert runner._request.call_args_list[0].args == ("GET", "/main/info")
        assert runner._request.call_args_list[1].args == (
            "POST", "/reviews/local",
        )
        # poll_job called once (review only).
        assert runner.poll_job.call_count == 1
        # Result records the skip.
        assert result["deploy_result"] == {
            "skipped": True, "reason": "PR already deployed",
        }

    def test_force_deploy_skips_idempotency_check(self):
        runner = MagicMock()
        # If force_deploy=True, /info should NOT be called — only deploy +
        # review.
        runner._request = MagicMock(side_effect=[
            {"ok": True, "job_id": "deploy-job"},
            {"ok": True, "job_id": "review-job"},
        ])
        runner.poll_job = MagicMock(side_effect=[
            {"restarted": True},
            {"workflows": []},
        ])
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_server_review(
                "https://h.ts.net:9189", "main",
                "owner", "repo", 1,
                force_deploy=True,
            )
        calls = runner._request.call_args_list
        assert calls[0].args == ("POST", "/main/deploy")
        assert calls[1].args == ("POST", "/reviews/local")
        assert all(c.args[0] != "GET" for c in calls)

    def test_passes_through_extras_to_review_body(self):
        runner = self._runner()
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_server_review(
                "https://h.ts.net:9189", "main",
                "o", "r", 1,
                github_token="ghp_x",
                download_token="hf_y",
                extra_models=[ModelEntry("m.safetensors", "https://h/m", "loras")],
                extra_workflows=["https://h/w.json"],
                allow_arbitrary_urls=True,
                skip_provisioning=True,
            )
        # Third _request is the review POST.
        review_body = runner._request.call_args_list[2].kwargs["json"]
        assert review_body["github_token"] == "ghp_x"
        assert review_body["download_token"] == "hf_y"
        assert review_body["extra_models"] == [
            {"name": "m.safetensors", "url": "https://h/m", "directory": "loras"},
        ]
        assert review_body["extra_workflows"] == ["https://h/w.json"]
        assert review_body["allow_arbitrary_urls"] is True
        assert review_body["skip_provisioning"] is True

    def test_omits_optional_fields_when_unset(self):
        runner = self._runner()
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_server_review(
                "https://h.ts.net:9189", "main",
                "o", "r", 1,
            )
        review_body = runner._request.call_args_list[2].kwargs["json"]
        assert "github_token" not in review_body
        assert "download_token" not in review_body
        assert "extra_models" not in review_body
        assert "extra_workflows" not in review_body
        assert review_body["allow_arbitrary_urls"] is False
        assert review_body["skip_provisioning"] is False

    def test_install_name_threaded_to_endpoints(self):
        runner = self._runner()
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_server_review(
                "https://h.ts.net:9189", "staging",
                "o", "r", 1,
            )
        calls = runner._request.call_args_list
        assert calls[0].args == ("GET", "/staging/info")
        assert calls[1].args == ("POST", "/staging/deploy")
        review_body = calls[2].kwargs["json"]
        assert review_body["install"] == "staging"

    def test_deploy_missing_job_id_raises(self):
        runner = MagicMock()
        runner._request = MagicMock(side_effect=[
            {},  # info — not at PR
            {"ok": True},  # deploy — no job_id
        ])
        runner.poll_job = MagicMock()
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            with pytest.raises(RuntimeError, match="job_id for deploy"):
                prepare_server_review(
                    "https://h.ts.net:9189", "main", "o", "r", 1,
                )

    def test_review_missing_job_id_raises(self):
        runner = MagicMock()
        runner._request = MagicMock(side_effect=[
            {},  # info
            {"ok": True, "job_id": "deploy-job"},
            {"ok": True},  # review — no job_id
        ])
        runner.poll_job = MagicMock(return_value={"restarted": True})
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            with pytest.raises(RuntimeError, match="job_id for review"):
                prepare_server_review(
                    "https://h.ts.net:9189", "main", "o", "r", 1,
                )

    def test_deploy_poll_failure_propagates(self):
        runner = MagicMock()
        runner._request = MagicMock(side_effect=[
            {},
            {"ok": True, "job_id": "deploy-job"},
        ])
        runner.poll_job = MagicMock(
            side_effect=RuntimeError("Job deploy-job failed: boom"),
        )
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                prepare_server_review(
                    "https://h.ts.net:9189", "main", "o", "r", 1,
                )

    def test_pipes_send_output_to_poll_jobs(self):
        sink: list[str] = []
        runner = self._runner()
        with patch(
            "comfy_runner.hosted.remote.RemoteRunner",
            return_value=runner,
        ):
            prepare_server_review(
                "https://h.ts.net:9189", "main", "o", "r", 1,
                send_output=lambda t: sink.append(t),
            )
        # Both poll_job calls got the same callback.
        for call in runner.poll_job.call_args_list:
            assert call.kwargs["on_output"] is not None
        # The wrapper printed at least the deploy/review banners.
        assert any("Deploying PR" in t for t in sink)
        assert any("Preparing review" in t for t in sink)
