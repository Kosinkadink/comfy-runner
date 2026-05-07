#!/usr/bin/env python3
"""
backport_release.py — automate a ComfyUI backport release.

OVERVIEW
========
A "backport release" is a patch (or minor) version cut by cherry-picking a
small set of commits from `master` onto a branch based on the previous
stable release. This script automates the mechanical parts of that process
for any ComfyUI-style repo with `pyproject.toml` + `comfyui_version.py`.

The script is **non-destructive by default**: it creates a fresh local
branch, never rewrites history, and only pushes when you opt in via
`--push`.

WHAT IT DOES
============
1. Picks the previous stable tag.
   - For a patch bump (X.Y.Z, Z > 0): highest existing `vX.Y.<Z` tag.
   - For a minor bump (X.Y.0):        highest existing `vX.{Y-1}.*` tag.
   - For a major bump (X.0.0):        refused — pass `--prev-tag` explicitly.
   You can always override with `--prev-tag vX.Y.Z`.

2. Picks the base ref to branch off:
   - If `<remote>/release/<prev-tag>` exists and contains the tag, branch
     off that release branch (the canonical "build on the previous backport
     branch" case).
   - Otherwise, branch off the tag commit itself (the "tag was cut directly
     off master" case — typical for the first patch in a minor series).
   This handles both today's mixed-convention repo and a future where every
   release lives on a `release/*` branch.

3. Creates `release/v<version>` (configurable via --branch-prefix /
   --branch-name) and cherry-picks the supplied commits in order.

4. Bumps `pyproject.toml` and `comfyui_version.py` to <version> and commits
   `ComfyUI v<version>` (skip with --no-version-bump).

5. Optionally pushes per --push policy. Use `auto` to try `<remote>/<branch>`
   first and gracefully fall back to a prep branch (e.g.
   `kosinkadink/release-vX.Y.Z-prep`) when origin rejects the create due to
   branch-protection rules.

CONFLICT HANDLING
=================
Cherry-picks frequently conflict on `requirements.txt` (frontend pin drift)
or other touch-everywhere files. Two modes:

  --on-conflict abort   (clean rollback, exit non-zero)
  --on-conflict pause   (default — leave the conflict in place, save state,
                         exit 2)

When paused, the script writes `.git/backport_release_state.json` and
prints:
  - the branch name + the conflicted commit
  - the explicit list of conflicted files (from `git diff -U`)
  - exact commands to resolve and continue

After you resolve the conflict and run `git cherry-pick --continue`, rerun
the script with `--resume` and it picks up at the next commit, finishes
the bump, and pushes.

To throw away an in-progress backport entirely, use `--abort` — it cleans
up the cherry-pick, deletes the local branch, and removes the state file.

USAGE
=====
1) Dry-run (plan only, no changes):
       python backport_release.py --repo ../ComfyUI --version 0.20.3 \\
           c55ff852 6917bce1 1b25f128 25757a53 c945a433 --dry-run

2) Run end-to-end, pause on conflict, then resume:
       python backport_release.py --repo ../ComfyUI --version 0.20.3 \\
           c55ff852 6917bce1 1b25f128 25757a53 c945a433 \\
           --on-conflict pause
       # ...resolve files, `git add`, `git cherry-pick --continue`...
       python backport_release.py --repo ../ComfyUI --resume

3) Run + push, falling back to a prep branch if origin rules block the
   canonical create:
       python backport_release.py --repo ../ComfyUI --version 0.20.3 \\
           --commits-file commits.txt --push auto

4) Run cherry-picks only (skip the version-bump commit so a human can do
   it locally without trailer pollution from agentic tooling):
       python backport_release.py --repo ../ComfyUI --version 0.20.3 \\
           <commits...> --no-version-bump --push auto

NOTES
=====
- Requires a clean working tree and no in-progress cherry-pick. Run
  `git fetch --all --tags` beforehand so the previous tag and release
  branches are available locally.
- All git operations happen inside the `--repo` directory; this script
  itself can live anywhere.
- The state file lives at `.git/backport_release_state.json`. Delete it
  (or use --abort) if you want to start over.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
STATE_FILENAME = "backport_release_state.json"

# Substrings GitHub returns when a push is rejected by a branch-protection
# rule that forbids creating new refs (rather than e.g. a non-fast-forward).
# Used by `--push auto` to decide whether to fall back to a prep branch.
PUSH_RULE_REJECTION_HINTS = (
    "creations being restricted",
    "repository rule violations",
    "GH013",
)


# ---------- helpers ----------------------------------------------------------
# Thin wrappers around git so every call goes through one place that handles
# cwd, capture, and error formatting consistently.


class BackportError(RuntimeError):
    """User-facing failure (printed without traceback)."""


@dataclass
class State:
    """Persisted between invocations to support --resume."""

    version: str
    branch: str
    base_ref: str
    base_kind: str  # "release-branch" or "tag"
    remaining_commits: list[str] = field(default_factory=list)
    completed_commits: list[str] = field(default_factory=list)
    bump_done: bool = False
    on_conflict: str = "pause"
    push: str = "none"
    fallback_prefix: str = "kosinkadink/"
    fallback_suffix: str = "-prep"
    remote: str = "origin"
    no_version_bump: bool = False


def run(
    cmd: list[str],
    cwd: Path,
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run a git/shell command. Returns CompletedProcess."""
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=capture,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        raise BackportError(
            f"Command failed ({proc.returncode}): {shlex.join(cmd)}\n{out.strip()}"
        )
    return proc


