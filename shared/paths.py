"""
Path resolution — single source of truth for filesystem layout.

All agents, modules and CLI commands must use this module to resolve
absolute paths. Hardcoded `~/Desktop/...`, `/tmp/...` or `python3` paths
are forbidden anywhere else.

`WORKFLOW_HOME` env var, if set, overrides the auto-detected repo root.
Otherwise the root is detected via `Path(__file__).parents[1]`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def workflow_home() -> Path:
    """Return absolute path to the claude-workflow repo root."""
    env = os.environ.get("WORKFLOW_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def config_dir() -> Path:
    """User-editable config layer (config/). May not exist before `workflow init`."""
    return workflow_home() / "config"


def config_example_dir() -> Path:
    """Templates shipped with the repo. Always exists."""
    return workflow_home() / "config" / "example"


def style_guides_dir() -> Path:
    """Resolved style-guides root.

    Prefers `config/style-guides/` (post-Phase-2) but falls back to the
    legacy `.claude/style-guides/` location for backward compatibility
    until users run `workflow migrate-legacy`.
    """
    primary = config_dir() / "style-guides"
    if primary.exists():
        return primary
    legacy = workflow_home() / ".claude" / "style-guides"
    return legacy if legacy.exists() else primary


def tasks_dir() -> Path:
    """Per-task working folders (tasks/<project>/<KEY>/)."""
    return workflow_home() / "tasks"


def task_folder(project: str, key: str) -> Path:
    return tasks_dir() / project / key


def cache_dir(scope: Optional[str] = None) -> Path:
    """Ephemeral cache root (was /tmp/...). Cross-platform safe."""
    base = workflow_home() / ".cache"
    return base / scope if scope else base


def jira_attachments_dir(key: str) -> Path:
    """Per-issue attachment download target (replaces /tmp/jira-attachments)."""
    return cache_dir("jira-attachments") / key


def tester_config_path(project: Optional[str] = None) -> Path:
    """Path to tester config.

    Post-Phase-2 lookup order:
      1. `config/tester/<project>.yaml` (per-project)
      2. `config/tester/config.yaml` (shared)
      3. `.claude/tester/config.yaml` (legacy)
    """
    cfg = config_dir() / "tester"
    if project:
        per_project = cfg / f"{project}.yaml"
        if per_project.exists():
            return per_project
    shared = cfg / "config.yaml"
    if shared.exists():
        return shared
    return workflow_home() / ".claude" / "tester" / "config.yaml"


def python_executable() -> str:
    """Portable Python interpreter for subprocess invocations.

    Avoid bare `python3` (absent on stock Windows) and bare `python`
    (may shadow Python 2 on some POSIX boxes). Uses sys.executable
    which is always the interpreter running the current process.
    """
    return sys.executable


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
