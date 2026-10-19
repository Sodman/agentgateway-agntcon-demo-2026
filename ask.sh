#!/usr/bin/env bash
# One request as a virtual key, by name:      ./ask.sh alice claude-sonnet-5
# Keep going until the key gets blocked:      ./ask.sh nightly-agent claude-sonnet-5 --until-blocked
set -euo pipefail
exec python3 "$(cd "$(dirname "$0")" && pwd)/scripts/agw.py" ask "$@"
