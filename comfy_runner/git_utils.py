"""Git operations — mirrors ComfyUI-Launcher/src/main/lib/git.ts."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def _git_env() -> dict[str, str]:
    """Build environment for git commands.

    Disables interactive prompts so git never blocks on auth dialogs.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def is_git_available() -> bool:
    """Check whether git is on PATH."""
    try:
        subprocess.run(
            ["git", "--version"],
            capture_output=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def git_clone(
    url: str,
    dest: str,
    send_output: Callable[[str], None] | None = None,
) -> int:
    """Clone a git repo. Returns exit code."""
    proc = subprocess.Popen(
        ["git", "clone", url, dest],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_git_env(),
        creationflags=_NO_WINDOW,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if send_output:
            send_output(line)
    proc.wait()
    return proc.returncode


def git_checkout(
    repo_path: str,
    ref: str | list[str],
    send_output: Callable[[str], None] | None = None,
) -> int:
    """Checkout a ref (branch, tag, commit) in an existing repo.

    *ref* can be a single string (``"main"``) or a list of args
    (``["-B", "pr-42", "FETCH_HEAD"]``).
    """
    args = ref if isinstance(ref, list) else [ref]
    return _run_git(repo_path, ["checkout"] + args, send_output)


def git_fetch(
    repo_path: str,
    args: list[str] | None = None,
    send_output: Callable[[str], None] | None = None,
) -> int:
    """Run git fetch with optional extra args."""
    cmd = ["fetch"] + (args or [])
    return _run_git(repo_path, cmd, send_output)


def read_git_head(repo_path: str) -> str | None:
    """Read the current HEAD commit SHA without shelling out to git.

    Mirrors ComfyUI-Launcher git.ts readGitHead — resolves .git files
    (worktrees/submodules) and packed-refs.
    """
    git_dir = _resolve_git_dir(Path(repo_path))
    if git_dir is None:
        return None
    head_path = git_dir / "HEAD"
    try:
        content = head_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    # Detached HEAD — raw sha
    if not content.startswith("ref: "):
        return content or None
    # Symbolic ref — resolve it
    ref_name = content[5:]
    ref_path = git_dir / ref_name
    try:
        return ref_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    # Try packed-refs fallback
    packed_path = git_dir / "packed-refs"
    try:
        for line in packed_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[1] == ref_name:
                return parts[0]
    except OSError:
        pass
    return None


def read_git_remote_url(repo_path: str) -> str | None:
    """Read origin remote URL from .git/config.

    Credentials are stripped to avoid leaking tokens in CLI output.
    Mirrors git.ts readGitRemoteUrl + redactUrl.
    """
    git_dir = _resolve_git_dir(Path(repo_path))
    if git_dir is None:
        return None
    config_path = git_dir / "config"
    try:
        content = config_path.read_text(encoding="utf-8")
        match = re.search(
            r'\[remote "origin"\][^\[]*?url\s*=\s*(.+)', content, re.DOTALL
        )
        if not match:
            return None
        return _redact_url(match.group(1).strip())
    except OSError:
        return None


def _redact_url(url: str) -> str:
    """Strip embedded credentials from a git remote URL.

    Mirrors git.ts redactUrl: parses as URL and removes user:pass,
    falls back to regex for non-standard URLs (e.g. git@github.com:...).
    """
    from urllib.parse import urlparse, urlunparse

    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            # Rebuild without credentials
            replaced = parsed._replace(
                netloc=parsed.hostname + (f":{parsed.port}" if parsed.port else "")
            )
            return urlunparse(replaced)
        return url
    except Exception:
        # Non-standard URL — strip user:pass@ if present
        return re.sub(r"//[^/@]+@", "//", url)


def _resolve_git_dir(repo_path: Path) -> Path | None:
    """Resolve actual .git directory, handling worktrees/submodules."""
    dot_git = repo_path / ".git"
    try:
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            content = dot_git.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.startswith("gitdir:"):
                    return (repo_path / line.split(":", 1)[1].strip()).resolve()
    except OSError:
        pass
    return None


def git_fetch_and_checkout(
    repo_path: str,
    commit: str,
    send_output: Callable[[str], None] | None = None,
) -> int:
    """Fetch from origin, then checkout the target commit.

    Mirrors git.ts gitFetchAndCheckout. Returns the worst exit code.
    """
    rc = git_fetch(repo_path, ["origin"], send_output)
    if rc != 0:
        if send_output:
            send_output(f"⚠ git fetch failed (exit {rc}), trying checkout anyway...\n")
    return git_checkout(repo_path, commit, send_output)


def git_diff_name_only(
    repo_path: str,
    ref_a: str,
    ref_b: str,
) -> list[str]:
    """Return list of changed file paths between two refs."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", ref_a, ref_b],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_NO_WINDOW,
        )
        if result.returncode != 0:
            return []
        return [f for f in result.stdout.strip().splitlines() if f]
    except (subprocess.TimeoutExpired, OSError):
        return []


def git_rev_parse(
    repo_path: str,
    ref: str,
) -> str | None:
    """Resolve a ref to a full SHA. Returns None on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_NO_WINDOW,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _run_git(
    cwd: str,
    args: list[str],
    send_output: Callable[[str], None] | None = None,
) -> int:
    """Run a git command in a given directory."""
    proc = subprocess.Popen(
        ["git"] + args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_git_env(),
        creationflags=_NO_WINDOW,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if send_output:
            send_output(line)
    proc.wait()
    return proc.returncode
