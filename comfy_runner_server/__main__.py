"""Allow running as `python -m comfy_runner_server`."""
import argparse
from comfy_runner_server.server import run_server

parser = argparse.ArgumentParser(prog="comfy-runner-server")
parser.add_argument("--listen", "--host", default="127.0.0.1",
                    help="Bind address (default: 127.0.0.1)")
parser.add_argument("--port", "-p", type=int, default=9189)
parser.add_argument("--tailscale", action="store_true",
                    help="Enable Tailscale serve integration")
parser.add_argument("--tunnels", action="store_true",
                    help="Enable tunnel management (ngrok/tailscale)")
args = parser.parse_args()
run_server(host=args.listen, port=args.port, tailscale=args.tailscale,
           tunnels=args.tunnels)
