"""Allow running as `python -m comfy_runner_server`."""
import argparse
from comfy_runner_server.server import run_server

parser = argparse.ArgumentParser(prog="comfy-runner-server")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", "-p", type=int, default=9189)
args = parser.parse_args()
run_server(host=args.host, port=args.port)
