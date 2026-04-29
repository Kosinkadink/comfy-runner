"""CLI entry point for comfy-runner."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows to prevent encoding errors with Unicode
# characters (Rich markup symbols, emoji, etc.) on legacy codepages like cp1252.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.table import Table

console = Console()


def _output(text: str) -> None:
    """Default send_output callback — prints to console."""
    console.print(text, end="", highlight=False)


from comfy_runner.lifecycle import maybe_tailscale_serve, maybe_tailscale_unserve, capture_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_env_args(env_list: list[str] | None) -> dict[str, str] | None:
    """Parse ['KEY=VALUE', ...] into a dict. Returns None if empty."""
    if not env_list:
        return None
    result = {}
    for item in env_list:
        if "=" not in item:
            raise ValueError(f"Invalid env format '{item}' — expected KEY=VALUE")
        key, value = item.split("=", 1)
        result[key] = value
    return result or None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> None:
    """Create a new ComfyUI installation."""
    from comfy_runner.installations import init_installation

    try:
        # Parse --torch-spec into a list if provided
        torch_spec = None
        if getattr(args, "torch_spec", None):
            torch_spec = args.torch_spec

        record = init_installation(
            name=args.name,
            variant=getattr(args, "variant", None),
            release_tag=getattr(args, "release", None),
            install_dir=getattr(args, "dir", None),
            send_output=None if args.json else _output,
            cuda_compat=getattr(args, "cuda_compat", False),
            build=getattr(args, "build", False),
            python_version=getattr(args, "python_version", None),
            pbs_release=getattr(args, "pbs_release", None),
            gpu=getattr(args, "gpu", None),
            cuda_tag=getattr(args, "cuda_tag", None),
            torch_version=getattr(args, "torch_version", None),
            torch_spec=torch_spec,
            torch_index_url=getattr(args, "torch_index_url", None),
            comfyui_ref=getattr(args, "comfyui_ref", None),
        )
        if args.json:
            print(json.dumps({"ok": True, "installation": record}, indent=2))
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> None:
    """List all installations."""
    from comfy_runner.installations import show_list
    from comfy_runner.process import get_status

    installations = show_list()

    # Enrich with live process status
    for inst in installations:
        try:
            status = get_status(inst["name"])
            inst["_running"] = status.get("running", False)
            inst["_port"] = status.get("port")
            inst["_pid"] = status.get("pid")
        except Exception:
            inst["_running"] = False

    if args.json:
        print(json.dumps(installations, indent=2))
        return

    if not installations:
        console.print("[dim]No installations found.[/dim]")
        return

    table = Table(title="ComfyUI Installations")
    table.add_column("Name", style="cyan")
    table.add_column("Variant", style="green")
    table.add_column("Release", style="yellow")
    table.add_column("Running", style="bold")
    table.add_column("Port")
    table.add_column("Path", style="dim")

    for inst in installations:
        from comfy_runner.environment import get_variant_label
        variant = inst.get("variant", "")
        label = get_variant_label(variant) if variant else ""
        running = inst.get("_running", False)
        running_str = "[green]● yes[/green]" if running else "[dim]○ no[/dim]"
        port_str = str(inst["_port"]) if running and inst.get("_port") else ""
        table.add_row(
            inst["name"],
            label,
            inst.get("release_tag", ""),
            running_str,
            port_str,
            inst.get("path", ""),
        )

    console.print(table)


def cmd_rm(args: argparse.Namespace) -> None:
    """Remove an installation."""
    from comfy_runner.installations import remove
    from comfy_runner.process import get_status

    out = None if args.json else _output
    try:
        # Stop tunnel and unserve Tailscale before removing
        try:
            status = get_status(args.name)
            if status.get("port"):
                from comfy_runner.tunnel import stop_tunnel
                try:
                    stop_tunnel(args.name, send_output=out)
                except Exception:
                    pass
                maybe_tailscale_unserve(status["port"], send_output=out)
        except Exception:
            pass
        remove(
            name=args.name,
            delete_files=not args.keep_files,
            send_output=out,
        )
        if args.json:
            print(json.dumps({"ok": True}))
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_releases(args: argparse.Namespace) -> None:
    """List available releases and their variants."""
    from comfy_runner.environment import (
        fetch_manifests,
        fetch_releases,
        get_platform_prefix,
        get_variant_label,
    )

    try:
        releases = fetch_releases(limit=args.limit)
        # Filter to releases that have manifests.json
        releases = [
            r for r in releases
            if any(a["name"] == "manifests.json" for a in r.get("assets", []))
        ]
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error fetching releases: {e}[/red]")
        sys.exit(1)

    if not releases:
        if args.json:
            print(json.dumps({"ok": True, "releases": []}, indent=2))
        else:
            console.print("[dim]No releases found.[/dim]")
        return

    prefix = get_platform_prefix()
    show_variants = args.variants

    if args.json:
        result = []
        for release in releases:
            entry: dict = {
                "tag": release["tag_name"],
                "name": release.get("name") or release["tag_name"],
            }
            if show_variants:
                try:
                    manifests = fetch_manifests(release)
                    entry["variants"] = [
                        {
                            "id": m["id"],
                            "label": get_variant_label(m["id"]),
                            "comfyui_ref": m.get("comfyui_ref", ""),
                            "python_version": m.get("python_version", ""),
                            "files": m.get("files", []),
                        }
                        for m in manifests
                        if m["id"].startswith(prefix)
                    ]
                except Exception:
                    entry["variants"] = []
            result.append(entry)
        print(json.dumps({"ok": True, "releases": result}, indent=2))
        return

    if show_variants:
        # Detailed view: one release at a time with variant table
        for release in releases:
            tag = release["tag_name"]
            name = release.get("name") or tag
            title = f"{tag}  —  {name}" if name != tag else tag

            try:
                manifests = fetch_manifests(release)
            except Exception as e:
                console.print(f"[yellow]{title}[/yellow]: [red]failed to fetch manifests: {e}[/red]")
                continue

            platform_manifests = [m for m in manifests if m["id"].startswith(prefix)]
            if not platform_manifests:
                console.print(f"[yellow]{title}[/yellow]: [dim]no variants for this platform[/dim]")
                continue

            # Resolve download sizes from release assets
            assets_by_name = {a["name"]: a for a in release.get("assets", [])}

            table = Table(title=title)
            table.add_column("Variant ID", style="cyan")
            table.add_column("Label", style="green")
            table.add_column("ComfyUI", style="yellow")
            table.add_column("Python", style="dim")
            table.add_column("Size", justify="right")

            for m in platform_manifests:
                files = m.get("files", [])
                total_size = sum(
                    assets_by_name[f]["size"]
                    for f in files
                    if f in assets_by_name
                )
                size_str = f"{total_size / 1048576:.0f} MB" if total_size else "?"

                table.add_row(
                    m["id"],
                    get_variant_label(m["id"]),
                    m.get("comfyui_ref", ""),
                    m.get("python_version", ""),
                    size_str,
                )

            console.print(table)
            console.print()
    else:
        # Summary view: just releases
        table = Table(title="Available Releases")
        table.add_column("Tag", style="cyan")
        table.add_column("Name", style="green")

        for release in releases:
            tag = release["tag_name"]
            name = release.get("name") or tag
            table.add_row(tag, name if name != tag else "")

        console.print(table)
        console.print(
            "\n[dim]Use [cyan]comfy-runner releases --variants[/cyan] "
            "to see available variants per release.[/dim]"
        )


def cmd_start(args: argparse.Namespace) -> None:
    """Start a ComfyUI installation."""
    from comfy_runner.process import start_foreground, start_installation

    name = args.name
    port = args.port
    extra = args.extra_args
    pc = args.port_conflict

    out = None if args.json else _output
    try:
        env_overrides = _parse_env_args(args.env)
        if args.background:
            result = start_installation(
                name=name,
                port_override=port,
                port_conflict=pc,
                extra_args=extra,
                send_output=out,
                env_overrides=env_overrides,
            )
            if result.get("port"):
                maybe_tailscale_serve(result["port"], send_output=out)
            if args.json:
                print(json.dumps({"ok": True, **result}, indent=2))
        else:
            if args.json:
                # Foreground + JSON doesn't mix well; use background mode
                result = start_installation(
                    name=name,
                    port_override=port,
                    port_conflict=pc,
                    extra_args=extra,
                    send_output=None,
                    env_overrides=env_overrides,
                )
                if result.get("port"):
                    maybe_tailscale_serve(result["port"])
                print(json.dumps({"ok": True, **result}, indent=2))
            else:
                start_foreground(
                    name=name,
                    port_override=port,
                    port_conflict=pc,
                    extra_args=extra,
                    send_output=_output,
                    env_overrides=env_overrides,
                )
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop a running ComfyUI installation."""
    from comfy_runner.process import get_status, stop_installation

    out = None if args.json else _output
    try:
        status = get_status(args.name)
        if status.get("port"):
            maybe_tailscale_unserve(status["port"], send_output=out)
        stop_installation(
            name=args.name,
            send_output=out,
        )
        if args.json:
            print(json.dumps({"ok": True}))
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_restart(args: argparse.Namespace) -> None:
    """Restart a running ComfyUI installation."""
    from comfy_runner.process import get_status, start_installation, stop_installation

    name = args.name
    out = None if args.json else _output
    try:
        env_overrides = _parse_env_args(args.env)
        # Unserve old port before stopping
        status = get_status(name)
        if status.get("port"):
            maybe_tailscale_unserve(status["port"], send_output=out)

        # Stop (ignore errors if not running)
        try:
            stop_installation(name=name, send_output=out)
        except RuntimeError:
            if not args.json:
                _output("(was not running)\n")

        result = start_installation(
            name=name,
            port_override=args.port,
            send_output=out,
            env_overrides=env_overrides,
        )
        if result.get("port"):
            maybe_tailscale_serve(result["port"], send_output=out)
        capture_snapshot(name, "restart", send_output=out)
        if args.json:
            print(json.dumps({"ok": True, **result}, indent=2))
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_status(args: argparse.Namespace) -> None:
    """Show running state of an installation."""
    from comfy_runner.process import get_status

    try:
        status = get_status(args.name)
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    if args.json:
        print(json.dumps({"ok": True, **status}, indent=2))
        return

    table = Table(title=f"Status: {args.name}")
    table.add_column("Field", style="cyan")
    table.add_column("Value")

    table.add_row("Status", status.get("status", ""))
    table.add_row("Running", "✓ yes" if status.get("running") else "✗ no")
    if status.get("running"):
        table.add_row("PID", str(status.get("pid", "")))
        table.add_row("Port", str(status.get("port", "")))
        table.add_row("Healthy", "✓" if status.get("healthy") else "✗")
        uptime = status.get("uptime_s")
        if uptime is not None:
            hrs, rem = divmod(int(uptime), 3600)
            mins, secs = divmod(rem, 60)
            table.add_row("Uptime", f"{hrs}h {mins}m {secs}s")

    console.print(table)


def cmd_logs(args: argparse.Namespace) -> None:
    """Show logs from a running installation."""
    from comfy_runner.process import get_log_output

    try:
        get_log_output(
            name=args.name,
            send_output=None if args.json else _output,
        )
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_deploy(args: argparse.Namespace) -> None:
    """Deploy a PR, branch, tag, commit, latest release, or pull current tracking."""
    from comfy_runner.config import get_installation, set_installation
    from comfy_runner.deployments import execute_deploy
    from comfy_runner.pip_utils import install_filtered_requirements
    from comfy_runner.process import get_status, start_installation, stop_installation

    name = args.name
    out = None if args.json else _output

    # Normalize --repo into a full HTTPS URL. Accepts owner/name shorthand
    # or any of the URL forms _parse_repo handles.
    repo_url = getattr(args, "repo_url", None)
    if repo_url and "://" not in repo_url:
        try:
            owner, repo_name = _parse_repo(repo_url)
            repo_url = f"https://github.com/{owner}/{repo_name}"
            args.repo_url = repo_url
        except ValueError as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            else:
                console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    try:
        record = get_installation(name)
        if not record:
            raise RuntimeError(f"Installation '{name}' not found.")

        install_path = record["path"]

        # Check if running — stop first if so
        status = get_status(name)
        was_running = status.get("running", False)
        running_port = status.get("port")

        if was_running:
            if running_port:
                maybe_tailscale_unserve(running_port, send_output=out)
            if out:
                out(f"Stopping '{name}' before deploy...\n")
            stop_installation(name, send_output=out)

        result, updates = execute_deploy(
            install_path, record,
            pr=getattr(args, "pr", None),
            branch=getattr(args, "branch", None),
            tag=getattr(args, "tag", None),
            commit=getattr(args, "commit", None),
            reset=getattr(args, "reset", False),
            latest=getattr(args, "latest", False),
            pull=getattr(args, "pull", False),
            repo_url=getattr(args, "repo_url", None),
            send_output=out,
        )

        # Check if requirements changed and install if so
        changed_files = result.get("changed_files", [])
        req_changed = any(
            f in ("requirements.txt", "manager_requirements.txt")
            for f in changed_files
        )

        if req_changed:
            if out:
                out("\nRequirements changed — installing dependencies...\n")
            from pathlib import Path

            req_path = Path(install_path) / "ComfyUI" / "requirements.txt"
            rc = install_filtered_requirements(
                install_path, req_path, send_output=out
            )
            if rc != 0:
                if out:
                    out(f"⚠ pip install exited with code {rc}\n")
            result["requirements_installed"] = rc == 0
        else:
            result["requirements_installed"] = False
            if out and changed_files:
                out("Requirements unchanged — skipping pip install.\n")

        # Apply record updates
        for k, v in updates.items():
            if v is None:
                record.pop(k, None)
            else:
                record[k] = v
        # Preserve repo/title from args for PR deploys
        if getattr(args, "pr", None):
            record["deployed_pr"] = args.pr
        set_installation(name, record)

        # Restart if it was running
        if was_running:
            if out:
                out(f"\nRestarting '{name}'...\n")
            start_result = start_installation(
                name,
                port_override=running_port,
                send_output=out,
            )
            result["restarted"] = True
            result["port"] = start_result.get("port")
            result["pid"] = start_result.get("pid")
            if start_result.get("port"):
                maybe_tailscale_serve(start_result["port"], send_output=out)
        else:
            result["restarted"] = False

        capture_snapshot(name, "post-update", send_output=out)

        if out:
            out(f"\n✓ Deploy complete.\n")

        if args.json:
            print(json.dumps({"ok": True, **result}, indent=2))

    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Review (PR-review preparation)
# ---------------------------------------------------------------------------

def _parse_review_target(spec: str | None) -> dict:
    """Parse a review target spec into ``{"kind": ..., ...}``.

    Formats (review-specific — distinct from station-test target specs):

    * ``local``                  — local installation named ``main``
    * ``local:<install-name>``   — named local installation
    * ``remote:<pod-name>``      — existing pod via the central station
    * ``runpod`` / ``runpod:<gpu>`` — fresh PR pod via the central station
    * ``server:<url>``           — direct against any reachable
      comfy-runner server (no station). URL must include scheme; on
      Tailscale use the full MagicDNS FQDN, e.g.
      ``server:https://mybox.tailnet.ts.net:9189``.

    ``None`` and the empty string both default to ``local``.
    """
    if spec is None or spec == "":
        return {"kind": "local", "install_name": "main"}
    if spec == "local":
        return {"kind": "local", "install_name": "main"}
    if spec.startswith("local:"):
        name = spec[len("local:") :].strip()
        if not name:
            raise ValueError("local: target requires an installation name")
        return {"kind": "local", "install_name": name}
    if spec.startswith("remote:"):
        pod = spec[len("remote:") :].strip()
        if not pod:
            raise ValueError("remote: target requires a pod name")
        return {"kind": "remote", "pod_name": pod}
    if spec == "runpod":
        return {"kind": "runpod", "gpu_type": None}
    if spec.startswith("runpod:"):
        gpu = spec[len("runpod:") :].strip()
        return {"kind": "runpod", "gpu_type": gpu or None}
    if spec.startswith("server:"):
        url = spec[len("server:") :].strip()
        if not url:
            raise ValueError("server: target requires a URL")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(
                f"server: target URL must include scheme (got {url!r}). "
                "On Tailscale, use the full MagicDNS FQDN, e.g. "
                "server:https://mybox.tailnet.ts.net:9189."
            )
        return {"kind": "server", "server_url": url.rstrip("/")}
    raise ValueError(
        f"Unknown target spec: {spec!r}. Use local, local:<install>, "
        "remote:<pod>, runpod[:<gpu>], or server:<url>."
    )


def _parse_repo(repo: str) -> tuple[str, str]:
    """Parse ``owner/name`` or full GitHub URL into ``(owner, name)``."""
    if not repo:
        raise ValueError("--repo is required (e.g. comfy-org/ComfyUI)")
    r = repo.strip()
    if "://" in r:
        r = r.split("://", 1)[1]
    if r.endswith(".git"):
        r = r[: -len(".git")]
    if r.startswith("github.com/"):
        r = r[len("github.com/") :]
    parts = [p for p in r.split("/") if p]
    if len(parts) < 2:
        raise ValueError(
            f"Could not parse --repo {repo!r}; expected 'owner/name' "
            "or a full GitHub URL"
        )
    return parts[-2], parts[-1]


def _parse_model_flag(spec: str) -> dict[str, str]:
    """Parse a ``--model NAME=URL=DIRECTORY`` CLI flag.

    Splits the *first* ``=`` for name and the *last* ``=`` for directory
    so URLs containing ``=`` (HuggingFace query strings, signed S3 URLs)
    survive intact in the middle.
    """
    name, sep, rest = spec.partition("=")
    if not sep:
        raise ValueError(
            f"--model must be 'name=url=directory' (got {spec!r})"
        )
    url, sep, directory = rest.rpartition("=")
    if not sep:
        raise ValueError(
            f"--model must be 'name=url=directory' (got {spec!r})"
        )
    name, url, directory = name.strip(), url.strip(), directory.strip()
    if not (name and url and directory):
        raise ValueError(f"--model fields cannot be empty: {spec!r}")
    return {"name": name, "url": url, "directory": directory}


def cmd_review(args: argparse.Namespace) -> None:
    """Prepare a PR for review on the chosen target.

    For ``local`` targets: deploy the PR into a named installation,
    fetch the PR's ``comfyrunner`` manifest block from GitHub, fetch
    any declared workflow URLs into ``user/default/workflows/``, and
    download missing models.

    Remote and ephemeral runpod targets land in subsequent PRs.
    """
    out = None if args.json else _output

    try:
        owner, repo_name = _parse_repo(args.repo)
        target = _parse_review_target(getattr(args, "target", None))
    except ValueError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    pr = args.pr

    extra_models: list = []
    for spec in (getattr(args, "model", None) or []):
        try:
            extra_models.append(_parse_model_flag(spec))
        except ValueError as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            else:
                console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)
    extra_workflows = list(getattr(args, "workflow", None) or [])

    from comfy_runner.manifest import ModelEntry
    extra_model_entries = [ModelEntry.from_dict(m) for m in extra_models]

    if target["kind"] == "remote":
        _cmd_review_remote(
            args, target, owner, repo_name, pr,
            extra_model_entries, extra_workflows, out,
        )
        return

    if target["kind"] == "runpod":
        _cmd_review_runpod(
            args, target, owner, repo_name, pr,
            extra_model_entries, extra_workflows, out,
        )
        return

    if target["kind"] == "server":
        _cmd_review_server(
            args, target, owner, repo_name, pr,
            extra_model_entries, extra_workflows, out,
        )
        return

    # ── local target ─────────────────────────────────────────────────────
    install_name = target["install_name"]
    target_label = f"local:{install_name}"

    if out:
        out(
            f"Preparing PR #{pr} ({owner}/{repo_name}) for review "
            f"on {target_label}...\n"
        )

    # --- Step 1: deploy the PR onto the local installation. -------------
    deploy_args = argparse.Namespace(
        name=install_name,
        pr=pr,
        branch=None,
        tag=None,
        commit=None,
        reset=False,
        latest=False,
        pull=False,
        repo_url=f"https://github.com/{owner}/{repo_name}",
        json=False,  # always stream so the user sees progress
    )
    # ``cmd_deploy`` calls ``sys.exit(1)`` on failure; let it propagate.
    if out:
        out(f"\n--- Deploying PR #{pr} to '{install_name}' ---\n")
    cmd_deploy(deploy_args)

    # --- Step 2: manifest fetch + workflow fetch + model provision. -----
    from comfy_runner.config import get_installation
    from comfy_runner.review import prepare_local_review

    record = get_installation(install_name)
    if not record:
        # Should not happen — cmd_deploy would have errored — but guard.
        msg = f"Installation '{install_name}' missing after deploy"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, indent=2))
        else:
            console.print(f"[red]Error: {msg}[/red]")
        sys.exit(1)
    install_path = record["path"]

    if out:
        out(f"\n--- Resolving manifest for PR #{pr} ---\n")

    review_result = prepare_local_review(
        install_path, owner, repo_name, pr,
        github_token=getattr(args, "github_token", None),
        download_token=getattr(args, "token", "") or "",
        extra_models=extra_model_entries,
        extra_workflows=extra_workflows,
        allow_arbitrary_urls=getattr(args, "allow_arbitrary_urls", False),
        skip_provisioning=getattr(args, "no_provision_models", False),
        send_output=out,
    )

    # --- Step 3: render. ------------------------------------------------
    review_result["target"] = target_label
    review_result["install_path"] = install_path
    review_result["pr"] = pr
    review_result["repo"] = f"{owner}/{repo_name}"

    if args.json:
        print(json.dumps({"ok": True, **review_result}, indent=2))
        if review_result.get("failed") or review_result.get("failures"):
            sys.exit(1)
        return

    _render_review_result(review_result, target_label, pr)
    console.print(f"  [dim]Start ComfyUI: comfy_runner.py {install_name} start[/dim]")

    if review_result.get("failed") or review_result.get("failures"):
        sys.exit(1)


