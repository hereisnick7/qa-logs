"""
Grafana API client. Read-only — proxies LogsQL queries to a VictoriaLogs
datasource through Grafana's datasource proxy, so a single Grafana service
account token is enough (no direct VictoriaLogs credentials needed).

No write methods exist; there is nothing to guard.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from shared.config_loader import load_module_env

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_DURATION_RE = re.compile(r"^(\d+)(s|m|h|d|w)$")

_DURATION_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}

VICTORIALOGS_DATASOURCE_TYPE = "victoriametrics-logs-datasource"


def strip_ansi(text: str) -> str:
    """Remove ANSI color codes — many backend pods (loguru/uvicorn) log with color escapes."""
    return _ANSI_RE.sub("", text or "")


def parse_duration(spec: str) -> timedelta:
    """Parse a relative duration like '30m', '6h', '2d', '1w' into a timedelta."""
    m = _DURATION_RE.match(spec.strip())
    if not m:
        raise ValueError(f"Invalid duration '{spec}' — expected <N><s|m|h|d|w>, e.g. '90m', '6h', '2d'")
    value, unit = m.groups()
    return timedelta(**{_DURATION_UNITS[unit]: int(value)})


class GrafanaClient:
    def __init__(self, account: str = "default"):
        env = load_module_env("grafana", account)
        token = env.get("GRAFANA_TOKEN", "")
        url = env.get("GRAFANA_URL", "")
        if not token:
            raise RuntimeError(
                f"Set GRAFANA_TOKEN for account '{account}' in config/grafana/.env "
                f"(bare keys, or GRAFANA__{account.upper()}__* for multi-account). "
                "Service account token, Viewer role is enough."
            )
        if not url:
            raise RuntimeError(
                f"Set GRAFANA_URL for account '{account}' in config/grafana/.env"
            )
        self.account = account
        self.base_url = url.rstrip("/")
        self.default_logs_datasource_uid = env.get("GRAFANA_LOGS_DATASOURCE_UID", "")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        _adapter = HTTPAdapter(
            max_retries=Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504]),
        )
        self.session.mount("https://", _adapter)
        self.session.mount("http://", _adapter)
        self._logs_uid_cache: Optional[str] = self.default_logs_datasource_uid or None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None, raw: bool = False):
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params, timeout=(5, 60))
        resp.raise_for_status()
        return resp.text if raw else resp.json()

    # ------------------------------------------------------------------
    # Health & datasources
    # ------------------------------------------------------------------

    def health(self) -> dict:
        return self._get("/api/health")

    def list_datasources(self) -> list:
        raw = self._get("/api/datasources")
        return [
            {"uid": d.get("uid"), "name": d.get("name"), "type": d.get("type")}
            for d in (raw if isinstance(raw, list) else [])
        ]

    def logs_datasource_uid(self) -> str:
        """UID of the VictoriaLogs datasource — GRAFANA_LOGS_DATASOURCE_UID override, else auto-detect by type."""
        if self._logs_uid_cache:
            return self._logs_uid_cache
        for ds in self.list_datasources():
            if ds.get("type") == VICTORIALOGS_DATASOURCE_TYPE:
                self._logs_uid_cache = ds["uid"]
                return self._logs_uid_cache
        raise RuntimeError(
            "No VictoriaLogs datasource found in Grafana. "
            "Set GRAFANA_LOGS_DATASOURCE_UID explicitly in config/grafana/.env."
        )

    # ------------------------------------------------------------------
    # Logs (LogsQL via VictoriaLogs)
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_time_range(
        start: Optional[str] = None,
        end: Optional[str] = None,
        last: Optional[str] = None,
    ) -> tuple:
        """
        Returns (start, end) as RFC3339 strings, or (None, None) if none of
        start/end/last is given (query then covers whatever LogsQL's own
        `_time:` filter, if any, or VictoriaLogs' own default window implies).

        `last` (e.g. '90m', '6h', '2d') takes precedence and is computed
        against "now" in UTC — wall-clock time, not the workflow-script
        Date.now() ban (this is plain Python, not a Workflow script).
        """
        if last:
            end_dt = datetime.now(timezone.utc)
            start_dt = end_dt - parse_duration(last)
            return start_dt.isoformat().replace("+00:00", "Z"), end_dt.isoformat().replace("+00:00", "Z")
        return start, end

    def query_logs(
        self,
        query: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        last: Optional[str] = None,
        limit: int = 200,
        datasource_uid: Optional[str] = None,
    ) -> list:
        """
        Run a LogsQL query against VictoriaLogs through the Grafana proxy.
        Returns entries sorted oldest→newest, each a dict with at least
        `_time` (RFC3339) and `_msg` (ANSI stripped).

        VictoriaLogs applies `limit` to whatever order its parallel shard scan
        happens to produce, NOT to a globally time-sorted stream — confirmed
        empirically: a bare `<query>` with `limit=10` returned an arbitrary
        subset skipping hours of matches, while `<query> | sort by (_time)`
        with the same limit returned the true earliest N. Without the pipe,
        truncation (when actual matches > limit) silently drops evidence in an
        unpredictable pattern instead of predictably dropping "the tail" — a
        root-cause search must never see that. So: append `| sort by (_time)`
        whenever the caller's query has no pipe of its own (an existing pipe,
        e.g. a `| stats ...` aggregation, is left alone — the caller owns
        ordering once they've reshaped the stream).
        """
        uid = datasource_uid or self.logs_datasource_uid()
        start, end = self.resolve_time_range(start, end, last)
        effective_query = query if "|" in query else f"{query} | sort by (_time)"
        params: dict = {"query": effective_query, "limit": limit}
        if start:
            params["start"] = start
        if end:
            params["end"] = end

        raw = self._get(f"/api/datasources/proxy/uid/{uid}/select/logsql/query", params=params, raw=True)

        entries = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            entry = _json_loads_line(line)
            if entry is None:
                continue
            if "_msg" in entry:
                entry["_msg"] = strip_ansi(entry["_msg"])
            entries.append(entry)

        entries.sort(key=lambda e: e.get("_time", ""))
        return entries

    def query_logs_hits(
        self,
        query: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        last: Optional[str] = None,
        step: str = "1h",
        datasource_uid: Optional[str] = None,
    ) -> list:
        """
        Log volume histogram over time (/select/logsql/hits) — useful to spot
        when a spike/anomaly started before pulling full log lines.
        """
        uid = datasource_uid or self.logs_datasource_uid()
        start, end = self.resolve_time_range(start, end, last)
        params: dict = {"query": query, "step": step}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        raw = self._get(f"/api/datasources/proxy/uid/{uid}/select/logsql/hits", params=params)
        return raw.get("hits") if isinstance(raw, dict) else raw


def _json_loads_line(line: str) -> Optional[dict]:
    import json
    try:
        return json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
