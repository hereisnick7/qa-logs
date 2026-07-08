#!/usr/bin/env python3
"""
workflow CLI — entry point for qa-logs agent.
Handles Grafana/VictoriaLogs and Jira commands only.

Usage:
    workflow <command> [args]
    workflow help
"""

import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

_WORKFLOW_ROOT = Path(__file__).resolve().parent.parent
if str(_WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKFLOW_ROOT))

from modules.grafana.cli import run_command as grafana_run  # noqa: E402
from modules.jira.cli import run_command as jira_run  # noqa: E402

HELP = """
  Grafana / VictoriaLogs (read-only, LogsQL):
    grafana-info                      — health check + datasources list
    grafana-logs "<query>" [--start=] [--end=] [--last=1h] [--limit=200]
    grafana-hits "<query>" [--start=] [--end=] [--last=6h] [--step=1h]  — volume histogram

  Jira:
    jira-task <KEY>                       — task details + comments
    jira-comments <KEY>                   — read back comments
    jira-mine [--status=...]              — my issues
    jira-search "<jql>"                   — search issues

  help                                    — this message
"""


def run_command(parts: list):
    cmd = parts[0].lower() if parts else ""

    if grafana_run(cmd, parts):
        return
    if jira_run(cmd, parts):
        return

    if cmd in ("help", "?", "h"):
        print(HELP)
    elif cmd in ("exit", "quit", "q"):
        sys.exit(0)
    else:
        print(f"  Unknown command: '{cmd}'. Type 'help'.")


def interactive():
    print("\n  workflow CLI (qa-logs)")
    print("  Type 'help' for commands.\n")
    while True:
        try:
            line = input("workflow> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        run_command(line.split())


def main(argv: list = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    if not args:
        interactive()
        return 0
    try:
        run_command(args)
        return 0
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
