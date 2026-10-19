#!/usr/bin/env bash
# Start over. Stops the demo gateway and deletes the demo's
# binary, config and database (budget usage included). Nothing outside $DEMO_HOME is touched.
set -euo pipefail
: "${DEMO_HOME:?run 'source demo.env' first}"
case "$DEMO_HOME" in "" | "/" | "$HOME" | "$HOME/") echo "refusing to delete DEMO_HOME=$DEMO_HOME" >&2; exit 1 ;; esac
if [ "${1:-}" != "-y" ]; then
  printf 'Delete %s (binary, config, database)? [y/N] ' "$DEMO_HOME"
  read -r answer
  case "$answer" in y | Y) ;; *) echo "nothing changed"; exit 1 ;; esac
fi
# stop the demo gateway: any agentgateway process whose binary lives under $DEMO_HOME
for pid in $(pgrep -x agentgateway 2>/dev/null || true); do
  exe=$(readlink "/proc/$pid/exe" 2>/dev/null || lsof -p "$pid" -Fn 2>/dev/null | awk '/^n.*agentgateway$/ {sub(/^n/, ""); print; exit}')
  case "$exe" in "$DEMO_HOME"/*) kill "$pid" && echo "stopped the demo gateway (pid $pid)" ;; esac
done
rm -rf "$DEMO_HOME"
mkdir -p "$DEMO_HOME/bin"
echo "✓ fresh start: $DEMO_HOME is empty. Next: ./scripts/preflight.sh"