def _cmd_review_remote(
    args: argparse.Namespace,
    target: dict,
    owner: str,
    repo_name: str,
    pr: int,
    extra_model_entries: list,
    extra_workflows: list[str],
    out,
) -> None:
    """``cmd_review`` branch for ``--target remote:<pod-name>``."""
    pod_name = target["pod_name"]
    install_name = getattr(args, "install", None) or "main"
    target_label = f"remote:{pod_name}"

    try:
        server = _station_server(args)
    except RuntimeError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    if out:
        out(
            f"Preparing PR #{pr} ({owner}/{repo_name}) for review "
            f"on {target_label} via {server}...\n"
        )

    from comfy_runner.review import prepare_remote_review
    try:
        review_result = prepare_remote_review(
            server, pod_name, install_name,
            owner, repo_name, pr,
            github_token=getattr(args, "github_token", None),
            download_token=getattr(args, "token", "") or "",
            extra_models=extra_model_entries,
            extra_workflows=extra_workflows,
            allow_arbitrary_urls=getattr(args, "allow_arbitrary_urls", False),
            skip_provisioning=getattr(args, "no_provision_models", False),
            force_purpose=getattr(args, "force_purpose", False),
            force_deploy=getattr(args, "force_deploy", False),
            idle_timeout_s=getattr(args, "idle_stop_after", None),
            send_output=out,
        )
    except RuntimeError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    review_result["target"] = target_label
    review_result["pr"] = pr
    review_result["repo"] = f"{owner}/{repo_name}"

    if args.json:
        print(json.dumps({"ok": True, **review_result}, indent=2))
        if review_result.get("failed") or review_result.get("failures"):
            sys.exit(1)
        return

    _render_review_result(review_result, target_label, pr)
    server_url = review_result.get("server_url") or ""
    if server_url:
        comfy_url = server_url.rsplit(":", 1)[0] + ":8188"
        console.print(f"  [dim]Pod ComfyUI: {comfy_url}[/dim]")

    if review_result.get("failed") or review_result.get("failures"):
        sys.exit(1)


def _cmd_review_runpod(
    args: argparse.Namespace,
    target: dict,
    owner: str,
    repo_name: str,
    pr: int,
    extra_model_entries: list,
    extra_workflows: list[str],
    out,
) -> None:
    """``cmd_review`` branch for ``--target runpod[:<gpu>]``."""
    gpu_type = target.get("gpu_type")
    install_name = getattr(args, "install", None) or "main"
    target_label = f"runpod:{gpu_type}" if gpu_type else "runpod"

    try:
        server = _station_server(args)
    except RuntimeError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    if out:
        out(
            f"Preparing PR #{pr} ({owner}/{repo_name}) for review "
            f"on {target_label} via {server}...\n"
        )

    from comfy_runner.review import prepare_runpod_review
    try:
        review_result = prepare_runpod_review(
            server, owner, repo_name, pr,
            install_name=install_name,
            gpu_type=gpu_type,
            idle_timeout_s=getattr(args, "idle_stop_after", None),
            github_token=getattr(args, "github_token", None),
            download_token=getattr(args, "token", "") or "",
            extra_models=extra_model_entries,
            extra_workflows=extra_workflows,
            allow_arbitrary_urls=getattr(args, "allow_arbitrary_urls", False),
            skip_provisioning=getattr(args, "no_provision_models", False),
            send_output=out,
        )
    except RuntimeError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    review_result["target"] = target_label
    review_result["pr"] = pr
    review_result["repo"] = f"{owner}/{repo_name}"

    # ── Optional cleanup (terminate the pod after review prep). ──────
    if getattr(args, "cleanup", False):
        pod_name = review_result.get("pod_name", "")
        if pod_name:
            if out:
                out(f"\n--- Cleaning up runpod pod '{pod_name}' ---\n")
            from comfy_runner.review import cleanup_runpod_review
            try:
                cleanup_runpod_review(server, pr)
                review_result["cleaned_up"] = True
            except RuntimeError as e:
                review_result["cleaned_up"] = False
                review_result["cleanup_error"] = str(e)
                if out:
                    out(f"⚠ Cleanup failed: {e}\n")

    if args.json:
        print(json.dumps({"ok": True, **review_result}, indent=2))
        if review_result.get("failed") or review_result.get("failures"):
            sys.exit(1)
        return

    _render_review_result(review_result, target_label, pr)
    server_url = review_result.get("server_url") or ""
    if server_url:
        comfy_url = server_url.rsplit(":", 1)[0] + ":8188"
        console.print(f"  [dim]Pod ComfyUI: {comfy_url}[/dim]")
    if review_result.get("created_new"):
        idle = review_result.get("idle_timeout_s")
        if idle:
            console.print(
                f"  [dim]Pod will idle-stop after {idle}s of inactivity. "
                f"Use 'review-cleanup {pr}' to terminate sooner.[/dim]"
            )
    if review_result.get("cleaned_up"):
        console.print("  [yellow]Pod terminated (--cleanup)[/yellow]")
    elif review_result.get("cleanup_error"):
        console.print(
            f"  [red]Cleanup failed: {review_result['cleanup_error']}[/red]"
        )

    if review_result.get("failed") or review_result.get("failures"):
        sys.exit(1)


def _cmd_review_server(
    args: argparse.Namespace,
    target: dict,
    owner: str,
    repo_name: str,
    pr: int,
    extra_model_entries: list,
    extra_workflows: list[str],
    out,
) -> None:
    """``cmd_review`` branch for ``--target server:<url>`` (no station).

    Talks directly to a comfy-runner server's ``/<install>/deploy`` and
    ``/reviews/local`` endpoints. Use this for tailnet workstations or
    any always-on comfy-runner you can reach via HTTP.
    """
    server_url = target["server_url"]
    install_name = getattr(args, "install", None) or "main"
    target_label = f"server:{server_url}"

    if out:
        out(
            f"Preparing PR #{pr} ({owner}/{repo_name}) for review "
            f"on {target_label}...\n"
        )

    from comfy_runner.review import prepare_server_review
    try:
        review_result = prepare_server_review(
            server_url, install_name,
            owner, repo_name, pr,
            github_token=getattr(args, "github_token", None),
            download_token=getattr(args, "token", "") or "",
            extra_models=extra_model_entries,
            extra_workflows=extra_workflows,
            allow_arbitrary_urls=getattr(args, "allow_arbitrary_urls", False),
            skip_provisioning=getattr(args, "no_provision_models", False),
            force_deploy=getattr(args, "force_deploy", False),
            send_output=out,
        )
    except RuntimeError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    review_result["target"] = target_label
    review_result["pr"] = pr
    review_result["repo"] = f"{owner}/{repo_name}"

    if args.json:
        print(json.dumps({"ok": True, **review_result}, indent=2))
        if review_result.get("failed") or review_result.get("failures"):
            sys.exit(1)
        return

    _render_review_result(review_result, target_label, pr)

    if review_result.get("failed") or review_result.get("failures"):
        sys.exit(1)


def cmd_review_cleanup(args: argparse.Namespace) -> None:
    """Terminate ephemeral PR pods (``purpose='pr'``) for a given PR.

    Targets pods whose record has ``purpose == "pr"`` AND
    ``pr_number == <pr>``. Does not touch ``persistent`` or ``test``
    pods.
    """
    out = None if args.json else _output

    try:
        server = _station_server(args)
    except RuntimeError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    from comfy_runner.review import cleanup_runpod_review
    try:
        result = cleanup_runpod_review(
            server, args.pr, dry_run=getattr(args, "dry_run", False),
        )
    except RuntimeError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    pr = result.get("pr", args.pr)
    total = result.get("total_found", 0)
    terminated = result.get("terminated", []) or []
    skipped = result.get("skipped", []) or []
    if total == 0:
        console.print(f"[dim]No PR-#{pr} pods found.[/dim]")
        return
    if result.get("dry_run"):
        console.print(
            f"[yellow]Dry run — would terminate {total} pod(s):[/yellow]"
        )
        for s in skipped:
            console.print(f"  • {s.get('name', '?')}")
        return
    console.print(
        f"[green]✓ Terminated {len(terminated)} of {total} "
        f"PR-#{pr} pod(s)[/green]"
    )
    for t in terminated:
        console.print(f"  [dim]{t.get('name', '?')} ({t.get('id', '?')})[/dim]")
    if skipped:
        console.print(f"[yellow]Skipped {len(skipped)}:[/yellow]")
        for s in skipped:
            console.print(
                f"  [yellow]{s.get('name', '?')}: "
                f"{s.get('error', s.get('reason', '?'))}[/yellow]"
            )


def cmd_review_init(args: argparse.Namespace) -> None:
    """Generate a ``comfyrunner`` block from a workflow JSON file.

    Reads ``args.workflow`` (a path on disk), pulls model declarations
    out of each node's ``properties.models``, and emits a fenced block
    suitable for pasting into a PR description.
    """
    from comfy_runner.review_authoring import generate_block

    workflow_path = Path(args.workflow)
    workflow_url = getattr(args, "workflow_url", None)
    try:
        block = generate_block(workflow_path, workflow_url=workflow_url)
    except ValueError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    if args.json:
        print(json.dumps({
            "ok": True,
            "manifest": block.manifest_dict,
            "block": block.text,
            "warnings": list(block.warnings),
        }, indent=2))
        return

    n_models = len(block.manifest_dict.get("models", []))
    console.print(
        f"[green]✓ Generated comfyrunner block "
        f"({n_models} model{'s' if n_models != 1 else ''}, "
        f"{len(block.manifest_dict.get('workflows', []))} workflow URL).[/green]"
    )
    console.print(
        "[dim]Paste the block below into your PR description "
        "between any two blank lines.[/dim]\n"
    )
    print(block.text)
    if block.warnings:
        console.print()
        for w in block.warnings:
            console.print(f"[yellow]⚠ {w}[/yellow]")


def cmd_review_validate(args: argparse.Namespace) -> None:
    """Lint a manifest source: file path, ``owner/repo#pr``, or PR URL.

    Exit code is 0 if the manifest parsed cleanly with no error
    findings, 1 otherwise.
    """
    from comfy_runner.review_authoring import lint_manifest_source

    github_token = getattr(args, "github_token", None)
    result = lint_manifest_source(args.source, github_token=github_token)

    if args.json:
        print(json.dumps({
            "ok": result.ok,
            "source": result.source,
            "found_block": result.found_block,
            "manifest": (
                {
                    "models": [m.to_dict() for m in result.manifest.models],
                    "workflows": list(result.manifest.workflows),
                }
                if result.manifest is not None
                else None
            ),
            "findings": [
                {
                    "severity": f.severity,
                    "message": f.message,
                    "path": f.path,
                }
                for f in result.findings
            ],
        }, indent=2))
        sys.exit(0 if result.ok else 1)

    console.print(f"[bold]Source:[/bold] {result.source}")
    if result.found_block:
        if result.manifest is not None:
            console.print(
                f"[green]✓ Found comfyrunner block "
                f"({len(result.manifest.models)} models, "
                f"{len(result.manifest.workflows)} workflows).[/green]"
            )
        else:
            console.print("[red]✗ Found comfyrunner block but it failed validation.[/red]")
    else:
        console.print("[yellow]⚠ No comfyrunner block found.[/yellow]")

    for f in result.findings:
        if f.severity == "error":
            tag = "[red]✗[/red]"
        elif f.severity == "warn":
            tag = "[yellow]⚠[/yellow]"
        else:
            tag = "[dim]ℹ[/dim]"
        loc = f" [dim]({f.path})[/dim]" if f.path else ""
        console.print(f"  {tag} {f.message}{loc}")

    sys.exit(0 if result.ok else 1)


def _render_review_result(
    review_result: dict, target_label: str, pr: int,
) -> None:
    """Render the human-readable summary common to local and remote review."""
    console.print()
    console.print(f"[green]✓ PR #{pr} ready on {target_label}[/green]")
    if review_result.get("install_path"):
        console.print(f"  Install: {review_result['install_path']}")
    if review_result.get("pod_name"):
        purpose = review_result.get("pod_purpose")
        purpose_str = f" [{purpose}]" if purpose else ""
        console.print(f"  Pod: {review_result['pod_name']}{purpose_str}")
    if review_result.get("server_url"):
        console.print(f"  Server: {review_result['server_url']}")
    if review_result.get("workflows"):
        console.print(
            f"  Workflows ({len(review_result['workflows'])}):"
        )
        for wf in review_result["workflows"]:
            console.print(f"    [dim]{wf}[/dim]")
    if review_result.get("downloaded"):
        console.print(
            f"  Models downloaded ({len(review_result['downloaded'])}):"
        )
        for m in review_result["downloaded"]:
            console.print(f"    [dim]{m}[/dim]")
    if review_result.get("skipped"):
        console.print(
            f"  Models already present ({len(review_result['skipped'])})"
        )
    if review_result.get("failed"):
        console.print(
            f"  [red]Models failed ({len(review_result['failed'])}):[/red]"
        )
        for m in review_result["failed"]:
            console.print(f"    [red]{m}[/red]")
    if review_result.get("failures"):
        console.print(
            f"  [yellow]Workflow fetch failures "
            f"({len(review_result['failures'])}):[/yellow]"
        )
        for f in review_result["failures"]:
            console.print(
                f"    [yellow]{f.get('url', '?')}: "
                f"{f.get('error', '?')}[/yellow]"
            )


def cmd_tunnel(args: argparse.Namespace) -> None:
    """Dispatch tunnel sub-subcommands."""
    action = getattr(args, "tunnel_action", None)
    if action == "start":
        cmd_tunnel_start(args)
    elif action == "stop":
        cmd_tunnel_stop(args)
    elif action == "config":
        cmd_tunnel_config(args)
    else:
        # No sub-subcommand — print help
        args._parser_tunnel.print_help()


def cmd_tunnel_start(args: argparse.Namespace) -> None:
    """Start a tunnel for a running installation."""
    from comfy_runner.tunnel import start_tunnel

    try:
        result = start_tunnel(
            name=args.name,
            provider=args.provider,
            send_output=None if args.json else _output,
            domain=getattr(args, "domain", "") or "",
        )
        if args.json:
            print(json.dumps({"ok": True, **result}, indent=2))
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_tunnel_stop(args: argparse.Namespace) -> None:
    """Stop the tunnel for an installation."""
    from comfy_runner.tunnel import stop_tunnel

    try:
        stop_tunnel(
            name=args.name,
            send_output=None if args.json else _output,
        )
        if args.json:
            print(json.dumps({"ok": True}))
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_tunnel_config(args: argparse.Namespace) -> None:
    """View or update tunnel provider configuration."""
    from comfy_runner.config import get_tunnel_config, set_tunnel_config

    provider = args.provider
    cfg = get_tunnel_config(provider)
    modified = False

    if args.authtoken is not None:
        cfg["authtoken"] = args.authtoken
        modified = True
    if args.region is not None:
        cfg["region"] = args.region
        modified = True
    if args.add_domain:
        domains = cfg.setdefault("domains", [])
        if args.add_domain not in domains:
            domains.append(args.add_domain)
            modified = True
        else:
            if not args.json:
                console.print(f"[dim]Domain already in pool: {args.add_domain}[/dim]")
    if args.rm_domain:
        domains = cfg.get("domains", [])
        if args.rm_domain in domains:
            domains.remove(args.rm_domain)
            cfg["domains"] = domains
            modified = True
        else:
            if not args.json:
                console.print(f"[dim]Domain not in pool: {args.rm_domain}[/dim]")

    if modified:
        set_tunnel_config(provider, cfg)
        if not args.json:
            console.print(f"[green]✓ Updated {provider} tunnel config[/green]")

    # Display current config
    if args.json:
        print(json.dumps({"ok": True, "provider": provider, "config": cfg}, indent=2))
    else:
        console.print(f"\n[bold]Tunnel config: {provider}[/bold]")
        token = cfg.get("authtoken", "")
        if token:
            masked = token[:4] + "…" + token[-4:] if len(token) > 8 else "****"
            console.print(f"  authtoken: [dim]{masked}[/dim]")
        else:
            console.print("  authtoken: [dim](not set)[/dim]")
        console.print(f"  region:    [dim]{cfg.get('region') or '(default)'}[/dim]")
        domains = cfg.get("domains", [])
        if domains:
            console.print(f"  domains:   {', '.join(domains)}")
        else:
            console.print("  domains:   [dim](none — random URLs)[/dim]")


def cmd_config(args: argparse.Namespace) -> None:
    """View or set global configuration."""
    from comfy_runner.config import get_shared_dir, get_hf_token, get_modelscope_token, load_config, set_shared_dir, set_hf_token, set_modelscope_token

    action = getattr(args, "config_action", None)

    if action == "set":
        key = args.key
        value = args.value
        allowed_keys = {"shared_dir", "hf_token", "modelscope_token"}
        if key not in allowed_keys:
            err = f"Unknown key '{key}'. Allowed: {', '.join(sorted(allowed_keys))}"
            if args.json:
                print(json.dumps({"ok": False, "error": err}))
                sys.exit(1)
            console.print(f"[red]{err}[/red]")
            sys.exit(1)

        if key == "shared_dir":
            if value:
                from comfy_runner.shared_paths import ensure_shared_dirs
                resolved = str(Path(value).resolve())
                ensure_shared_dirs(resolved)
                set_shared_dir(resolved)
                if not args.json:
                    console.print(f"[green]✓ shared_dir = {resolved}[/green]")
                    console.print(f"[dim]Created shared directory structure at {resolved}[/dim]")
            else:
                set_shared_dir("")
                if not args.json:
                    console.print("[green]✓ shared_dir cleared[/green]")

        elif key == "hf_token":
            set_hf_token(value)
            if not args.json:
                display = "(cleared)" if not value else f"{value[:8]}..."
                console.print(f"[green]✓ hf_token = {display}[/green]")

        elif key == "modelscope_token":
            set_modelscope_token(value)
            if not args.json:
                display = "(cleared)" if not value else f"{value[:8]}..."
                console.print(f"[green]✓ modelscope_token = {display}[/green]")

        if args.json:
            print(json.dumps({"ok": True, "key": key, "value": value}))

    elif action == "show" or action is None:
        config = load_config()
        shared = get_shared_dir()
        if args.json:
            # Don't expose sensitive fields
            safe = {
                "shared_dir": shared,
                "installations_dir": config.get("installations_dir", ""),
            }
            print(json.dumps({"ok": True, "config": safe}, indent=2))
        else:
            console.print("[bold]comfy-runner configuration[/bold]\n")
            console.print(f"  shared_dir:        [bold]{shared or '(not set)'}[/bold]")
            console.print(f"  installations_dir: {config.get('installations_dir', '')}")
            hf = get_hf_token()
            ms = get_modelscope_token()
            console.print(f"  hf_token:          [bold]{(hf[:8] + '...') if hf else '(not set)'}[/bold]")
            console.print(f"  modelscope_token:  [bold]{(ms[:8] + '...') if ms else '(not set)'}[/bold]")
            tunnel_cfg = config.get("tunnel", {})
            if tunnel_cfg:
                for provider, pcfg in tunnel_cfg.items():
                    console.print(f"  tunnel.{provider}:  {json.dumps(pcfg)}")
    else:
        console.print("[dim]Usage: comfy-runner config {show,set}[/dim]")


def cmd_config_env(args: argparse.Namespace) -> None:
    """Manage persistent environment variables for an installation."""
    from comfy_runner.config import get_installation, set_installation

    name = args.name
    action = args.env_action

    record = get_installation(name)
    if not record:
        if args.json:
            print(json.dumps({"ok": False, "error": f"Installation '{name}' not found."}))
            sys.exit(1)
        console.print(f"[red]Installation '{name}' not found.[/red]")
        sys.exit(1)

    env = dict(record.get("env", {}) or {})

    if action == "list":
        if args.json:
            print(json.dumps({"ok": True, "env": env}, indent=2))
        elif not env:
            console.print("[dim]No environment variables set.[/dim]")
        else:
            table = Table(title=f"Environment Variables: {name}")
            table.add_column("Key", style="cyan")
            table.add_column("Value", style="green")
            for k, v in sorted(env.items()):
                table.add_row(k, v)
            console.print(table)

    elif action == "set":
        key, value = args.key, args.value
        env[key] = value
        record["env"] = env
        set_installation(name, record)
        if args.json:
            print(json.dumps({"ok": True, "key": key, "value": value}))
        else:
            console.print(f"[green]Set {key}={value}[/green]")

    elif action == "unset":
        key = args.key
        removed = env.pop(key, None)
        record["env"] = env
        set_installation(name, record)
        if args.json:
            print(json.dumps({"ok": True, "key": key, "removed": removed is not None}))
        else:
            if removed is not None:
                console.print(f"[yellow]Removed {key}[/yellow]")
            else:
                console.print(f"[dim]{key} was not set[/dim]")


