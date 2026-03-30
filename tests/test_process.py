"""Tests for comfy_runner.process — pidfile, port, command building, redaction."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from comfy_runner.process import (
    DEFAULT_PORT,
    _parse_port_from_args,
    _pidfile_path,
    _read_pidfile,
    _redact_cmd_line,
    _remove_pidfile,
    _write_pidfile,
    build_launch_command,
    find_available_port,
    is_port_in_use,
    is_process_alive,
    read_port_lock,
    remove_port_lock,
    write_port_lock,
)


# ---------------------------------------------------------------------------
# Pidfile helpers
# ---------------------------------------------------------------------------

class TestPidfile:
    def test_pidfile_path(self, tmp_path: Path):
        assert _pidfile_path(tmp_path) == tmp_path / ".comfy-runner.pid"

    def test_roundtrip(self, tmp_path: Path):
        _write_pidfile(tmp_path, pid=1234, port=8188)
        data = _read_pidfile(tmp_path)
        assert data is not None
        assert data["pid"] == 1234
        assert data["port"] == 8188
        assert "started_at" in data

    def test_read_missing(self, tmp_path: Path):
        assert _read_pidfile(tmp_path) is None

    def test_remove(self, tmp_path: Path):
        _write_pidfile(tmp_path, pid=1, port=2)
        _remove_pidfile(tmp_path)
        assert _read_pidfile(tmp_path) is None



# ---------------------------------------------------------------------------
# is_process_alive
# ---------------------------------------------------------------------------

class TestIsProcessAlive:
    def test_current_process(self):
        assert is_process_alive(os.getpid()) is True

    def test_nonexistent(self):
        # PID 2**22 is almost certainly unused
        assert is_process_alive(4_194_304) is False


# ---------------------------------------------------------------------------
# Port helpers
# ---------------------------------------------------------------------------

class TestPortInUse:
    def test_random_high_port_not_in_use(self):
        assert is_port_in_use(59123) is False


# ---------------------------------------------------------------------------
# Port lock files
# ---------------------------------------------------------------------------

class TestPortLock:
    def test_roundtrip(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "comfy_runner.process._port_lock_dir", lambda: tmp_path
        )
        # write_port_lock uses the current process PID so read_port_lock
        # considers it alive and returns data instead of cleaning up.
        write_port_lock(9999, os.getpid(), "test-install")
        data = read_port_lock(9999)
        assert data is not None
        assert data["pid"] == os.getpid()
        assert data["installationName"] == "test-install"

        remove_port_lock(9999)
        # After removal, file is gone → returns None
        assert read_port_lock(9999) is None

    def test_read_missing(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "comfy_runner.process._port_lock_dir", lambda: tmp_path
        )
        assert read_port_lock(11111) is None


# ---------------------------------------------------------------------------
# find_available_port
# ---------------------------------------------------------------------------

class TestFindAvailablePort:
    def test_finds_next(self, monkeypatch):
        # Simulate: start_port+1 is busy, start_port+2 is free
        calls = iter([True, False])  # is_port_in_use results
        monkeypatch.setattr(
            "comfy_runner.process.is_port_in_use",
            lambda port, host="127.0.0.1": next(calls),
        )
        monkeypatch.setattr(
            "comfy_runner.process.read_port_lock",
            lambda port: None,
        )
        result = find_available_port(8000, search_range=10)
        # start_port+1 busy → start_port+2 free
        assert result == 8002

    def test_none_when_all_busy(self, monkeypatch):
        monkeypatch.setattr(
            "comfy_runner.process.is_port_in_use",
            lambda port, host="127.0.0.1": True,
        )
        result = find_available_port(8000, search_range=3)
        assert result is None


# ---------------------------------------------------------------------------
# _parse_port_from_args
# ---------------------------------------------------------------------------

class TestParsePortFromArgs:
    def test_extracts_port(self):
        assert _parse_port_from_args(["--port", "9000"]) == 9000

    def test_default(self):
        assert _parse_port_from_args(["--foo", "bar"]) == DEFAULT_PORT

    def test_port_at_end(self):
        assert _parse_port_from_args(["--port"]) == DEFAULT_PORT

    def test_empty(self):
        assert _parse_port_from_args([]) == DEFAULT_PORT


# ---------------------------------------------------------------------------
# build_launch_command — error path
# ---------------------------------------------------------------------------

class TestBuildLaunchCommand:
    def test_raises_when_python_not_found(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "comfy_runner.process.get_active_python_path",
            lambda p: None,
        )
        with pytest.raises(RuntimeError, match="Python not found"):
            build_launch_command(tmp_path)


# ---------------------------------------------------------------------------
# _redact_cmd_line
# ---------------------------------------------------------------------------

class TestRedactCmdLine:
    def test_redacts_api_key(self):
        result = _redact_cmd_line(
            "python", ["-s", "main.py", "--api-key", "SECRET123"]
        )
        assert "SECRET123" not in result
        assert '***' in result

    def test_redacts_token(self):
        result = _redact_cmd_line(
            "python", ["-s", "main.py", "--token", "tok_abc"]
        )
        assert "tok_abc" not in result
        assert '***' in result

    def test_redacts_password(self):
        result = _redact_cmd_line(
            "python", ["-s", "main.py", "--password", "pw"]
        )
        assert "pw" not in result or '"***"' in result

    def test_preserves_non_sensitive(self):
        result = _redact_cmd_line(
            "python", ["-s", "main.py", "--port", "8188"]
        )
        assert "8188" in result
        assert "***" not in result

    def test_redacts_api_key_underscore(self):
        result = _redact_cmd_line(
            "python", ["-s", "main.py", "--api_key", "SECRET"]
        )
        assert "SECRET" not in result
