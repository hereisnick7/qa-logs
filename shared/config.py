"""
Policy constants — thin facade over `config/policies.yaml`.

Existing modules (guards.py, gitops_qa.py) import `PROTECTED_REFS` etc. as
module-level names. Phase 2 moves the source-of-truth into YAML; this module
loads it once and exposes the same names so callers don't need to change.

If `config/policies.yaml` is missing (fresh checkout before `workflow init`),
we fall back to safe defaults — the same values that used to be literals here.
"""

from __future__ import annotations

# Defaults — also used when config_loader is unavailable (circular-import edge cases).
_DEFAULT_PROTECTED_REFS = {"master", "main", "release"}
_DEFAULT_PROTECTED_REF_PREFIXES = ("release/",)
_DEFAULT_PROTECTED_ENVIRONMENTS = {"production", "staging"}
_DEFAULT_ALLOWED_QA_SLOTS = {"qa1", "qa2", "qa3", "qa4", "qa5", "qa6"}


def _load() -> dict:
    try:
        from shared.config_loader import load_policies  # local import to avoid cycle
        return load_policies()
    except Exception:
        return {}


_data = _load()

PROTECTED_REFS = set(_data.get("protected_refs") or _DEFAULT_PROTECTED_REFS)
PROTECTED_REF_PREFIXES = tuple(_data.get("protected_ref_prefixes") or _DEFAULT_PROTECTED_REF_PREFIXES)
PROTECTED_ENVIRONMENTS = set(_data.get("protected_environments") or _DEFAULT_PROTECTED_ENVIRONMENTS)
ALLOWED_QA_SLOTS = set(_data.get("allowed_qa_slots") or _DEFAULT_ALLOWED_QA_SLOTS)
