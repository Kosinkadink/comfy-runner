"""ComfyUI git clone + checkout + PR deploy — mirrors standalone.ts install step."""

from __future__ import annotations

import time
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

# Runtime-generated paths inside the ComfyUI repo. Untracked changes under
# these prefixes are ignored when deciding whether the working tree is
# "dirty" — they are normal byproducts of running ComfyUI (saved styles,
# generated outputs, uploaded inputs, etc.) and should never block a deploy.
#
# Tracked changes under these prefixes are still treated as dirty, because
# silently dropping them could destroy real user edits to upstream files.
_RUNTIME_IGNORE_PREFIXES: tuple[str, ...] = (
    "styles/",
    "output/",
    "input/",
    "temp/",
    "user/",
    "models/",
)


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


def _parse_porcelain(stdout: str) -> list[tuple[str, str]]:
    """Parse ``git status --porcelain`` output into ``[(code, path), ...]``.

    The two-character status code is preserved as-is so callers can
    distinguish untracked (``??``) from tracked changes (``M ``, ``MM``,
    ``AM``, etc.). Renames (``R  old -> new``) report the destination path.
    """
    entries: list[tuple[str, str]] = []
    for line in (stdout or "").splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        rest = line[3:]
        # Renames: "R  old -> new" / "C  old -> new"
        if " -> " in rest and code[0] in ("R", "C"):
            rest = rest.split(" -> ", 1)[1]
        if rest:
            entries.append((code, rest))
    return entries


def _is_runtime_ignored(path: str) -> bool:
    """True if *path* lives under a ComfyUI runtime-generated directory."""
    norm = path.replace("\\", "/")
    return any(norm.startswith(prefix) for prefix in _RUNTIME_IGNORE_PREFIXES)


