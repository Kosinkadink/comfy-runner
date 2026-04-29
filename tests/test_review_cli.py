"""Tests for the ``review`` CLI subcommand and its parser helpers."""

from __future__ import annotations

import argparse
import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfy_runner_cli.cli import (  # noqa: E402
    _parse_model_flag,
    _parse_repo,
    _parse_review_target,
    cmd_review,
    cmd_review_cleanup,
)


# ---------------------------------------------------------------------------
# _parse_review_target
# ---------------------------------------------------------------------------

class TestParseReviewTarget:
    def test_none_defaults_local_main(self):
        assert _parse_review_target(None) == {"kind": "local", "install_name": "main"}

    def test_empty_string_defaults_local_main(self):
        assert _parse_review_target("") == {"kind": "local", "install_name": "main"}

    def test_local_bare(self):
        assert _parse_review_target("local") == {"kind": "local", "install_name": "main"}

    def test_local_with_install_name(self):
        assert _parse_review_target("local:dev") == {"kind": "local", "install_name": "dev"}

    def test_local_strips_whitespace(self):
        assert _parse_review_target("local: spaced ") == {
            "kind": "local", "install_name": "spaced",
        }

    def test_local_empty_install_name_rejected(self):
        with pytest.raises(ValueError, match="local"):
            _parse_review_target("local:")

    def test_remote_with_pod(self):
        assert _parse_review_target("remote:my-pod") == {
            "kind": "remote", "pod_name": "my-pod",
        }

    def test_remote_empty_rejected(self):
        with pytest.raises(ValueError, match="remote"):
            _parse_review_target("remote:")

    def test_runpod_bare(self):
        assert _parse_review_target("runpod") == {"kind": "runpod", "gpu_type": None}

    def test_runpod_with_gpu(self):
        assert _parse_review_target("runpod:RTX_4090") == {
            "kind": "runpod", "gpu_type": "RTX_4090",
        }

    def test_runpod_empty_gpu(self):
        # ``runpod:`` is allowed and means "any GPU".
        assert _parse_review_target("runpod:") == {"kind": "runpod", "gpu_type": None}

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError, match="Unknown target"):
            _parse_review_target("cloud:foo")

    def test_garbage_rejected(self):
        with pytest.raises(ValueError, match="Unknown target"):
            _parse_review_target("nonsense")


# ---------------------------------------------------------------------------
# _parse_repo
# ---------------------------------------------------------------------------

class TestParseRepo:
    def test_owner_name(self):
        assert _parse_repo("comfy-org/ComfyUI") == ("comfy-org", "ComfyUI")

    def test_https_url(self):
        assert _parse_repo("https://github.com/comfy-org/ComfyUI") == (
            "comfy-org", "ComfyUI",
        )

    def test_https_url_with_git_suffix(self):
        assert _parse_repo("https://github.com/comfy-org/ComfyUI.git") == (
            "comfy-org", "ComfyUI",
        )

    def test_http_url_accepted(self):
        # We accept http:// for paste-friendliness — we never fetch via this URL.
        assert _parse_repo("http://github.com/comfy-org/ComfyUI") == (
            "comfy-org", "ComfyUI",
        )

    def test_bare_github_com_prefix(self):
        assert _parse_repo("github.com/comfy-org/ComfyUI") == (
            "comfy-org", "ComfyUI",
        )

    def test_trailing_slash_tolerated(self):
        assert _parse_repo("comfy-org/ComfyUI/") == ("comfy-org", "ComfyUI")

    def test_strips_whitespace(self):
        assert _parse_repo("  comfy-org/ComfyUI  ") == ("comfy-org", "ComfyUI")

    def test_single_segment_rejected(self):
        with pytest.raises(ValueError, match="owner/name"):
            _parse_repo("ComfyUI")

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="required"):
            _parse_repo("")


# ---------------------------------------------------------------------------
# _parse_model_flag
# ---------------------------------------------------------------------------