def cmd_server(args: argparse.Namespace) -> None:
    """Start the HTTP control API server."""
    from comfy_runner_server.server import run_server

    if args.json:
        print(json.dumps({"ok": False, "error": "Server cannot run in JSON mode"}))
        sys.exit(1)

    host = args.listen
    port = args.port
    tailscale_active = False

    # --tunnels: enable tunnel API (tailscale funnel / ngrok for public internet exposure)
    tunnels_active = args.tunnels
    if tunnels_active:
        from comfy_runner_server.server import set_tunnels_enabled
        set_tunnels_enabled(True)

    # Always clean up stale tailscale serves from previous sessions
    # (tailscale serve --bg persists across reboots in Tailscale's own config)
    from comfy_runner.tunnel import cleanup_stale_serves
    cleanup_stale_serves(send_output=_output)

    # Clean up leftover staging files from interrupted model downloads
    from comfy_runner.workflow_models import cleanup_staging_all
    cleanup_staging_all(send_output=_output)

    # --tailscale: tailscale serve handles external access, server binds localhost
    if args.tailscale:
        from comfy_runner.tunnel import start_tailscale_serve
        from comfy_runner_server.server import set_tailscale_mode
        try:
            ts_url = start_tailscale_serve(port=port, send_output=_output)
            host = "127.0.0.1"
            set_tailscale_mode(True)
            tailscale_active = True
            console.print(
                f"\n[green]Tailscale URL:[/green] [bold]{ts_url}[/bold]\n"
                f"[dim]Add this as a runner server in pr-tracker config.[/dim]\n"
            )
        except RuntimeError as e:
            console.print(f"[red]Tailscale setup failed: {e}[/red]")
            console.print("[dim]Continuing with local-only server...[/dim]\n")

    # In tailscale mode, register serves for any already-running instances
    if tailscale_active:
        from comfy_runner.installations import show_list
        from comfy_runner.process import get_status
        from comfy_runner.tunnel import start_tailscale_serve_port
        for inst in show_list():
            try:
                status = get_status(inst["name"])
                if status.get("running") and status.get("port"):
                    try:
                        start_tailscale_serve_port(status["port"], send_output=_output)
                    except Exception as e:
                        console.print(f"[dim]⚠ Could not register port {status['port']}: {e}[/dim]")
            except Exception:
                pass

    def _shutdown() -> None:
        if tailscale_active:
            if not args.keep_instances:
                console.print("\n[dim]Stopping ComfyUI instances...[/dim]")
                from comfy_runner.installations import show_list
                from comfy_runner.process import get_status, stop_installation
                for inst in show_list():
                    try:
                        status = get_status(inst["name"])
                        if status.get("running"):
                            stop_installation(inst["name"], send_output=_output)
                    except Exception:
                        pass
            else:
                console.print("\n[dim]Keeping ComfyUI instances running.[/dim]")
            console.print("[dim]Cleaning up tailscale serve...[/dim]")
            from comfy_runner.tunnel import cleanup_stale_serves
            cleanup_stale_serves(send_output=_output)
        console.print("\nServer stopped.")

    # Register shutdown via atexit so it runs regardless of how
    # waitress exits (it swallows KeyboardInterrupt internally)
    import atexit
    atexit.register(_shutdown)

    console.print(
        f"Starting control server on [cyan]{host}:{port}[/cyan] "
        f"(manages all installations)"
    )
    console.print("[dim]Press Ctrl+C to stop[/dim]\n")

    run_server(
        host=host,
        port=port,
    )


def cmd_tailscale_serve(args: argparse.Namespace) -> None:
    """Manage tailscale serve for the runner server."""
    from comfy_runner.tunnel import (
        get_tailscale_hostname,
        get_tailscale_serve_status,
        start_tailscale_serve,
        stop_tailscale_serve,
    )

    action = getattr(args, "ts_action", None)

    if action == "start":
        try:
            url = start_tailscale_serve(port=args.port, send_output=_output)
            if args.json:
                print(json.dumps({"ok": True, "url": url}))
            else:
                console.print(
                    f"\n[green]Runner server is now accessible at:[/green]\n"
                    f"  [bold]{url}[/bold]\n"
                    f"\n"
                    f"[dim]Add to pr-tracker config:[/dim]\n"
                    f"  pr_tracker server add mybox={url}\n"
                )
        except RuntimeError as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif action == "stop":
        stop_tailscale_serve(send_output=None if args.json else _output)
        if args.json:
            print(json.dumps({"ok": True}))

    elif action == "status":
        status = get_tailscale_serve_status()
        if args.json:
            print(json.dumps({"ok": True, **status}, indent=2))
        else:
            if status.get("active"):
                console.print(f"[green]Active[/green]  {status.get('url', '?')}")
            else:
                reason = status.get("reason", "not active")
                console.print(f"[dim]Inactive ({reason})[/dim]")
            hostname = get_tailscale_hostname()
            if hostname:
                console.print(f"[dim]Tailscale hostname: {hostname}[/dim]")

    else:
        args._parser_ts.print_help()


def cmd_nodes(args: argparse.Namespace) -> None:
    """Manage custom nodes."""
    from comfy_runner.config import get_installation
    from comfy_runner.nodes import (
        add_cnr_node,
        add_git_node,
        disable_node,
        enable_node,
        remove_node,
        scan_custom_nodes,
    )

    action = args.nodes_action
    if not action:
        console.print("[dim]Usage: comfy-runner nodes {list,add,rm,enable,disable}[/dim]")
        return

    out = None if args.json else _output

    try:
        if action == "list":
            record = get_installation(args.name)
            if not record:
                raise RuntimeError(f"Installation '{args.name}' not found.")

            nodes = scan_custom_nodes(record["path"])

            if args.json:
                print(json.dumps({"ok": True, "nodes": nodes}, indent=2))
                return

            if not nodes:
                console.print("[dim]No custom nodes found.[/dim]")
                return

            table = Table(title=f"Custom Nodes ({args.name})")
            table.add_column("Name", style="cyan")
            table.add_column("Type", style="green")
            table.add_column("Enabled", style="bold")
            table.add_column("Version/Commit", style="yellow")
            table.add_column("URL", style="dim")

            for node in nodes:
                ver = node.get("version") or (node.get("commit") or "")[:12]
                table.add_row(
                    node.get("dir_name", ""),
                    node.get("type", ""),
                    "✓" if node.get("enabled") else "✗",
                    ver,
                    node.get("url", ""),
                )
            console.print(table)

        elif action == "add":
            record = get_installation(args.name)
            if not record:
                raise RuntimeError(f"Installation '{args.name}' not found.")

            source = args.source
            install_path = record["path"]

            # Detect git URL vs CNR node ID
            if source.startswith(("http://", "https://", "git@", "git://")):
                node = add_git_node(install_path, source, send_output=out)
            else:
                node = add_cnr_node(
                    install_path, source, version=args.version, send_output=out
                )

            if args.json:
                print(json.dumps({"ok": True, "node": node}, indent=2))

        elif action == "rm":
            record = get_installation(args.name)
            if not record:
                raise RuntimeError(f"Installation '{args.name}' not found.")

            remove_node(record["path"], args.node_name, send_output=out)

            if args.json:
                print(json.dumps({"ok": True}))

        elif action == "enable":
            record = get_installation(args.name)
            if not record:
                raise RuntimeError(f"Installation '{args.name}' not found.")

            enable_node(record["path"], args.node_name, send_output=out)

            if args.json:
                print(json.dumps({"ok": True}))

        elif action == "disable":
            record = get_installation(args.name)
            if not record:
                raise RuntimeError(f"Installation '{args.name}' not found.")

            disable_node(record["path"], args.node_name, send_output=out)

            if args.json:
                print(json.dumps({"ok": True}))

    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_snapshot(args: argparse.Namespace) -> None:
    """Manage snapshots for an installation."""
    from comfy_runner.config import get_installation

    action = getattr(args, "snapshot_action", None)
    if not action:
        console.print("[dim]Usage: comfy-runner snapshot {capture,save,list,show,diff,restore,export,import}[/dim]")
        return

    out = None if args.json else _output

    try:
        if action == "capture":
            from comfy_runner.snapshot import (
                _iso_now, capture_state, capture_external_state,
            )

            install_path = Path(args.path).resolve()
            comfyui_dir = install_path / "ComfyUI"
            venv_override = getattr(args, "venv", None)

            if comfyui_dir.is_dir():
                # Standard comfy-runner layout: install_path/ComfyUI/
                if out:
                    out(f"Capturing snapshot from {install_path}...\n")
                state = capture_state(install_path)
            elif (install_path / "custom_nodes").is_dir():
                # Manual/portable install: path IS the ComfyUI dir
                if out:
                    out(f"Capturing snapshot from manual install at {install_path}...\n")
                state = capture_external_state(
                    install_path,
                    venv_path=venv_override,
                )
            else:
                raise RuntimeError(
                    f"Not a valid ComfyUI installation: {install_path}\n"
                    f"Expected ComfyUI/ subdirectory or custom_nodes/ directory"
                )
            snapshot = {
                "version": 1,
                "createdAt": _iso_now(),
                "trigger": "manual",
                "label": args.label,
                "comfyui": state["comfyui"],
                "customNodes": state["customNodes"],
                "pipPackages": state["pipPackages"],
            }
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

            if args.json:
                print(json.dumps({"ok": True, "file": str(output_path)}))
            else:
                console.print(f"[green]Snapshot written to:[/green] {output_path}")

            if out:
                comfyui = state["comfyui"]
                nodes = state["customNodes"]
                pips = state["pipPackages"]
                out(f"  ComfyUI: {comfyui.get('ref', '?')} ({(comfyui.get('commit') or '?')[:12]})\n")
                out(f"  Custom nodes: {len(nodes)}\n")
                out(f"  Pip packages: {len(pips)}\n")

        elif action == "save":
            from comfy_runner.snapshot import save_snapshot

            record = get_installation(args.name)
            if not record:
                raise RuntimeError(f"Installation '{args.name}' not found.")

            filename = save_snapshot(
                record["path"], trigger="manual", label=args.label or None,
            )
            if args.json:
                print(json.dumps({"ok": True, "filename": filename}))
            else:
                console.print(f"[green]Saved snapshot:[/green] {filename}")

        elif action == "list":
            from comfy_runner.snapshot import list_snapshots

            record = get_installation(args.name)
            if not record:
                raise RuntimeError(f"Installation '{args.name}' not found.")

            entries = list_snapshots(record["path"])

            if args.json:
                print(json.dumps({"ok": True, "snapshots": [
                    {
                        "filename": e["filename"],
                        "createdAt": e["snapshot"]["createdAt"],
                        "trigger": e["snapshot"]["trigger"],
                        "label": e["snapshot"].get("label"),
                        "nodeCount": len(e["snapshot"].get("customNodes", [])),
                        "pipPackageCount": len(e["snapshot"].get("pipPackages", {})),
                    }
                    for e in entries
                ], "totalCount": len(entries)}, indent=2))
                return

            if not entries:
                console.print("[dim]No snapshots found.[/dim]")
                return

            table = Table(title=f"Snapshots ({args.name}) — {len(entries)} total")
            table.add_column("#", style="dim")
            table.add_column("Filename", style="cyan")
            table.add_column("Date", style="green")
            table.add_column("Trigger", style="yellow")
            table.add_column("Label")
            table.add_column("Nodes", justify="right")
            table.add_column("Packages", justify="right")

            for i, entry in enumerate(entries):
                s = entry["snapshot"]
                created = s.get("createdAt", "")[:19].replace("T", " ")
                label = s.get("label") or ""
                marker = "★ " if i == 0 else "  "
                table.add_row(
                    f"{marker}{i + 1}",
                    entry["filename"],
                    created,
                    s.get("trigger", ""),
                    label,
                    str(len(s.get("customNodes", []))),
                    str(len(s.get("pipPackages", {}))),
                )
            console.print(table)

        elif action == "show":
            from comfy_runner.snapshot import load_snapshot, resolve_snapshot_id

            record = get_installation(args.name)
            if not record:
                raise RuntimeError(f"Installation '{args.name}' not found.")

            snapshot = resolve_snapshot_id(record["path"], args.id)
            data = load_snapshot(record["path"], snapshot)

            if args.json:
                print(json.dumps({"ok": True, "filename": snapshot, "snapshot": data}, indent=2))
                return

            console.print(f"[bold]Snapshot:[/bold] {snapshot}")
            console.print(f"[bold]Created:[/bold] {data.get('createdAt', '')}")
            console.print(f"[bold]Trigger:[/bold] {data.get('trigger', '')}")
            label = data.get("label")
            if label:
                console.print(f"[bold]Label:[/bold] {label}")
            comfyui = data.get("comfyui", {})
            console.print(f"[bold]ComfyUI:[/bold] {comfyui.get('ref', '?')} ({(comfyui.get('commit') or '?')[:12]})")
            console.print(f"[bold]Release:[/bold] {comfyui.get('releaseTag', '?')}  Variant: {comfyui.get('variant', '?')}")
            if data.get("pythonVersion"):
                console.print(f"[bold]Python:[/bold] {data['pythonVersion']}")
            if data.get("updateChannel"):
                console.print(f"[bold]Channel:[/bold] {data['updateChannel']}")

            nodes = data.get("customNodes", [])
            console.print(f"\n[bold]Custom Nodes ({len(nodes)}):[/bold]")
            for n in nodes:
                status = "✓" if n.get("enabled") else "✗"
                ver = n.get("version") or (n.get("commit") or "")[:12]
                console.print(f"  {status} {n.get('dirName') or n.get('dir_name', '')}"
                              f"  [{n.get('type', '')}]  {ver}")

            pips = data.get("pipPackages", {})
            console.print(f"\n[bold]Pip Packages:[/bold] {len(pips)} total")

        elif action == "diff":
            from comfy_runner.snapshot import diff_against_current, load_snapshot, resolve_snapshot_id

            record = get_installation(args.name)
            if not record:
                raise RuntimeError(f"Installation '{args.name}' not found.")

            snapshot_file = resolve_snapshot_id(record["path"], args.id)
            target = load_snapshot(record["path"], snapshot_file)
            diff = diff_against_current(record["path"], target)

            if args.json:
                print(json.dumps({"ok": True, "diff": diff}, indent=2))
                return

            _print_diff(diff, snapshot_file)

        elif action == "restore":
            from comfy_runner.process import get_status, stop_installation
            from comfy_runner.snapshot import resolve_snapshot_id, restore_snapshot

            record = get_installation(args.name)
            if not record:
                raise RuntimeError(f"Installation '{args.name}' not found.")

            # Stop ComfyUI if running — pip ops fail with file locks on Windows
            status = get_status(args.name)
            if status.get("running"):
                if out:
                    out(f"Stopping '{args.name}' before restore…\n")
                stop_installation(args.name, send_output=out)

            snapshot_file = resolve_snapshot_id(record["path"], args.id)
            result = restore_snapshot(
                record["path"], snapshot_file, send_output=out,
            )
            if args.json:
                print(json.dumps({"ok": True, "result": result}, indent=2))

        elif action == "export":
            from comfy_runner.snapshot import export_snapshot, resolve_snapshot_id

            record = get_installation(args.name)
            if not record:
                raise RuntimeError(f"Installation '{args.name}' not found.")

            snapshot_file = resolve_snapshot_id(record["path"], args.id)
            dest = args.output or f"{snapshot_file}"
            export_snapshot(record["path"], snapshot_file, dest, installation_name=args.name)

            if args.json:
                print(json.dumps({"ok": True, "file": dest}))
            else:
                console.print(f"[green]Exported to:[/green] {dest}")

        elif action == "import":
            from comfy_runner.snapshot import import_snapshots, validate_export_envelope

            record = get_installation(args.name)
            if not record:
                raise RuntimeError(f"Installation '{args.name}' not found.")

            import_path = args.file
            data = json.loads(Path(import_path).read_text("utf-8"))
            envelope = validate_export_envelope(data)
            result = import_snapshots(record["path"], envelope)

            if args.json:
                print(json.dumps({"ok": True, **result}))
            else:
                console.print(f"[green]Imported {result['imported']} snapshot(s), "
                              f"skipped {result['skipped']} duplicate(s).[/green]")

    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def _print_diff(diff: dict, snapshot_file: str) -> None:
    """Print a snapshot diff in human-readable format."""
    has_changes = False

    if diff.get("comfyuiChanged"):
        has_changes = True
        c = diff["comfyui"]
        f_ref = c["from"].get("ref", "?")
        f_commit = (c["from"].get("commit") or "?")[:12]
        t_ref = c["to"].get("ref", "?")
        t_commit = (c["to"].get("commit") or "?")[:12]
        console.print(f"[bold]ComfyUI:[/bold] {f_ref} ({f_commit}) -> {t_ref} ({t_commit})")

    if diff.get("updateChannelChanged"):
        has_changes = True
        ch = diff["updateChannel"]
        console.print(f"[bold]Channel:[/bold] {ch['from']} -> {ch['to']}")

    added = diff.get("nodesAdded", [])
    removed = diff.get("nodesRemoved", [])
    changed = diff.get("nodesChanged", [])
    if added or removed or changed:
        has_changes = True
        console.print(f"\n[bold]Custom Nodes:[/bold] +{len(added)} -{len(removed)} ~{len(changed)}")
        for n in added:
            ver = n.get("version") or (n.get("commit") or "")[:12]
            console.print(f"  [green]+[/green] {n.get('id', '')}  {ver}")
        for n in removed:
            ver = n.get("version") or (n.get("commit") or "")[:12]
            console.print(f"  [red]-[/red] {n.get('id', '')}  {ver}")
        for n in changed:
            f_ver = n["from"].get("version") or (n["from"].get("commit") or "")[:12]
            t_ver = n["to"].get("version") or (n["to"].get("commit") or "")[:12]
            console.print(f"  [yellow]~[/yellow] {n.get('id', '')}  {f_ver} -> {t_ver}")

    pip_added = diff.get("pipsAdded", [])
    pip_removed = diff.get("pipsRemoved", [])
    pip_changed = diff.get("pipsChanged", [])
    if pip_added or pip_removed or pip_changed:
        has_changes = True
        console.print(f"\n[bold]Pip Packages:[/bold] +{len(pip_added)} -{len(pip_removed)} ~{len(pip_changed)}")

    if not has_changes:
        console.print("[dim]No differences — environment matches the snapshot.[/dim]")


def cmd_info(args: argparse.Namespace) -> None:
    """Show detailed info about an installation."""
    from comfy_runner.config import get_installation
    from comfy_runner.environment import get_variant_label

    record = get_installation(args.name)
    if not record:
        if args.json:
            print(json.dumps({"ok": False, "error": f"Installation '{args.name}' not found."}))
            sys.exit(1)
        console.print(f"[red]Installation '{args.name}' not found.[/red]")
        sys.exit(1)

    if args.json:
        print(json.dumps({"ok": True, "installation": {"name": args.name, **record}}, indent=2))
        return

    variant = record.get("variant", "")
    table = Table(title=f"Installation: {args.name}")
    table.add_column("Field", style="cyan")
    table.add_column("Value")

    table.add_row("Path", record.get("path", ""))
    table.add_row("Status", record.get("status", ""))
    table.add_row("Variant", f"{get_variant_label(variant)} ({variant})" if variant else "")
    table.add_row("Release", record.get("release_tag", ""))
    table.add_row("ComfyUI Ref", record.get("comfyui_ref", ""))
    table.add_row("Python", record.get("python_version", ""))
    table.add_row("HEAD Commit", record.get("head_commit", "")[:12])
    table.add_row("Launch Args", record.get("launch_args", "") or "(none)")
    table.add_row("Created", record.get("created_at", ""))

    console.print(table)


def cmd_sysinfo(args: argparse.Namespace) -> None:
    """Show system hardware information."""
    from comfy_runner.system_info import get_system_info

    try:
        info = get_system_info()
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    if args.json:
        print(json.dumps({"ok": True, "system_info": info}, indent=2))
        return

    table = Table(title="System Information")
    table.add_column("Field", style="cyan")
    table.add_column("Value")

    # GPU
    gpu_label = info.get("gpu_label") or "None (CPU only)"
    table.add_row("GPU Vendor", gpu_label)
    for i, gpu in enumerate(info.get("gpus", [])):
        vram = f"{gpu['vram_mb']} MB" if gpu.get("vram_mb") else "unknown"
        driver = gpu.get("driver_version") or ""
        table.add_row(f"  GPU {i}", f"{gpu.get('model', '?')}  ({vram})" + (f"  driver {driver}" if driver else ""))
    if info.get("nvidia_driver_version"):
        supported = info.get("nvidia_driver_supported")
        icon = "✓" if supported else "✗"
        table.add_row("NVIDIA Driver", f"{info['nvidia_driver_version']}  {icon} {'supported' if supported else 'unsupported (need ≥580)'}")

    # CPU
    table.add_row("CPU", info.get("cpu_model", "Unknown"))
    cores = f"{info.get('cpu_cores', '?')} logical"
    if info.get("cpu_physical_cores"):
        cores += f", {info['cpu_physical_cores']} physical"
    if info.get("cpu_speed_ghz"):
        cores += f", {info['cpu_speed_ghz']} GHz"
    table.add_row("  Cores", cores)

    # Memory
    table.add_row("RAM", f"{info.get('total_memory_gb', '?')} GB")

    # OS
    distro = info.get("os_distro") or info.get("platform", "?")
    table.add_row("OS", f"{distro}  ({info.get('arch', '?')})")
    table.add_row("Kernel", info.get("os_version", "?"))

    # Disk
    table.add_row("Disk", f"{info.get('disk_free_gb', '?')} GB free / {info.get('disk_total_gb', '?')} GB total")

    # Installations
    table.add_row("Installations", str(info.get("installation_count", 0)))

    console.print(table)


def cmd_set(args: argparse.Namespace) -> None:
    """Set a configuration value on an installation."""
    from comfy_runner.config import get_installation, set_installation

    record = get_installation(args.name)
    if not record:
        if args.json:
            print(json.dumps({"ok": False, "error": f"Installation '{args.name}' not found."}))
            sys.exit(1)
        console.print(f"[red]Installation '{args.name}' not found.[/red]")
        sys.exit(1)

    key = args.key
    value = args.value

    allowed_keys = {"launch_args"}
    if key not in allowed_keys:
        err = f"Unknown key '{key}'. Allowed: {', '.join(sorted(allowed_keys))}"
        if args.json:
            print(json.dumps({"ok": False, "error": err}))
            sys.exit(1)
        console.print(f"[red]{err}[/red]")
        sys.exit(1)

    record[key] = value
    set_installation(args.name, record)

    if args.json:
        print(json.dumps({"ok": True, "key": key, "value": value}))
    else:
        console.print(f"✓ [cyan]{args.name}[/cyan] {key} = [bold]{value or '(empty)'}[/bold]")


