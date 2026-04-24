"""Allow running as `python -m comfy_runner_server`.

Delegates to the CLI's ``server`` subcommand so that argument parsing,
tailscale setup, shutdown handlers, and all other server logic is
defined in exactly one place.
"""
import sys

from comfy_runner_cli.cli import main

# Forward all args to the CLI's "server" subcommand.
# e.g. `python -m comfy_runner_server --listen 0.0.0.0 --tailscale`
#   → `comfy-runner server --listen 0.0.0.0 --tailscale`
main(["server"] + sys.argv[1:])
