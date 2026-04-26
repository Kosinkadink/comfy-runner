"""CLI entry point for comfy-runner."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()


def _output(text: str) -> None:
    """Default send_output callback — prints to console."""
    console.print(text, end="", highlight=False)


from comfy_runner.lifecycle import maybe_tailscale_serve, maybe_tailscale_unserve, capture_snapshot


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> None:
    """Create a new ComfyUI installation."""
    from comfy_runner.installations import init_installation

    try:
        record = init_installation(
            name=args.name,
            variant=args.variant,
            release_tag=args.release,
            install_dir=args.dir,
            send_output=None if args.json else _output,
            cuda_compat=getattr(args, "cuda_compat", False),
            python_version=getattr(args, "python_version", None),
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
        if args.background:
            result = start_installation(
                name=name,
                port_override=port,
                port_conflict=pc,
                extra_args=extra,
                send_output=out,
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

            server_url = f"https://{pod.id}-9189.proxy.runpod.net"
            comfy_url = f"https://{pod.id}-8188.proxy.runpod.net"
            if args.json:
                print(json.dumps({"ok": True, "pod": {
                    "id": pod.id, "name": pod.name, "status": pod.status,
                    "gpu_type": pod.gpu_type, "datacenter": pod.datacenter,
                    "cost_per_hr": pod.cost_per_hr, "image": pod.image,
                    "server_url": server_url, "comfy_url": comfy_url,
                }}, indent=2))
            else:
                console.print(f"✓ Pod [cyan]{pod.name}[/cyan] created (id: {pod.id}, {pod.gpu_type}, ${pod.cost_per_hr}/hr)")
                console.print(f"  Server: {server_url}")
                console.print(f"  ComfyUI: {comfy_url}")
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
                    url = f"https://{pod.id}-8188.proxy.runpod.net"
                    console.print(f"  ComfyUI:    [link={url}]{url}[/link]")
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

        server_url = f"https://{pod.id}-9189.proxy.runpod.net"
        comfy_url = f"https://{pod.id}-8188.proxy.runpod.net"

        if args.json:
            result: dict = {
                "ok": True,
                "pod": {
                    "id": pod.id, "name": pod.name, "status": pod.status,
                    "gpu_type": pod.gpu_type, "datacenter": pod.datacenter,
                    "cost_per_hr": pod.cost_per_hr, "image": pod.image,
                    "server_url": server_url, "comfy_url": comfy_url,
                },
            }
            if volume_id:
                result["volume"] = {"id": volume_id, "name": volume_name}
            print(json.dumps(result, indent=2))
        else:
            console.print(f"✓ Pod [cyan]{args.name}[/cyan] created (id: {pod.id}, {pod.gpu_type}, ${pod.cost_per_hr}/hr)")
            console.print(f"  Server:  {server_url}")
            console.print(f"  ComfyUI: {comfy_url}")
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
    """Resolve a pod name to its comfy-runner server URL."""
    from comfy_runner.hosted.config import get_pod_record
    rec = get_pod_record("runpod", pod_name)
    if not rec:
        raise RuntimeError(
            f"No pod record for '{pod_name}'. "
            f"Create one with 'hosted init' or 'hosted pod create'."
        )
    return f"https://{rec['id']}-9189.proxy.runpod.net"


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
    p_init.add_argument("--python-version", help="Python version for the venv (e.g. 3.12). Uses uv to fetch that Python instead of the bundled one.")
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

    # start
    p_start = sub.add_parser("start", help="Start a ComfyUI installation")
    p_start.add_argument("name", nargs="?", default="main", help="Installation name (default: main)")
    p_start.add_argument("--port", "-p", type=int, help="Override port (default: 8188)")
    p_start.add_argument("--port-conflict", choices=["auto", "fail"], default="auto",
                         help="Port conflict mode: auto=find next free port, fail=error (default: auto)")
    p_start.add_argument("--background", "-b", action="store_true", help="Run in background (detached)")
    p_start.add_argument("--extra-args", "-e", default="", help="Extra args to pass to ComfyUI")
    p_start.set_defaults(func=cmd_start)

    # stop
    p_stop = sub.add_parser("stop", help="Stop a running ComfyUI installation")
    p_stop.add_argument("name", nargs="?", default="main", help="Installation name (default: main)")
    p_stop.set_defaults(func=cmd_stop)

    # restart
    p_restart = sub.add_parser("restart", help="Restart a ComfyUI installation")
    p_restart.add_argument("name", nargs="?", default="main", help="Installation name (default: main)")
    p_restart.add_argument("--port", "-p", type=int, help="Override port (default: 8188)")
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
    p_deploy.set_defaults(func=cmd_deploy)

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
