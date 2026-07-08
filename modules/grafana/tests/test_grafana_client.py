import pytest

from modules.grafana.client import GrafanaClient, parse_duration, strip_ansi

BASE = "https://test-grafana.example.com"


@pytest.fixture(autouse=True)
def grafana_env(monkeypatch):
    """Process-env override always wins over config/grafana/.env (see config_loader),
    so real credentials on the developer machine never leak into these tests."""
    monkeypatch.setenv("GRAFANA_URL", BASE)
    monkeypatch.setenv("GRAFANA_TOKEN", "fake-token")
    monkeypatch.delenv("GRAFANA_LOGS_DATASOURCE_UID", raising=False)


# ------------------------------------------------------------------
# parse_duration / strip_ansi
# ------------------------------------------------------------------

class TestParseDuration:
    @pytest.mark.parametrize("spec,seconds", [
        ("30s", 30),
        ("90m", 90 * 60),
        ("6h", 6 * 3600),
        ("2d", 2 * 86400),
        ("1w", 7 * 86400),
    ])
    def test_valid(self, spec, seconds):
        assert parse_duration(spec).total_seconds() == seconds

    @pytest.mark.parametrize("spec", ["", "5", "5x", "h5", "-1h"])
    def test_invalid(self, spec):
        with pytest.raises(ValueError):
            parse_duration(spec)


def test_strip_ansi():
    colored = "\x1b[32m2026-07-08 03:28:12\x1b[0m | \x1b[1mINFO\x1b[0m | plain text"
    assert strip_ansi(colored) == "2026-07-08 03:28:12 | INFO | plain text"


def test_strip_ansi_handles_none():
    assert strip_ansi(None) == ""


# ------------------------------------------------------------------
# resolve_time_range
# ------------------------------------------------------------------

class TestResolveTimeRange:
    def test_last_takes_precedence(self):
        start, end = GrafanaClient.resolve_time_range(start="ignored", end="ignored", last="1h")
        assert start.endswith("Z")
        assert end.endswith("Z")
        assert start < end

    def test_explicit_start_end_passthrough(self):
        start, end = GrafanaClient.resolve_time_range(start="2026-07-08T03:00:00Z", end="2026-07-08T05:00:00Z")
        assert (start, end) == ("2026-07-08T03:00:00Z", "2026-07-08T05:00:00Z")

    def test_no_bounds_given(self):
        assert GrafanaClient.resolve_time_range() == (None, None)


# ------------------------------------------------------------------
# Client — missing config
# ------------------------------------------------------------------

class TestGrafanaClientConfig:
    def test_missing_token_raises(self, monkeypatch):
        monkeypatch.setenv("GRAFANA_TOKEN", "")
        with pytest.raises(RuntimeError, match="GRAFANA_TOKEN"):
            GrafanaClient()

    def test_missing_url_raises(self, monkeypatch):
        monkeypatch.setenv("GRAFANA_URL", "")
        with pytest.raises(RuntimeError, match="GRAFANA_URL"):
            GrafanaClient()


# ------------------------------------------------------------------
# Client — health & datasources
# ------------------------------------------------------------------

class TestGrafanaClientReads:
    def test_health(self, requests_mock):
        requests_mock.get(f"{BASE}/api/health", json={"database": "ok"})
        assert GrafanaClient().health() == {"database": "ok"}

    def test_list_datasources(self, requests_mock):
        requests_mock.get(
            f"{BASE}/api/datasources",
            json=[
                {"uid": "vls", "name": "VictoriaLogs", "type": "victoriametrics-logs-datasource"},
                {"uid": "am", "name": "Alertmanager", "type": "alertmanager"},
            ],
        )
        datasources = GrafanaClient().list_datasources()
        assert datasources == [
            {"uid": "vls", "name": "VictoriaLogs", "type": "victoriametrics-logs-datasource"},
            {"uid": "am", "name": "Alertmanager", "type": "alertmanager"},
        ]

    def test_logs_datasource_uid_auto_detect(self, requests_mock):
        requests_mock.get(
            f"{BASE}/api/datasources",
            json=[
                {"uid": "am", "name": "Alertmanager", "type": "alertmanager"},
                {"uid": "vls", "name": "VictoriaLogs", "type": "victoriametrics-logs-datasource"},
            ],
        )
        assert GrafanaClient().logs_datasource_uid() == "vls"

    def test_logs_datasource_uid_missing_raises(self, requests_mock):
        requests_mock.get(f"{BASE}/api/datasources", json=[])
        with pytest.raises(RuntimeError, match="No VictoriaLogs datasource"):
            GrafanaClient().logs_datasource_uid()

    def test_logs_datasource_uid_env_override_skips_lookup(self, monkeypatch, requests_mock):
        monkeypatch.setenv("GRAFANA_LOGS_DATASOURCE_UID", "custom-uid")
        # No /api/datasources mock registered — a call would raise NoMockAddress.
        assert GrafanaClient().logs_datasource_uid() == "custom-uid"