class TestParseModelFlag:
    def test_simple(self):
        assert _parse_model_flag("a.safetensors=https://h/x=checkpoints") == {
            "name": "a.safetensors",
            "url": "https://h/x",
            "directory": "checkpoints",
        }

    def test_url_with_equals_signs_preserved(self):
        # Splits on first two `=` only, so URLs with `=` survive intact.
        spec = "model.safetensors=https://h/x?download=true&sig=abc=loras"
        assert _parse_model_flag(spec) == {
            "name": "model.safetensors",
            "url": "https://h/x?download=true&sig=abc",
            "directory": "loras",
        }

    def test_only_one_equals_rejected(self):
        with pytest.raises(ValueError, match="name=url=directory"):
            _parse_model_flag("name=url")

    def test_no_equals_rejected(self):
        with pytest.raises(ValueError, match="name=url=directory"):
            _parse_model_flag("just-a-name")

    def test_empty_name_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            _parse_model_flag("=https://h/x=checkpoints")

    def test_empty_directory_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            _parse_model_flag("name=https://h/x=")

    def test_strips_whitespace(self):
        assert _parse_model_flag(" name = https://h/x = checkpoints ") == {
            "name": "name",
            "url": "https://h/x",
            "directory": "checkpoints",
        }


# ---------------------------------------------------------------------------
# cmd_review — top-level command behavior
# ---------------------------------------------------------------------------

