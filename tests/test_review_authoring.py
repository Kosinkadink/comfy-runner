"""Tests for comfy_runner.review_authoring — block generator + linter
used by the ``review-init`` and ``review-validate`` CLI commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comfy_runner.review_authoring import (  # noqa: E402
    PLACEHOLDER_WORKFLOW_URL,
    GeneratedBlock,
    LintFinding,
    LintResult,
    _resolve_source,
    generate_block,
    lint_manifest_json,
    lint_manifest_source,
    lint_manifest_text,
)
from comfy_runner.manifest import MAX_PR_BODY_BYTES  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_workflow(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "workflow.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _node(models: list[dict] | None = None) -> dict:
    n: dict = {"id": 1, "type": "CheckpointLoader"}
    if models is not None:
        n["properties"] = {"models": models}
    return n


# ===========================================================================
# generate_block
# ===========================================================================

class TestGenerateBlock:
    def test_workflow_with_models_emits_block(self, tmp_path):
        wf = _write_workflow(tmp_path, {"nodes": [
            _node([
                {
                    "name": "sdxl_base.safetensors",
                    "url": "https://huggingface.co/example/base.safetensors",
                    "directory": "checkpoints",
                },
                {
                    "name": "sdxl_vae.safetensors",
                    "url": "https://huggingface.co/example/vae.safetensors",
                    "directory": "vae",
                },
            ]),
        ]})

        result = generate_block(
            wf,
            workflow_url="https://raw.githubusercontent.com/o/r/branch/wf.json",
        )

        assert isinstance(result, GeneratedBlock)
        assert result.text.startswith("```comfyrunner\n")
        assert result.text.endswith("\n```")
        assert result.manifest_dict["workflows"] == [
            "https://raw.githubusercontent.com/o/r/branch/wf.json"
        ]
        assert result.manifest_dict["models"] == [
            {
                "name": "sdxl_base.safetensors",
                "url": "https://huggingface.co/example/base.safetensors",
                "directory": "checkpoints",
            },
            {
                "name": "sdxl_vae.safetensors",
                "url": "https://huggingface.co/example/vae.safetensors",
                "directory": "vae",
            },
        ]
        assert result.warnings == []

    def test_no_workflow_url_uses_placeholder_and_warns(self, tmp_path):
        wf = _write_workflow(tmp_path, {"nodes": [
            _node([{
                "name": "m.safetensors",
                "url": "https://huggingface.co/foo.safetensors",
                "directory": "checkpoints",
            }]),
        ]})

        result = generate_block(wf)

        assert result.manifest_dict["workflows"] == [PLACEHOLDER_WORKFLOW_URL]
        assert any("workflow-url" in w.lower() for w in result.warnings)

    def test_empty_workflow_warns_about_no_models(self, tmp_path):
        wf = _write_workflow(tmp_path, {"nodes": []})

        result = generate_block(wf, workflow_url="https://github.com/o/r/raw/main/wf.json")

        assert result.manifest_dict["models"] == []
        assert any("model" in w.lower() for w in result.warnings)

    def test_dedup_by_name_and_directory(self, tmp_path):
        wf = _write_workflow(tmp_path, {"nodes": [
            _node([{
                "name": "m.safetensors",
                "url": "https://huggingface.co/a.safetensors",
                "directory": "checkpoints",
            }]),
            _node([{
                "name": "m.safetensors",
                "url": "https://huggingface.co/b.safetensors",  # different URL
                "directory": "checkpoints",  # same dir + name -> deduped
            }]),
        ]})

        result = generate_block(wf, workflow_url="https://github.com/o/r/raw/main/wf.json")

        assert len(result.manifest_dict["models"]) == 1

    def test_models_missing_fields_skipped(self, tmp_path):
        # parse_workflow_models filters entries missing any of name/url/directory.
        wf = _write_workflow(tmp_path, {"nodes": [
            _node([
                {"name": "good.safetensors", "url": "https://huggingface.co/g", "directory": "checkpoints"},
                {"name": "no-url.safetensors", "directory": "checkpoints"},  # missing url
                {"url": "https://huggingface.co/x", "directory": "checkpoints"},  # missing name
                {"name": "no-dir.safetensors", "url": "https://huggingface.co/d"},  # missing dir
            ]),
        ]})

        result = generate_block(wf, workflow_url="https://github.com/o/r/raw/main/wf.json")

        assert len(result.manifest_dict["models"]) == 1
        assert result.manifest_dict["models"][0]["name"] == "good.safetensors"

    def test_missing_file_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            generate_block(tmp_path / "nope.json")

    def test_invalid_json_raises_value_error(self, tmp_path):
        wf = tmp_path / "wf.json"
        wf.write_text("not json {", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            generate_block(wf)

    def test_binary_file_raises_value_error(self, tmp_path):
        wf = tmp_path / "wf.json"
        wf.write_bytes(b"\xff\xfe\xfd\x80")
        with pytest.raises(ValueError, match="UTF-8"):
            generate_block(wf)

    def test_top_level_array_rejected(self, tmp_path):
        wf = tmp_path / "wf.json"
        wf.write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON object"):
            generate_block(wf)

    def test_block_inner_is_valid_json(self, tmp_path):
        wf = _write_workflow(tmp_path, {"nodes": [_node([{
            "name": "m.safetensors",
            "url": "https://huggingface.co/m",
            "directory": "checkpoints",
        }])]})

        result = generate_block(wf, workflow_url="https://github.com/o/r/raw/main/wf.json")

        # Strip the fence and re-parse to confirm round-trip.
        inner = result.text.split("\n", 1)[1].rsplit("\n", 1)[0]
        parsed = json.loads(inner)
        assert parsed == result.manifest_dict


# ===========================================================================
# lint_manifest_text
# ===========================================================================

class TestLintManifestText:
    def test_valid_fenced_block(self):
        text = (
            "Some PR description.\n\n"
            "```comfyrunner\n"
            '{\n'
            '  "workflows": ["https://raw.githubusercontent.com/o/r/main/w.json"],\n'
            '  "models": [{"name":"m.safetensors","url":"https://huggingface.co/m","directory":"checkpoints"}]\n'
            "}\n"
            "```\n"
        )
        found, manifest, findings = lint_manifest_text(text)
        assert found is True
        assert manifest is not None
        assert len(manifest.models) == 1
        assert [f for f in findings if f.severity == "error"] == []

    def test_no_block(self):
        found, manifest, findings = lint_manifest_text("just prose, no manifest")
        assert found is False
        assert manifest is None
        assert findings and findings[0].severity == "info"

    def test_malformed_json_in_block(self):
        text = "```comfyrunner\n{not json}\n```\n"
        found, manifest, findings = lint_manifest_text(text)
        assert found is True
        assert manifest is None
        assert any(f.severity == "error" and "JSON" in f.message for f in findings)

    def test_missing_required_model_field(self):
        text = (
            "```comfyrunner\n"
            '{"models": [{"name": "m.safetensors", "directory": "checkpoints"}]}\n'
            "```\n"
        )
        found, manifest, findings = lint_manifest_text(text)
        assert found is True
        assert manifest is None
        assert any(f.severity == "error" for f in findings)

    def test_non_https_model_url(self):
        text = (
            "```comfyrunner\n"
            '{"models": [{"name":"m.safetensors","url":"http://example.com/m","directory":"checkpoints"}]}\n'
            "```\n"
        )
        found, manifest, findings = lint_manifest_text(text)
        assert found is True
        assert manifest is not None
        errors = [f for f in findings if f.severity == "error"]
        assert errors
        assert any("HTTPS" in f.message for f in errors)
        assert errors[0].path == "models[0].url"

    def test_non_allowlisted_host_warns(self):
        text = (
            "```comfyrunner\n"
            '{"models": [{"name":"m.safetensors","url":"https://example.com/m","directory":"checkpoints"}]}\n'
            "```\n"
        )
        found, manifest, findings = lint_manifest_text(text)
        assert found is True
        assert manifest is not None
        warns = [f for f in findings if f.severity == "warn"]
        assert warns
        assert any("allowlist" in f.message for f in warns)

    def test_empty_manifest_warns(self):
        text = '```comfyrunner\n{"models": [], "workflows": []}\n```\n'
        found, manifest, findings = lint_manifest_text(text)
        assert found is True
        assert manifest is not None
        assert any(
            f.severity == "warn" and "empty" in f.message
            for f in findings
        )

    def test_workflow_url_must_be_https(self):
        text = (
            "```comfyrunner\n"
            '{"workflows": ["http://example.com/w.json"]}\n'
            "```\n"
        )
        found, manifest, findings = lint_manifest_text(text)
        assert found is True
        assert manifest is not None
        errors = [f for f in findings if f.severity == "error"]
        assert any("HTTPS" in f.message and f.path == "workflows[0]" for f in errors)

    def test_raw_json_text_not_treated_as_manifest(self):
        # Free-form text starting with ``{`` (e.g. a PR description that
        # begins with a JSON snippet) must NOT be parsed as a raw
        # manifest. lint_manifest_text only looks for a fenced block.
        text = json.dumps({
            "workflows": ["https://raw.githubusercontent.com/o/r/main/w.json"],
            "models": [],
        })
        found, manifest, findings = lint_manifest_text(text)
        assert found is False
        assert manifest is None
        # Only the info-level "no block" finding — never an error.
        assert [f for f in findings if f.severity == "error"] == []
        assert any(f.severity == "info" for f in findings)

    def test_raw_garbage_starting_with_brace(self):
        # Same: malformed text starting with ``{`` isn't an error,
        # because lint_manifest_text only cares about fenced blocks.
        text = "{ not json"
        found, manifest, findings = lint_manifest_text(text)
        assert found is False
        assert manifest is None
        assert [f for f in findings if f.severity == "error"] == []
        assert any(f.severity == "info" for f in findings)

    def test_oversize_input_refused(self):
        # Way too large to scan; report found_block=False so the user
        # doesn't see the misleading "block failed validation" message.
        text = "x" * (MAX_PR_BODY_BYTES + 1)
        found, manifest, findings = lint_manifest_text(text)
        assert found is False
        assert manifest is None
        errors = [f for f in findings if f.severity == "error"]
        assert errors
        assert any("too large" in f.message for f in errors)

    def test_model_name_with_slash_rejected(self):
        text = (
            "```comfyrunner\n"
            '{"models": [{"name":"sub/m.safetensors","url":"https://huggingface.co/m","directory":"checkpoints"}]}\n'
            "```\n"
        )
        found, manifest, findings = lint_manifest_text(text)
        assert found is True
        assert manifest is not None
        errors = [f for f in findings if f.severity == "error"]
        assert any("bare filename" in f.message for f in errors)

    def test_model_name_dotdot_rejected(self):
        text = (
            "```comfyrunner\n"
            '{"models": [{"name":"..","url":"https://huggingface.co/m","directory":"checkpoints"}]}\n'
            "```\n"
        )
        found, manifest, findings = lint_manifest_text(text)
        assert found is True
        assert manifest is not None
        errors = [f for f in findings if f.severity == "error"]
        assert any("bare filename" in f.message for f in errors)

    def test_model_directory_with_dotdot_rejected(self):
        text = (
            "```comfyrunner\n"
            '{"models": [{"name":"m.safetensors","url":"https://huggingface.co/m","directory":"../etc"}]}\n'
            "```\n"
        )
        found, manifest, findings = lint_manifest_text(text)
        assert found is True
        assert manifest is not None
        errors = [f for f in findings if f.severity == "error"]
        assert any("relative" in f.message for f in errors)

    def test_model_directory_absolute_posix_rejected(self):
        text = (
            "```comfyrunner\n"
            '{"models": [{"name":"m.safetensors","url":"https://huggingface.co/m","directory":"/etc/checkpoints"}]}\n'
            "```\n"
        )
        found, manifest, findings = lint_manifest_text(text)
        assert found is True
        assert manifest is not None
        errors = [f for f in findings if f.severity == "error"]
        assert any("relative" in f.message for f in errors)

    def test_model_directory_nested_traversal_rejected(self):
        text = (
            "```comfyrunner\n"
            '{"models": [{"name":"m.safetensors","url":"https://huggingface.co/m","directory":"checkpoints/../../etc"}]}\n'
            "```\n"
        )
        found, manifest, findings = lint_manifest_text(text)
        assert found is True
        assert manifest is not None
        errors = [f for f in findings if f.severity == "error"]
        assert any(
            "relative" in f.message and f.path == "models[0].directory"
            for f in errors
        )


# ===========================================================================
# lint_manifest_json (used for explicit raw-JSON sources, e.g. .json files)
# ===========================================================================

class TestLintManifestJson:
    def test_valid_raw_json_accepted(self):
        text = json.dumps({
            "workflows": ["https://raw.githubusercontent.com/o/r/main/w.json"],
            "models": [],
        })
        found, manifest, findings = lint_manifest_json(text)
        assert found is True
        assert manifest is not None
        assert [f for f in findings if f.severity == "error"] == []

    def test_malformed_json_is_error(self):
        found, manifest, findings = lint_manifest_json("{ not json")
        assert found is True
        assert manifest is None
        errors = [f for f in findings if f.severity == "error"]
        assert errors
        assert any("malformed" in f.message for f in errors)

    def test_bad_schema_is_error(self):
        text = json.dumps({"workflows": "not a list"})
        found, manifest, findings = lint_manifest_json(text)
        assert found is True
        assert manifest is None
        assert any(f.severity == "error" for f in findings)


# ===========================================================================
# _resolve_source
# ===========================================================================

class TestResolveSource:
    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            _resolve_source("")

    def test_local_file_path(self, tmp_path):
        p = tmp_path / "manifest.md"
        p.write_text("hello", encoding="utf-8")
        resolved = _resolve_source(str(p))
        assert resolved.text == "hello"
        assert resolved.label == str(p)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="not found"):
            _resolve_source(str(tmp_path / "nope.txt"))

    def test_binary_file_raises_runtime_error(self, tmp_path):
        # Bytes that are not valid UTF-8 (lone continuation bytes).
        p = tmp_path / "binary.bin"
        p.write_bytes(b"\xff\xfe\xfd\x00\x80")
        with pytest.raises(RuntimeError, match="UTF-8"):
            _resolve_source(str(p))

    def test_shorthand_calls_fetch_pr_body(self):
        with patch(
            "comfy_runner.review_authoring.fetch_pr_body",
            return_value="PR body contents",
        ) as mock_fetch:
            resolved = _resolve_source("comfyanonymous/ComfyUI#1234")
        mock_fetch.assert_called_once_with(
            "comfyanonymous", "ComfyUI", 1234, github_token=None,
        )
        assert resolved.text == "PR body contents"
        assert "comfyanonymous/ComfyUI#1234" in resolved.label

    def test_pr_url_calls_fetch_pr_body(self):
        with patch(
            "comfy_runner.review_authoring.fetch_pr_body",
            return_value="body",
        ) as mock_fetch:
            resolved = _resolve_source(
                "https://github.com/comfyanonymous/ComfyUI/pull/777",
            )
        mock_fetch.assert_called_once_with(
            "comfyanonymous", "ComfyUI", 777, github_token=None,
        )
        assert "comfyanonymous/ComfyUI#777" in resolved.label
        assert resolved.text == "body"

    def test_pr_url_with_trailing_query(self):
        with patch(
            "comfy_runner.review_authoring.fetch_pr_body",
            return_value="body",
        ) as mock_fetch:
            _resolve_source(
                "https://github.com/o/r/pull/42/files?diff=split",
            )
        mock_fetch.assert_called_once_with("o", "r", 42, github_token=None)

    def test_github_token_threaded_through(self):
        with patch(
            "comfy_runner.review_authoring.fetch_pr_body",
            return_value="body",
        ) as mock_fetch:
            _resolve_source("o/r#1", github_token="tok")
        mock_fetch.assert_called_once_with("o", "r", 1, github_token="tok")


# ===========================================================================
# lint_manifest_source (integration-ish)
# ===========================================================================

class TestLintManifestSource:
    def test_file_with_valid_block(self, tmp_path):
        body = (
            "Description\n\n```comfyrunner\n"
            '{"workflows":["https://raw.githubusercontent.com/o/r/main/w.json"],"models":[]}\n'
            "```\n"
        )
        p = tmp_path / "PR_BODY.md"
        p.write_text(body, encoding="utf-8")

        result = lint_manifest_source(str(p))
        assert result.found_block is True
        assert result.manifest is not None
        assert result.ok is True

    def test_file_missing(self, tmp_path):
        result = lint_manifest_source(str(tmp_path / "nope"))
        assert result.found_block is False
        assert result.manifest is None
        assert result.ok is False
        assert any("not found" in f.message for f in result.findings)

    def test_pr_shorthand_routes_through_fetch(self):
        body = (
            "```comfyrunner\n"
            '{"workflows":["https://raw.githubusercontent.com/o/r/main/w.json"],"models":[]}\n'
            "```"
        )
        with patch(
            "comfy_runner.review_authoring.fetch_pr_body",
            return_value=body,
        ):
            result = lint_manifest_source("o/r#5")

        assert result.found_block is True
        assert result.ok is True
        assert "o/r#5" in result.source

    def test_fetch_failure_surfaced_as_error(self):
        with patch(
            "comfy_runner.review_authoring.fetch_pr_body",
            side_effect=RuntimeError("boom"),
        ):
            result = lint_manifest_source("o/r#5")

        assert result.found_block is False
        assert result.ok is False
        assert any("boom" in f.message for f in result.findings)

    def test_json_file_uses_raw_json_path(self, tmp_path):
        # A ``.json`` file is treated as a raw manifest body, NOT
        # scanned for a fenced block.
        p = tmp_path / "manifest.json"
        p.write_text(json.dumps({
            "workflows": ["https://raw.githubusercontent.com/o/r/main/w.json"],
            "models": [],
        }), encoding="utf-8")

        result = lint_manifest_source(str(p))

        assert result.found_block is True
        assert result.manifest is not None
        assert result.ok is True

    def test_json_file_with_bad_schema_reports_error(self, tmp_path):
        p = tmp_path / "manifest.json"
        p.write_text(json.dumps({"workflows": "not a list"}), encoding="utf-8")

        result = lint_manifest_source(str(p))

        assert result.found_block is True
        assert result.ok is False
        assert any(f.severity == "error" for f in result.findings)

    def test_binary_file_surfaced_as_error_finding(self, tmp_path):
        p = tmp_path / "garbage.md"
        p.write_bytes(b"\xff\xfe\xfd")

        result = lint_manifest_source(str(p))

        assert result.found_block is False
        assert result.ok is False
        assert any("UTF-8" in f.message for f in result.findings)

    def test_pr_body_starting_with_brace_not_an_error(self):
        # Regression: PR descriptions that begin with a JSON snippet
        # (or any text starting with ``{``) must not be penalized with
        # an error finding just because they resemble a raw manifest.
        body = '{"some": "unrelated json snippet"}\n\nNo manifest block here.'
        with patch(
            "comfy_runner.review_authoring.fetch_pr_body",
            return_value=body,
        ):
            result = lint_manifest_source("o/r#7")

        assert result.found_block is False
        assert result.ok is False  # no block was found
        assert [f for f in result.findings if f.severity == "error"] == []
        assert any(f.severity == "info" for f in result.findings)


# ===========================================================================
# CLI integration: cmd_review_init / cmd_review_validate
# ===========================================================================

import argparse  # noqa: E402

from comfy_runner_cli.cli import (  # noqa: E402
    cmd_review_init,
    cmd_review_validate,
)


def _init_args(workflow: str, **overrides) -> argparse.Namespace:
    defaults = {
        "workflow": workflow,
        "workflow_url": None,
        "json": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _validate_args(source: str, **overrides) -> argparse.Namespace:
    defaults = {
        "source": source,
        "github_token": None,
        "json": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestCmdReviewInit:
    def test_json_output_round_trips(self, tmp_path, capsys):
        wf = _write_workflow(tmp_path, {"nodes": [_node([{
            "name": "m.safetensors",
            "url": "https://huggingface.co/m",
            "directory": "checkpoints",
        }])]})
        args = _init_args(
            str(wf),
            workflow_url="https://raw.githubusercontent.com/o/r/main/wf.json",
            json=True,
        )
        cmd_review_init(args)
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["ok"] is True
        assert payload["manifest"]["models"][0]["name"] == "m.safetensors"
        assert payload["block"].startswith("```comfyrunner")

    def test_missing_file_exits_1(self, tmp_path, capsys):
        args = _init_args(str(tmp_path / "missing.json"), json=True)
        with pytest.raises(SystemExit) as exc:
            cmd_review_init(args)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["ok"] is False

    def test_text_output_includes_block(self, tmp_path, capsys):
        wf = _write_workflow(tmp_path, {"nodes": [_node([{
            "name": "m.safetensors",
            "url": "https://huggingface.co/m",
            "directory": "checkpoints",
        }])]})
        args = _init_args(
            str(wf),
            workflow_url="https://raw.githubusercontent.com/o/r/main/wf.json",
        )
        cmd_review_init(args)
        captured = capsys.readouterr()
        # The block itself is printed via plain `print` so it lands on stdout.
        assert "```comfyrunner" in captured.out


class TestCmdReviewValidate:
    def test_valid_block_exits_0(self, tmp_path, capsys):
        body = (
            "```comfyrunner\n"
            '{"workflows":["https://raw.githubusercontent.com/o/r/main/w.json"],"models":[]}\n'
            "```\n"
        )
        p = tmp_path / "PR.md"
        p.write_text(body, encoding="utf-8")
        args = _validate_args(str(p), json=True)
        with pytest.raises(SystemExit) as exc:
            cmd_review_validate(args)
        assert exc.value.code == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["ok"] is True
        assert payload["found_block"] is True

    def test_bad_json_exits_1(self, tmp_path, capsys):
        body = "```comfyrunner\n{not json}\n```\n"
        p = tmp_path / "PR.md"
        p.write_text(body, encoding="utf-8")
        args = _validate_args(str(p), json=True)
        with pytest.raises(SystemExit) as exc:
            cmd_review_validate(args)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["ok"] is False
        assert any(f["severity"] == "error" for f in payload["findings"])

    def test_no_block_exits_1(self, tmp_path):
        p = tmp_path / "PR.md"
        p.write_text("just prose", encoding="utf-8")
        args = _validate_args(str(p), json=True)
        with pytest.raises(SystemExit) as exc:
            cmd_review_validate(args)
        assert exc.value.code == 1

    def test_pr_shorthand_routed(self, capsys):
        body = (
            "```comfyrunner\n"
            '{"workflows":["https://raw.githubusercontent.com/o/r/main/w.json"],"models":[]}\n'
            "```"
        )
        with patch(
            "comfy_runner.review_authoring.fetch_pr_body",
            return_value=body,
        ):
            args = _validate_args("o/r#5", json=True)
            with pytest.raises(SystemExit) as exc:
                cmd_review_validate(args)
        assert exc.value.code == 0
