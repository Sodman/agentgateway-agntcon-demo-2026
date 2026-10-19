#!/usr/bin/env bash
# Writes the budgets and the model allow-list from the README through the API.
#   ./scripts/apply-policies.sh [5-hour USD] [weekly USD]     (defaults: 0.02 and 1)
# nightly-agent gets a 5-hour and a weekly budget (Block); intern is limited to *-mini models.
set -euo pipefail
exec python3 "$(cd "$(dirname "$0")" && pwd)/agw.py" apply-policies "$@"