def _prepare_clean_tree(
    repo: str,
    *,
    force: bool = False,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Make the working tree safe for ``git checkout`` to overwrite.

    comfy-runner owns the install's git state, so a dirty tree must never
    block a deploy. Behavior:

    * Run ``git status --porcelain`` and split entries into:
      - **runtime**: untracked paths under :data:`_RUNTIME_IGNORE_PREFIXES`
        (``styles/``, ``output/``, etc.). These are byproducts of running
        ComfyUI and are silently left in place.
      - **consequential**: everything else — tracked modifications,
        deletions, renames, and untracked files outside the runtime dirs.
    * If only runtime entries exist, return ``{"ignored_runtime": [...]}``
      without touching the tree.
    * Otherwise:
      - ``force=False`` (default): ``git stash push -u`` the consequential
        entries with a tagged message, returning ``{"stashed_sha": ...,
        "stash_message": ..., "stashed_paths": [...]}``. The stash is
        recoverable with ``git stash list`` / ``git stash pop``.
      - ``force=True``: ``git reset --hard HEAD`` (drop tracked changes)
        followed by ``git clean -fd`` excluding the runtime allowlist
        (drop untracked files outside it). Returns
        ``{"force_cleaned_paths": [...]}``. Destructive — use only when
        the caller has accepted that local edits are forfeit.

    On any internal failure (status command, stash, reset) we return
    ``{"error": "..."}`` and let the subsequent checkout surface the real
    git error rather than masking it.
    """
    import subprocess as _sp
    from .git_utils import _NO_WINDOW, _git_env

    try:
        result = _sp.run(
            ["git", "status", "--porcelain"],
            cwd=repo, capture_output=True, text=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
    except Exception as e:
        return {"error": f"git status failed: {e}"}

    entries = _parse_porcelain(result.stdout or "")
    if not entries:
        return {}

    runtime: list[str] = []
    consequential: list[tuple[str, str]] = []
    for code, path in entries:
        if code == "??" and _is_runtime_ignored(path):
            runtime.append(path)
        else:
            consequential.append((code, path))

    if not consequential:
        if send_output and runtime:
            preview = ", ".join(runtime[:5])
            extra = "" if len(runtime) <= 5 else f" (+{len(runtime) - 5} more)"
            send_output(
                f"Ignoring {len(runtime)} runtime-generated path(s): "
                f"{preview}{extra}\n"
            )
        return {"ignored_runtime": runtime}

    paths = [p for _, p in consequential]

    if force:
        if send_output:
            preview = ", ".join(paths[:5])
            extra = "" if len(paths) <= 5 else f" (+{len(paths) - 5} more)"
            send_output(
                f"force=true — discarding {len(paths)} local change(s): "
                f"{preview}{extra}\n"
            )
        try:
            _sp.run(
                ["git", "reset", "--hard", "HEAD"],
                cwd=repo, capture_output=True, text=True, timeout=30,
                env=_git_env(), creationflags=_NO_WINDOW, check=True,
            )
            # Clean untracked files but preserve runtime dirs so the user
            # doesn't lose styles/outputs/etc. when they asked to force a
            # deploy of upstream code.
            clean_args = ["git", "clean", "-fd"]
            for prefix in _RUNTIME_IGNORE_PREFIXES:
                clean_args += ["-e", prefix.rstrip("/")]
            _sp.run(
                clean_args,
                cwd=repo, capture_output=True, text=True, timeout=30,
                env=_git_env(), creationflags=_NO_WINDOW, check=True,
            )
        except _sp.CalledProcessError as e:
            return {"error": f"force-clean failed: {(e.stderr or e.stdout or '').strip()}"}
        return {
            "force_cleaned_paths": paths,
            "ignored_runtime": runtime,
        }

    # Default path: stash so nothing is lost.
    stash_message = f"comfy-runner pre-deploy {int(time.time())}"
    if send_output:
        preview = ", ".join(paths[:5])
        extra = "" if len(paths) <= 5 else f" (+{len(paths) - 5} more)"
        send_output(
            f"Stashing {len(paths)} local change(s) before deploy: "
            f"{preview}{extra}\n"
        )
    # Use pathspecs so the stash only captures the consequential paths;
    # without this, ``git stash -u`` sweeps in every untracked file
    # including runtime artefacts (styles/, output/, ...) we explicitly
    # filtered out above.
    try:
        stash_result = _sp.run(
            ["git", "stash", "push", "-u", "-m", stash_message, "--"] + paths,
            cwd=repo, capture_output=True, text=True, timeout=30,
            env=_git_env(), creationflags=_NO_WINDOW, check=True,
        )
    except _sp.CalledProcessError as e:
        return {"error": f"git stash failed: {(e.stderr or e.stdout or '').strip()}"}

    # Resolve the stash sha for traceability. ``stash@{0}`` is the entry
    # we just created; rev-parse on it gives the commit sha that holds
    # the working-tree contents.
    sha: str | None = None
    try:
        sha_result = _sp.run(
            ["git", "rev-parse", "stash@{0}"],
            cwd=repo, capture_output=True, text=True, timeout=10,
            env=_git_env(), creationflags=_NO_WINDOW,
        )
        if sha_result.returncode == 0:
            sha = sha_result.stdout.strip() or None
    except Exception:
        sha = None

    if send_output:
        send_output(
            f"  stashed as {sha[:12] if sha else '?'} "
            f"(recover with: git stash list / git stash pop)\n"
        )

    return {
        "stashed_sha": sha,
        "stash_message": stash_message,
        "stashed_paths": paths,
        "ignored_runtime": runtime,
        "stash_output": (stash_result.stdout or "").strip(),
    }


def deploy_pr(
    install_path: str | Path,
    pr_number: int,
    repo_url: str | None = None,
    force: bool = False,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch and checkout a GitHub PR.

    Fetches into FETCH_HEAD then creates/updates the local branch with -B
    to avoid "refusing to fetch into checked-out branch" errors on re-deploy.

    If *repo_url* is provided and differs from the clone's ``origin``,
    a temporary ``deploy-pr`` remote is added and the PR is fetched from
    there. This lets review work for PRs opened on a fork without
    permanently changing the install's origin.

    If *force* is true, any non-runtime local changes are dropped via
    ``git reset --hard`` + ``git clean``; otherwise they are stashed.
    See :func:`_prepare_clean_tree` for the exact rules.

    Returns dict with: ref, previous_head, new_head, changed_files,
    pre_deploy_cleanup.
    """
    repo = str(_comfyui_dir(install_path))
    ref = f"pr-{pr_number}"
    previous_head = read_git_head(repo)

    cleanup = _prepare_clean_tree(repo, force=force, send_output=send_output)

    remote = "origin"
    if repo_url:
        # Normalise for comparison (strip .git suffix, lowercase, trailing slash)
        import subprocess as _sp
        from .git_utils import _NO_WINDOW
        try:
            origin_result = _sp.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo, capture_output=True, text=True, timeout=10,
                creationflags=_NO_WINDOW,
            )
            origin_url = origin_result.stdout.strip()
        except Exception:
            origin_url = ""

        def _norm(u: str) -> str:
            return u.rstrip("/").removesuffix(".git").lower()

        if _norm(repo_url) != _norm(origin_url):
            remote = "deploy-pr"
            if send_output:
                send_output(f"Adding remote '{remote}' -> {repo_url}\n")
            _sp.run(["git", "remote", "remove", remote], cwd=repo,
                    capture_output=True, creationflags=_NO_WINDOW)  # ignore if absent
            _sp.run(["git", "remote", "add", remote, repo_url], cwd=repo,
                    capture_output=True, creationflags=_NO_WINDOW)

    if send_output:
        send_output(
            f"Fetching PR #{pr_number}"
            + (f" from {remote}" if remote != "origin" else "")
            + "...\n"
        )

    # Fetch to FETCH_HEAD (not a named branch) to avoid conflicts with
    # the currently checked-out branch on re-deploys of the same PR.
    rc = git_fetch(repo, [remote, f"pull/{pr_number}/head"], send_output)
    if rc != 0:
        raise RuntimeError(
            f"Failed to fetch PR #{pr_number} from {remote} (exit code {rc})"
        )

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
        "pre_deploy_cleanup": cleanup,
    }


