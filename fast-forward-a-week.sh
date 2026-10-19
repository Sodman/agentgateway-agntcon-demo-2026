#!/usr/bin/env bash
# Skip ahead a week. Adds the remaining providers, models and keys through the same
# API the UI uses, replays a week's worth of traffic, then dates those log rows across the past 7 days.
# Options: --scale 0.5 (fewer requests)  --concurrency 8
set -euo pipefail
exec python3 "$(cd "$(dirname "$0")" && pwd)/scripts/agw.py" fast-forward "$@"
