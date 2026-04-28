"""Process spawn, kill, health-check, port management.

Mirrors ComfyUI-Launcher process.ts: spawnProcess, killProcTree,
waitForReady, findPidsByPort, port lock files.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import requests

from .config import get_installation, set_installation
from .environment import get_active_python_path, migrate_env_layout

DEFAULT_PORT = 8188
PORT_SEARCH_RANGE = 100  # try up to 100 ports above the default
HEALTH_ENDPOINT = "/api/system_stats"
HEALTH_TIMEOUT_S = 300
HEALTH_POLL_INTERVAL_S = 1.0
STOP_TIMEOUT_S = 10
PORT_RELEASE_TIMEOUT_S = 5

# Sensitive arg names whose *values* should be redacted in logged commands
_SENSITIVE_ARG_RE_STR = r"^--(api[-_]?key|token|secret|password|auth)$"


# ---------------------------------------------------------------------------
# Pidfile — persists running state per installation
# ---------------------------------------------------------------------------

def _pidfile_path(install_path: str | Path) -> Path:
    return Path(install_path) / ".comfy-runner.pid"


def _write_pidfile(install_path: str | Path, pid: int, port: int) -> None:
    data = {"pid": pid, "port": port, "started_at": time.time()}
    _pidfile_path(install_path).write_text(
        json.dumps(data) + "\n", encoding="utf-8"
    )


def _read_pidfile(install_path: str | Path) -> dict[str, Any] | None:
    pf = _pidfile_path(install_path)
    if not pf.exists():
        return None
    try:
        return json.loads(pf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _remove_pidfile(install_path: str | Path) -> None:
    _pidfile_path(install_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Process alive check
# ---------------------------------------------------------------------------

def is_process_alive(pid: int) -> bool:
    """Check whether a process with the given PID is running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # alive but we can't signal it


# ---------------------------------------------------------------------------
# Port helpers — mirror process.ts findPidsByPort, findAvailablePort
# ---------------------------------------------------------------------------

def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Quick TCP connect check."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect((host, port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def find_pids_by_port(port: int) -> list[int]:
    """Find PIDs listening on a TCP port. Mirrors process.ts findPidsByPort."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (subprocess.TimeoutExpired, OSError):
            return []
        pids: set[int] = set()
        target = f":{port}"
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING":
                addr = parts[1]
                if addr.endswith(target):
                    try:
                        pid = int(parts[4])
                        if pid > 0:
                            pids.add(pid)
                    except ValueError:
                        pass
        return list(pids)
    else:
        try:
            result = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []
        pids_list = []
        for s in result.stdout.strip().split():
            try:
                pid = int(s)
                if pid > 0:
                    pids_list.append(pid)
            except ValueError:
                pass
        return pids_list


# ---------------------------------------------------------------------------
# Port lock files — mirrors process.ts writePortLock/readPortLock/removePortLock
# Stored under ~/.comfy-runner/port-locks/ (own namespace for now).
# ---------------------------------------------------------------------------

def _port_lock_dir() -> Path:
    """Port lock directory under comfy-runner's own config dir."""
    from .config import CONFIG_DIR
    return CONFIG_DIR / "port-locks"


def write_port_lock(port: int, pid: int, installation_name: str) -> None:
    """Write a port lock file so Desktop 2.0 can identify the owner."""
    lock_dir = _port_lock_dir()
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        data = {"pid": pid, "installationName": installation_name, "timestamp": int(time.time() * 1000)}
        (lock_dir / f"port-{port}.json").write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def read_port_lock(port: int) -> dict[str, Any] | None:
    """Read a port lock. Returns None if stale or missing."""
    lock_file = _port_lock_dir() / f"port-{port}.json"
    try:
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        if not data or not data.get("pid") or not is_process_alive(data["pid"]):
            remove_port_lock(port)
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def remove_port_lock(port: int) -> None:
    """Remove a port lock file."""
    try:
        (_port_lock_dir() / f"port-{port}.json").unlink(missing_ok=True)
    except OSError:
        pass