# ------------------------------------------------------------------
# Client — query_logs
# ------------------------------------------------------------------

NDJSON_BODY = (
    '{"_time": "2026-07-08T03:28:13.218Z", "_msg": "\\u001b[32mtrue_device_id case\\u001b[0m", '
    '"kubernetes.pod_name": "backend-648zf", "level": "INFO"}\n'
    '{"_time": "2026-07-08T03:28:12.992Z", "_msg": "cookie_hash case", '
    '"kubernetes.pod_name": "backend-dx9mn", "level": "INFO"}\n'
)


class TestQueryLogs:
    def test_parses_and_sorts_ndjson(self, monkeypatch, requests_mock):
        monkeypatch.setenv("GRAFANA_LOGS_DATASOURCE_UID", "vls")
        requests_mock.get(
            f"{BASE}/api/datasources/proxy/uid/vls/select/logsql/query",
            text=NDJSON_BODY,
        )
        entries = GrafanaClient().query_logs("900001 AND 5001", last="1h")
        assert [e["_time"] for e in entries] == [
            "2026-07-08T03:28:12.992Z",
            "2026-07-08T03:28:13.218Z",
        ]
        # ANSI codes stripped from _msg
        assert entries[1]["_msg"] == "true_device_id case"

    def test_last_populates_start_and_end_params(self, monkeypatch, requests_mock):
        monkeypatch.setenv("GRAFANA_LOGS_DATASOURCE_UID", "vls")
        requests_mock.get(
            f"{BASE}/api/datasources/proxy/uid/vls/select/logsql/query",
            text="",
        )
        GrafanaClient().query_logs("foo", last="90m", limit=50)
        qs = requests_mock.last_request.qs
        assert qs["query"] == ["foo | sort by (_time)"]
        assert qs["limit"] == ["50"]
        assert "start" in qs and "end" in qs

    def test_appends_sort_pipe_when_query_has_no_pipe(self, monkeypatch, requests_mock):
        monkeypatch.setenv("GRAFANA_LOGS_DATASOURCE_UID", "vls")
        requests_mock.get(
            f"{BASE}/api/datasources/proxy/uid/vls/select/logsql/query",
            text="",
        )
        GrafanaClient().query_logs('"900001" AND "5001"')
        assert requests_mock.last_request.qs["query"] == ['"900001" and "5001" | sort by (_time)']

    def test_does_not_append_sort_pipe_when_query_already_has_one(self, monkeypatch, requests_mock):
        monkeypatch.setenv("GRAFANA_LOGS_DATASOURCE_UID", "vls")
        requests_mock.get(
            f"{BASE}/api/datasources/proxy/uid/vls/select/logsql/query",
            text="",
        )
        GrafanaClient().query_logs('foo | stats count() by (level)')
        assert requests_mock.last_request.qs["query"] == ['foo | stats count() by (level)']

    def test_explicit_start_end_passed_through(self, monkeypatch, requests_mock):
        monkeypatch.setenv("GRAFANA_LOGS_DATASOURCE_UID", "vls")
        requests_mock.get(
            f"{BASE}/api/datasources/proxy/uid/vls/select/logsql/query",
            text="",
        )
        GrafanaClient().query_logs("foo", start="2026-07-08T03:00:00Z", end="2026-07-08T05:00:00Z")
        qs = requests_mock.last_request.qs
        assert qs["start"] == ["2026-07-08t03:00:00z"]
        assert qs["end"] == ["2026-07-08t05:00:00z"]

    def test_ignores_blank_lines_and_malformed_json(self, monkeypatch, requests_mock):
        monkeypatch.setenv("GRAFANA_LOGS_DATASOURCE_UID", "vls")
        requests_mock.get(
            f"{BASE}/api/datasources/proxy/uid/vls/select/logsql/query",
            text='\n{"_time": "2026-07-08T03:00:00Z", "_msg": "ok"}\n\nnot-json\n',
        )
        entries = GrafanaClient().query_logs("foo")
        assert len(entries) == 1
        assert entries[0]["_msg"] == "ok"


# ------------------------------------------------------------------
# Client — query_logs_hits
# ------------------------------------------------------------------

class TestQueryLogsHits:
    def test_returns_hits_list(self, monkeypatch, requests_mock):
        monkeypatch.setenv("GRAFANA_LOGS_DATASOURCE_UID", "vls")
        requests_mock.get(
            f"{BASE}/api/datasources/proxy/uid/vls/select/logsql/hits",
            json={"hits": [{"timestamps": ["2026-07-08T03:00:00Z"], "values": [47], "total": 47}]},
        )
        hits = GrafanaClient().query_logs_hits("900001", last="6h", step="1h")
        assert hits == [{"timestamps": ["2026-07-08T03:00:00Z"], "values": [47], "total": 47}]