def deploy_ref(
    install_path: str | Path,
    ref: str,
    fetch_first: bool = True,
    repo_url: str | None = None,
    force: bool = False,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch and checkout a branch, tag, or commit.

    If *repo_url* is provided and differs from the clone's ``origin``,
    a temporary remote is added so branches from other GitHub repos
    (e.g. forks or entirely different projects) can be fetched.

    If *force* is true, any non-runtime local changes are dropped via
    ``git reset --hard`` + ``git clean``; otherwise they are stashed.
    See :func:`_prepare_clean_tree` for the exact rules.

    Returns dict with: ref, previous_head, new_head, changed_files,
    pre_deploy_cleanup.
    """
    repo = str(_comfyui_dir(install_path))
    previous_head = read_git_head(repo)

    cleanup = _prepare_clean_tree(repo, force=force, send_output=send_output)

    remote = "origin"
    if repo_url and fetch_first:
        # Check if repo_url differs from origin — if so, add a temp remote
        import subprocess as _sp
        from .git_utils import _git_env, _NO_WINDOW
        try:
            result = _sp.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo, capture_output=True, text=True, timeout=10,
                creationflags=_NO_WINDOW,
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
            env = _git_env()
            _sp.run(["git", "remote", "remove", remote], cwd=repo,
                    capture_output=True, creationflags=_NO_WINDOW)  # ignore if absent
            _sp.run(["git", "remote", "add", remote, repo_url], cwd=repo,
                    capture_output=True, creationflags=_NO_WINDOW)

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

    # For branches, create/update a local branch tracking the remote ref.
    # This avoids detached HEAD which breaks tools like ComfyUI-Manager
    # that expect a local branch (e.g. `repo.heads.master`).
    remote_ref = f"{remote}/{ref}"
    is_branch = git_rev_parse(repo, remote_ref) is not None

    if send_output:
        send_output(f"Checking out {ref}...\n")

    if is_branch:
        # -B creates or resets the local branch to match the remote
        rc = git_checkout(repo, ["-B", ref, remote_ref], send_output)
    else:
        # Tags, commits, etc. — detached HEAD is expected
        rc = git_checkout(repo, ref, send_output)
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
        "pre_deploy_cleanup": cleanup,
    }


def deploy_reset(
    install_path: str | Path,
    original_ref: str,
    force: bool = False,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Reset back to the installation's original release ref.

    Returns dict with: ref, previous_head, new_head, changed_files,
    pre_deploy_cleanup.
    """
    if send_output:
        send_output(f"Resetting to {original_ref}...\n")

    return deploy_ref(
        install_path, original_ref, fetch_first=True,
        force=force, send_output=send_output,
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