def git(repo: Path, *args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return run(["git", *args], repo, check=check, capture=capture)


def git_ok(repo: Path, *args: str) -> bool:
    return git(repo, *args, check=False).returncode == 0


def state_path(repo: Path) -> Path:
    return repo / ".git" / STATE_FILENAME


def load_state(repo: Path) -> Optional[State]:
    p = state_path(repo)
    if not p.exists():
        return None
    return State(**json.loads(p.read_text()))


def save_state(repo: Path, state: State) -> None:
    state_path(repo).write_text(json.dumps(asdict(state), indent=2) + "\n")


def clear_state(repo: Path) -> None:
    p = state_path(repo)
    if p.exists():
        p.unlink()


def working_tree_clean(repo: Path) -> bool:
    return not git(repo, "status", "--porcelain").stdout.strip()


def cherry_pick_in_progress(repo: Path) -> bool:
    return (repo / ".git" / "CHERRY_PICK_HEAD").exists()


def parse_version(s: str) -> str:
    s = s.lstrip("v")
    if not VERSION_RE.match(s):
        raise BackportError(f"Invalid version: {s!r} (expected X.Y.Z)")
    return s


def _highest_patch_tag(repo: Path, major: int, minor: int, max_patch: Optional[int] = None) -> Optional[str]:
    """Highest existing vMAJOR.MINOR.* tag, optionally below max_patch."""
    tags = git(repo, "tag", "--list", f"v{major}.{minor}.*").stdout.splitlines()
    candidates: list[tuple[int, str]] = []
    for tag in tags:
        m = re.match(rf"^v{major}\.{minor}\.(\d+)$", tag.strip())
        if not m:
            continue
        patch = int(m.group(1))
        if max_patch is not None and patch >= max_patch:
            continue
        candidates.append((patch, tag.strip()))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def detect_prev_tag(repo: Path, version: str) -> str:
    """
    Detect the previous stable tag.

    - For a patch bump (X.Y.Z, Z > 0): highest vX.Y.<Z tag.
    - For a minor bump (X.Y.0): highest vX.{Y-1}.* tag.
    - For a major bump (X.0.0): refused — pass --prev-tag explicitly.
    """
    major, minor, patch = (int(x) for x in version.split("."))
    if patch > 0:
        tag = _highest_patch_tag(repo, major, minor, max_patch=patch)
        if tag is None:
            raise BackportError(
                f"No previous tag found matching v{major}.{minor}.<{patch}. "
                "Pass --prev-tag explicitly."
            )
        return tag

    # patch == 0 — minor or major bump.
    if minor == 0:
        raise BackportError(
            f"Cannot auto-detect previous tag for {version} (major bump). "
            "Pass --prev-tag explicitly."
        )
    tag = _highest_patch_tag(repo, major, minor - 1)
    if tag is None:
        raise BackportError(
            f"No previous tag found matching v{major}.{minor - 1}.*. "
            "Pass --prev-tag explicitly."
        )
    return tag


def determine_base(repo: Path, prev_tag: str, remote: str) -> tuple[str, str]:
    """
    Decide the base ref for the new release branch.

    Returns (base_ref, kind) where kind is 'release-branch' or 'tag'.
    """
    # Look for a release branch on the remote whose tip contains the prev tag.
    candidate = f"{remote}/release/{prev_tag}"
    branches = git(repo, "branch", "-r", "--contains", prev_tag).stdout.splitlines()
    branches = [b.strip() for b in branches]
    for b in branches:
        if b == candidate:
            return candidate, "release-branch"
    # Fall back to the tag itself.
    return prev_tag, "tag"


def commit_subject(repo: Path, sha: str) -> str:
    return git(repo, "log", "-1", "--format=%h %s", sha).stdout.strip()


def resolve_commits(commits: list[str], repo: Path) -> list[str]:
    """Validate commits exist; return resolved short hashes in input order."""
    out = []
    for c in commits:
        c = c.strip()
        if not c or c.startswith("#"):
            continue
        proc = git(repo, "rev-parse", "--verify", c, check=False)
        if proc.returncode != 0:
            raise BackportError(f"Commit not found in repo: {c}")
        out.append(proc.stdout.strip())
    if not out:
        raise BackportError("No commits provided to cherry-pick.")
    return out


def read_commits_file(path: Path) -> list[str]:
    if not path.exists():
        raise BackportError(f"Commits file not found: {path}")
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def bump_version_files(repo: Path, version: str) -> None:
    pyproject = repo / "pyproject.toml"
    version_file = repo / "comfyui_version.py"
    if not pyproject.exists():
        raise BackportError(f"Missing {pyproject}")
    if not version_file.exists():
        raise BackportError(f"Missing {version_file}")

    # pyproject.toml — replace the first `version = "..."` line under [project].
    text = pyproject.read_text()
    new_text, n = re.subn(
        r'(?m)^version\s*=\s*"[^"]+"',
        f'version = "{version}"',
        text,
        count=1,
    )
    if n != 1:
        raise BackportError(f"Failed to bump version in {pyproject}")
    pyproject.write_text(new_text)

    # comfyui_version.py — replace __version__ = "..."
    vtext = version_file.read_text()
    new_vtext, n = re.subn(
        r'__version__\s*=\s*"[^"]+"',
        f'__version__ = "{version}"',
        vtext,
        count=1,
    )
    if n != 1:
        raise BackportError(f"Failed to bump version in {version_file}")
    version_file.write_text(new_vtext)


# ---------- main flow --------------------------------------------------------


def cmd_abort(repo: Path) -> int:
    state = load_state(repo)
    if not state:
        print("No backport in progress (no state file).", file=sys.stderr)
        return 1
    if cherry_pick_in_progress(repo):
        git(repo, "cherry-pick", "--abort", check=False)
    # Move off the branch before deleting it.
    current = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current == state.branch:
        # Best-effort: jump to base ref.
        git(repo, "checkout", state.base_ref, check=False)
    git(repo, "branch", "-D", state.branch, check=False)
    clear_state(repo)
    print(f"Aborted backport: deleted branch {state.branch} and cleared state.")
    return 0


def attempt_push(repo: Path, state: State) -> int:
    """Push per state.push policy. Returns 0 on success or skip."""
    if state.push == "none":
        print(f"Skipping push (--push none). Branch {state.branch!r} ready locally.")
        return 0

    canonical_remote_branch = state.branch  # e.g. release/v0.20.3
    fallback_branch = (
        f"{state.fallback_prefix}{state.branch.replace('/', '-')}{state.fallback_suffix}"
    )

    def push_to(remote_ref: str) -> tuple[int, str]:
        proc = git(
            repo,
            "push",
            "-u",
            state.remote,
            f"{state.branch}:{remote_ref}",
            check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    if state.push in ("origin", "auto"):
        print(f"Pushing {state.branch} -> {state.remote}/{canonical_remote_branch} ...")
        rc, out = push_to(canonical_remote_branch)
        print(out.strip())
        if rc == 0:
            return 0
        if state.push == "origin":
            raise BackportError(
                f"Push to {state.remote}/{canonical_remote_branch} failed."
            )
        # auto: detect rule rejection and fall through to fallback
        if not any(h in out for h in PUSH_RULE_REJECTION_HINTS):
            raise BackportError(
                f"Push failed (not a rule rejection); aborting auto-fallback.\n{out}"
            )
        print("Branch creation blocked by repo rules; falling back to prep branch.")

    # fallback or auto-after-rejection
    print(f"Pushing {state.branch} -> {state.remote}/{fallback_branch} ...")
    rc, out = push_to(fallback_branch)
    print(out.strip())
    if rc != 0:
        raise BackportError(f"Fallback push failed.\n{out}")
    print(
        f"\nNote: pushed to fallback ref {fallback_branch!r}. A maintainer with "
        f"create-branch permission can promote it via:\n"
        f"    git push {state.remote} {state.remote}/{fallback_branch}:{canonical_remote_branch}"
    )
    return 0


def do_run(repo: Path, state: State, *, dry_run: bool) -> int:
    """
    Drive the cherry-pick loop, version bump, and push for an in-progress
    backport. Reentrant via --resume: it always restarts from
    state.remaining_commits[0].
    """
    print(
        f"Backport plan:\n"
        f"  repo:        {repo}\n"
        f"  version:     {state.version}\n"
        f"  base ({state.base_kind}): {state.base_ref}\n"
        f"  new branch:  {state.branch}\n"
        f"  cherry-pick: {len(state.remaining_commits) + len(state.completed_commits)} commit(s)"
    )
    for sha in state.completed_commits:
        print(f"    [done] {commit_subject(repo, sha)}")
    for sha in state.remaining_commits:
        print(f"    [todo] {commit_subject(repo, sha)}")
    print(f"  push:        {state.push}")
    if dry_run:
        print("\nDry run; no changes made.")
        return 0

    # Cherry-pick remaining commits one by one.
    while state.remaining_commits:
        sha = state.remaining_commits[0]
        subj = commit_subject(repo, sha)
        print(f"\nCherry-picking {subj}")
        proc = git(repo, "cherry-pick", sha, check=False, capture=True)
        sys.stdout.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
        if proc.returncode != 0:
            if state.on_conflict == "abort":
                git(repo, "cherry-pick", "--abort", check=False)
                raise BackportError(
                    f"Conflict applying {sha} ({subj}); aborted (--on-conflict abort)."
                )
            # pause: persist state and exit so the user can resolve.
            save_state(repo, state)
            # Explicitly list conflicted files so the caller (human or agent)
            # doesn't have to grep cherry-pick's output for them.
            conflicts_proc = git(
                repo, "diff", "--name-only", "--diff-filter=U", check=False
            )
            conflicted = [
                ln.strip()
                for ln in (conflicts_proc.stdout or "").splitlines()
                if ln.strip()
            ]
            print(
                "\nCherry-pick conflict — paused.\n"
                f"  Branch: {state.branch}\n"
                f"  Commit: {sha} ({subj})"
            )
            if conflicted:
                print("  Conflicted files:")
                for f in conflicted:
                    print(f"    - {f}")
            else:
                print("  Conflicted files: (none reported by `git diff -U`)")
            print(
                "\nResolve the conflicts, then:\n"
                "    git add <files>\n"
                "    git cherry-pick --continue\n"
                f"    python {Path(__file__).name} --repo {repo} --resume\n"
                f"Or to abort entirely:\n"
                f"    python {Path(__file__).name} --repo {repo} --abort\n"
            )
            return 2
        state.completed_commits.append(state.remaining_commits.pop(0))
        save_state(repo, state)

    # Version bump commit.
    if not state.no_version_bump and not state.bump_done:
        print(f"\nBumping version to {state.version} ...")
        bump_version_files(repo, state.version)
        git(repo, "add", "pyproject.toml", "comfyui_version.py")
        git(repo, "commit", "-m", f"ComfyUI v{state.version}")
        state.bump_done = True
        save_state(repo, state)

    head = git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    print(f"\nBranch {state.branch} ready at {head}.")

    rc = attempt_push(repo, state)
    if rc == 0:
        clear_state(repo)
    return rc


def cmd_resume(repo: Path) -> int:
    state = load_state(repo)
    if not state:
        raise BackportError("No backport in progress (no state file).")
    current = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current != state.branch:
        raise BackportError(
            f"Expected to be on {state.branch} but HEAD is {current}. "
            f"Run: git checkout {state.branch}"
        )
    if cherry_pick_in_progress(repo):
        raise BackportError(
            "A cherry-pick is still in progress. "
            "Resolve conflicts, `git add` the files, then run "
            "`git cherry-pick --continue` before resuming."
        )
    # Move the freshly-resumed commit from remaining -> completed.
    if state.remaining_commits:
        state.completed_commits.append(state.remaining_commits.pop(0))
        save_state(repo, state)
    return do_run(repo, state, dry_run=False)


def cmd_start(repo: Path, args: argparse.Namespace) -> int:
    if not working_tree_clean(repo):
        raise BackportError("Working tree is not clean. Commit or stash first.")
    if cherry_pick_in_progress(repo):
        raise BackportError(
            "A cherry-pick is in progress. Resolve or abort it first."
        )

    if load_state(repo):
        raise BackportError(
            "An in-progress backport already exists. "
            "Resume with --resume, or abort with --abort."
        )

    version = parse_version(args.version)
    prev_tag = args.prev_tag or detect_prev_tag(repo, version)
    if not TAG_RE.match(prev_tag):
        raise BackportError(f"Invalid --prev-tag: {prev_tag!r}")
    if not git_ok(repo, "rev-parse", "--verify", prev_tag):
        raise BackportError(f"Tag {prev_tag} not found locally. Run `git fetch --tags`.")

    base_ref, base_kind = determine_base(repo, prev_tag, args.remote)
    branch = args.branch_name or f"{args.branch_prefix}v{version}"

    if git_ok(repo, "rev-parse", "--verify", branch):
        raise BackportError(f"Local branch {branch!r} already exists.")
    if git_ok(repo, "rev-parse", "--verify", f"{args.remote}/{branch}"):
        raise BackportError(
            f"Remote branch {args.remote}/{branch} already exists. "
            "Pick a different version or branch name."
        )

    # Collect commits.
    commits = list(args.commits)
    if args.commits_file:
        commits.extend(read_commits_file(Path(args.commits_file)))
    resolved = resolve_commits(commits, repo)

    state = State(
        version=version,
        branch=branch,
        base_ref=base_ref,
        base_kind=base_kind,
        remaining_commits=resolved,
        on_conflict=args.on_conflict,
        push=args.push,
        fallback_prefix=args.fallback_prefix,
        fallback_suffix=args.fallback_suffix,
        remote=args.remote,
        no_version_bump=args.no_version_bump,
    )

    if args.dry_run:
        return do_run(repo, state, dry_run=True)

    # Create branch.
    print(f"Creating branch {branch} from {base_ref} ({base_kind}) ...")
    git(repo, "checkout", "-b", branch, base_ref)
    save_state(repo, state)
    return do_run(repo, state, dry_run=False)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--repo", required=True, type=Path, help="Path to ComfyUI git repo.")
    p.add_argument("--version", help="Target version (e.g. 0.20.3).")
    p.add_argument(
        "--prev-tag",
        help="Previous stable tag (e.g. v0.20.2). Auto-detected if omitted.",
    )
    p.add_argument(
        "commits",
        nargs="*",
        help="Commits to cherry-pick (in master chronological order).",
    )
    p.add_argument(
        "--commits-file",
        help="File with one commit SHA per line (# comments allowed).",
    )
    p.add_argument("--branch-prefix", default="release/", help="Default: release/")
    p.add_argument(
        "--branch-name",
        help="Override full branch name (otherwise <prefix>v<version>).",
    )
    p.add_argument(
        "--on-conflict",
        choices=["pause", "abort"],
        default="pause",
        help=(
            "pause: stop on conflict, save state, let user resolve and --resume. "
            "abort: rollback the cherry-pick and exit."
        ),
    )
    p.add_argument(
        "--push",
        choices=["none", "origin", "fallback", "auto"],
        default="none",
        help=(
            "none: don't push. origin: push to <remote>/<branch>. "
            "fallback: push to <remote>/<fallback-prefix><branch-with-dashes><fallback-suffix>. "
            "auto: try origin, fall back if rejected by branch-protection rules."
        ),
    )
    p.add_argument("--remote", default="origin")
    p.add_argument("--fallback-prefix", default="kosinkadink/")
    p.add_argument("--fallback-suffix", default="-prep")
    p.add_argument(
        "--no-version-bump",
        action="store_true",
        help="Skip the pyproject.toml + comfyui_version.py bump commit.",
    )
    p.add_argument("--dry-run", action="store_true", help="Plan only; make no changes.")
    p.add_argument("--resume", action="store_true", help="Resume a paused backport.")
    p.add_argument(
        "--abort",
        action="store_true",
        help="Abort an in-progress backport (deletes the local branch).",
    )

    args = p.parse_args(argv)
    repo: Path = args.repo.resolve()
    if not (repo / ".git").exists():
        print(f"Not a git repo: {repo}", file=sys.stderr)
        return 1

    try:
        if args.abort:
            return cmd_abort(repo)
        if args.resume:
            return cmd_resume(repo)
        if not args.version:
            print("--version is required (or use --resume / --abort).", file=sys.stderr)
            return 1
        return cmd_start(repo, args)
    except BackportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