def _review_args(**overrides) -> argparse.Namespace:
    """Build a Namespace shaped like argparse would produce for ``review``."""
    defaults = {
        "pr": 123,
        "repo": "comfy-org/ComfyUI",
        "target": "local",
        "workflow": [],
        "model": [],
        "token": None,
        "github_token": None,
        "no_provision_models": False,
        "allow_arbitrary_urls": False,
        "json": False,
        "server": None,
        "install": "main",
        "force_purpose": False,
        "cleanup": False,
        "force_deploy": False,
        "idle_stop_after": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestCmdReviewParseErrors:
    def test_invalid_repo_exits_1(self):
        args = _review_args(repo="bogus")
        with pytest.raises(SystemExit) as exc:
            cmd_review(args)
        assert exc.value.code == 1

    def test_invalid_target_exits_1(self):
        args = _review_args(target="cloud:foo")
        with pytest.raises(SystemExit) as exc:
            cmd_review(args)
        assert exc.value.code == 1

    def test_invalid_model_flag_exits_1(self):
        args = _review_args(model=["only-one-equals=foo"])
        # Repo / target parse first; cmd_deploy is reached before --model is
        # parsed in the current implementation, so we mock cmd_deploy.
        with patch("comfy_runner_cli.cli.cmd_deploy"):
            with pytest.raises(SystemExit) as exc:
                cmd_review(args)
        assert exc.value.code == 1


class TestCmdReviewLocalHappy:
    def _patches(self, install_path: Path, review_result: dict):
        """Common patch set for happy-path local review tests."""
        return [
            patch("comfy_runner_cli.cli.cmd_deploy"),
            patch(
                "comfy_runner.config.get_installation",
                return_value={"status": "installed", "path": str(install_path)},
            ),
            patch(
                "comfy_runner.review.prepare_local_review",
                return_value=review_result,
            ),
        ]

    def test_json_output_shape(self, tmp_path: Path, capsys, tmp_config_dir):
        install_path = tmp_path / "install"
        install_path.mkdir()
        review_result = {
            "manifest": {"models": [], "workflows": []},
            "resolved": None,
            "downloaded": [],
            "skipped": [],
            "failed": [],
            "errors": [],
            "workflows": [],
            "workflows_dir": str(install_path / "ComfyUI" / "user" / "default" / "workflows"),
            "failures": [],
        }
        args = _review_args(json=True)
        with patch("comfy_runner_cli.cli.cmd_deploy"), \
             patch(
                 "comfy_runner.config.get_installation",
                 return_value={"status": "installed", "path": str(install_path)},
             ), \
             patch(
                 "comfy_runner.review.prepare_local_review",
                 return_value=review_result,
             ):
            cmd_review(args)
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["target"] == "local:main"
        assert payload["pr"] == 123
        assert payload["repo"] == "comfy-org/ComfyUI"
        assert payload["install_path"] == str(install_path)

    def test_failures_cause_nonzero_exit(self, tmp_path: Path, tmp_config_dir):
        install_path = tmp_path / "install"
        install_path.mkdir()
        review_result = {
            "manifest": {"models": [], "workflows": []},
            "resolved": None,
            "downloaded": [],
            "skipped": [],
            "failed": [],
            "errors": [],
            "workflows": [],
            "workflows_dir": str(install_path),
            "failures": [{"url": "https://h/bad.json", "error": "404"}],
        }
        args = _review_args(json=True)
        with patch("comfy_runner_cli.cli.cmd_deploy"), \
             patch(
                 "comfy_runner.config.get_installation",
                 return_value={"status": "installed", "path": str(install_path)},
             ), \
             patch(
                 "comfy_runner.review.prepare_local_review",
                 return_value=review_result,
             ):
            with pytest.raises(SystemExit) as exc:
                cmd_review(args)
        assert exc.value.code == 1

    def test_failed_models_cause_nonzero_exit(self, tmp_path: Path, tmp_config_dir):
        install_path = tmp_path / "install"
        install_path.mkdir()
        review_result = {
            "manifest": {"models": [], "workflows": []},
            "resolved": None,
            "downloaded": [],
            "skipped": [],
            "failed": ["checkpoints/x"],
            "errors": ["404"],
            "workflows": [],
            "workflows_dir": str(install_path),
            "failures": [],
        }
        args = _review_args(json=False)
        with patch("comfy_runner_cli.cli.cmd_deploy"), \
             patch(
                 "comfy_runner.config.get_installation",
                 return_value={"status": "installed", "path": str(install_path)},
             ), \
             patch(
                 "comfy_runner.review.prepare_local_review",
                 return_value=review_result,
             ):
            with pytest.raises(SystemExit) as exc:
                cmd_review(args)
        assert exc.value.code == 1

    def test_flags_threaded_to_prepare(self, tmp_path: Path, tmp_config_dir):
        install_path = tmp_path / "install"
        install_path.mkdir()
        review_result = {
            "manifest": None, "resolved": None,
            "downloaded": [], "skipped": [], "failed": [], "errors": [],
            "workflows": [], "workflows_dir": str(install_path), "failures": [],
        }
        args = _review_args(
            workflow=["https://huggingface.co/wf.json"],
            model=["a.safetensors=https://h/a=checkpoints"],
            token="hf_tok",
            github_token="ghp_tok",
            no_provision_models=True,
            allow_arbitrary_urls=True,
            json=True,
        )
        with patch("comfy_runner_cli.cli.cmd_deploy"), \
             patch(
                 "comfy_runner.config.get_installation",
                 return_value={"status": "installed", "path": str(install_path)},
             ), \
             patch(
                 "comfy_runner.review.prepare_local_review",
                 return_value=review_result,
             ) as mock_prep:
            cmd_review(args)
        kwargs = mock_prep.call_args.kwargs
        assert kwargs["github_token"] == "ghp_tok"
        assert kwargs["download_token"] == "hf_tok"
        assert kwargs["skip_provisioning"] is True
        assert kwargs["allow_arbitrary_urls"] is True
        assert kwargs["extra_workflows"] == ["https://huggingface.co/wf.json"]
        assert len(kwargs["extra_models"]) == 1
        assert kwargs["extra_models"][0].name == "a.safetensors"

    def test_deploy_failure_propagates(self, tmp_path: Path, tmp_config_dir):
        install_path = tmp_path / "install"
        install_path.mkdir()
        # cmd_deploy calling sys.exit(1) must propagate; prepare_local_review
        # must NOT be called.
        with patch("comfy_runner_cli.cli.cmd_deploy", side_effect=SystemExit(1)), \
             patch("comfy_runner.review.prepare_local_review") as mock_prep:
            args = _review_args()
            with pytest.raises(SystemExit) as exc:
                cmd_review(args)
        assert exc.value.code == 1
        mock_prep.assert_not_called()

    def test_missing_install_after_deploy_errors(self, tmp_path: Path, tmp_config_dir):
        # cmd_deploy succeeds (mocked) but get_installation returns None — the
        # guard should error rather than crash later.
        with patch("comfy_runner_cli.cli.cmd_deploy"), \
             patch("comfy_runner.config.get_installation", return_value=None), \
             patch("comfy_runner.review.prepare_local_review") as mock_prep:
            args = _review_args(json=True)
            with pytest.raises(SystemExit) as exc:
                cmd_review(args)
        assert exc.value.code == 1
        mock_prep.assert_not_called()

    def test_local_target_with_install_name(self, tmp_path: Path, tmp_config_dir):
        install_path = tmp_path / "dev-install"
        install_path.mkdir()
        review_result = {
            "manifest": None, "resolved": None,
            "downloaded": [], "skipped": [], "failed": [], "errors": [],
            "workflows": [], "workflows_dir": str(install_path), "failures": [],
        }
        # When the user passes ``--target local:dev`` the deploy step must be
        # called with the dev install name.
        with patch("comfy_runner_cli.cli.cmd_deploy") as mock_deploy, \
             patch(
                 "comfy_runner.config.get_installation",
                 return_value={"status": "installed", "path": str(install_path)},
             ), \
             patch(
                 "comfy_runner.review.prepare_local_review",
                 return_value=review_result,
             ):
            args = _review_args(target="local:dev", json=True)
            cmd_review(args)
        deploy_args = mock_deploy.call_args.args[0]
        assert deploy_args.name == "dev"
        assert deploy_args.pr == 123


# ---------------------------------------------------------------------------
# cmd_review — remote target (item 2)
# ---------------------------------------------------------------------------

class TestCmdReviewRemote:
    """``--target remote:<pod>`` dispatches to prepare_remote_review."""

    _OK_RESULT = {
        "manifest": None,
        "resolved": None,
        "downloaded": [],
        "skipped": [],
        "failed": [],
        "errors": [],
        "workflows": [],
        "workflows_dir": "/x",
        "failures": [],
        "pod_name": "pod-a",
        "server_url": "https://pod-a.ts.net:9189",
        "deploy_result": {"restarted": True},
    }

    def test_calls_prepare_remote_review_with_station_url(
        self, tmp_config_dir, capsys,
    ):
        args = _review_args(
            target="remote:pod-a", server="https://station.example", json=True,
        )
        with patch(
            "comfy_runner.review.prepare_remote_review",
            return_value=dict(self._OK_RESULT),
        ) as mock_prep:
            cmd_review(args)
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["target"] == "remote:pod-a"
        assert payload["pod_name"] == "pod-a"
        assert payload["server_url"] == "https://pod-a.ts.net:9189"
        assert payload["pr"] == 123
        # prepare_remote_review got the right station URL + pod + repo args.
        call = mock_prep.call_args
        assert call.args[0] == "https://station.example"
        assert call.args[1] == "pod-a"
        assert call.args[2] == "main"
        assert call.args[3] == "comfy-org"
        assert call.args[4] == "ComfyUI"
        assert call.args[5] == 123

    def test_install_override_passes_through(
        self, tmp_config_dir, capsys,
    ):
        args = _review_args(
            target="remote:pod-a", server="https://station.example",
            install="staging", json=True,
        )
        with patch(
            "comfy_runner.review.prepare_remote_review",
            return_value=dict(self._OK_RESULT),
        ) as mock_prep:
            cmd_review(args)
        assert mock_prep.call_args.args[2] == "staging"

    def test_no_station_config_errors(self, tmp_path: Path, monkeypatch, capsys):
        # No station.json anywhere, no --server flag → friendly error,
        # exit 1, prepare_remote_review never called.
        monkeypatch.chdir(tmp_path)
        args = _review_args(target="remote:pod-a", server=None, json=True)
        with patch(
            "comfy_runner.review.prepare_remote_review",
        ) as mock_prep:
            with pytest.raises(SystemExit) as exc:
                cmd_review(args)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["ok"] is False
        assert "station.json" in payload["error"]
        mock_prep.assert_not_called()

    def test_remote_failure_propagates(self, tmp_config_dir, capsys):
        args = _review_args(
            target="remote:pod-a", server="https://station.example", json=True,
        )
        with patch(
            "comfy_runner.review.prepare_remote_review",
            side_effect=RuntimeError("Remote server error: pod terminated"),
        ):
            with pytest.raises(SystemExit) as exc:
                cmd_review(args)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["ok"] is False
        assert "pod terminated" in payload["error"]

    def test_remote_failures_cause_exit_1(self, tmp_config_dir):
        bad = dict(self._OK_RESULT)
        bad["failures"] = [{"url": "https://h/x", "error": "404"}]
        args = _review_args(
            target="remote:pod-a", server="https://station.example", json=True,
        )
        with patch(
            "comfy_runner.review.prepare_remote_review",
            return_value=bad,
        ):
            with pytest.raises(SystemExit) as exc:
                cmd_review(args)
        assert exc.value.code == 1

    def test_remote_does_not_call_local_deploy(self, tmp_config_dir):
        args = _review_args(
            target="remote:pod-a", server="https://station.example", json=True,
        )
        with patch(
            "comfy_runner.review.prepare_remote_review",
            return_value=dict(self._OK_RESULT),
        ), patch("comfy_runner_cli.cli.cmd_deploy") as mock_deploy, \
             patch("comfy_runner.review.prepare_local_review") as mock_local:
            cmd_review(args)
        mock_deploy.assert_not_called()
        mock_local.assert_not_called()

    def test_remote_threads_extras(self, tmp_config_dir):
        args = _review_args(
            target="remote:pod-a", server="https://station.example",
            workflow=["https://h/wf.json"],
            model=["a.safetensors=https://h/a=checkpoints"],
            token="hf_tok", github_token="ghp_tok",
            no_provision_models=True, allow_arbitrary_urls=True,
            json=True,
        )
        with patch(
            "comfy_runner.review.prepare_remote_review",
            return_value=dict(self._OK_RESULT),
        ) as mock_prep:
            cmd_review(args)
        kwargs = mock_prep.call_args.kwargs
        assert kwargs["github_token"] == "ghp_tok"
        assert kwargs["download_token"] == "hf_tok"
        assert kwargs["skip_provisioning"] is True
        assert kwargs["allow_arbitrary_urls"] is True
        assert kwargs["extra_workflows"] == ["https://h/wf.json"]
        assert len(kwargs["extra_models"]) == 1
        assert kwargs["extra_models"][0].name == "a.safetensors"

    def test_force_purpose_default_false(self, tmp_config_dir):
        args = _review_args(
            target="remote:pod-a", server="https://station.example",
            json=True,
        )
        with patch(
            "comfy_runner.review.prepare_remote_review",
            return_value=dict(self._OK_RESULT),
        ) as mock_prep:
            cmd_review(args)
        assert mock_prep.call_args.kwargs["force_purpose"] is False

    def test_force_purpose_flag_threaded(self, tmp_config_dir):
        args = _review_args(
            target="remote:pod-a", server="https://station.example",
            force_purpose=True, json=True,
        )
        with patch(
            "comfy_runner.review.prepare_remote_review",
            return_value=dict(self._OK_RESULT),
        ) as mock_prep:
            cmd_review(args)
        assert mock_prep.call_args.kwargs["force_purpose"] is True

    def test_force_deploy_flag_threaded(self, tmp_config_dir):
        args = _review_args(
            target="remote:pod-a", server="https://station.example",
            force_deploy=True, json=True,
        )
        with patch(
            "comfy_runner.review.prepare_remote_review",
            return_value=dict(self._OK_RESULT),
        ) as mock_prep:
            cmd_review(args)
        assert mock_prep.call_args.kwargs["force_deploy"] is True

    def test_idle_stop_after_flag_threaded(self, tmp_config_dir):
        args = _review_args(
            target="remote:pod-a", server="https://station.example",
            idle_stop_after=600, json=True,
        )
        with patch(
            "comfy_runner.review.prepare_remote_review",
            return_value=dict(self._OK_RESULT),
        ) as mock_prep:
            cmd_review(args)
        assert mock_prep.call_args.kwargs["idle_timeout_s"] == 600

    def test_force_deploy_default_false(self, tmp_config_dir):
        args = _review_args(
            target="remote:pod-a", server="https://station.example",
            json=True,
        )
        with patch(
            "comfy_runner.review.prepare_remote_review",
            return_value=dict(self._OK_RESULT),
        ) as mock_prep:
            cmd_review(args)
        assert mock_prep.call_args.kwargs["force_deploy"] is False
        assert mock_prep.call_args.kwargs["idle_timeout_s"] is None


# ---------------------------------------------------------------------------
# cmd_review — runpod target (item 3)
# ---------------------------------------------------------------------------

class TestCmdReviewRunpod:
    """``--target runpod[:<gpu>]`` dispatches to prepare_runpod_review."""

    _OK_RESULT = {
        "manifest": None, "resolved": None,
        "downloaded": [], "skipped": [], "failed": [], "errors": [],
        "workflows": [], "workflows_dir": "/x", "failures": [],
        "pod_name": "pr-foo-123", "pod_purpose": "pr",
        "server_url": "https://pod-a.ts.net:9189",
        "deploy_result": {"restarted": True},
        "created_new": True,
        "idle_timeout_s": 1800,
    }

    def test_calls_prepare_runpod_with_gpu_type(
        self, tmp_config_dir, capsys,
    ):
        args = _review_args(
            target="runpod:RTX_4090",
            server="https://station.example", json=True,
        )
        with patch(
            "comfy_runner.review.prepare_runpod_review",
            return_value=dict(self._OK_RESULT),
        ) as mock_prep:
            cmd_review(args)
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["target"] == "runpod:RTX_4090"
        assert payload["pod_name"] == "pr-foo-123"
        assert payload["created_new"] is True
        assert payload["idle_timeout_s"] == 1800

        kwargs = mock_prep.call_args.kwargs
        assert kwargs["gpu_type"] == "RTX_4090"

    def test_runpod_bare_omits_gpu_type(self, tmp_config_dir):
        args = _review_args(
            target="runpod",
            server="https://station.example", json=True,
        )
        with patch(
            "comfy_runner.review.prepare_runpod_review",
            return_value=dict(self._OK_RESULT),
        ) as mock_prep:
            cmd_review(args)
        # gpu_type=None is the "any GPU" sentinel from _parse_review_target.
        assert mock_prep.call_args.kwargs["gpu_type"] is None

    def test_cleanup_flag_calls_cleanup_runpod_review(
        self, tmp_config_dir,
    ):
        args = _review_args(
            target="runpod:RTX_4090",
            server="https://station.example",
            cleanup=True, json=True,
        )
        with patch(
            "comfy_runner.review.prepare_runpod_review",
            return_value=dict(self._OK_RESULT),
        ), patch(
            "comfy_runner.review.cleanup_runpod_review",
            return_value={"ok": True, "total_terminated": 1},
        ) as mock_cleanup:
            cmd_review(args)
        # --cleanup hits the station's /reviews/cleanup with this PR.
        assert mock_cleanup.call_args.args[0] == "https://station.example"
        assert mock_cleanup.call_args.args[1] == 123

    def test_cleanup_failure_does_not_break_review(
        self, tmp_config_dir, capsys,
    ):
        args = _review_args(
            target="runpod:RTX_4090",
            server="https://station.example",
            cleanup=True, json=True,
        )
        with patch(
            "comfy_runner.review.prepare_runpod_review",
            return_value=dict(self._OK_RESULT),
        ), patch(
            "comfy_runner.review.cleanup_runpod_review",
            side_effect=RuntimeError("network glitch"),
        ):
            cmd_review(args)
        out = capsys.readouterr().out
        payload = json.loads(out)
        # The review still succeeds (ok=True); cleanup_error surfaces.
        assert payload["ok"] is True
        assert payload["cleanup_error"] == "network glitch"

    def test_no_station_config_errors(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        args = _review_args(
            target="runpod:RTX_4090", server=None, json=True,
        )
        with patch(
            "comfy_runner.review.prepare_runpod_review",
        ) as mock_prep:
            with pytest.raises(SystemExit) as exc:
                cmd_review(args)
        assert exc.value.code == 1
        mock_prep.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert "station.json" in payload["error"]

    def test_idle_stop_after_flag_threaded(self, tmp_config_dir):
        args = _review_args(
            target="runpod:RTX_4090",
            server="https://station.example",
            idle_stop_after=900, json=True,
        )
        with patch(
            "comfy_runner.review.prepare_runpod_review",
            return_value=dict(self._OK_RESULT),
        ) as mock_prep:
            cmd_review(args)
        # idle_timeout_s is plumbed to launch-pr's body via prepare_runpod_review.
        assert mock_prep.call_args.kwargs["idle_timeout_s"] == 900


# ---------------------------------------------------------------------------
# cmd_review_cleanup (item 3)
# ---------------------------------------------------------------------------

def _cleanup_args(**overrides) -> argparse.Namespace:
    defaults = {
        "pr": 123,
        "server": "https://station.example",
        "dry_run": False,
        "json": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestCmdReviewCleanup:
    def test_terminates_returns_summary(self, tmp_config_dir, capsys):
        args = _cleanup_args(json=True)
        result = {
            "ok": True, "pr": 123, "dry_run": False,
            "terminated": [{"name": "pr-foo-123", "id": "abc"}],
            "skipped": [], "removed_records": ["pr-foo-123"],
            "total_found": 1, "total_terminated": 1,
        }
        with patch(
            "comfy_runner.review.cleanup_runpod_review",
            return_value=result,
        ) as mock_clean:
            cmd_review_cleanup(args)
        mock_clean.assert_called_once_with(
            "https://station.example", 123, dry_run=False,
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["total_terminated"] == 1

    def test_dry_run_passes_flag(self, tmp_config_dir):
        args = _cleanup_args(dry_run=True, json=True)
        with patch(
            "comfy_runner.review.cleanup_runpod_review",
            return_value={"ok": True, "total_found": 0, "terminated": []},
        ) as mock_clean:
            cmd_review_cleanup(args)
        assert mock_clean.call_args.kwargs == {"dry_run": True}

    def test_no_matches_quiet(self, tmp_config_dir, capsys):
        args = _cleanup_args(json=False)
        with patch(
            "comfy_runner.review.cleanup_runpod_review",
            return_value={
                "ok": True, "pr": 123, "total_found": 0,
                "terminated": [], "skipped": [],
            },
        ):
            cmd_review_cleanup(args)
        out = capsys.readouterr().out
        assert "No PR-#123 pods" in out

    def test_no_station_config_errors(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        args = _cleanup_args(server=None, json=True)
        with patch(
            "comfy_runner.review.cleanup_runpod_review",
        ) as mock_clean:
            with pytest.raises(SystemExit) as exc:
                cmd_review_cleanup(args)
        assert exc.value.code == 1
        mock_clean.assert_not_called()

    def test_runtime_error_propagates(self, tmp_config_dir, capsys):
        args = _cleanup_args(json=True)
        with patch(
            "comfy_runner.review.cleanup_runpod_review",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(SystemExit) as exc:
                cmd_review_cleanup(args)
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert "boom" in payload["error"]
