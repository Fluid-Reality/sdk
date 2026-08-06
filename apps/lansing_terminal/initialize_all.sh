#!/usr/bin/env sh
# Linux/macOS launcher for the shared Lansing initialization workflow.
# Override the default interpreter with PYTHON_EXECUTABLE=/path/to/python.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_COMMAND=${PYTHON_EXECUTABLE:-python3}

exec "$PYTHON_COMMAND" "$SCRIPT_DIR/initialize_all.py" "$@"