def cmd_download_model(args: argparse.Namespace) -> None:
    """Download a single model by URL."""
    from comfy_runner.config import get_installation
    from comfy_runner.workflow_models import download_models, resolve_models_dir
    from urllib.parse import urlparse, unquote

    try:
        record = get_installation(args.name)
        if not record:
            raise RuntimeError(f"Installation '{args.name}' not found.")

        url = args.url
        directory = args.dir
        filename = args.filename
        if not filename:
            # Derive from URL path
            path = urlparse(url).path
            filename = unquote(path.rsplit("/", 1)[-1]) or "download"
            # Strip query params that got included
            if "?" in filename:
                filename = filename.split("?")[0]

        models_dir = resolve_models_dir(record["path"])
        dest = models_dir / directory / filename

        if dest.is_file():
            if args.json:
                print(json.dumps({"ok": True, "skipped": True, "path": str(dest)}))
            else:
                console.print(f"[dim]Already exists: {dest}[/dim]")
            return

        if not args.json:
            console.print(f"Downloading [bold]{filename}[/bold] → {directory}/")
            console.print(f"Models dir: [dim]{models_dir}[/dim]")

        model = {"name": filename, "url": url, "directory": directory}
        result = download_models([model], models_dir, send_output=None if args.json else _output, token=getattr(args, "token", "") or "")

        if args.json:
            print(json.dumps({"ok": True, **result}, indent=2))
        elif result["downloaded"]:
            console.print("[green]✓ Done[/green]")
        elif result["failed"]:
            console.print(f"[red]✗ Failed: {result['errors'][0]}[/red]")
            sys.exit(1)

    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


class _ProgressStream:
    """Wrapper around a file stream that updates a rich progress bar."""

    def __init__(self, stream: Any, progress: Any, task_id: Any) -> None:
        self._stream = stream
        self._progress = progress
        self._task_id = task_id

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        if data:
            self._progress.advance(self._task_id, len(data))
        return data


def _hash_with_progress(
    path: Path, algorithm: str, progress: Any, task_id: Any,
) -> str:
    """Hash a file while updating a rich progress bar."""
    import blake3 as _b3

    chunk_size = 1024 * 1024  # 1MB for speed

    if algorithm == "blake3":
        h = _b3.blake3()
    else:
        import hashlib
        h = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
            progress.advance(task_id, len(chunk))

    return h.hexdigest()


def cmd_remote_upload(args: argparse.Namespace) -> None:
    """Upload a model file to a remote comfy-runner server."""
    from comfy_runner.hosted.remote import RemoteRunner
    from comfy_runner.upload import compute_file_hash

    try:
        file_path = Path(args.file)
        if not file_path.is_file():
            raise RuntimeError(f"File not found: {file_path}")

        server = args.server
        name = args.name
        directory = args.dir
        filename = args.filename or file_path.name
        hash_type = getattr(args, "hash_type", "blake3") or "blake3"
        file_size = file_path.stat().st_size

        remote = RemoteRunner(server)

        # Check for resume
        offset = 0
        if args.resume:
            status = remote.get_upload_status(name, directory, filename)
            if status.get("complete"):
                if args.json:
                    print(json.dumps({"ok": True, "skipped": True, "path": f"{directory}/{filename}"}))
                else:
                    console.print(f"[dim]Already exists on server: {directory}/{filename}[/dim]")
                return
            if status.get("exists") and not status.get("complete"):
                offset = status.get("bytes_received", 0)

        if args.json:
            file_hash = compute_file_hash(file_path, hash_type)
            result = remote.upload_model(
                name, str(file_path), directory,
                filename=filename, offset=offset,
                expected_hash=file_hash, hash_type=hash_type,
            )
            print(json.dumps({"ok": True, **result}, indent=2))
            return

        from rich.progress import (
            BarColumn,
            DownloadColumn,
            Progress,
            TextColumn,
            TransferSpeedColumn,
            TimeRemainingColumn,
        )

        with Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            # Step 1: Hash locally
            hash_task = progress.add_task(f"Hashing ({hash_type})", total=file_size)
            file_hash = _hash_with_progress(file_path, hash_type, progress, hash_task)
            progress.update(hash_task, description=f"[green]✓[/green] Hash ({hash_type})")

            # Step 2: Upload to server
            send_size = file_size - offset
            desc = f"Uploading {filename}"
            if offset > 0:
                desc = f"Resuming {filename}"
            upload_task = progress.add_task(desc, total=send_size)

            def _on_progress(sent: int, total: int) -> None:
                progress.update(upload_task, completed=sent)

            result = remote.upload_model(
                name, str(file_path), directory,
                filename=filename, offset=offset,
                expected_hash=file_hash, hash_type=hash_type,
                on_progress=_on_progress,
            )

            if result.get("skipped"):
                progress.update(upload_task, description=f"[dim]Skipped (exists)[/dim]")
            else:
                progress.update(upload_task, description=f"[green]✓[/green] {filename}")

        if result.get("skipped"):
            console.print(f"[dim]Already exists: {directory}/{filename}[/dim]")
        else:
            size_mb = result.get("size", 0) / 1048576
            h = result.get("hash", "")
            console.print(
                f"[green]✓[/green] {directory}/{filename}  "
                f"[dim]{size_mb:.1f} MB  {hash_type}:{h[:16]}…  → {server}[/dim]"
            )

    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_workflow_models(args: argparse.Namespace) -> None:
    """Download models referenced in a workflow template."""
    from comfy_runner.workflow_models import (
        check_missing_models,
        download_models,
        parse_workflow_models,
        resolve_models_dir,
    )

    try:
        from comfy_runner.config import get_installation

        wf_path = Path(args.file)
        if not wf_path.exists():
            raise FileNotFoundError(f"File not found: {wf_path}")
        workflow = json.loads(wf_path.read_text(encoding="utf-8"))

        record = get_installation(args.name)
        if not record:
            raise RuntimeError(f"Installation '{args.name}' not found.")

        models = parse_workflow_models(workflow)
        models_dir = resolve_models_dir(record["path"])
        missing, existing = check_missing_models(models, models_dir)

        if args.json:
            result: dict = {
                "ok": True,
                "total": len(models),
                "missing": len(missing),
                "existing": len(existing),
                "models": models,
                "missing_models": missing,
            }
            if args.dry_run:
                print(json.dumps(result, indent=2))
                return
            downloads = download_models(missing, models_dir)
            result["downloads"] = downloads
            print(json.dumps(result, indent=2))
            return

        console.print(f"Found [bold]{len(models)}[/bold] model(s) in workflow")
        console.print(f"Models dir: [dim]{models_dir}[/dim]")

        if not missing:
            console.print("[green]All models already present.[/green]")
            return

        console.print(f"[yellow]{len(missing)} missing:[/yellow]")
        for m in missing:
            console.print(f"  • {m['directory']}/{m['name']}")

        if args.dry_run:
            return

        console.print()
        download_models(missing, models_dir, send_output=_output)
        console.print("[green]Done.[/green]")

    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Test commands
# ---------------------------------------------------------------------------

def cmd_test(args: argparse.Namespace) -> None:
    """Dispatch test subcommands."""
    action = getattr(args, "test_action", None)

    if action == "run":
        _test_run(args)
    elif action == "list":
        _test_list(args)
    elif action == "baseline":
        _test_baseline(args)
    elif action == "report":
        _test_report(args)
    elif action == "fleet":
        _test_fleet(args)
    else:
        args._parser_test.print_help()


def _test_run(args: argparse.Namespace) -> None:
    """Run a test suite against a ComfyUI instance."""
    # RunPod one-shot mode
    if getattr(args, "runpod", False):
        if args.target:
            msg = "Cannot use --target with --runpod"
            if args.json:
                print(json.dumps({"ok": False, "error": msg}, indent=2))
            else:
                console.print(f"[red]Error: {msg}[/red]")
            sys.exit(1)
        return _test_run_runpod(args)

    if not args.target:
        if args.json:
            print(json.dumps({"ok": False, "error": "--target is required (or use --runpod)"}, indent=2))
        else:
            console.print("[red]Error: --target is required (or use --runpod)[/red]")
        sys.exit(1)

    from comfy_runner.testing.client import ComfyTestClient
    from comfy_runner.testing.report import build_report, render_console, write_report
    from comfy_runner.testing.runner import run_suite
    from comfy_runner.testing.suite import load_suite

    try:
        suite = load_suite(args.suite)
    except ValueError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    target_url = args.target
    # If target looks like a pod name (no ://), resolve it
    if "://" not in target_url:
        try:
            target_url = _resolve_server_url(target_url)
        except RuntimeError:
            pass
        # Assume it's a direct ComfyUI URL if resolution fails
        if "://" not in target_url:
            target_url = f"http://{target_url}"

    client = ComfyTestClient(target_url, timeout=args.http_timeout)
    out_dir = Path(args.output) if args.output else Path(args.suite) / "runs" / _run_id()
    send_output = None if args.json else (lambda t: console.print(t, end=""))

    # Suite-level watchdog: --max-runtime overrides suite.json.
    from comfy_runner.testing.client import watchdog as _watchdog
    budget = getattr(args, "max_runtime", None)
    if budget is None and isinstance(suite.max_runtime_s, int):
        budget = suite.max_runtime_s

    def _on_abort() -> None:
        try:
            client.interrupt()
        except Exception:
            pass

    try:
        with _watchdog(budget, on_abort=_on_abort) as cancelled:
            suite_run = run_suite(
                client, suite, out_dir,
                timeout=args.timeout,
                send_output=send_output,
                cancelled=cancelled,
            )
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    target_info = {"name": args.target, "url": target_url}
    report = build_report(suite_run, target_info=target_info)

    if args.json:
        from comfy_runner.testing.report import render_json
        print(render_json(report))
    else:
        console.print()
        console.print(render_console(report))
        formats = [f.strip() for f in args.format.split(",")]
        written = write_report(report, out_dir, formats=formats)
        for fmt, path in written.items():
            console.print(f"  [dim]{fmt}: {path}[/dim]")

    # Non-zero exit when any test failed or the watchdog aborted.
    if report.failed > 0 or report.timed_out:
        sys.exit(1)


def _test_run_runpod(args: argparse.Namespace) -> None:
    """Run tests on an ephemeral RunPod pod (provision → deploy → test → teardown)."""
    from comfy_runner.testing.runpod import RunPodTestConfig, run_on_runpod

    on_overrun = getattr(args, "on_overrun", None)
    # Default ``terminate`` for runpod targets — the same default the
    # server applies for kind=="runpod".
    if on_overrun is None:
        on_overrun = "terminate"
    config = RunPodTestConfig(
        suite_path=args.suite,
        gpu_type=getattr(args, "gpu", None),
        image=getattr(args, "image", None),
        volume_id=getattr(args, "volume_id", None),
        pr=getattr(args, "pr", None),
        branch=getattr(args, "branch", None),
        commit=getattr(args, "commit", None),
        pod_name=getattr(args, "pod_name", None),
        timeout=args.timeout,
        http_timeout=args.http_timeout,
        formats=args.format,
        terminate=not getattr(args, "no_terminate", False),
        install_name=getattr(args, "install_name", None) or "main",
        max_runtime_s=getattr(args, "max_runtime", None),
        on_overrun=on_overrun,
    )

    send_output = None if args.json else (lambda t: console.print(t, end=""))

    try:
        result = run_on_runpod(config, send_output=send_output)
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    failed = (result.test_result or {}).get("failed", 0) if result.test_result else 0
    nonzero = bool(result.error or result.timed_out or (failed > 0))

    if args.json:
        output: dict[str, object] = {
            "ok": not nonzero,
            "pod_id": result.pod_id,
            "pod_name": result.pod_name,
            "server_url": result.server_url,
            "terminated": result.terminated,
            "timed_out": result.timed_out,
            "aborted_reason": result.aborted_reason,
        }
        if result.error:
            output["error"] = result.error
        if result.deploy_result:
            output["deploy"] = result.deploy_result
        if result.test_result:
            output["test"] = result.test_result
        print(json.dumps(output, indent=2))
    else:
        if result.error:
            console.print(f"\n[red]Error: {result.error}[/red]")
        elif result.test_result:
            tr = result.test_result
            total = tr.get("total", 0)
            passed = tr.get("passed", 0)
            failed = tr.get("failed", 0)
            console.print(f"\n[bold]Results:[/bold] {passed}/{total} passed", end="")
            if failed:
                console.print(f", [red]{failed} failed[/red]")
            else:
                console.print()
            if tr.get("output_dir"):
                console.print(f"  Output: {tr['output_dir']}")
            if result.timed_out:
                console.print(
                    "  [red]Aborted by watchdog (overrun).[/red]"
                )
        if result.terminated:
            console.print(f"  [dim]Pod '{result.pod_name}' terminated[/dim]")

    if nonzero:
        sys.exit(1)


def _test_list(args: argparse.Namespace) -> None:
    """List available test suites."""
    from comfy_runner.testing.suite import discover_suites

    search_dir = Path(args.dir) if args.dir else Path(".")
    suites = discover_suites(search_dir)

    if args.json:
        print(json.dumps({
            "ok": True,
            "suites": [
                {
                    "name": s.name,
                    "path": str(s.path),
                    "description": s.description,
                    "workflows": len(s.workflows),
                    "required_models": s.required_models,
                }
                for s in suites
            ],
        }, indent=2))
    else:
        if not suites:
            console.print("[dim]No test suites found.[/dim]")
            return
        table = Table(title="Test Suites")
        table.add_column("Name", style="cyan")
        table.add_column("Workflows", justify="right")
        table.add_column("Path", style="dim")
        table.add_column("Description")
        for s in suites:
            table.add_row(s.name, str(len(s.workflows)), str(s.path), s.description)
        console.print(table)


def _test_baseline(args: argparse.Namespace) -> None:
    """Approve test outputs as new baselines."""
    import shutil

    from comfy_runner.testing.suite import load_suite

    try:
        suite = load_suite(args.suite)
    except ValueError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        msg = f"Run directory not found: {run_dir}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, indent=2))
        else:
            console.print(f"[red]{msg}[/red]")
        sys.exit(1)

    # Determine which workflows to approve
    if args.approve_all:
        workflow_names = [wf.stem for wf in suite.workflows]
    elif args.workflow:
        # Sanitize to prevent path traversal from user input
        from safe_file import is_safe_path_component
        if not is_safe_path_component(args.workflow):
            msg = f"Invalid workflow name: {args.workflow!r}"
            if args.json:
                print(json.dumps({"ok": False, "error": msg}, indent=2))
            else:
                console.print(f"[red]{msg}[/red]")
            sys.exit(1)
        workflow_names = [args.workflow]
    else:
        msg = "Specify --workflow <name> or --approve-all"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, indent=2))
        else:
            console.print(f"[red]{msg}[/red]")
        sys.exit(1)

    baselines_dir = suite.baselines_dir
    approved: list[str] = []
    skipped: list[str] = []
    collisions: list[str] = []

    for wf_name in workflow_names:
        wf_run_dir = run_dir / wf_name
        if not wf_run_dir.is_dir():
            skipped.append(wf_name)
            continue

        # Collect all output files from subdirectories (node_id dirs)
        output_files = [f for f in wf_run_dir.rglob("*") if f.is_file()]
        if not output_files:
            skipped.append(wf_name)
            continue

        bl_dir = baselines_dir / wf_name
        bl_dir.mkdir(parents=True, exist_ok=True)

        seen_names: set[str] = set()
        for src in output_files:
            safe_name = src.name
            if safe_name in seen_names:
                collisions.append(f"{wf_name}/{safe_name}")
            seen_names.add(safe_name)
            dest = bl_dir / safe_name
            shutil.copy2(str(src), str(dest))

        approved.append(wf_name)

    if args.json:
        result: dict[str, object] = {
            "ok": True,
            "approved": approved,
            "skipped": skipped,
        }
        if collisions:
            result["collisions"] = collisions
        print(json.dumps(result, indent=2))
    else:
        for name in approved:
            console.print(f"  [green]✓[/green] {name}")
        for name in skipped:
            console.print(f"  [dim]- {name} (no outputs)[/dim]")
        for c in collisions:
            console.print(f"  [yellow]⚠ collision: {c} (last copy wins)[/yellow]")
        if approved:
            console.print(f"\n[green]Approved {len(approved)} baseline(s)[/green]")
        else:
            console.print("[yellow]No baselines approved[/yellow]")