def find_available_port(
    start_port: int,
    host: str = "127.0.0.1",
    search_range: int = PORT_SEARCH_RANGE,
) -> int | None:
    """Find the next available port starting from start_port+1.

    Mirrors process.ts findAvailablePort. Returns None if no port found.
    """
    for port in range(start_port + 1, start_port + 1 + search_range):
        if not is_port_in_use(port, host):
            # Also check port locks — another launcher may own this port
            lock = read_port_lock(port)
            if lock is None:
                return port
    return None


def wait_for_port_release(
    port: int,
    timeout_s: float = PORT_RELEASE_TIMEOUT_S,
    send_output: Callable[[str], None] | None = None,
) -> bool:
    """Wait until a port is no longer in use. Returns True if released."""
    if not is_port_in_use(port):
        return True
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(0.3)
        if not is_port_in_use(port):
            return True
    if send_output:
        send_output(f"⚠ Port {port} still in use after {timeout_s}s\n")
    return False


def kill_port_occupants(
    port: int,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Find and kill any processes listening on a port."""
    pids = find_pids_by_port(port)
    if not pids:
        return
    if send_output:
        send_output(f"Killing stale process(es) on port {port}: {pids}\n")
    for pid in pids:
        kill_process_tree(pid)


# ---------------------------------------------------------------------------
# Build launch command — mirrors standalone.ts getLaunchCommand
# ---------------------------------------------------------------------------

def _parse_port_from_args(args: list[str]) -> int:
    """Extract --port value from args, or return DEFAULT_PORT."""
    try:
        idx = args.index("--port")
        if idx + 1 < len(args):
            return int(args[idx + 1])
    except (ValueError, IndexError):
        pass
    return DEFAULT_PORT


def build_launch_command(
    install_path: str | Path,
    extra_args: str = "",
    port_override: int | None = None,
) -> dict[str, Any]:
    """Build the command to launch ComfyUI.

    Returns dict with: cmd, args, cwd, port.
    Mirrors standalone.ts getLaunchCommand:
      {envPython} -s ComfyUI/main.py {launch_args}
    """
    install_path = Path(install_path)
    python_path = get_active_python_path(install_path)
    main_py = install_path / "ComfyUI" / "main.py"

    if not python_path:
        raise RuntimeError(f"Python not found for installation at {install_path}")
    if not main_py.exists():
        raise RuntimeError(f"ComfyUI main.py not found at {main_py}")

    # Build args: -s ComfyUI/main.py {user_args}
    args = ["-s", str(Path("ComfyUI") / "main.py")]

    if extra_args.strip():
        try:
            # posix=False on Windows to preserve backslash paths
            args.extend(shlex.split(extra_args, posix=(sys.platform != "win32")))
        except ValueError:
            args.extend(extra_args.split())

    # Auto-inject shared model paths config if configured
    from .config import get_shared_dir
    shared_dir = get_shared_dir()
    if shared_dir:
        from .shared_paths import get_shared_io_args, sync_custom_model_folders
        # Pre-launch sync: discover extra folders, create in shared, write YAML
        sync = sync_custom_model_folders(install_path, shared_dir)
        args.extend(["--extra-model-paths-config", sync["yaml_path"]])
        # Add --input-directory / --output-directory if not already specified
        if "--input-directory" not in args and "--output-directory" not in args:
            args.extend(get_shared_io_args(shared_dir))

    # Determine port
    port = _parse_port_from_args(args)
    if port_override is not None:
        # Replace or append --port
        try:
            idx = args.index("--port")
            args[idx + 1] = str(port_override)
        except (ValueError, IndexError):
            args.extend(["--port", str(port_override)])
        port = port_override

    return {
        "cmd": str(python_path),
        "args": args,
        "cwd": str(install_path),
        "port": port,
    }


# ---------------------------------------------------------------------------
# Spawn + health check — mirrors process.ts spawnProcess + waitForPort
# ---------------------------------------------------------------------------

def _redact_cmd_line(cmd: str, args: list[str]) -> str:
    """Build a display command line with sensitive arg values redacted."""
    import re
    parts = [cmd]
    for i, arg in enumerate(args):
        if i > 0 and re.match(_SENSITIVE_ARG_RE_STR, args[i - 1], re.IGNORECASE):
            parts.append('"***"')
        else:
            parts.append(f'"{arg}"' if " " in arg else arg)
    return " ".join(parts)


def _merge_env(
    record_env: dict[str, str] | None,
    overrides: dict[str, str] | None,
) -> dict[str, str] | None:
    """Merge persisted env vars with runtime overrides. Returns None if empty."""
    merged = dict(record_env or {})
    if overrides:
        merged.update(overrides)
    return merged or None


def spawn_comfyui(
    install_path: str | Path,
    extra_args: str = "",
    port_override: int | None = None,
    port_conflict: str = "auto",
    installation_name: str = "",
    stdout: Any = None,
    stderr: Any = None,
    send_output: Callable[[str], None] | None = None,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Spawn the ComfyUI process.

    port_conflict controls behaviour when the port is busy:
      "auto" — find the next free port automatically (mirrors Desktop 2.0)
      "fail" — raise immediately

    When port_override is set, the port was explicitly chosen by the user,
    so auto-increment is skipped regardless of port_conflict.

    stdout/stderr default to subprocess.DEVNULL to avoid pipe deadlocks
    in background mode. Pass subprocess.PIPE for foreground streaming.

    Returns dict with: pid, port, process (subprocess.Popen).
    """
    launch = build_launch_command(install_path, extra_args, port_override)
    port = launch["port"]
    port_is_explicit = port_override is not None

    # Check port conflict before spawning
    if is_port_in_use(port) or read_port_lock(port) is not None:
        if port_conflict == "auto" and not port_is_explicit:
            next_port = find_available_port(port)
            if next_port is not None:
                if send_output:
                    send_output(f"Port {port} is busy, using {next_port} instead\n")
                # Update the args with the new port
                try:
                    idx = launch["args"].index("--port")
                    launch["args"][idx + 1] = str(next_port)
                except (ValueError, IndexError):
                    launch["args"].extend(["--port", str(next_port)])
                port = next_port
                launch["port"] = next_port
            else:
                raise RuntimeError(
                    f"Port {port} is in use and no free port found in range "
                    f"{port + 1}–{port + PORT_SEARCH_RANGE}."
                )
        else:
            pids = find_pids_by_port(port)
            pid_info = f" (PIDs: {pids})" if pids else ""
            raise RuntimeError(
                f"Port {port} is already in use{pid_info}. "
                f"Use --port to specify a different port."
            )

    cmd_line = _redact_cmd_line(launch["cmd"], launch["args"])
    if send_output:
        send_output(f"> {cmd_line}\n\n")

    # Spawn with PYTHONIOENCODING=utf-8 (mirrors Desktop 2.0)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env_overrides or {})}

    kwargs: dict[str, Any] = {
        "cwd": launch["cwd"],
        "stdout": stdout if stdout is not None else subprocess.DEVNULL,
        "stderr": stderr if stderr is not None else subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen([launch["cmd"], *launch["args"]], **kwargs)

    # Write pidfile + Desktop 2.0-compatible port lock
    _write_pidfile(install_path, proc.pid, port)
    write_port_lock(port, proc.pid, installation_name)

    return {"pid": proc.pid, "port": port, "process": proc}


def wait_for_ready(
    port: int,
    host: str = "127.0.0.1",
    timeout_s: float = HEALTH_TIMEOUT_S,
    interval_s: float = HEALTH_POLL_INTERVAL_S,
    pid: int | None = None,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Poll /api/system_stats until ComfyUI is ready. Mirrors waitForPort.

    If pid is provided, also checks that the process hasn't exited early.
    """
    url = f"http://{host}:{port}{HEALTH_ENDPOINT}"
    start = time.monotonic()
    attempt = 0

    while True:
        elapsed = time.monotonic() - start
        if elapsed > timeout_s:
            raise RuntimeError(
                f"Timed out waiting for ComfyUI on port {port} "
                f"after {int(elapsed)}s"
            )

        # Check if process died early
        if pid is not None and not is_process_alive(pid):
            raise RuntimeError(
                f"ComfyUI process (PID {pid}) exited before becoming ready."
            )

        attempt += 1
        if send_output and attempt % 5 == 1:
            send_output(f"\rWaiting for ComfyUI... ({int(elapsed)}s)")

        try:
            resp = requests.get(url, timeout=2)
            if resp.status_code == 200:
                if send_output:
                    send_output(f"\rComfyUI ready on port {port} ({int(elapsed)}s)\n")
                return
        except (requests.ConnectionError, requests.Timeout):
            pass

        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# Process tree kill — mirrors process.ts killProcTree / killProcessTree
# ---------------------------------------------------------------------------

def kill_process_tree(pid: int) -> None:
    """Kill a process and all its children. Mirrors process.ts killProcessTree."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        # taskkill /T /F kills the entire process tree
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
    else:
        # Kill the process group
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass


# ---------------------------------------------------------------------------
# High-level start / stop / status
# ---------------------------------------------------------------------------

def start_installation(
    name: str,
    port_override: int | None = None,
    port_conflict: str = "auto",
    extra_args: str | None = None,
    send_output: Callable[[str], None] | None = None,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Start a ComfyUI installation in background mode. Returns running state dict."""
    record = get_installation(name)
    if not record:
        raise RuntimeError(f"Installation '{name}' not found.")

    if record.get("status") != "installed":
        raise RuntimeError(
            f"Installation '{name}' is not ready (status: {record.get('status')})."
        )

    install_path = record["path"]

    # Migrate legacy envs/default → ComfyUI/.venv on first launch
    migrate_env_layout(install_path, send_output)

    # Check if already running
    pf = _read_pidfile(install_path)
    if pf and is_process_alive(pf["pid"]):
        raise RuntimeError(
            f"Installation '{name}' is already running "
            f"(PID {pf['pid']} on port {pf['port']})."
        )

    # Merge launch args: record's launch_args + any extra_args
    launch_args = record.get("launch_args", "") or ""
    if extra_args:
        launch_args = f"{launch_args} {extra_args}".strip()

    merged_env = _merge_env(record.get("env"), env_overrides)

    if send_output:
        send_output(f"Starting '{name}'...\n")

    # Rotate previous log and open a fresh one for this session
    from .log_utils import open_log
    log_fh, log_file = open_log(install_path)
    log_fh.flush()

    # Capture baseline model folders BEFORE spawn so custom-node folders
    # created during boot show up in the diff.
    from .config import get_shared_dir
    shared_dir = get_shared_dir()
    pre_extras: set[str] | None = None
    if shared_dir:
        from .shared_paths import discover_extra_model_folders
        pre_extras = discover_extra_model_folders(install_path, shared_dir)

    try:
        result = spawn_comfyui(
            install_path=install_path,
            extra_args=launch_args,
            port_override=port_override,
            port_conflict=port_conflict,
            installation_name=name,
            stdout=log_fh,
            stderr=log_fh,
            send_output=send_output,
            env_overrides=merged_env or None,
        )
    except Exception:
        log_fh.close()
        raise

    # Close the file handle in the parent — the child process inherited a
    # copy of the fd.  Keeping it open here locks the file on Windows and
    # prevents rotate_log() from renaming it on next restart.
    log_fh.close()

    # Wait for health check — kill the process if it never becomes ready
    try:
        wait_for_ready(
            port=result["port"],
            pid=result["pid"],
            send_output=send_output,
        )
    except RuntimeError:
        # Clean up the spawned process so it doesn't linger
        kill_process_tree(result["pid"])
        _remove_pidfile(install_path)
        remove_port_lock(result["port"])
        raise

    # Post-boot: check if custom nodes created new model folders
    if shared_dir and pre_extras is not None:
        from .shared_paths import sync_custom_model_folders
        sync_result = sync_custom_model_folders(install_path, shared_dir, pre_extras)
        if sync_result["new_folders"]:
            new = sync_result["new_folders"]
            if send_output:
                send_output(
                    f"\nNew model folders detected ({', '.join(new)}) "
                    f"— restarting with updated config...\n"
                )
            kill_process_tree(result["pid"])
            # Wait for process to die
            deadline = time.monotonic() + STOP_TIMEOUT_S
            while time.monotonic() < deadline and is_process_alive(result["pid"]):
                time.sleep(0.3)
            _remove_pidfile(install_path)
            remove_port_lock(result["port"])
            # Kill orphaned children and wait for port release before respawn
            if is_port_in_use(result["port"]):
                kill_port_occupants(result["port"], send_output=send_output)
            wait_for_port_release(result["port"], send_output=send_output)

            # Append to current log and respawn (YAML was already rewritten by sync)
            log_fh2 = open(log_file, "a", encoding="utf-8")
            log_fh2.write(f"\n--- restart (model folders: {', '.join(new)}) ---\n")
            log_fh2.flush()
            result = spawn_comfyui(
                install_path=install_path,
                extra_args=launch_args,
                port_override=result["port"],
                port_conflict="auto",
                installation_name=name,
                stdout=log_fh2,
                stderr=log_fh2,
                send_output=send_output,
                env_overrides=merged_env or None,
            )
            log_fh2.close()
            wait_for_ready(
                port=result["port"],
                pid=result["pid"],
                send_output=send_output,
            )

    if send_output:
        send_output(f"\n✓ '{name}' is running (PID {result['pid']}, port {result['port']})\n")

    return {
        "name": name,
        "pid": result["pid"],
        "port": result["port"],
        "started_at": time.time(),
    }


def stop_installation(
    name: str,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Stop a running ComfyUI installation."""
    record = get_installation(name)
    if not record:
        raise RuntimeError(f"Installation '{name}' not found.")

    install_path = record["path"]
    pf = _read_pidfile(install_path)

    if not pf:
        raise RuntimeError(f"Installation '{name}' is not running (no pidfile).")

    pid = pf["pid"]
    if not is_process_alive(pid):
        _remove_pidfile(install_path)
        raise RuntimeError(
            f"Installation '{name}' is not running (PID {pid} is dead)."
        )

    if send_output:
        send_output(f"Stopping '{name}' (PID {pid})...\n")

    port = pf.get("port")

    kill_process_tree(pid)

    # Wait for process to exit
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            break
        time.sleep(0.3)

    if is_process_alive(pid):
        if send_output:
            send_output(f"⚠ Process {pid} did not exit within {STOP_TIMEOUT_S}s\n")
    else:
        if send_output:
            send_output(f"✓ '{name}' stopped.\n")

    _remove_pidfile(install_path)
    if port:
        remove_port_lock(port)
        # Kill any orphaned child processes still holding the port
        if is_port_in_use(port):
            kill_port_occupants(port, send_output=send_output)
        wait_for_port_release(port, send_output=send_output)


def get_status(name: str) -> dict[str, Any]:
    """Get the running status of an installation."""
    record = get_installation(name)
    if not record:
        raise RuntimeError(f"Installation '{name}' not found.")

    install_path = record["path"]
    pf = _read_pidfile(install_path)

    result: dict[str, Any] = {
        "name": name,
        "status": record.get("status", "unknown"),
        "path": install_path,
        "running": False,
    }

    if pf:
        pid = pf["pid"]
        alive = is_process_alive(pid)
        if alive:
            result["running"] = True
            result["pid"] = pid
            result["port"] = pf["port"]
            result["started_at"] = pf.get("started_at")
            if result["started_at"]:
                result["uptime_s"] = time.time() - result["started_at"]
            # Health check
            try:
                resp = requests.get(
                    f"http://127.0.0.1:{pf['port']}{HEALTH_ENDPOINT}",
                    timeout=1,
                )
                result["healthy"] = resp.status_code == 200
            except (requests.ConnectionError, requests.Timeout):
                result["healthy"] = False
        else:
            # Stale pidfile
            _remove_pidfile(install_path)

    return result


def get_log_output(
    name: str,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Show recent log output for an installation.

    Reads from the current session's log file.
    """
    from .log_utils import read_current_log

    record = get_installation(name)
    if not record:
        raise RuntimeError(f"Installation '{name}' not found.")

    result = read_current_log(record["path"], max_lines=50)
    if not result["lines"]:
        if send_output:
            pf = _read_pidfile(record["path"])
            if pf and is_process_alive(pf["pid"]):
                send_output(
                    f"No log file found for '{name}'.\n"
                    f"Process is running as PID {pf['pid']} on port {pf['port']}.\n"
                )
            else:
                send_output(f"No log file found for '{name}'.\n")
        return

    if send_output:
        for line in result["lines"]:
            send_output(line + "\n")


def start_foreground(
    name: str,
    port_override: int | None = None,
    port_conflict: str = "auto",
    extra_args: str | None = None,
    send_output: Callable[[str], None] | None = None,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Start ComfyUI and stream output to send_output, blocking until exit.

    This is the primary 'start' mode: spawns the process, streams stdout/stderr
    to send_output in real time, and writes to a log file. Blocks until the
    process exits or KeyboardInterrupt.
    """
    record = get_installation(name)
    if not record:
        raise RuntimeError(f"Installation '{name}' not found.")

    if record.get("status") != "installed":
        raise RuntimeError(
            f"Installation '{name}' is not ready (status: {record.get('status')})."
        )

    install_path = record["path"]

    # Check if already running
    pf = _read_pidfile(install_path)
    if pf and is_process_alive(pf["pid"]):
        raise RuntimeError(
            f"Installation '{name}' is already running "
            f"(PID {pf['pid']} on port {pf['port']})."
        )

    # Merge launch args
    launch_args = record.get("launch_args", "") or ""
    if extra_args:
        launch_args = f"{launch_args} {extra_args}".strip()

    merged_env = _merge_env(record.get("env"), env_overrides)

    result = spawn_comfyui(
        install_path=install_path,
        extra_args=launch_args,
        port_override=port_override,
        port_conflict=port_conflict,
        installation_name=name,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        send_output=send_output,
        env_overrides=merged_env or None,
    )

    proc = result["process"]
    port = result["port"]

    from .log_utils import LOG_FILENAME, rotate_log
    rotate_log(install_path)
    log_file = Path(install_path) / LOG_FILENAME

    if send_output:
        send_output(f"ComfyUI started (PID {proc.pid}, port {port})\n\n")

    try:
        with open(log_file, "w", encoding="utf-8") as lf:
            while True:
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    text = line.decode("utf-8", errors="replace")
                    if send_output:
                        send_output(text)
                    lf.write(text)
                    lf.flush()
    except KeyboardInterrupt:
        if send_output:
            send_output("\nInterrupted — stopping ComfyUI...\n")
        kill_process_tree(proc.pid)
        proc.wait(timeout=STOP_TIMEOUT_S)
    finally:
        if proc.poll() is None:
            kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                pass
        _remove_pidfile(install_path)
        remove_port_lock(port)

    exit_code = proc.returncode
    if send_output:
        send_output(f"\nComfyUI exited with code {exit_code}\n")

    return {"exit_code": exit_code, "pid": proc.pid, "port": port}
