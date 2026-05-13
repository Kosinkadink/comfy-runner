"""Pre-flight model provisioning for test suites.

Test suites declare a ``models`` manifest in ``suite.json``
(``[{name, directory, url, [token]}]``).  Before the suite runs we
make sure each entry exists on the target by calling the comfy-runner
server's ``POST /{install}/download-model`` endpoint.  Existing files
are skipped server-side, so reruns are essentially free.
"""

from __future__ import annotations

from typing import Callable

import requests

from comfy_runner.hosted.remote import RemoteRunner

from .suite import Suite


def ensure_suite_models(
    runner: RemoteRunner,
    install_name: str,
    suite: Suite,
    timeout: int = 1800,
    send_output: Callable[[str], None] | None = None,
    comfy_url: str | None = None,
) -> dict[str, int]:
    """Download every model declared in *suite* onto the target.

    Returns a summary dict::

        {"requested": <int>, "skipped": <int>, "downloaded": <int>}

    Raises ``RuntimeError`` if any download job fails.  Individual
    download jobs are polled with ``timeout`` seconds each (default
    1800s = 30 min, since model files can be large).
    """
    out = send_output or (lambda _: None)

    if not suite.models:
        return {"requested": 0, "skipped": 0, "downloaded": 0}

    summary = {"requested": len(suite.models), "skipped": 0, "downloaded": 0}
    out(f"Pre-flight: ensuring {len(suite.models)} model(s) on '{install_name}'\n")

    for entry in suite.models:
        name = entry["name"]
        directory = entry["directory"]
        url = entry["url"]
        token = entry.get("token", "")

        resp = runner.download_model(
            install_name,
            url=url,
            directory=directory,
            filename=name,
            token=token,
        )

        if resp.get("skipped"):
            summary["skipped"] += 1
            out(f"  - {directory}/{name}: already present\n")
            continue

        job_id = resp.get("job_id")
        if not job_id:
            raise RuntimeError(
                f"Download of {directory}/{name} returned no job_id: {resp!r}"
            )

        out(f"  - {directory}/{name}: downloading...\n")
        runner.poll_job(job_id, timeout=timeout, on_output=out)
        summary["downloaded"] += 1

    out(
        f"Pre-flight complete: {summary['skipped']} already present, "
        f"{summary['downloaded']} downloaded.\n"
    )

    # If we downloaded any new files, refresh ComfyUI's folder_paths cache
    # by hitting /object_info.  ComfyUI rebuilds INPUT_TYPES (and thus the
    # filename dropdowns for UNETLoader/CLIPLoader/etc.) on each call, so
    # newly downloaded models become visible without a server restart.
    if summary["downloaded"] > 0 and comfy_url:
        try:
            requests.get(f"{comfy_url.rstrip('/')}/object_info", timeout=60)
            out("Refreshed ComfyUI model lists via /object_info.\n")
        except requests.RequestException as exc:
            out(f"Warning: failed to refresh /object_info: {exc}\n")

    return summary
