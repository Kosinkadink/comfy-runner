"""ComfyUI git clone + checkout + PR deploy — mirrors standalone.ts install step."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .git_utils import (
    git_checkout,
    git_clone,
    git_diff_name_only,
    git_fetch,
    git_rev_parse,
    read_git_head,
)

COMFYUI_REPO_URL = "https://github.com/Comfy-Org/ComfyUI.git"


def clone_comfyui(
    install_path: str | Path,
    ref: str | None = None,
    send_output: Callable[[str], None] | None = None,
) -> str | None:
    """Clone ComfyUI into {install_path}/ComfyUI.

    If ref is provided (e.g. a tag from manifest.comfyui_ref),
    checks out that ref after cloning.

    Returns the HEAD commit sha, or None on failure.
    """
    install_path = Path(install_path)
    comfyui_dir = install_path / "ComfyUI"

    if comfyui_dir.exists():
        if send_output:
            send_output(f"ComfyUI already exists at {comfyui_dir}\n")
        head = read_git_head(str(comfyui_dir))
        return head

    if send_output:
        send_output(f"Cloning ComfyUI into {comfyui_dir}...\n")

    exit_code = git_clone(COMFYUI_REPO_URL, str(comfyui_dir), send_output)
    if exit_code != 0:
        raise RuntimeError(f"git clone failed with exit code {exit_code}")

    if ref:
        if send_output:
            send_output(f"Checking out {ref}...\n")
        exit_code = git_checkout(str(comfyui_dir), ref, send_output)
        if exit_code != 0:
            raise RuntimeError(f"git checkout {ref} failed with exit code {exit_code}")

    head = read_git_head(str(comfyui_dir))
    if send_output:
        send_output(f"ComfyUI HEAD: {head or 'unknown'}\n")
    return head


# ---------------------------------------------------------------------------
# Deploy helpers — PR fetch, branch/tag/commit checkout, reset
# ---------------------------------------------------------------------------

def _comfyui_dir(install_path: str | Path) -> Path:
    d = Path(install_path) / "ComfyUI"
    if not d.exists():
        raise RuntimeError(f"ComfyUI not found at {d}")
    return d


def deploy_pr(
    install_path: str | Path,
    pr_number: int,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch and checkout a GitHub PR.

    Fetches into FETCH_HEAD then creates/updates the local branch with -B
    to avoid "refusing to fetch into checked-out branch" errors on re-deploy.

    Returns dict with: ref, previous_head, new_head, changed_files.
    """
    repo = str(_comfyui_dir(install_path))
    ref = f"pr-{pr_number}"
    previous_head = read_git_head(repo)

    if send_output:
        send_output(f"Fetching PR #{pr_number}...\n")

    # Fetch to FETCH_HEAD (not a named branch) to avoid conflicts with
    # the currently checked-out branch on re-deploys of the same PR.
    rc = git_fetch(repo, ["origin", f"pull/{pr_number}/head"], send_output)
    if rc != 0:
        raise RuntimeError(f"Failed to fetch PR #{pr_number} (exit code {rc})")

    if send_output:
        send_output(f"Checking out {ref}...\n")

    # -B creates or resets the branch to FETCH_HEAD
    rc = git_checkout(repo, ["-B", ref, "FETCH_HEAD"], send_output)
    if rc != 0:
        raise RuntimeError(f"Failed to checkout {ref} (exit code {rc})")

    new_head = read_git_head(repo)
    changed = _changed_files(repo, previous_head, new_head)

    if send_output:
        send_output(f"HEAD: {(new_head or 'unknown')[:12]}\n")

    return {
        "ref": ref,
        "previous_head": previous_head,
        "new_head": new_head,
        "changed_files": changed,
    }


def deploy_ref(
    install_path: str | Path,
    ref: str,
    fetch_first: bool = True,
    repo_url: str | None = None,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch and checkout a branch, tag, or commit.

    If *repo_url* is provided and differs from the clone's ``origin``,
    a temporary remote is added so branches from other GitHub repos
    (e.g. forks or entirely different projects) can be fetched.

    Returns dict with: ref, previous_head, new_head, changed_files.
    """
    repo = str(_comfyui_dir(install_path))
    previous_head = read_git_head(repo)

    remote = "origin"
    if repo_url and fetch_first:
        # Check if repo_url differs from origin — if so, add a temp remote
        import subprocess as _sp
        _cf = _sp.CREATE_NO_WINDOW if hasattr(_sp, "CREATE_NO_WINDOW") else 0
        try:
            result = _sp.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo, capture_output=True, text=True, timeout=10,
                creationflags=_cf,
            )
            origin_url = result.stdout.strip()
        except Exception:
            origin_url = ""
        # Normalise for comparison (strip .git suffix, lowercase)
        def _norm(u: str) -> str:
            return u.rstrip("/").removesuffix(".git").lower()
        if _norm(repo_url) != _norm(origin_url):
            remote = "deploy-branch"
            if send_output:
                send_output(f"Adding remote '{remote}' -> {repo_url}\n")
            _sp.run(["git", "remote", "remove", remote], cwd=repo,
                    capture_output=True, creationflags=_cf)  # ignore if absent
            _sp.run(["git", "remote", "add", remote, repo_url], cwd=repo,
                    capture_output=True, creationflags=_cf)

    if fetch_first:
        if send_output:
            send_output(f"Fetching {ref} from {remote}...\n")
        # Fetch the specific ref — the clone may have a restricted refspec
        # that prevents a bare `git fetch <remote>` from getting all branches.
        rc = git_fetch(repo, [remote, f"refs/heads/{ref}:refs/remotes/{remote}/{ref}"], send_output)
        if rc != 0:
            # Fallback: try a plain fetch (works for tags, commits, etc.)
            if send_output:
                send_output(f"Retrying with full fetch from {remote}...\n")
            rc = git_fetch(repo, [remote], send_output)
            if rc != 0 and send_output:
                send_output("Warning: fetch failed, trying checkout anyway\n")

    # For branches, try remote/<ref> first (detached HEAD avoids local branch issues)
    target = ref
    if git_rev_parse(repo, f"{remote}/{ref}") is not None:
        target = f"{remote}/{ref}"

    if send_output:
        send_output(f"Checking out {ref}...\n")

    rc = git_checkout(repo, target, send_output)
    if rc != 0:
        raise RuntimeError(f"Failed to checkout {ref} (exit code {rc})")

    new_head = read_git_head(repo)
    changed = _changed_files(repo, previous_head, new_head)

    if send_output:
        send_output(f"HEAD: {(new_head or 'unknown')[:12]}\n")

    return {
        "ref": ref,
        "previous_head": previous_head,
        "new_head": new_head,
        "changed_files": changed,
    }


def deploy_reset(
    install_path: str | Path,
    original_ref: str,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Reset back to the installation's original release ref.

    Returns dict with: ref, previous_head, new_head, changed_files.
    """
    if send_output:
        send_output(f"Resetting to {original_ref}...\n")

    return deploy_ref(
        install_path, original_ref, fetch_first=True, send_output=send_output
    )


def _changed_files(
    repo_path: str,
    old_head: str | None,
    new_head: str | None,
) -> list[str]:
    """Get list of changed files between two commits."""
    if not old_head or not new_head or old_head == new_head:
        return []
    return git_diff_name_only(repo_path, old_head, new_head)
