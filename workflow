#!/usr/bin/env bash
# claude-workflow shim — calls the CLI without requiring `pip install` / PATH-installed entry-point.
# Safe to call as `workflow <args>` from any cwd (uses absolute path to this file's dir).
set -e
REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
for py in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$py" >/dev/null 2>&1; then
    exec "$py" "$REPO/cli/agent.py" "$@"
  fi
done
echo "error: no python3 found" >&2
exit 1
