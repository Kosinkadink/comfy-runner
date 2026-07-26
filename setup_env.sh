#!/usr/bin/env bash
# Setup Python venv for comfy-runner on Linux/macOS
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -x "$VENV_DIR/bin/python" ]; then
    if [ -d "$VENV_DIR" ]; then
        echo "Recreating incomplete virtual environment..."
        python3 -m venv --clear "$VENV_DIR"
    else
        echo "Creating virtual environment..."
        python3 -m venv "$VENV_DIR"
    fi
elif ! "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
    echo "Repairing incomplete virtual environment..."
    python3 -m venv --upgrade "$VENV_DIR"
else
    echo "Virtual environment already exists."
fi

echo "Installing dependencies..."
"$VENV_DIR/bin/python" -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt"

echo ""
echo "Setup complete. Run comfy-runner with:"
echo "  .venv/bin/python comfy_runner.py <command>"
