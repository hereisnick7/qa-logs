"""
Grafana CLI commands. Called from cli/agent.py dispatcher.
run_command() returns True if the command was handled, False otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

_WORKFLOW_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKFLOW_ROOT))

_grafana_clients: dict = {}
_active_account = "default"


def _get_grafana(account: str | None = None):
    """Return a GrafanaClient for `account` (or the active one), cached per account."""
    acc = account or _active_account
    if acc not in _grafana_clients:
        from modules.grafana.client import GrafanaClient
        _grafana_clients[acc] = GrafanaClient(account=acc)
    return _grafana_clients[acc]


def _resolve_active_account(parts: list) -> str:
    """Account for this Grafana command: explicit --account=, else WORKFLOW_ACCOUNT / default."""
    from shared.config_loader import resolve_account
    explicit = next((p.split("=", 1)[1] for p in parts if p.startswith("--account=")), None)
    if explicit:
        return explicit
    return resolve_account("grafana")


def _parse_kv_flags(parts: list) -> dict:
    flags: dict = {}
    for part in parts:
        if part.startswith("--") and "=" in part:
            key, value = part[2:].split("=", 1)
            flags[key] = value
    return flags


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------

_MAX_MSG_CHARS = 2000
# Calibrated against a real sample (973 log lines for one entity_id over 24h):
# median _msg was already 536 chars and legitimate structured business events
# routinely ran 500-1500 — an earlier, much lower cutoff silently amputated a
# field that mattered (a stated status-change reason sitting a bit past it)
# and made a live qa-logs-agent test report [UNKNOWN] for a reason that was
# sitting right there in the log line. 2000 chars only catches the actual
# outlier tail in that sample — verified to be third-party webhook payloads
# (>2500 chars, unrelated push-notification noise), not business events.
# Re-calibrate this against your own data — see config/qa-logs-context.md.


def fmt_log_entry(entry: dict) -> str:
    ts = (entry.get("_time") or "")[:23]
    pod = (
        entry.get("kubernetes.pod_name")
        or entry.get("kubernetes.container_name")
        or "?"
    )
    level = entry.get("level") or ""
    msg = (entry.get("_msg") or "").strip()
    if len(msg) > _MAX_MSG_CHARS:
        msg = f"{msg[:_MAX_MSG_CHARS]}… (+{len(msg) - _MAX_MSG_CHARS} chars, re-query narrower for full line)"
    prefix = f"{ts} | {pod}"
    if level:
        prefix += f" | {level}"
    return f"{prefix} | {msg}"


def fmt_datasource(ds: dict) -> str:
    return f"  {ds.get('uid', '?'):<20} {ds.get('name', '?'):<25} [{ds.get('type', '?')}]"


# ------------------------------------------------------------------
# Command implementations
# ------------------------------------------------------------------

def cmd_grafana_info():
    client = _get_grafana()
    health = client.health()
    print(f"\n  Grafana: {client.base_url}")
    print(f"  Health:  {health}")
    print(f"\n  Datasources:")
    for ds in client.list_datasources():
        print(fmt_datasource(ds))
    print()


def cmd_grafana_logs(query: str, start=None, end=None, last=None, limit: int = 200, datasource_uid=None):
    client = _get_grafana()
    entries = client.query_logs(query, start=start, end=end, last=last, limit=limit, datasource_uid=datasource_uid)
    window = last or f"{start or '…'} .. {end or '…'}"
    print(f"\n  VictoriaLogs — query='{query}' window={window} ({len(entries)} entries):\n")
    if not entries:
        print("  (нет совпадений — если окно шире пары часов, проверь конвертацию времени в UTC перед тем как считать 'логов нет')\n")
        return
    for entry in entries:
        print(fmt_log_entry(entry))
    if len(entries) >= limit:
        print(
            f"\n  ⚠ вернулось ровно limit={limit} строк — результат мог быть обрезан. "
            "Не считай эту выборку полной: сузь --start/--end или добавь AND-фильтр и повтори, "
            "либо сверься с `grafana-hits` на этом же окне (total > limit ⇒ точно обрезано)."
        )
    print()


def cmd_grafana_hits(query: str, start=None, end=None, last=None, step: str = "1h", datasource_uid=None):
    client = _get_grafana()
    hits = client.query_logs_hits(query, start=start, end=end, last=last, step=step, datasource_uid=datasource_uid)
    print(f"\n  VictoriaLogs volume — query='{query}' step={step}:\n")
    if not hits:
        print("  (нет данных)\n")
        return
    for series in hits:
        timestamps = series.get("timestamps") or []
        values = series.get("values") or []
        for ts, v in zip(timestamps, values):
            print(f"  {ts}  {v}")
        print(f"  total: {series.get('total')}")
    print()


# ------------------------------------------------------------------
# Command router
# ------------------------------------------------------------------

GRAFANA_COMMANDS = {"grafana-info", "grafana-logs", "grafana-hits"}


def run_command(cmd: str, parts: list) -> bool:
    """Route a Grafana command. Returns True if handled, False otherwise."""
    if cmd not in GRAFANA_COMMANDS:
        return False

    global _active_account
    _active_account = _resolve_active_account(parts)

    try:
        if cmd == "grafana-info":
            cmd_grafana_info()

        elif cmd == "grafana-logs":
            if len(parts) < 2:
                print('  Использование: grafana-logs "<LogsQL query>" [--start=] [--end=] [--last=1h] [--limit=200]')
            else:
                query_parts = []
                for part in parts[1:]:
                    if not (part.startswith("--") and "=" in part):
                        query_parts.append(part)
                flags = _parse_kv_flags(parts[1:])
                # No blanket .strip('"\'') here (unlike sentry-search's CLI parsing this
                # was copied from) — LogsQL's own phrase-quotes are meaningful and almost
                # always sit at the start/end of the joined string (`"<id>" AND "<kw>"`),
                # so stripping outer quote chars silently corrupted the first and last term.
                query = " ".join(query_parts)
                cmd_grafana_logs(
                    query,
                    start=flags.get("start"),
                    end=flags.get("end"),
                    last=flags.get("last"),
                    limit=int(flags.get("limit", 200)),
                    datasource_uid=flags.get("datasource"),
                )

        elif cmd == "grafana-hits":
            if len(parts) < 2:
                print('  Использование: grafana-hits "<LogsQL query>" [--start=] [--end=] [--last=6h] [--step=1h]')
            else:
                query_parts = []
                for part in parts[1:]:
                    if not (part.startswith("--") and "=" in part):
                        query_parts.append(part)
                flags = _parse_kv_flags(parts[1:])
                # No blanket .strip('"\'') here (unlike sentry-search's CLI parsing this
                # was copied from) — LogsQL's own phrase-quotes are meaningful and almost
                # always sit at the start/end of the joined string (`"<id>" AND "<kw>"`),
                # so stripping outer quote chars silently corrupted the first and last term.
                query = " ".join(query_parts)
                cmd_grafana_hits(
                    query,
                    start=flags.get("start"),
                    end=flags.get("end"),
                    last=flags.get("last"),
                    step=flags.get("step", "1h"),
                    datasource_uid=flags.get("datasource"),
                )

    except Exception as e:
        print(f"  Ошибка: {e}\n")

    return True
