#!/usr/bin/env bash
# Checks tools, provider keys and models, network, free ports, and the budget windows.
set -euo pipefail
exec python3 "$(cd "$(dirname "$0")" && pwd)/agw.py" preflight "$@"