def _test_report(args: argparse.Namespace) -> None:
    """Regenerate reports from a previous run's summary.json."""
    from comfy_runner.testing.report import (
        SuiteReport,
        write_report,
    )

    run_dir = Path(args.run_dir)
    summary_path = run_dir / "summary.json"
    report_path = run_dir / "report.json"

    # Try report.json first (richer), fall back to summary.json
    source = report_path if report_path.is_file() else summary_path
    if not source.is_file():
        msg = f"No summary.json or report.json in {run_dir}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, indent=2))
        else:
            console.print(f"[red]{msg}[/red]")
        sys.exit(1)

    try:
        with open(source, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        msg = f"Failed to read {source}: {e}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, indent=2))
        else:
            console.print(f"[red]{msg}[/red]")
        sys.exit(1)

    # Build a SuiteReport from the JSON data
    try:
        if "suite_name" in data:
            # It's a full report.json
            report = SuiteReport(**{
                k: data[k] for k in ("suite_name", "timestamp", "duration",
                                      "total", "passed", "failed")
            })
        else:
            # It's a summary.json (simpler)
            from datetime import datetime, timezone
            report = SuiteReport(
                suite_name=data.get("suite", "unknown"),
                timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                duration=data.get("duration", 0),
                total=data.get("total", 0),
                passed=data.get("passed", 0),
                failed=data.get("failed", 0),
            )
    except (KeyError, TypeError) as e:
        msg = f"Malformed report data in {source}: {e}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, indent=2))
        else:
            console.print(f"[red]{msg}[/red]")
        sys.exit(1)

    formats = [f.strip() for f in args.format.split(",")]
    written = write_report(report, run_dir, formats=formats)

    if args.json:
        print(json.dumps({
            "ok": True,
            "files": {k: str(v) for k, v in written.items()},
        }, indent=2))
    else:
        for fmt, path in written.items():
            console.print(f"  [green]✓[/green] {fmt}: {path}")


def _run_id() -> str:
    """Generate a timestamped run ID."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _test_fleet(args: argparse.Namespace) -> None:
    """Run a test suite across multiple targets in parallel."""
    from comfy_runner.testing.fleet import (
        parse_target_spec,
        render_fleet_console,
        run_fleet,
    )

    # Parse target specs
    target_specs: list[str] = args.target
    if not target_specs:
        msg = "At least one --target is required"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, indent=2))
        else:
            console.print(f"[red]Error: {msg}[/red]")
        sys.exit(1)

    targets = []
    for spec in target_specs:
        try:
            target = parse_target_spec(spec)
        except ValueError as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            else:
                console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

        # Apply shared deploy options to ephemeral targets
        from comfy_runner.testing.fleet import EphemeralTarget
        if isinstance(target, EphemeralTarget):
            if getattr(args, "pr", None) is not None:
                target._pr = args.pr
            elif getattr(args, "branch", None):
                target._branch = args.branch
            elif getattr(args, "commit", None):
                target._commit = args.commit

        targets.append(target)

    out_dir = Path(args.output) if args.output else Path(args.suite) / "runs" / f"fleet-{_run_id()}"
    send_output = None if args.json else (lambda t: console.print(t, end=""))

    # Fleet-level watchdog: --max-runtime overrides suite.json.
    from comfy_runner.testing.client import watchdog as _watchdog
    from comfy_runner.testing.suite import load_suite as _load_suite_for_budget
    budget = getattr(args, "max_runtime", None)
    if budget is None:
        try:
            _suite = _load_suite_for_budget(args.suite)
            if isinstance(_suite.max_runtime_s, int):
                budget = _suite.max_runtime_s
        except Exception:
            pass

    try:
        with _watchdog(budget) as cancelled:
            fleet_result = run_fleet(
                targets=targets,
                suite_path=args.suite,
                output_dir=out_dir,
                timeout=args.timeout,
                max_workers=getattr(args, "max_workers", None),
                send_output=send_output,
                formats=args.format,
                cancelled=cancelled,
            )
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        else:
            console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    timed_out = cancelled.is_set()

    if args.json:
        output = {
            "ok": fleet_result.targets_failed == 0 and not timed_out,
            "timed_out": timed_out,
        }
        output.update(fleet_result.to_dict())
        print(json.dumps(output, indent=2))
    else:
        console.print()
        console.print(render_fleet_console(fleet_result))
        console.print(f"\n  [dim]Output: {out_dir}[/dim]")
        if timed_out:
            console.print("  [red]Aborted by watchdog (overrun).[/red]")

    if fleet_result.targets_failed > 0 or timed_out:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Hosted commands
# ---------------------------------------------------------------------------

def cmd_hosted(args: argparse.Namespace) -> None:
    """Dispatch hosted subcommands."""
    action = getattr(args, "hosted_action", None)

    if action == "config":
        _hosted_config(args)
    elif action == "volume":
        _hosted_volume(args)
    elif action == "pod":
        _hosted_pod(args)
    elif action == "init":
        _hosted_init(args)
    elif action == "deploy":
        _hosted_deploy(args)
    elif action == "sysinfo":
        _hosted_sysinfo(args)
    elif action == "status":
        _hosted_status(args)
    elif action == "start-comfy":
        _hosted_start_comfy(args)
    elif action == "stop-comfy":
        _hosted_stop_comfy(args)
    elif action == "logs":
        _hosted_logs(args)
    else:
        args._parser_hosted.print_help()


_SENSITIVE_SUBSTRINGS = ("key", "secret", "token", "password")


def _redact_config(data: dict) -> dict:
    """Deep-copy a config dict, replacing sensitive values with '***'."""
    import copy
    out = copy.deepcopy(data)
    def _walk(d: dict) -> None:
        for k, v in d.items():
            if isinstance(v, dict):
                _walk(v)
            elif isinstance(v, str) and v and any(s in k.lower() for s in _SENSITIVE_SUBSTRINGS):
                d[k] = "***"
    _walk(out)
    return out


def _hosted_config(args: argparse.Namespace) -> None:
    """Handle hosted config show/set."""
    from comfy_runner.hosted.config import (
        get_hosted_config,
        set_provider_value,
    )

    config_action = getattr(args, "hosted_config_action", None)

    if config_action == "show":
        data = get_hosted_config()
        redacted = _redact_config(data)
        if args.json:
            print(json.dumps({"ok": True, "config": redacted}, indent=2))
        else:
            if not redacted:
                console.print("[dim]No hosted config set.[/dim]")
            else:
                console.print_json(json.dumps(redacted, indent=2))

    elif config_action == "set":
        key = args.key
        value = args.value
        # Key format: runpod.api_key → provider=runpod, key=api_key
        parts = key.split(".", 1)
        if len(parts) < 2:
            console.print("[red]Error: key must be in format provider.key (e.g. runpod.api_key)[/red]")
            sys.exit(1)
        provider, config_key = parts
        try:
            set_provider_value(provider, config_key, value)
        except ValueError as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            else:
                console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)
        if args.json:
            print(json.dumps({"ok": True}))
        else:
            # Redact sensitive values
            display = "***" if any(s in config_key.lower() for s in _SENSITIVE_SUBSTRINGS) else value
            console.print(f"Set [cyan]{provider}[/cyan].[cyan]{config_key}[/cyan] = {display}")
    else:
        args._parser_hosted_config.print_help()


def _hosted_volume(args: argparse.Namespace) -> None:
    """Handle hosted volume create/list/rm."""
    from comfy_runner.hosted.config import (
        get_volume_config,
        list_volume_configs,
        remove_volume_config,
        set_volume_config,
    )
    from comfy_runner.hosted.runpod_provider import RunPodProvider

    volume_action = getattr(args, "hosted_volume_action", None)

    if volume_action == "create":
        try:
            provider = RunPodProvider()
            vol = provider.create_volume(
                name=args.name,
                size_gb=args.size,
                datacenter=args.region,
            )
            set_volume_config("runpod", args.name, {
                "id": vol.id,
                "datacenter": vol.datacenter,
                "size_gb": vol.size_gb,
            })
            if args.json:
                print(json.dumps({"ok": True, "volume": {
                    "id": vol.id, "name": vol.name,
                    "datacenter": vol.datacenter, "size_gb": vol.size_gb,
                }}, indent=2))
            else:
                console.print(f"✓ Volume [cyan]{args.name}[/cyan] created (id: {vol.id}, {vol.datacenter}, {vol.size_gb} GB)")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif volume_action == "list":
        try:
            local_volumes = list_volume_configs("runpod")
            if args.json:
                # Also fetch live data from RunPod API
                try:
                    provider = RunPodProvider()
                    remote = [
                        {"id": v.id, "name": v.name, "datacenter": v.datacenter, "size_gb": v.size_gb}
                        for v in provider.list_volumes()
                    ]
                except Exception:
                    remote = []
                print(json.dumps({
                    "ok": True,
                    "local": local_volumes,
                    "remote": remote,
                }, indent=2))
            else:
                if not local_volumes:
                    console.print("[dim]No volumes configured.[/dim]")
                    return
                table = Table(title="Hosted Volumes (RunPod)")
                table.add_column("Name", style="cyan")
                table.add_column("ID", style="dim")
                table.add_column("Datacenter", style="green")
                table.add_column("Size (GB)", style="yellow", justify="right")
                for vname, vdata in local_volumes.items():
                    table.add_row(
                        vname,
                        vdata.get("id", ""),
                        vdata.get("datacenter", ""),
                        str(vdata.get("size_gb", "")),
                    )
                console.print(table)
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif volume_action == "rm":
        try:
            vol_cfg = get_volume_config("runpod", args.name)
            if not vol_cfg:
                raise RuntimeError(f"Volume '{args.name}' not found in config.")
            vol_id = vol_cfg["id"]
            remote_error = None
            if not args.keep_remote:
                try:
                    provider = RunPodProvider()
                    provider.delete_volume(vol_id)
                    if not args.json:
                        console.print(f"Deleted volume [cyan]{vol_id}[/cyan] from RunPod.")
                except Exception as e:
                    remote_error = str(e)
                    if not args.json:
                        console.print(f"[yellow]⚠ Remote deletion failed: {e}[/yellow]")
                        console.print("[dim]Removing local config anyway.[/dim]")
            remove_volume_config("runpod", args.name)
            if args.json:
                result: dict = {"ok": True}
                if remote_error:
                    result["warning"] = f"Remote deletion failed: {remote_error}"
                print(json.dumps(result, indent=2))
            else:
                console.print(f"✓ Volume [cyan]{args.name}[/cyan] removed.")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    else:
        args._parser_hosted_volume.print_help()


def _resolve_pod_id(raw: str) -> str:
    """Resolve a pod name to its RunPod ID, or return *raw* if it's already an ID."""
    from comfy_runner.hosted.config import get_pod_record
    rec = get_pod_record("runpod", raw)
    if rec:
        return rec["id"]
    return raw


def _build_pod_info(pod: Any, server_url: str, comfy_url: str, ts_url: Any) -> dict:
    """Build a JSON-serializable pod info dict (shared by init and pod create)."""
    info: dict = {
        "id": pod.id, "name": pod.name, "status": pod.status,
        "gpu_type": pod.gpu_type, "datacenter": pod.datacenter,
        "cost_per_hr": pod.cost_per_hr, "image": pod.image,
        "server_url": server_url, "comfy_url": comfy_url,
    }
    if isinstance(ts_url, str):
        info["tailscale_url"] = ts_url
    return info


def _hosted_pod(args: argparse.Namespace) -> None:
    """Handle hosted pod create/list/show/start/stop/terminate/url."""
    from comfy_runner.hosted.runpod_provider import RunPodProvider

    pod_action = getattr(args, "hosted_pod_action", None)

    # Resolve pod_id: accept a friendly name or raw ID
    if hasattr(args, "pod_id") and args.pod_id:
        args.pod_id = _resolve_pod_id(args.pod_id)

    if pod_action == "create":
        try:
            provider = RunPodProvider()
            # Resolve volume: --volume can be a config name or raw ID
            volume_id = None
            volume_name = None
            if args.volume:
                from comfy_runner.hosted.config import get_volume_config
                vol_cfg = get_volume_config("runpod", args.volume)
                if vol_cfg:
                    volume_id = vol_cfg["id"]
                    volume_name = args.volume
                else:
                    volume_id = args.volume

            cuda_versions = None
            if getattr(args, "cuda_versions", None):
                cuda_versions = [v.strip() for v in args.cuda_versions.split(",")]
            pod = provider.create_pod(
                name=args.name,
                gpu_type=args.gpu,
                image=args.image,
                volume_id=volume_id,
                datacenter=args.region,
                cloud_type=args.cloud_type,
                allowed_cuda_versions=cuda_versions,
                gpu_count=getattr(args, "gpu_count", 1) or 1,
            )
            # Save pod record for tracking
            from comfy_runner.hosted.config import set_pod_record
            record: dict = {
                "id": pod.id,
                "gpu_type": pod.gpu_type,
                "datacenter": pod.datacenter,
                "image": pod.image,
            }
            if volume_id:
                record["volume_id"] = volume_id
            if volume_name:
                record["volume_name"] = volume_name
            set_pod_record("runpod", args.name, record)

            # Tailscale-only access -- no public RunPod proxy URLs.
            server_url = provider.get_pod_tailscale_url(args.name, port=9189) or ""
            comfy_url = provider.get_pod_tailscale_url(args.name, port=8188) or ""
            pod_info = _build_pod_info(pod, server_url, comfy_url, server_url)
            if args.json:
                print(json.dumps({"ok": True, "pod": pod_info}, indent=2))
            else:
                console.print(f"Pod [cyan]{pod.name}[/cyan] created (id: {pod.id}, {pod.gpu_type}, ${pod.cost_per_hr}/hr)")
                if server_url:
                    console.print(f"  Server:    {server_url}")
                    console.print(f"  ComfyUI:   {comfy_url}")
                else:
                    console.print(
                        "  [yellow]Tailscale not configured -- set "
                        "tailscale_auth_key (and tailscale_domain) in "
                        "the runpod provider config to get a URL.[/yellow]",
                    )
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "list":
        try:
            provider = RunPodProvider()
            pods = provider.list_pods()
            if args.json:
                print(json.dumps({"ok": True, "pods": [
                    {"id": p.id, "name": p.name, "status": p.status,
                     "gpu_type": p.gpu_type, "datacenter": p.datacenter,
                     "cost_per_hr": p.cost_per_hr, "image": p.image}
                    for p in pods
                ]}, indent=2))
            else:
                if not pods:
                    console.print("[dim]No pods found.[/dim]")
                    return
                table = Table(title="RunPod Pods")
                table.add_column("Name", style="cyan")
                table.add_column("ID", style="dim")
                table.add_column("Status", style="bold")
                table.add_column("GPU", style="green")
                table.add_column("Datacenter", style="yellow")
                table.add_column("$/hr", justify="right")
                for p in pods:
                    status_style = "green" if p.status == "RUNNING" else "red" if p.status == "EXITED" else "yellow"
                    table.add_row(
                        p.name, p.id,
                        f"[{status_style}]{p.status}[/{status_style}]",
                        p.gpu_type, p.datacenter,
                        f"{p.cost_per_hr:.2f}",
                    )
                console.print(table)
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "show":
        try:
            provider = RunPodProvider()
            pod = provider.get_pod(args.pod_id)
            if not pod:
                raise RuntimeError(f"Pod '{args.pod_id}' not found.")
            if args.json:
                print(json.dumps({"ok": True, "pod": {
                    "id": pod.id, "name": pod.name, "status": pod.status,
                    "gpu_type": pod.gpu_type, "datacenter": pod.datacenter,
                    "cost_per_hr": pod.cost_per_hr, "image": pod.image,
                }}, indent=2))
            else:
                status_style = "green" if pod.status == "RUNNING" else "red" if pod.status == "EXITED" else "yellow"
                console.print(f"[cyan]{pod.name}[/cyan] ({pod.id})")
                console.print(f"  Status:     [{status_style}]{pod.status}[/{status_style}]")
                console.print(f"  GPU:        {pod.gpu_type}")
                console.print(f"  Datacenter: {pod.datacenter}")
                console.print(f"  Image:      {pod.image}")
                console.print(f"  Cost:       ${pod.cost_per_hr:.2f}/hr")
                if pod.status == "RUNNING":
                    url = provider.get_pod_tailscale_url(pod.name, port=8188)
                    if url:
                        console.print(f"  ComfyUI:    [link={url}]{url}[/link]")
                    else:
                        console.print(
                            "  ComfyUI:    [dim](no Tailscale URL -- "
                            "configure tailscale_auth_key)[/dim]",
                        )
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "start":
        try:
            provider = RunPodProvider()
            pod = provider.start_pod(args.pod_id)
            if args.json:
                print(json.dumps({"ok": True, "pod": {
                    "id": pod.id, "name": pod.name, "status": pod.status,
                }}, indent=2))
            else:
                console.print(f"✓ Pod [cyan]{pod.name}[/cyan] started ({pod.id})")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "stop":
        try:
            provider = RunPodProvider()
            provider.stop_pod(args.pod_id)
            if args.json:
                print(json.dumps({"ok": True}, indent=2))
            else:
                console.print(f"✓ Pod [cyan]{args.pod_id}[/cyan] stopped.")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "terminate":
        try:
            provider = RunPodProvider()
            provider.terminate_pod(args.pod_id)
            # Remove pod record if tracked by this ID
            from comfy_runner.hosted.config import list_pod_records, remove_pod_record
            for pname, prec in list_pod_records("runpod").items():
                if prec.get("id") == args.pod_id:
                    remove_pod_record("runpod", pname)
                    break
            if args.json:
                print(json.dumps({"ok": True}, indent=2))
            else:
                console.print(f"✓ Pod [cyan]{args.pod_id}[/cyan] terminated.")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "url":
        try:
            provider = RunPodProvider()
            port = args.port or 8188
            url = provider.get_pod_url(args.pod_id, port)
            if args.json:
                print(json.dumps({"ok": True, "url": url}, indent=2))
            else:
                if url:
                    console.print(f"[link={url}]{url}[/link]")
                else:
                    console.print("[dim]Pod is not running — no URL available.[/dim]")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    else:
        args._parser_hosted_pod.print_help()


def _hosted_init(args: argparse.Namespace) -> None:
    """Create a volume + pod in one shot, ready to receive API commands."""
    from comfy_runner.hosted.config import (
        get_volume_config,
        set_pod_record,
        set_volume_config,
    )
    from comfy_runner.hosted.runpod_provider import RunPodProvider

    try:
        provider = RunPodProvider()

        # Resolve or create volume
        volume_id = None
        volume_name = args.volume
        if volume_name:
            vol_cfg = get_volume_config("runpod", volume_name)
            if vol_cfg:
                volume_id = vol_cfg["id"]
                if not args.json:
                    console.print(f"Using existing volume [cyan]{volume_name}[/cyan] ({volume_id})")
            else:
                # Create a new volume
                size = args.volume_size or 50
                if not args.json:
                    console.print(f"Creating volume [cyan]{volume_name}[/cyan] ({size} GB)...")
                vol = provider.create_volume(
                    name=volume_name,
                    size_gb=size,
                    datacenter=args.region,
                )
                volume_id = vol.id
                set_volume_config("runpod", volume_name, {
                    "id": vol.id,
                    "datacenter": vol.datacenter,
                    "size_gb": vol.size_gb,
                })
                if not args.json:
                    console.print(f"✓ Volume created (id: {vol.id}, {vol.datacenter}, {vol.size_gb} GB)")

        # Create pod
        if not args.json:
            console.print(f"Creating pod [cyan]{args.name}[/cyan]...")
        cuda_versions = None
        if getattr(args, "cuda_versions", None):
            cuda_versions = [v.strip() for v in args.cuda_versions.split(",")]
        pod = provider.create_pod(
            name=args.name,
            gpu_type=args.gpu,
            image=args.image,
            volume_id=volume_id,
            datacenter=args.region,
            cloud_type=args.cloud_type,
            allowed_cuda_versions=cuda_versions,
        )

        # Save pod record
        record: dict = {
            "id": pod.id,
            "gpu_type": pod.gpu_type,
            "datacenter": pod.datacenter,
            "image": pod.image,
        }
        if volume_id:
            record["volume_id"] = volume_id
        if volume_name:
            record["volume_name"] = volume_name
        set_pod_record("runpod", args.name, record)

        # Tailscale-only access -- no public RunPod proxy URLs.
        server_url = provider.get_pod_tailscale_url(args.name, port=9189) or ""
        comfy_url = provider.get_pod_tailscale_url(args.name, port=8188) or ""
        pod_info = _build_pod_info(pod, server_url, comfy_url, server_url)

        if args.json:
            result: dict = {"ok": True, "pod": pod_info}
            if volume_id:
                result["volume"] = {"id": volume_id, "name": volume_name}
            print(json.dumps(result, indent=2))
        else:
            console.print(f"Pod [cyan]{args.name}[/cyan] created (id: {pod.id}, {pod.gpu_type}, ${pod.cost_per_hr}/hr)")
            if server_url:
                console.print(f"  Server:    {server_url}")
                console.print(f"  ComfyUI:   {comfy_url}")
            else:
                console.print(
                    "  [yellow]Tailscale not configured -- set "
                    "tailscale_auth_key (and tailscale_domain) in the "
                    "runpod provider config to get a URL.[/yellow]",
                )
            console.print()
            console.print("[dim]The pod is booting comfy-runner server. Once ready, use the server URL[/dim]")
            console.print("[dim]to deploy, start ComfyUI, etc. via the API.[/dim]")

    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def _resolve_server_url(pod_name: str) -> str:
    """Resolve a pod name to its comfy-runner server URL.

    Pods are reachable only via the Tailscale tunnel; the RunPod public
    proxy is no longer enabled. Raises ``RuntimeError`` if Tailscale
    isn't configured (no auth key / domain).
    """
    from comfy_runner.hosted.config import get_pod_record
    rec = get_pod_record("runpod", pod_name)
    if not rec:
        raise RuntimeError(
            f"No pod record for '{pod_name}'. "
            f"Create one with 'hosted init' or 'hosted pod create'."
        )
    from comfy_runner.hosted.runpod_provider import RunPodProvider
    try:
        provider = RunPodProvider()
        ts_url = provider.get_pod_tailscale_url(pod_name)
        if isinstance(ts_url, str):
            return ts_url
    except RuntimeError:
        pass
    raise RuntimeError(
        f"Cannot resolve server URL for pod '{pod_name}' -- Tailscale "
        f"is not configured. Set tailscale_auth_key (and tailscale_domain) "
        f"in the runpod provider config."
    )


def _hosted_deploy(args: argparse.Namespace) -> None:
    """Deploy a PR/branch/tag/commit to a hosted pod."""
    from comfy_runner.hosted.remote import RemoteRunner

    try:
        runner = RemoteRunner(_resolve_server_url(args.pod_name))
        install_name = getattr(args, "install_name", None) or "main"

        data = runner.deploy(
            install_name,
            pr=args.pr,
            branch=args.branch,
            tag=args.tag,
            commit=args.commit,
            reset=args.reset,
            start=getattr(args, "start", False),
            launch_args=getattr(args, "launch_args", None),
            cuda_compat=getattr(args, "cuda_compat", False),
        )

        job_id = data.get("job_id")
        if not job_id:
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                console.print("✓ Deploy completed (sync)")
            return

        if not args.json:
            console.print(f"Deploy started (job: {job_id}). Polling...")

        result = runner.poll_job(job_id, on_output=None if args.json else _output)

        if args.json:
            print(json.dumps({"ok": True, "job_id": job_id, "result": result}, indent=2))
        else:
            console.print(f"\n✓ Deploy complete.")
            if result.get("port"):
                console.print(f"  ComfyUI running on port {result['port']}")

    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def _hosted_sysinfo(args: argparse.Namespace) -> None:
    """Show system/hardware info from a hosted pod."""
    from comfy_runner.hosted.remote import RemoteRunner

    try:
        runner = RemoteRunner(_resolve_server_url(args.pod_name))
        info = runner.get_system_info()

        if args.json:
            print(json.dumps({"ok": True, "system_info": info}, indent=2))
        else:
            console.print(f"[bold]OS:[/bold]       {info.get('os_distro', '?')}")
            console.print(f"[bold]Arch:[/bold]     {info.get('arch', '?')}")
            console.print(f"[bold]CPU:[/bold]      {info.get('cpu_model', '?')} ({info.get('cpu_cores', '?')} cores)")
            console.print(f"[bold]Memory:[/bold]   {info.get('total_memory_gb', '?')} GB")
            driver = info.get("nvidia_driver_version", "N/A")
            console.print(f"[bold]Driver:[/bold]   {driver}")
            for gpu in info.get("gpus", []):
                vram = gpu.get("vram_mb", 0)
                vram_gb = f"{vram / 1024:.1f} GB" if vram else "?"
                console.print(f"[bold]GPU:[/bold]      {gpu.get('model', '?')} ({vram_gb})")

    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def _hosted_status(args: argparse.Namespace) -> None:
    """Show status of a hosted pod's installations."""
    from comfy_runner.hosted.remote import RemoteRunner

    try:
        runner = RemoteRunner(_resolve_server_url(args.pod_name))
        install_name = getattr(args, "install_name", None)

        if install_name:
            data = runner.get_status(install_name)
        else:
            data = {"ok": True, "installations": runner.list_installations()}

        if args.json:
            print(json.dumps(data, indent=2))
        else:
            if install_name:
                running = data.get("running", False)
                status_str = "[green]RUNNING[/green]" if running else "[dim]stopped[/dim]"
                console.print(f"[cyan]{install_name}[/cyan]: {status_str}")
                if data.get("port"):
                    console.print(f"  Port: {data['port']}")
                if data.get("pid"):
                    console.print(f"  PID:  {data['pid']}")
            else:
                installs = data.get("installations", [])
                if not installs:
                    console.print("[dim]No installations on this pod.[/dim]")
                else:
                    for inst in installs:
                        status = inst.get("_status", {})
                        running = status.get("running", False)
                        status_str = "[green]RUNNING[/green]" if running else "[dim]stopped[/dim]"
                        console.print(f"  [cyan]{inst.get('name', '?')}[/cyan]: {status_str}")

    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def _hosted_start_comfy(args: argparse.Namespace) -> None:
    """Restart ComfyUI on a hosted pod."""
    from comfy_runner.hosted.remote import RemoteRunner

    try:
        runner = RemoteRunner(_resolve_server_url(args.pod_name))
        install_name = getattr(args, "install_name", None) or "main"

        data = runner.restart(install_name)
        job_id = data.get("job_id")

        if job_id:
            if not args.json:
                console.print(f"Starting ComfyUI (job: {job_id})...")
            result = runner.poll_job(job_id, on_output=None if args.json else _output)
            if args.json:
                print(json.dumps({"ok": True, "job_id": job_id, "result": result}, indent=2))
            else:
                console.print(f"\n✓ ComfyUI started.")
                if result.get("port"):
                    console.print(f"  Port: {result['port']}")
        else:
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                console.print("✓ ComfyUI started.")

    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def _hosted_stop_comfy(args: argparse.Namespace) -> None:
    """Stop ComfyUI on a hosted pod."""
    from comfy_runner.hosted.remote import RemoteRunner

    try:
        runner = RemoteRunner(_resolve_server_url(args.pod_name))
        install_name = getattr(args, "install_name", None) or "main"

        data = runner.stop(install_name)
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            was_running = data.get("was_running", False)
            if was_running:
                console.print("✓ ComfyUI stopped.")
            else:
                console.print("[dim]ComfyUI was not running.[/dim]")

    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def _hosted_logs(args: argparse.Namespace) -> None:
    """Show logs from a hosted pod's ComfyUI instance."""
    from comfy_runner.hosted.remote import RemoteRunner

    try:
        runner = RemoteRunner(_resolve_server_url(args.pod_name))
        install_name = getattr(args, "install_name", None) or "main"

        data = runner._request("GET", f"/{install_name}/logs")
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            lines = data.get("lines", [])
            if not lines:
                console.print("[dim]No logs available.[/dim]")
            else:
                for line in lines:
                    console.print(line, end="", highlight=False)

    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Station helpers
# ---------------------------------------------------------------------------

def _find_station_config() -> dict:
    """Find and load station.json by walking up from cwd.

    Raises RuntimeError if not found.
    """
    p = Path.cwd()
    while True:
        candidate = p / "station.json"
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
        parent = p.parent
        if parent == p:
            break
        p = parent
    raise RuntimeError(
        "station.json not found. Run this from a comfy-runner-station directory."
    )


def _station_server(args: argparse.Namespace) -> str:
    """Return the central server URL from station.json or --server flag."""
    server = getattr(args, "server", None)
    if server:
        return server
    config = _find_station_config()
    return config["central_server"]


def _station_config(args: argparse.Namespace) -> dict:
    """Return the full station config."""
    return _find_station_config()


def _resolve_suite_path(suite_name: str) -> str:
    """Resolve a suite name to a full path.

    Checks: ./test-suites/{name}, then treats as literal path.
    """
    # Check test-suites/ relative to station.json location
    p = Path.cwd()
    while True:
        if (p / "station.json").is_file():
            candidate = p / "test-suites" / suite_name
            if (candidate / "suite.json").is_file():
                return str(candidate.resolve())
            break
        parent = p.parent
        if parent == p:
            break
        p = parent
    # Literal path
    literal = Path(suite_name)
    if (literal / "suite.json").is_file():
        return str(literal.resolve())
    raise RuntimeError(f"Suite not found: {suite_name}")


def _parse_station_target(spec: str) -> dict:
    """Parse a target spec string into a target dict for the API.

    Formats: local:<url>, remote:<pod_name>, runpod:<gpu_type>
    """
    if spec.startswith("local:"):
        url = spec[6:]
        if "://" not in url:
            url = f"http://{url}"
        return {"kind": "local", "url": url}
    elif spec.startswith("remote:"):
        value = spec[7:]
        if "://" in value:
            return {"kind": "remote", "server_url": value}
        return {"kind": "remote", "pod_name": value}
    elif spec.startswith("runpod:"):
        return {"kind": "runpod", "gpu_type": spec[7:]}
    else:
        # Default: treat as local URL
        url = spec
        if "://" not in url:
            url = f"http://{url}"
        return {"kind": "local", "url": url}


def _push_suite_to_server(runner, suite_name: str, suite_path: str) -> dict:
    """Upload a local suite to the central server.

    Reads suite.json, config.json, and workflows/*.json from the local
    suite directory and POSTs them to /suites/{name}.
    """
    import json as _json
    sp = Path(suite_path)

    suite_meta = _json.loads((sp / "suite.json").read_text(encoding="utf-8"))

    config = {}
    config_path = sp / "config.json"
    if config_path.is_file():
        config = _json.loads(config_path.read_text(encoding="utf-8"))

    workflows = {}
    wf_dir = sp / "workflows"
    if wf_dir.is_dir():
        for wf in sorted(wf_dir.glob("*.json")):
            workflows[wf.name] = _json.loads(wf.read_text(encoding="utf-8"))

    body = {"suite": suite_meta, "config": config, "workflows": workflows}
    return runner._request("POST", f"/suites/{suite_name}", json=body)


# ---------------------------------------------------------------------------
# Station commands
# ---------------------------------------------------------------------------

def cmd_station(args: argparse.Namespace) -> None:
    """Dispatch station subcommands."""
    action = getattr(args, "station_action", None)
    if action == "pods":
        _station_pods(args)
    elif action == "tests":
        _station_tests(args)
    elif action == "dashboard":
        _station_dashboard(args)
    elif action == "jobs":
        _station_jobs(args)
    elif action == "info":
        _station_info(args)
    elif action == "suites":
        _station_suites(args)
    else:
        args._parser_station.print_help()


def _station_pods(args: argparse.Namespace) -> None:
    """Handle station pods subcommands."""
    from comfy_runner.hosted.remote import RemoteRunner

    pod_action = getattr(args, "station_pod_action", None)

    if pod_action is None or pod_action == "list":
        # GET /pods
        try:
            runner = RemoteRunner(_station_server(args))
            data = runner._request("GET", "/pods")
            pods = data.get("pods", [])
            if args.json:
                print(json.dumps({"ok": True, "pods": pods}, indent=2))
                return
            if not pods:
                console.print("[dim]No pods configured.[/dim]")
                return
            table = Table(title="Pods")
            table.add_column("Name", style="cyan")
            table.add_column("Status", style="bold")
            table.add_column("GPU")
            table.add_column("$/hr")
            table.add_column("Server URL", style="dim")
            for p in pods:
                status = p.get("status", "?")
                status_style = {"RUNNING": "green", "EXITED": "yellow", "TERMINATED": "red"}.get(status, "dim")
                cost = f"${p.get('cost_per_hr', 0):.2f}" if p.get("cost_per_hr") else "-"
                url = p.get("server_url", "") or "-"
                table.add_row(p["name"], f"[{status_style}]{status}[/{status_style}]", p.get("gpu_type", ""), cost, url)
            console.print(table)
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "create":
        try:
            runner = RemoteRunner(_station_server(args))
            config = _station_config(args)
            defaults = config.get("defaults", {})
            body = {"name": args.pod_name}
            if getattr(args, "gpu", None):
                body["gpu_type"] = args.gpu
            elif defaults.get("gpu_type"):
                body["gpu_type"] = defaults["gpu_type"]
            if getattr(args, "image", None):
                body["image"] = args.image
            if getattr(args, "datacenter", None):
                body["datacenter"] = args.datacenter
            elif defaults.get("datacenter"):
                body["datacenter"] = defaults["datacenter"]
            if getattr(args, "no_wait", False):
                body["wait_ready"] = False

            data = runner._request("POST", "/pods/create", json=body)
            job_id = data.get("job_id")
            if not args.json:
                console.print(f"Creating pod [cyan]{args.pod_name}[/cyan] (job: {job_id})...")
            result = runner.poll_job(job_id, timeout=600, on_output=None if args.json else _output)
            if args.json:
                print(json.dumps({"ok": True, "job_id": job_id, "result": result}, indent=2))
            else:
                console.print(f"\n✓ Pod [cyan]{args.pod_name}[/cyan] ready.")
                if result.get("server_url"):
                    console.print(f"  Server: {result['server_url']}")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "deploy":
        try:
            runner = RemoteRunner(_station_server(args))
            body = {}
            if getattr(args, "pr", None) is not None:
                body["pr"] = args.pr
            if getattr(args, "branch", None):
                body["branch"] = args.branch
            if getattr(args, "tag", None):
                body["tag"] = args.tag
            if getattr(args, "commit", None):
                body["commit"] = args.commit
            if getattr(args, "reset", False):
                body["reset"] = True
            if getattr(args, "latest", False):
                body["latest"] = True
            if getattr(args, "pull_deploy", False):
                body["pull"] = True

            data = runner._request("POST", f"/pods/{args.pod_name}/deploy", json=body)
            job_id = data.get("job_id")
            if not args.json:
                console.print(f"Deploying to [cyan]{args.pod_name}[/cyan] (job: {job_id})...")
            result = runner.poll_job(job_id, timeout=600, on_output=None if args.json else _output)
            if args.json:
                print(json.dumps({"ok": True, "job_id": job_id, "result": result}, indent=2))
            else:
                console.print(f"\n✓ Deploy to [cyan]{args.pod_name}[/cyan] complete.")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "stop":
        try:
            runner = RemoteRunner(_station_server(args))
            runner._request("POST", f"/pods/{args.pod_name}/stop")
            if args.json:
                print(json.dumps({"ok": True}))
            else:
                console.print(f"✓ Pod [cyan]{args.pod_name}[/cyan] stopped.")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "start":
        try:
            runner = RemoteRunner(_station_server(args))
            data = runner._request("POST", f"/pods/{args.pod_name}/start")
            job_id = data.get("job_id")
            if not args.json:
                console.print(f"Starting pod [cyan]{args.pod_name}[/cyan] (job: {job_id})...")
            result = runner.poll_job(job_id, timeout=600, on_output=None if args.json else _output)
            if args.json:
                print(json.dumps({"ok": True, "job_id": job_id, "result": result}, indent=2))
            else:
                console.print(f"\n✓ Pod [cyan]{args.pod_name}[/cyan] started.")
                if result.get("server_url"):
                    console.print(f"  Server: {result['server_url']}")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "terminate":
        try:
            runner = RemoteRunner(_station_server(args))
            runner._request("DELETE", f"/pods/{args.pod_name}")
            if args.json:
                print(json.dumps({"ok": True}))
            else:
                console.print(f"✓ Pod [cyan]{args.pod_name}[/cyan] terminated.")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "launch-pr":
        try:
            runner = RemoteRunner(_station_server(args))
            config = _station_config(args)
            defaults = config.get("defaults", {})
            body: dict = {"pr": args.pr}
            if getattr(args, "repo", None):
                body["repo"] = args.repo
            if getattr(args, "gpu", None):
                body["gpu_type"] = args.gpu
            elif defaults.get("gpu_type"):
                body["gpu_type"] = defaults["gpu_type"]
            if getattr(args, "datacenter", None):
                body["datacenter"] = args.datacenter
            elif defaults.get("datacenter"):
                body["datacenter"] = defaults["datacenter"]
            if getattr(args, "image", None):
                body["image"] = args.image
            if getattr(args, "install", None):
                body["install"] = args.install
            if getattr(args, "idle_timeout", None) is not None:
                body["idle_timeout_s"] = args.idle_timeout
            data = runner._request("POST", "/pods/launch-pr", json=body)
            job_id = data.get("job_id")
            pod_name = data.get("name")
            if not args.json:
                console.print(
                    f"Launching PR #[cyan]{args.pr}[/cyan] on pod "
                    f"[cyan]{pod_name}[/cyan] (job: {job_id})...",
                )
            result = runner.poll_job(
                job_id, timeout=900,
                on_output=None if args.json else _output,
            )
            if args.json:
                print(json.dumps({"ok": True, "job_id": job_id, "result": result}, indent=2))
            else:
                console.print(f"\n✓ PR #[cyan]{args.pr}[/cyan] ready on pod [cyan]{pod_name}[/cyan].")
                if result.get("server_url"):
                    console.print(f"  Server:   {result['server_url']}")
                if result.get("comfy_url"):
                    console.print(f"  ComfyUI:  {result['comfy_url']}")
                if result.get("idle_timeout_s"):
                    console.print(f"  Idle in:  {result['idle_timeout_s']}s of inactivity")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "touch":
        try:
            runner = RemoteRunner(_station_server(args))
            data = runner._request("POST", f"/pods/{args.pod_name}/touch")
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                console.print(
                    f"✓ Pod [cyan]{args.pod_name}[/cyan] touched. "
                    f"Idle in {data.get('idle_in_s', '?')}s.",
                )
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif pod_action == "cleanup":
        try:
            runner = RemoteRunner(_station_server(args))
            prefix = getattr(args, "prefix", "test-")
            dry_run = getattr(args, "dry_run", False)
            body = {"prefix": prefix, "dry_run": dry_run}
            data = runner._request("POST", "/pods/cleanup", json=body)
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                found = data.get("total_found", 0)
                terminated = data.get("total_terminated", 0)
                if dry_run:
                    console.print(f"[yellow]Dry run:[/yellow] found {found} pod(s) matching prefix '{prefix}'")
                    for p in data.get("skipped", []):
                        console.print(f"  {p['name']} ({p.get('status', '?')})")
                elif terminated > 0:
                    console.print(f"[green]✓ Terminated {terminated} pod(s)[/green] matching prefix '{prefix}'")
                    for p in data.get("terminated", []):
                        console.print(f"  {p['name']}")
                else:
                    console.print(f"[dim]No pods found matching prefix '{prefix}'[/dim]")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    else:
        args._parser_station_pods.print_help()


def _station_tests(args: argparse.Namespace) -> None:
    """Handle station tests subcommands."""
    from comfy_runner.hosted.remote import RemoteRunner

    test_action = getattr(args, "station_test_action", None)

    if test_action is None or test_action == "list":
        try:
            runner = RemoteRunner(_station_server(args))
            data = runner._request("GET", "/tests")
            runs = data.get("runs", [])
            if args.json:
                print(json.dumps({"ok": True, "runs": runs}, indent=2))
                return
            if not runs:
                console.print("[dim]No test runs.[/dim]")
                return
            table = Table(title="Test Runs")
            table.add_column("ID", style="cyan")
            table.add_column("Kind")
            table.add_column("Status", style="bold")
            table.add_column("Suite")
            table.add_column("Targets")
            for r in runs:
                status = r.get("status", "?")
                status_style = {"running": "yellow", "done": "green", "error": "red"}.get(status, "dim")
                suite_name = Path(r.get("suite", "")).name if r.get("suite") else "-"
                targets = str(len(r.get("targets", [])))
                table.add_row(r["id"], r.get("kind", "?"), f"[{status_style}]{status}[/{status_style}]", suite_name, targets)
            console.print(table)
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif test_action == "run":
        try:
            runner = RemoteRunner(_station_server(args))
            suite_path = _resolve_suite_path(args.suite)
            if not args.json:
                console.print(f"Pushing suite [cyan]{args.suite}[/cyan] to server...")
            _push_suite_to_server(runner, args.suite, suite_path)

            # Parse target spec
            spec = args.target
            target = _parse_station_target(spec)

            body = {"suite": args.suite, "target": target}
            if getattr(args, "timeout", None):
                body["timeout"] = args.timeout
            if getattr(args, "max_runtime", None) is not None:
                body["max_runtime_s"] = args.max_runtime
            if getattr(args, "on_overrun", None) is not None:
                body["on_overrun"] = args.on_overrun

            data = runner._request("POST", "/tests/run", json=body)
            job_id = data.get("job_id")
            if not args.json:
                console.print(f"Test started (job: {job_id})...")
            result = runner.poll_job(job_id, timeout=3600, on_output=None if args.json else _output)
            timed_out = bool(result.get("timed_out"))
            failed = result.get("failed", 0) or 0
            if args.json:
                print(json.dumps({
                    "ok": failed == 0 and not timed_out,
                    "job_id": job_id,
                    "timed_out": timed_out,
                    "result": result,
                }, indent=2))
            else:
                passed = result.get("passed", 0)
                total = result.get("total", 0) if result.get("total") else (passed + failed)
                duration = result.get("duration", 0) or 0
                if timed_out:
                    console.print(
                        f"\n[red]✗ Aborted by watchdog (overrun): "
                        f"{failed}/{total} failed[/red] ({duration:.1f}s)"
                    )
                elif failed == 0:
                    console.print(f"\n[green]✓ All {total} tests passed[/green] ({duration:.1f}s)")
                else:
                    console.print(f"\n[red]✗ {failed}/{total} tests failed[/red] ({duration:.1f}s)")
            if failed > 0 or timed_out:
                sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif test_action == "fleet":
        try:
            runner = RemoteRunner(_station_server(args))
            suite_path = _resolve_suite_path(args.suite)
            if not args.json:
                console.print(f"Pushing suite [cyan]{args.suite}[/cyan] to server...")
            _push_suite_to_server(runner, args.suite, suite_path)

            targets = [_parse_station_target(spec) for spec in args.target]

            body = {"suite": args.suite, "targets": targets}
            if getattr(args, "timeout", None):
                body["timeout"] = args.timeout
            if getattr(args, "max_workers", None):
                body["max_workers"] = args.max_workers
            if getattr(args, "max_runtime", None) is not None:
                body["max_runtime_s"] = args.max_runtime
            if getattr(args, "on_overrun", None) is not None:
                body["on_overrun"] = args.on_overrun

            data = runner._request("POST", "/tests/fleet", json=body)
            job_id = data.get("job_id")
            if not args.json:
                console.print(f"Fleet test started ({len(targets)} targets, job: {job_id})...")
            result = runner.poll_job(job_id, timeout=3600, on_output=None if args.json else _output)
            timed_out = bool(result.get("timed_out"))
            failed = result.get("targets_failed", 0) or 0
            if args.json:
                print(json.dumps({
                    "ok": failed == 0 and not timed_out,
                    "job_id": job_id,
                    "timed_out": timed_out,
                    "result": result,
                }, indent=2))
            else:
                passed = result.get("targets_passed", 0)
                total = result.get("total_targets", 0)
                duration = result.get("total_duration", 0) or 0
                if timed_out:
                    console.print(
                        f"\n[red]✗ Fleet aborted by watchdog (overrun): "
                        f"{failed}/{total} targets failed[/red] ({duration:.1f}s)"
                    )
                elif failed == 0:
                    console.print(f"\n[green]✓ All {total} targets passed[/green] ({duration:.1f}s)")
                else:
                    console.print(f"\n[red]✗ {failed}/{total} targets failed[/red] ({duration:.1f}s)")
            if failed > 0 or timed_out:
                sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif test_action == "status":
        try:
            runner = RemoteRunner(_station_server(args))
            data = runner._request("GET", f"/tests/{args.test_id}")
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                console.print_json(json.dumps(data, indent=2))
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif test_action == "report":
        try:
            runner = RemoteRunner(_station_server(args))
            data = runner._request("GET", f"/tests/{args.test_id}/report")
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                console.print_json(json.dumps(data, indent=2))
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    else:
        args._parser_station_tests.print_help()


def _station_dashboard(args: argparse.Namespace) -> None:
    """Open the station dashboard."""
    try:
        config = _station_config(args)
        url = config.get("dashboard_url", config["central_server"] + "/dashboard")
        if args.json:
            print(json.dumps({"ok": True, "url": url}))
        else:
            console.print(f"Dashboard: {url}")
            import webbrowser
            webbrowser.open(url)
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def _station_jobs(args: argparse.Namespace) -> None:
    """List active jobs on the central server."""
    from comfy_runner.hosted.remote import RemoteRunner
    try:
        runner = RemoteRunner(_station_server(args))
        data = runner._request("GET", "/jobs")
        jobs = data.get("jobs", [])
        if args.json:
            print(json.dumps({"ok": True, "jobs": jobs}, indent=2))
            return
        if not jobs:
            console.print("[dim]No active jobs.[/dim]")
            return
        table = Table(title="Active Jobs")
        table.add_column("ID", style="cyan")
        table.add_column("Status", style="bold")
        table.add_column("Label")
        for j in jobs:
            status = j.get("status", "?")
            status_style = {"running": "yellow", "done": "green", "error": "red"}.get(status, "dim")
            table.add_row(j["id"], f"[{status_style}]{status}[/{status_style}]", j.get("label", ""))
        console.print(table)
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def _station_info(args: argparse.Namespace) -> None:
    """Show station config and server status."""
    from comfy_runner.hosted.remote import RemoteRunner
    try:
        config = _station_config(args)
        server = config["central_server"]
        if args.json:
            result = {"ok": True, "station": config}
            try:
                runner = RemoteRunner(server)
                sys_info = runner.get_system_info()
                result["server_info"] = sys_info
                result["connected"] = True
            except Exception:
                result["connected"] = False
            print(json.dumps(result, indent=2))
        else:
            console.print(f"[bold]Central server:[/bold]  {server}")
            console.print(f"[bold]Tailnet domain:[/bold]  {config.get('tailnet_domain', '?')}")
            console.print(f"[bold]Dashboard:[/bold]       {config.get('dashboard_url', '?')}")
            defaults = config.get("defaults", {})
            if defaults:
                console.print(f"[bold]Default GPU:[/bold]     {defaults.get('gpu_type', '?')}")
                console.print(f"[bold]Default DC:[/bold]      {defaults.get('datacenter', '?')}")
            console.print()
            try:
                runner = RemoteRunner(server)
                info = runner.get_system_info()
                console.print(f"[green]✓ Connected[/green]")
                console.print(f"  OS:     {info.get('os_distro', '?')}")
                console.print(f"  CPU:    {info.get('cpu_model', '?')}")
                for gpu in info.get("gpus", []):
                    vram = gpu.get("vram_mb", 0)
                    vram_str = f"{vram / 1024:.0f} GB" if vram else "?"
                    console.print(f"  GPU:    {gpu.get('model', '?')} ({vram_str})")
            except Exception as e:
                console.print(f"[yellow]✗ Not connected[/yellow]: {e}")
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def _station_suites(args: argparse.Namespace) -> None:
    """Handle station suites subcommands."""
    from comfy_runner.hosted.remote import RemoteRunner

    suite_action = getattr(args, "station_suite_action", None)

    if suite_action is None or suite_action == "list":
        try:
            runner = RemoteRunner(_station_server(args))
            data = runner._request("GET", "/suites")
            suites = data.get("suites", [])
            if args.json:
                print(json.dumps({"ok": True, "suites": suites}, indent=2))
                return
            if not suites:
                console.print("[dim]No suites on server.[/dim]")
                return
            table = Table(title="Test Suites")
            table.add_column("Name", style="cyan")
            table.add_column("Title")
            table.add_column("Workflows")
            table.add_column("Runs")
            table.add_column("Description", style="dim")
            for s in suites:
                table.add_row(
                    s["name"], s.get("title", ""), str(s.get("workflow_count", 0)),
                    str(s.get("run_count", 0)), s.get("description", "")
                )
            console.print(table)
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif suite_action == "push":
        try:
            runner = RemoteRunner(_station_server(args))
            if getattr(args, "all_suites", False):
                # Push all local suites
                config = _find_station_config()
                # Find station.json dir
                p = Path.cwd()
                while True:
                    if (p / "station.json").is_file():
                        break
                    parent = p.parent
                    if parent == p:
                        raise RuntimeError("station.json not found")
                    p = parent
                suites_dir = p / "test-suites"
                if not suites_dir.is_dir():
                    raise RuntimeError("No test-suites/ directory found")
                pushed = []
                for d in sorted(suites_dir.iterdir()):
                    if d.is_dir() and (d / "suite.json").is_file():
                        result = _push_suite_to_server(runner, d.name, str(d))
                        pushed.append(d.name)
                        if not args.json:
                            console.print(f"  ✓ {d.name}")
                if args.json:
                    print(json.dumps({"ok": True, "pushed": pushed}, indent=2))
                else:
                    console.print(f"\n[green]Pushed {len(pushed)} suite(s)[/green]")
            else:
                suite_name = args.suite_name
                suite_path = _resolve_suite_path(suite_name)
                result = _push_suite_to_server(runner, suite_name, suite_path)
                if args.json:
                    print(json.dumps({"ok": True, **result}, indent=2))
                else:
                    console.print(f"✓ Suite [cyan]{suite_name}[/cyan] pushed to server")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    elif suite_action == "rm":
        try:
            runner = RemoteRunner(_station_server(args))
            params = {}
            if getattr(args, "force", False):
                params["force"] = "true"
            if getattr(args, "include_runs", False):
                params["include_runs"] = "true"
            query = "&".join(f"{k}={v}" for k, v in params.items())
            path = f"/suites/{args.suite_name}"
            if query:
                path += f"?{query}"
            runner._request("DELETE", path)
            if args.json:
                print(json.dumps({"ok": True}))
            else:
                console.print(f"✓ Suite [cyan]{args.suite_name}[/cyan] removed from server")
        except Exception as e:
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, indent=2))
                sys.exit(1)
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)

    else:
        args._parser_station_suites.print_help()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="comfy-runner",
        description="CLI tool for managing ComfyUI installations",
    )
    parser.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output"
    )
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Create a new ComfyUI installation")
    p_init.add_argument("--name", "-n", default="main", help="Installation name (default: main)")
    p_init.add_argument("--variant", "-v", help="Explicit variant ID (e.g. win-nvidia-cu128)")
    p_init.add_argument("--release", "-r", help="Specific release tag (e.g. v0.2.1)")
    p_init.add_argument("--dir", "-d", help="Custom installation directory")
    p_init.add_argument("--cuda-compat", action="store_true", default=False,
                         help="Auto-detect host NVIDIA driver and swap torch CUDA build if needed")
    p_init.add_argument("--comfyui-ref", help="ComfyUI branch/tag/commit to checkout")
    # Ad-hoc build options
    p_init.add_argument("--build", action="store_true", default=False,
                         help="Build standalone env locally instead of downloading a pre-built release")
    p_init.add_argument("--python-version", help="Python version for ad-hoc build (e.g. 3.12, 3.13.12)")
    p_init.add_argument("--pbs-release", help="python-build-standalone release tag (e.g. 20260211)")
    p_init.add_argument("--gpu", help="GPU type override (nvidia/amd/intel/mps/cpu)")
    p_init.add_argument("--cuda-tag", help="CUDA/ROCm/XPU tag (e.g. cu128, cu130, rocm7.1, xpu)")
    p_init.add_argument("--torch-version", help="PyTorch version (e.g. 2.10.0)")
    p_init.add_argument("--torch-spec", nargs="+",
                         help="Full custom torch package specs (e.g. torch==2.10.0+cu128 torchvision==0.25.0+cu128)")
    p_init.add_argument("--torch-index-url", help="Custom PyTorch index URL")
    p_init.set_defaults(func=cmd_init)

    # releases
    p_rel = sub.add_parser("releases", help="List available releases and variants")
    p_rel.add_argument("--variants", "-v", action="store_true", help="Show variants for each release")
    p_rel.add_argument("--limit", "-l", type=int, default=10, help="Number of releases to fetch (default: 10)")
    p_rel.set_defaults(func=cmd_releases)

    # list
    p_list = sub.add_parser("list", aliases=["ls"], help="List all installations")
    p_list.set_defaults(func=cmd_list)

    # rm
    p_rm = sub.add_parser("rm", help="Remove an installation")
    p_rm.add_argument("name", help="Installation name to remove")
    p_rm.add_argument("--keep-files", action="store_true", help="Remove record but keep files on disk")
    p_rm.set_defaults(func=cmd_rm)

    # info
    p_info = sub.add_parser("info", help="Show detailed info about an installation")
    p_info.add_argument("name", nargs="?", default="main", help="Installation name (default: main)")
    p_info.set_defaults(func=cmd_info)

    # sysinfo
    p_sysinfo = sub.add_parser("sysinfo", help="Show system hardware information")
    p_sysinfo.set_defaults(func=cmd_sysinfo)

    # set
    p_set = sub.add_parser("set", help="Set a config value on an installation (e.g. launch_args)")
    p_set.add_argument("name", help="Installation name")
    p_set.add_argument("key", help="Config key (e.g. launch_args)")
    p_set.add_argument("value", help="Value to set")
    p_set.set_defaults(func=cmd_set)

    # config
    p_config = sub.add_parser("config", help="View or set global configuration")
    config_sub = p_config.add_subparsers(dest="config_action")

    p_config_show = config_sub.add_parser("show", help="Show current configuration")

    p_config_set = config_sub.add_parser("set", help="Set a configuration value")
    p_config_set.add_argument("key", help="Config key (e.g. shared_dir)")
    p_config_set.add_argument("value", help="Value to set")

    p_config.set_defaults(func=cmd_config)

    # config env
    p_config_env = config_sub.add_parser("env", help="Manage persistent environment variables")
    config_env_sub = p_config_env.add_subparsers(dest="env_action")

    p_env_list = config_env_sub.add_parser("list", help="List environment variables")
    p_env_list.add_argument("name", nargs="?", default="main")

    p_env_set = config_env_sub.add_parser("set", help="Set an environment variable")
    p_env_set.add_argument("name", nargs="?", default="main")
    p_env_set.add_argument("key", help="Variable name")
    p_env_set.add_argument("value", help="Variable value")

    p_env_unset = config_env_sub.add_parser("unset", help="Remove an environment variable")
    p_env_unset.add_argument("name", nargs="?", default="main")
    p_env_unset.add_argument("key", help="Variable name to remove")

    p_config_env.set_defaults(func=cmd_config_env)

    # start
    p_start = sub.add_parser("start", help="Start a ComfyUI installation")
    p_start.add_argument("name", nargs="?", default="main", help="Installation name (default: main)")
    p_start.add_argument("--port", "-p", type=int, help="Override port (default: 8188)")
    p_start.add_argument("--port-conflict", choices=["auto", "fail"], default="auto",
                         help="Port conflict mode: auto=find next free port, fail=error (default: auto)")
    p_start.add_argument("--background", "-b", action="store_true", help="Run in background (detached)")
    p_start.add_argument("--extra-args", "-e", default="", help="Extra args to pass to ComfyUI")
    p_start.add_argument("--env", action="append", metavar="KEY=VALUE",
                         help="Set environment variable for this run (repeatable)")
    p_start.set_defaults(func=cmd_start)

    # stop
    p_stop = sub.add_parser("stop", help="Stop a running ComfyUI installation")
    p_stop.add_argument("name", nargs="?", default="main", help="Installation name (default: main)")
    p_stop.set_defaults(func=cmd_stop)

    # restart
    p_restart = sub.add_parser("restart", help="Restart a ComfyUI installation")
    p_restart.add_argument("name", nargs="?", default="main", help="Installation name (default: main)")
    p_restart.add_argument("--port", "-p", type=int, help="Override port (default: 8188)")
    p_restart.add_argument("--env", action="append", metavar="KEY=VALUE",
                           help="Set environment variable for this run (repeatable)")
    p_restart.set_defaults(func=cmd_restart)

    # status
    p_status = sub.add_parser("status", help="Show running state of an installation")
    p_status.add_argument("name", nargs="?", default="main", help="Installation name (default: main)")
    p_status.set_defaults(func=cmd_status)

    # logs
    p_logs = sub.add_parser("logs", help="Show logs from a running installation")
    p_logs.add_argument("name", nargs="?", default="main", help="Installation name (default: main)")
    p_logs.set_defaults(func=cmd_logs)

    # nodes
    p_nodes = sub.add_parser("nodes", help="Manage custom nodes")
    nodes_sub = p_nodes.add_subparsers(dest="nodes_action")

    p_nodes_list = nodes_sub.add_parser("list", help="List custom nodes")
    p_nodes_list.add_argument("name", nargs="?", default="main")

    p_nodes_add = nodes_sub.add_parser("add", help="Add a custom node")
    p_nodes_add.add_argument("source", help="Git URL or CNR node ID")
    p_nodes_add.add_argument("name", nargs="?", default="main")
    p_nodes_add.add_argument("--version", help="CNR version (optional)")

    p_nodes_rm = nodes_sub.add_parser("rm", help="Remove a custom node")
    p_nodes_rm.add_argument("node_name", help="Node directory name")
    p_nodes_rm.add_argument("name", nargs="?", default="main")

    p_nodes_enable = nodes_sub.add_parser("enable", help="Enable a disabled node")
    p_nodes_enable.add_argument("node_name", help="Node directory name")
    p_nodes_enable.add_argument("name", nargs="?", default="main")

    p_nodes_disable = nodes_sub.add_parser("disable", help="Disable a node")
    p_nodes_disable.add_argument("node_name", help="Node directory name")
    p_nodes_disable.add_argument("name", nargs="?", default="main")

    p_nodes.set_defaults(func=cmd_nodes)

    # snapshot
    p_snap = sub.add_parser("snapshot", help="Manage environment snapshots")
    snap_sub = p_snap.add_subparsers(dest="snapshot_action")

    p_snap_capture = snap_sub.add_parser("capture", help="Capture snapshot from any ComfyUI directory (no registration required)")
    p_snap_capture.add_argument("--path", required=True, help="Path to ComfyUI installation directory (managed or manual/portable)")
    p_snap_capture.add_argument("--venv", help="Explicit venv path for manual installs (auto-detected if omitted)")
    p_snap_capture.add_argument("--label", "-l", help="Optional label for the snapshot")
    p_snap_capture.add_argument("--output", "-o", required=True, help="Output file path for the snapshot JSON")

    p_snap_save = snap_sub.add_parser("save", help="Capture current state of a registered installation")
    p_snap_save.add_argument("name", nargs="?", default="main")
    p_snap_save.add_argument("--label", "-l", help="Optional label for the snapshot")

    p_snap_list = snap_sub.add_parser("list", help="List snapshots")
    p_snap_list.add_argument("name", nargs="?", default="main")

    p_snap_show = snap_sub.add_parser("show", help="Show snapshot details")
    p_snap_show.add_argument("id", help="Snapshot filename, #index, or partial match")
    p_snap_show.add_argument("name", nargs="?", default="main")

    p_snap_diff = snap_sub.add_parser("diff", help="Diff snapshot against current state")
    p_snap_diff.add_argument("id", help="Snapshot filename, #index, or partial match")
    p_snap_diff.add_argument("name", nargs="?", default="main")

    p_snap_restore = snap_sub.add_parser("restore", help="Restore to a snapshot")
    p_snap_restore.add_argument("id", help="Snapshot filename, #index, or partial match")
    p_snap_restore.add_argument("name", nargs="?", default="main")

    p_snap_export = snap_sub.add_parser("export", help="Export a snapshot to a file")
    p_snap_export.add_argument("id", help="Snapshot filename, #index, or partial match")
    p_snap_export.add_argument("name", nargs="?", default="main")
    p_snap_export.add_argument("--output", "-o", help="Output file path")

    p_snap_import = snap_sub.add_parser("import", help="Import snapshots from a file")
    p_snap_import.add_argument("file", help="Path to snapshot export file")
    p_snap_import.add_argument("name", nargs="?", default="main")

    p_snap.set_defaults(func=cmd_snapshot)

    # server
    p_server = sub.add_parser("server", help="Start HTTP control API server")
    p_server.add_argument("--listen", nargs="?", default="127.0.0.1", const="0.0.0.0",
                          help="Bind address (default: 127.0.0.1, --listen alone = 0.0.0.0)")
    p_server.add_argument("--port", "-p", type=int, default=9189, help="Server port (default: 9189)")
    p_server.add_argument("--tailscale", action="store_true",
                          help="Expose via tailscale serve (tailnet-private)")
    p_server.add_argument("--tunnels", action="store_true",
                           help="Enable tunnel API endpoints (tailscale funnel for public internet exposure)")
    p_server.add_argument("--keep-instances", action="store_true",
                          help="Don't stop ComfyUI instances when server shuts down")
    p_server.set_defaults(func=cmd_server)

    # tailscale-serve
    p_ts = sub.add_parser("tailscale-serve", help="Manage tailscale serve for the runner server")
    ts_sub = p_ts.add_subparsers(dest="ts_action")
    p_ts_start = ts_sub.add_parser("start", help="Start tailscale serve (expose runner server to tailnet)")
    p_ts_start.add_argument("--port", "-p", type=int, default=9189, help="Port to expose (default: 9189)")
    ts_sub.add_parser("stop", help="Stop tailscale serve")
    ts_sub.add_parser("status", help="Show tailscale serve status")
    p_ts.set_defaults(func=cmd_tailscale_serve, _parser_ts=p_ts)

    # deploy
    p_deploy = sub.add_parser("deploy", help="Deploy a PR, branch, tag, commit, or update to latest release")
    p_deploy.add_argument("name", nargs="?", default="main", help="Installation name (default: main)")
    deploy_group = p_deploy.add_mutually_exclusive_group()
    deploy_group.add_argument("--pr", type=int, help="PR number to deploy")
    deploy_group.add_argument("--branch", help="Branch name to checkout")
    deploy_group.add_argument("--tag", help="Tag to checkout")
    deploy_group.add_argument("--commit", help="Commit SHA to checkout")
    deploy_group.add_argument("--reset", action="store_true", help="Reset to the original release ref")
    deploy_group.add_argument("--latest", action="store_true", help="Update to the latest standalone release's ComfyUI ref")
    deploy_group.add_argument("--pull", action="store_true", help="Re-fetch the currently tracked PR or branch")
    p_deploy.add_argument(
        "--repo", dest="repo_url",
        help="Repo URL or owner/name to fetch from (overrides install's "
             "origin for this deploy; mainly used for fork PRs).",
    )
    p_deploy.set_defaults(func=cmd_deploy)

    # review (PR-review preparation)
    p_review = sub.add_parser(
        "review",
        help="Prepare a PR for review: deploy + manifest + model provisioning",
    )
    p_review.add_argument("pr", type=int, help="GitHub PR number")
    p_review.add_argument(
        "--repo", required=True,
        help="Target repo as 'owner/name' or full GitHub URL "
             "(e.g. comfy-org/ComfyUI)",
    )
    p_review.add_argument(
        "--target", default="local",
        help="Target spec: local, local:<install-name>, "
             "remote:<pod-name>, runpod[:<gpu>], or server:<url> "
             "(default: local). 'server:' targets a comfy-runner server "
             "directly (no central station) — use the full Tailscale "
             "MagicDNS FQDN, e.g. server:https://mybox.tailnet.ts.net:9189.",
    )
    p_review.add_argument(
        "--workflow", action="append", default=[],
        help="Extra workflow URL to fetch (repeatable). Merges with "
             "any URLs in the PR's comfyrunner manifest block.",
    )
    p_review.add_argument(
        "--model", action="append", default=[],
        help="Extra model entry as 'name=url=directory' (repeatable). "
             "Merges with any models in the PR's manifest.",
    )
    p_review.add_argument(
        "--token",
        help="Bearer token for authenticated model downloads "
             "(HuggingFace / ModelScope). Not stored.",
    )
    p_review.add_argument(
        "--github-token", dest="github_token",
        help="GitHub token for fetching the PR body. Defaults to "
             "$GITHUB_TOKEN; not required for public repos.",
    )
    p_review.add_argument(
        "--no-provision-models", dest="no_provision_models",
        action="store_true",
        help="Fetch the manifest and save workflows but skip model "
             "downloads.",
    )
    p_review.add_argument(
        "--allow-arbitrary-urls", dest="allow_arbitrary_urls",
        action="store_true",
        help="Allow workflow URLs whose host is not in the default "
             "allowlist (huggingface.co, civitai.com, modelscope.cn, "
             "raw.githubusercontent.com, gist.githubusercontent.com, "
             "github.com).",
    )
    p_review.add_argument(
        "--server",
        help="Override central station URL for remote/runpod targets "
             "(default: read from station.json walking up from cwd).",
    )
    p_review.add_argument(
        "--install",
        default="main",
        help="Installation name on the target server for remote/runpod/"
             "server targets (default: main). Ignored for local targets — "
             "use --target local:<install-name> instead.",
    )
    p_review.add_argument(
        "--force-purpose", dest="force_purpose", action="store_true",
        help="Allow review against pods tagged purpose='test' (e2e test "
             "pods). By default such pods are refused because reviews "
             "would clobber their curated install state.",
    )
    p_review.add_argument(
        "--cleanup", action="store_true",
        help="For runpod target only: terminate the ephemeral PR pod "
             "after review-prep finishes. Use 'review-cleanup' instead "
             "if you want to clean up an existing pod later.",
    )
    p_review.add_argument(
        "--force-deploy", dest="force_deploy", action="store_true",
        help="For remote and server targets: always deploy even if the "
             "install already has this PR deployed. Default is idempotent "
             "(skip deploy if already current). No effect on local/runpod "
             "targets.",
    )
    p_review.add_argument(
        "--idle-stop-after", dest="idle_stop_after", type=int, default=None,
        metavar="SECONDS",
        help="For remote/runpod targets: update the pod's idle timeout "
             "to this many seconds. The central station's idle reaper "
             "auto-stops purpose='pr' pods that have been idle this long.",
    )
    p_review.set_defaults(func=cmd_review)

    # review-cleanup
    p_review_cleanup = sub.add_parser(
        "review-cleanup",
        help="Terminate ephemeral PR pods (purpose='pr') for a given PR.",
    )
    p_review_cleanup.add_argument("pr", type=int, help="PR number")
    p_review_cleanup.add_argument(
        "--server",
        help="Override central station URL (default: from station.json).",
    )
    p_review_cleanup.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="List matching pods without terminating them.",
    )
    p_review_cleanup.set_defaults(func=cmd_review_cleanup)

    # review-init (generate a comfyrunner block from a workflow JSON)
    p_review_init = sub.add_parser(
        "review-init",
        help="Generate a comfyrunner manifest block from a workflow JSON.",
    )
    p_review_init.add_argument(
        "workflow",
        help="Path to a workflow JSON file on disk.",
    )
    p_review_init.add_argument(
        "--workflow-url", dest="workflow_url",
        help="Public HTTPS URL the workflow file will be served from "
             "(typically a raw.githubusercontent.com URL on the PR's "
             "branch). If omitted, a placeholder is emitted.",
    )
    p_review_init.set_defaults(func=cmd_review_init)

    # review-validate (lint a manifest file or PR)
    p_review_validate = sub.add_parser(
        "review-validate",
        help="Lint a comfyrunner manifest from a file, PR shorthand, or PR URL.",
    )
    p_review_validate.add_argument(
        "source",
        help="Path to a file (markdown with comfyrunner block, or raw "
             "manifest JSON), an 'owner/repo#pr' shorthand, or a "
             "GitHub PR URL.",
    )
    p_review_validate.add_argument(
        "--github-token", dest="github_token",
        help="GitHub token for fetching PR bodies. Defaults to "
             "$GITHUB_TOKEN; not required for public repos.",
    )
    p_review_validate.set_defaults(func=cmd_review_validate)

    # download-model
    p_dlm = sub.add_parser("download-model", help="Download a model by URL to a specific directory")
    p_dlm.add_argument("--url", required=True, help="Download URL")
    p_dlm.add_argument("--dir", required=True, help="Target subdirectory under models/ (e.g. checkpoints, loras)")
    p_dlm.add_argument("--name", dest="filename", help="Filename to save as (default: derived from URL)")
    p_dlm.add_argument("--token", help="Bearer token for authenticated downloads (not stored)")
    p_dlm.add_argument("name", nargs="?", default="main", help="Installation name")
    p_dlm.set_defaults(func=cmd_download_model)

    # remote
    p_remote = sub.add_parser("remote", help="Operations on a remote comfy-runner server")
    remote_sub = p_remote.add_subparsers(dest="remote_action")

    p_remote_upload = remote_sub.add_parser("upload-model", help="Upload a model file to a remote server")
    p_remote_upload.add_argument("--server", required=True, help="Remote server URL (e.g. https://mybox.ts.net:9189)")
    p_remote_upload.add_argument("--file", required=True, help="Path to the local model file")
    p_remote_upload.add_argument("--dir", required=True, help="Target subdirectory under models/ (e.g. checkpoints)")
    p_remote_upload.add_argument("--name", dest="filename", help="Filename to save as (default: derived from file path)")
    p_remote_upload.add_argument("--resume", action="store_true", help="Resume a previously interrupted upload")
    p_remote_upload.add_argument("--hash-type", dest="hash_type", choices=["blake3", "sha256"], default="blake3",
                                 help="Hash algorithm for integrity verification (default: blake3)")
    p_remote_upload.add_argument("name", nargs="?", default="main", help="Installation name on the remote server")
    p_remote_upload.set_defaults(func=cmd_remote_upload)

    # workflow-models
    p_wfm = sub.add_parser("workflow-models", help="Download models referenced in a workflow template")
    p_wfm.add_argument("file", help="Path to workflow template JSON")
    p_wfm.add_argument("name", nargs="?", default="main", help="Installation name")
    p_wfm.add_argument("--dry-run", action="store_true", help="List models without downloading")
    p_wfm.set_defaults(func=cmd_workflow_models)

    # test
    p_test = sub.add_parser("test", help="Run regression tests against ComfyUI")
    test_sub = p_test.add_subparsers(dest="test_action")

    # test run
    p_test_run = test_sub.add_parser("run", help="Run a test suite")
    p_test_run.add_argument("suite", help="Path to test suite directory")
    p_test_run.add_argument("--target", "-t",
                            help="ComfyUI target (URL, host:port, or pod name; required unless --runpod)")
    p_test_run.add_argument("--output", "-o", help="Output directory (default: suite/runs/<timestamp>)")
    p_test_run.add_argument("--timeout", type=int, default=600,
                            help="Per-workflow timeout in seconds (default: 600)")
    p_test_run.add_argument("--http-timeout", type=int, default=30,
                            help="HTTP request timeout in seconds (default: 30)")
    p_test_run.add_argument("--format", default="json,html,markdown",
                            help="Report formats: json,html,markdown,console (default: json,html,markdown)")
    # RunPod one-shot options
    p_test_run.add_argument("--runpod", action="store_true",
                            help="Run on an ephemeral RunPod pod (provision → deploy → test → teardown)")
    p_test_run.add_argument("--gpu", help="GPU type for RunPod pod (e.g. 'NVIDIA L40S')")
    p_test_run.add_argument("--image", help="Docker image for RunPod pod")
    p_test_run.add_argument("--volume-id", help="Attach an existing RunPod network volume")
    deploy_group = p_test_run.add_mutually_exclusive_group()
    deploy_group.add_argument("--pr", type=int, help="Deploy a PR before testing")
    deploy_group.add_argument("--branch", help="Deploy a branch before testing")
    deploy_group.add_argument("--commit", help="Deploy a commit before testing")
    p_test_run.add_argument("--pod-name", help="Pod name (reuse existing or name new pod)")
    p_test_run.add_argument("--no-terminate", action="store_true",
                            help="Keep the pod running after tests complete")
    p_test_run.add_argument("--install-name", default="main",
                            help="Installation name on the remote pod (default: main)")
    p_test_run.add_argument(
        "--max-runtime", type=int, default=None,
        help=(
            "Suite-level wall-clock budget in seconds. Overrides "
            "suite.json's max_runtime_s. The watchdog aborts the run on "
            "overrun and (for runpod targets) tears down the pod."
        ),
    )
    p_test_run.add_argument(
        "--on-overrun",
        choices=("none", "stop", "terminate"),
        default=None,
        help=(
            "Pod action when the watchdog aborts. Defaults: terminate "
            "for runpod targets, none otherwise (local mode)."
        ),
    )

    # test list
    p_test_list = test_sub.add_parser("list", help="Discover available test suites")
    p_test_list.add_argument("--dir", "-d", help="Directory to search (default: current)")

    # test baseline
    p_test_baseline = test_sub.add_parser("baseline", help="Approve test outputs as new baselines")
    p_test_baseline.add_argument("suite", help="Path to test suite directory")
    p_test_baseline.add_argument("run_dir", help="Path to a test run output directory")
    baseline_group = p_test_baseline.add_mutually_exclusive_group()
    baseline_group.add_argument("--workflow", "-w", help="Approve a specific workflow")
    baseline_group.add_argument("--approve-all", action="store_true",
                                help="Approve all workflows in the run")

    # test report
    p_test_report = test_sub.add_parser("report", help="Regenerate reports from a previous run")
    p_test_report.add_argument("run_dir", help="Path to a test run output directory")
    p_test_report.add_argument("--format", default="json,html,markdown",
                               help="Report formats (default: json,html,markdown)")

    # test fleet
    p_test_fleet = test_sub.add_parser("fleet", help="Run a test suite across multiple targets in parallel")
    p_test_fleet.add_argument("suite", help="Path to test suite directory")
    p_test_fleet.add_argument("--target", "-t", action="append", required=True,
                              help="Target spec: local:<url>, remote:<server_url>, runpod:<gpu_type> (repeatable)")
    p_test_fleet.add_argument("--output", "-o", help="Output directory (default: suite/runs/fleet-<timestamp>)")
    p_test_fleet.add_argument("--timeout", type=int, default=600,
                              help="Per-workflow timeout in seconds (default: 600)")
    p_test_fleet.add_argument("--http-timeout", type=int, default=30,
                              help="HTTP request timeout in seconds (default: 30)")
    p_test_fleet.add_argument("--format", default="json,html,markdown",
                              help="Report formats (default: json,html,markdown)")
    p_test_fleet.add_argument("--max-workers", type=int, default=None,
                              help="Max parallel workers (default: min(targets, 4))")
    fleet_deploy_group = p_test_fleet.add_mutually_exclusive_group()
    fleet_deploy_group.add_argument("--pr", type=int, help="Deploy a PR before testing (ephemeral targets)")
    fleet_deploy_group.add_argument("--branch", help="Deploy a branch before testing (ephemeral targets)")
    fleet_deploy_group.add_argument("--commit", help="Deploy a commit before testing (ephemeral targets)")
    p_test_fleet.add_argument(
        "--max-runtime", type=int, default=None,
        help=(
            "Fleet-level wall-clock budget in seconds. Overrides "
            "suite.json's max_runtime_s. The watchdog aborts the run on "
            "overrun and (for runpod/remote targets) dispatches the pod "
            "action."
        ),
    )
    p_test_fleet.add_argument(
        "--on-overrun",
        choices=("none", "stop", "terminate"),
        default=None,
        help=(
            "Pod action when the watchdog aborts. Defaults per target "
            "kind: terminate (runpod), stop (remote), none (local)."
        ),
    )

    p_test.set_defaults(func=cmd_test, _parser_test=p_test)

    # hosted
    p_hosted = sub.add_parser("hosted", help="Manage hosted GPU deployments (RunPod, etc.)")
    hosted_sub = p_hosted.add_subparsers(dest="hosted_action")

    # hosted config
    p_hosted_config = hosted_sub.add_parser("config", help="View or set hosted provider configuration")
    hosted_config_sub = p_hosted_config.add_subparsers(dest="hosted_config_action")
    hosted_config_sub.add_parser("show", help="Show hosted configuration")
    p_hc_set = hosted_config_sub.add_parser("set", help="Set a configuration value")
    p_hc_set.add_argument("key", help="Config key (e.g. runpod.api_key)")
    p_hc_set.add_argument("value", help="Value to set")
    p_hosted_config.set_defaults(_parser_hosted_config=p_hosted_config)

    # hosted volume
    p_hosted_volume = hosted_sub.add_parser("volume", help="Manage hosted network volumes")
    hosted_volume_sub = p_hosted_volume.add_subparsers(dest="hosted_volume_action")

    p_hv_create = hosted_volume_sub.add_parser("create", help="Create a network volume")
    p_hv_create.add_argument("--name", "-n", required=True, help="Volume name (local config label)")
    p_hv_create.add_argument("--size", "-s", type=int, required=True, help="Volume size in GB")
    p_hv_create.add_argument("--region", "-r", help="Datacenter ID (default: from config)")

    hosted_volume_sub.add_parser("list", help="List configured volumes")

    p_hv_rm = hosted_volume_sub.add_parser("rm", help="Remove a volume")
    p_hv_rm.add_argument("name", help="Volume name to remove")
    p_hv_rm.add_argument("--keep-remote", action="store_true",
                         help="Remove local config only, keep volume on RunPod")

    p_hosted_volume.set_defaults(_parser_hosted_volume=p_hosted_volume)

    # hosted pod
    p_hosted_pod = hosted_sub.add_parser("pod", help="Manage hosted GPU pods")
    hosted_pod_sub = p_hosted_pod.add_subparsers(dest="hosted_pod_action")

    p_hp_create = hosted_pod_sub.add_parser("create", help="Create a new pod")
    p_hp_create.add_argument("--name", "-n", required=True, help="Pod name")
    p_hp_create.add_argument("--gpu", "-g", help="GPU type (default: from config)")
    p_hp_create.add_argument("--image", "-i", help="Docker image (default: runpod/ubuntu:24.04)")
    p_hp_create.add_argument("--volume", "-v", help="Volume name (from config) or volume ID")
    p_hp_create.add_argument("--region", "-r", help="Datacenter ID (default: from config)")
    p_hp_create.add_argument("--cloud-type", choices=["SECURE", "COMMUNITY", "ALL"],
                             help="Cloud type (default: from config)")
    p_hp_create.add_argument("--gpu-count", type=int, default=1,
                             help="Number of GPUs (default: 1)")
    p_hp_create.add_argument("--cuda-versions",
                             help="Comma-separated CUDA versions to allow (default: 12.4,12.6,12.8,13.0)")

    hosted_pod_sub.add_parser("list", help="List all pods")

    p_hp_show = hosted_pod_sub.add_parser("show", help="Show pod details")
    p_hp_show.add_argument("pod_id", help="Pod name or ID")

    p_hp_start = hosted_pod_sub.add_parser("start", help="Start a stopped pod")
    p_hp_start.add_argument("pod_id", help="Pod name or ID")

    p_hp_stop = hosted_pod_sub.add_parser("stop", help="Stop a running pod")
    p_hp_stop.add_argument("pod_id", help="Pod name or ID")

    p_hp_terminate = hosted_pod_sub.add_parser("terminate", help="Permanently terminate a pod")
    p_hp_terminate.add_argument("pod_id", help="Pod name or ID")

    p_hp_url = hosted_pod_sub.add_parser("url", help="Get proxy URL for a running pod")
    p_hp_url.add_argument("pod_id", help="Pod name or ID")
    p_hp_url.add_argument("--port", "-p", type=int, help="Port (default: 8188)")

    p_hosted_pod.set_defaults(_parser_hosted_pod=p_hosted_pod)

    # hosted init
    p_hosted_init = hosted_sub.add_parser("init", help="Create a volume + pod in one shot")
    p_hosted_init.add_argument("--name", "-n", required=True, help="Pod name")
    p_hosted_init.add_argument("--gpu", "-g", help="GPU type (default: from config)")
    p_hosted_init.add_argument("--image", "-i", help="Docker image (default: from config)")
    p_hosted_init.add_argument("--volume", "-v", help="Volume name (reuse existing or create new)")
    p_hosted_init.add_argument("--volume-size", type=int, help="Volume size in GB if creating new (default: 50)")
    p_hosted_init.add_argument("--region", "-r", help="Datacenter ID (default: from config)")
    p_hosted_init.add_argument("--cloud-type", choices=["SECURE", "COMMUNITY", "ALL"],
                               help="Cloud type (default: from config)")
    p_hosted_init.add_argument("--cuda-versions",
                               help="Comma-separated CUDA versions to allow (default: 12.4,12.6,12.8,13.0)")

    # hosted deploy
    p_hosted_deploy = hosted_sub.add_parser("deploy", help="Deploy a PR/branch/tag/commit to a hosted pod")
    p_hosted_deploy.add_argument("pod_name", help="Pod name (from config)")
    deploy_group = p_hosted_deploy.add_mutually_exclusive_group()
    deploy_group.add_argument("--pr", type=int, help="PR number to deploy")
    deploy_group.add_argument("--branch", help="Branch name to checkout")
    deploy_group.add_argument("--tag", help="Tag to checkout")
    deploy_group.add_argument("--commit", help="Commit SHA to checkout")
    deploy_group.add_argument("--reset", action="store_true", help="Reset to original release ref")
    p_hosted_deploy.add_argument("--start", action="store_true", help="Start ComfyUI after deploy")
    p_hosted_deploy.add_argument("--launch-args", help="Launch args to pass to ComfyUI")
    p_hosted_deploy.add_argument("--install", dest="install_name", help="Installation name on pod (default: main)")
    p_hosted_deploy.add_argument("--cuda-compat", action="store_true", default=False,
                                 help="Auto-detect host NVIDIA driver and swap torch CUDA build if needed")

    # hosted sysinfo
    p_hosted_sysinfo = hosted_sub.add_parser("sysinfo", help="Show system/hardware info from a hosted pod")
    p_hosted_sysinfo.add_argument("pod_name", help="Pod name (from config)")

    # hosted status
    p_hosted_status = hosted_sub.add_parser("status", help="Show status of a hosted pod")
    p_hosted_status.add_argument("pod_name", help="Pod name (from config)")
    p_hosted_status.add_argument("--install", dest="install_name", help="Specific installation name")

    # hosted start-comfy
    p_hosted_start = hosted_sub.add_parser("start-comfy", help="Start/restart ComfyUI on a hosted pod")
    p_hosted_start.add_argument("pod_name", help="Pod name (from config)")
    p_hosted_start.add_argument("--install", dest="install_name", help="Installation name (default: main)")

    # hosted stop-comfy
    p_hosted_stop = hosted_sub.add_parser("stop-comfy", help="Stop ComfyUI on a hosted pod")
    p_hosted_stop.add_argument("pod_name", help="Pod name (from config)")
    p_hosted_stop.add_argument("--install", dest="install_name", help="Installation name (default: main)")

    # hosted logs
    p_hosted_logs = hosted_sub.add_parser("logs", help="Show ComfyUI logs from a hosted pod")
    p_hosted_logs.add_argument("pod_name", help="Pod name (from config)")
    p_hosted_logs.add_argument("--install", dest="install_name", help="Installation name (default: main)")

    p_hosted.set_defaults(func=cmd_hosted, _parser_hosted=p_hosted)

    # tunnel
    p_tunnel = sub.add_parser("tunnel", help="Manage tunnel exposure")
    tunnel_sub = p_tunnel.add_subparsers(dest="tunnel_action")

    p_tunnel_start = tunnel_sub.add_parser("start", help="Start tunnel")
    p_tunnel_start.add_argument("name", nargs="?", default="main")
    p_tunnel_start.add_argument("--provider", choices=["ngrok", "tailscale"], default="tailscale")
    p_tunnel_start.add_argument("--domain", help="Explicit ngrok domain (overrides pool)")

    p_tunnel_stop = tunnel_sub.add_parser("stop", help="Stop tunnel")
    p_tunnel_stop.add_argument("name", nargs="?", default="main")

    p_tunnel_config = tunnel_sub.add_parser("config", help="View or set tunnel configuration")
    p_tunnel_config.add_argument("--provider", choices=["ngrok"], default="ngrok",
                                 help="Provider to configure (default: ngrok)")
    p_tunnel_config.add_argument("--authtoken", help="Set ngrok authtoken")
    p_tunnel_config.add_argument("--region", help="Set ngrok region (e.g. us, eu, ap)")
    p_tunnel_config.add_argument("--add-domain", help="Add a domain to the ngrok domain pool")
    p_tunnel_config.add_argument("--rm-domain", help="Remove a domain from the ngrok domain pool")

    p_tunnel.set_defaults(func=cmd_tunnel, _parser_tunnel=p_tunnel)

    # station
    p_station = sub.add_parser("station", help="Interact with a central comfy-runner fleet server")
    station_sub = p_station.add_subparsers(dest="station_action")

    # station info
    station_sub.add_parser("info", help="Show station config and server connectivity")

    # station dashboard
    station_sub.add_parser("dashboard", help="Open the fleet dashboard in browser")

    # station jobs
    station_sub.add_parser("jobs", help="List active jobs on the central server")

    # station pods
    p_st_pods = station_sub.add_parser("pods", help="Manage fleet pods")
    st_pods_sub = p_st_pods.add_subparsers(dest="station_pod_action")

    st_pods_sub.add_parser("list", help="List all pods")

    p_st_pod_create = st_pods_sub.add_parser("create", help="Create a new pod")
    p_st_pod_create.add_argument("pod_name", help="Pod name")
    p_st_pod_create.add_argument("--gpu", "-g", help="GPU type (default: from station config)")
    p_st_pod_create.add_argument("--image", "-i", help="Docker image")
    p_st_pod_create.add_argument("--datacenter", help="Datacenter ID")
    p_st_pod_create.add_argument("--no-wait", action="store_true", help="Don't wait for server readiness")

    p_st_pod_deploy = st_pods_sub.add_parser("deploy", help="Deploy a PR/branch/commit to a pod")
    p_st_pod_deploy.add_argument("pod_name", help="Pod name")
    deploy_group = p_st_pod_deploy.add_mutually_exclusive_group(required=True)
    deploy_group.add_argument("--pr", type=int, help="PR number")
    deploy_group.add_argument("--branch", help="Branch name")
    deploy_group.add_argument("--tag", help="Tag")
    deploy_group.add_argument("--commit", help="Commit SHA")
    deploy_group.add_argument("--reset", action="store_true", help="Reset to original release")
    deploy_group.add_argument("--latest", action="store_true", help="Update to latest release")
    deploy_group.add_argument("--pull", dest="pull_deploy", action="store_true", help="Re-fetch current PR/branch")

    p_st_pod_stop = st_pods_sub.add_parser("stop", help="Stop a pod")
    p_st_pod_stop.add_argument("pod_name", help="Pod name")

    p_st_pod_start = st_pods_sub.add_parser("start", help="Start a stopped pod")
    p_st_pod_start.add_argument("pod_name", help="Pod name")

    p_st_pod_terminate = st_pods_sub.add_parser("terminate", help="Terminate a pod permanently")
    p_st_pod_terminate.add_argument("pod_name", help="Pod name")

    p_st_pod_cleanup = st_pods_sub.add_parser("cleanup", help="Terminate orphaned test pods")
    p_st_pod_cleanup.add_argument("--prefix", default="test-", help="Pod name prefix to match (default: test-)")
    p_st_pod_cleanup.add_argument("--dry-run", action="store_true", help="List matching pods without terminating")

    p_st_pod_launch_pr = st_pods_sub.add_parser(
        "launch-pr",
        help="Create-or-wake a pod for a PR and deploy the PR to it",
    )
    p_st_pod_launch_pr.add_argument("pr", type=int, help="GitHub PR number")
    p_st_pod_launch_pr.add_argument("--repo", help="GitHub repo (URL or 'owner/name')")
    p_st_pod_launch_pr.add_argument("--gpu", "-g", help="GPU type (default: from station config)")
    p_st_pod_launch_pr.add_argument("--datacenter", help="Datacenter ID")
    p_st_pod_launch_pr.add_argument("--image", help="Docker image")
    p_st_pod_launch_pr.add_argument("--install", help="Installation name on the pod (default: main)")
    p_st_pod_launch_pr.add_argument(
        "--idle-timeout", type=int, dest="idle_timeout",
        help="Seconds of inactivity before the pod is auto-stopped (default: 600)",
    )

    p_st_pod_touch = st_pods_sub.add_parser(
        "touch",
        help="Reset the idle timer on a pod (defers the auto-stop reaper)",
    )
    p_st_pod_touch.add_argument("pod_name", help="Pod name")

    p_st_pods.set_defaults(_parser_station_pods=p_st_pods)

    # station tests
    p_st_tests = station_sub.add_parser("tests", help="Run and manage fleet tests")
    st_tests_sub = p_st_tests.add_subparsers(dest="station_test_action")

    st_tests_sub.add_parser("list", help="List recent test runs")

    p_st_test_run = st_tests_sub.add_parser("run", help="Run a test suite against a target")
    p_st_test_run.add_argument("suite", help="Suite name (from test-suites/) or path")
    p_st_test_run.add_argument("--target", "-t", required=True,
                               help="Target: local:<url>, remote:<pod_name>, runpod:<gpu_type>")
    p_st_test_run.add_argument("--timeout", type=int, help="Per-workflow timeout (seconds)")
    p_st_test_run.add_argument(
        "--max-runtime", type=int, default=None,
        help=(
            "Suite-level wall-clock budget in seconds. Overrides "
            "suite.json's max_runtime_s. The watchdog aborts the run "
            "on overrun and dispatches the on-overrun pod action."
        ),
    )
    p_st_test_run.add_argument(
        "--on-overrun",
        choices=("none", "stop", "terminate"),
        default=None,
        help=(
            "Pod action when the watchdog aborts. Defaults per target "
            "kind: terminate (runpod), stop (remote), none (local)."
        ),
    )

    p_st_test_fleet = st_tests_sub.add_parser("fleet", help="Run a test suite across multiple targets")
    p_st_test_fleet.add_argument("suite", help="Suite name or path")
    p_st_test_fleet.add_argument("--target", "-t", action="append", required=True,
                                 help="Target spec (repeatable)")
    p_st_test_fleet.add_argument("--timeout", type=int, help="Per-workflow timeout (seconds)")
    p_st_test_fleet.add_argument("--max-workers", type=int, help="Max parallel workers")
    p_st_test_fleet.add_argument(
        "--max-runtime", type=int, default=None,
        help=(
            "Fleet-level wall-clock budget in seconds. Overrides "
            "suite.json's max_runtime_s."
        ),
    )
    p_st_test_fleet.add_argument(
        "--on-overrun",
        choices=("none", "stop", "terminate"),
        default=None,
        help=(
            "Pod action when the watchdog aborts. Defaults per target "
            "kind: terminate (runpod), stop (remote), none (local)."
        ),
    )

    p_st_test_status = st_tests_sub.add_parser("status", help="Check test run status")
    p_st_test_status.add_argument("test_id", help="Test run ID")

    p_st_test_report = st_tests_sub.add_parser("report", help="Get test report")
    p_st_test_report.add_argument("test_id", help="Test run ID")

    p_st_tests.set_defaults(_parser_station_tests=p_st_tests)

    # station suites
    p_st_suites = station_sub.add_parser("suites", help="Manage test suites on the central server")
    st_suites_sub = p_st_suites.add_subparsers(dest="station_suite_action")

    st_suites_sub.add_parser("list", help="List suites on the server")

    p_st_suite_push = st_suites_sub.add_parser("push", help="Upload a suite to the server")
    p_st_suite_push.add_argument("suite_name", nargs="?", help="Suite name (from test-suites/)")
    p_st_suite_push.add_argument("--all", dest="all_suites", action="store_true", help="Push all local suites")

    p_st_suite_rm = st_suites_sub.add_parser("rm", help="Remove a suite from the server")
    p_st_suite_rm.add_argument("suite_name", help="Suite name")
    p_st_suite_rm.add_argument("--force", action="store_true", help="Force removal even if runs exist")
    p_st_suite_rm.add_argument("--include-runs", action="store_true", help="Also delete test run data")

    p_st_suites.set_defaults(_parser_station_suites=p_st_suites)

    # Global --server override for station commands
    p_station.add_argument("--server", help="Override central server URL (default: from station.json)")

    p_station.set_defaults(func=cmd_station, _parser_station=p_station)

    effective_argv = argv if argv is not None else sys.argv[1:]
    args = parser.parse_args(effective_argv)

    # Propagate --json to subcommand namespace
    if not hasattr(args, "json"):
        args.json = False

    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
