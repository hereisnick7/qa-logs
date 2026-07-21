# QA Logs Agent — Domain Knowledge Base

Persistent knowledge for `qa-logs-agent` and, if you use it, `qa-database-agent`.
Read at the start of every investigation. Manually curated — neither agent is a
curator: they read this file but never propose changes to it (no Seed Mode).
Update it by hand when an investigation surfaces something durable worth keeping.

---

## Data Source

Grafana (`config/grafana/.env`) proxies to a VictoriaLogs datasource. CLI:

```bash
workflow grafana-info                                             # health + datasource UIDs
workflow grafana-logs "<LogsQL query>" [--last=1h|--start=..--end=] [--limit=200]
workflow grafana-hits "<LogsQL query>" [--last=6h] [--step=1h]     # volume histogram — find WHEN before pulling lines
```

`grafana-hits` first to locate the spike/window, then `grafana-logs` scoped to
that window — cheaper than paging through `grafana-logs` blindly.

---

## Timezone

VictoriaLogs `_time` and all `--start`/`--end`/`--last` bounds are **UTC**.

**Fill this in for your own product**: what timezone does your admin panel /
Grafana org render timestamps in? Verify by finding one event visible in both
places (e.g. an admin-panel note plus its log line) and comparing. Example
entry once confirmed:

> `[CONFIRMED YYYY-MM-DD]` The admin panel renders timestamps at **UTC+N**.
> Verified exact match: admin panel showed `<timestamp>` for `<event>`; the
> corresponding log line's `_time` was `<UTC timestamp>`. Re-verify this
> offset if a future investigation's timestamps don't line up — it depends on
> the reporting tool's configured display timezone, not a VictoriaLogs property.

---

## Known Services

| Pod / container | Role |
|---|---|
| `<service-name>` | `<what it does, what it publishes/consumes>` |

`kubernetes.pod_name` has a random suffix per replica (e.g. `<service>-<hash>-<id>`)
— multiple replicas can independently consume the same published event, which
matters when duplicate-looking log entries need explaining (see Known Patterns).

---

## ID / Rule Glossary

Fill in whatever domain-specific IDs your logs carry (fraud rule IDs, event
type codes, status enums) that aren't self-explanatory from the log line alone.

| id | Name | Meaning |
|---|---|---|
| `<id>` | `<human name>` | `<what triggers it>` |

---

## Database

Optional — only relevant if you also run `qa-database-agent` (read-only Postgres
cross-check, second-line escalation from `qa-logs-agent` when logs alone can't
resolve something a current DB snapshot could). CLI:

```bash
workflow db-info                                              # connectivity + version
workflow db-tables [--schema=public]                          # explore schema
workflow db-describe <table> [--schema=public]                # columns for a table
workflow db-query "<SELECT ...>" [--limit=200]                 # the actual check
```

**A DB row is CURRENT state, not history** — unless a table is explicitly an
audit/history table with its own per-row timestamp, a query only tells you
"what is true right now", not "what was true at the incident time". Cite it
accordingly.

**Check your timestamp storage convention before trusting a comparison.** Some
schemas store business timestamps as epoch integers (seconds since epoch)
rather than a native timestamp type — if yours does, cast before citing:
`to_timestamp(<col>) AT TIME ZONE 'UTC'`. Verify empirically once per table
family, don't assume.

**Check your money-column scaling convention before citing an amount.** Some
schemas store monetary amounts in minor units (e.g. cents, so ÷100 to get the
real-world value) rather than face value — citing the raw integer as-is can
overstate a value 100× and manufacture a false match against a user-reported
figure. Verify empirically (compare a raw value against what the admin
panel/user actually sees for the same row) before trusting the scale for a
table you haven't checked yet — don't assume every numeric money-shaped column
uses the same convention.

Document your own schema's tables/columns/enums here once confirmed, the same
way the `Known Services` / `ID Glossary` sections above document services and
rule IDs — this section intentionally ships empty as a template.

---

## Known Patterns

Confirmed root-cause mechanisms worth remembering, so the next investigation
that hits the same shape doesn't have to re-derive it from scratch. Example
shape (replace with your own confirmed findings):

**`[CONFIRMED YYYY-MM-DD]` Duplicate-looking "X" records are not a backend bug.**
`<publisher-service>` (`<module.path:function>`) publishes one **separate**
event per matched signal instead of merging a multi-signal match into a single
event. When a check matches on N signals at once, N events fire within a short
window; each is consumed independently (often by a different pod replica) and
logged as its own record — producing N duplicate-looking entries with an
identical rule/timestamp/entity pair. Root cause is the publisher's
fan-out-per-signal design, not duplicate consumption downstream. Diagnostic
query:
```
"<entity_id>" AND "<the specific log marker that identifies this pattern>"
```
If this returns N>1 lines at the same second for the same rule/entity, it's
this pattern — not a new bug.

---

## LogsQL Cheatsheet

- Phrase match: quote it — `"some-id"`, `"some marker text"`
- Combine: `"id" AND "marker"`, `"error" OR "exception"`
- Time-scope inline (alternative to `--start`/`--end`): `_time:[2026-01-01T00:00:00Z, 2026-01-01T01:00:00Z]`
- Useful fields to project/read: `kubernetes.pod_name`, `kubernetes.pod_namespace`,
  `kubernetes.container_name`, `level`, `_msg`, `_time`
- Log lines from `loguru`/`uvicorn`-style services carry ANSI color codes in `_msg`
  — `workflow grafana-logs` strips these automatically before printing.
- `workflow grafana-logs` sorts by `_time` server-side automatically (appends
  `| sort by (_time)` unless your query already has a `|` pipe). **Without
  that pipe, VictoriaLogs applies `limit` to its raw parallel-shard-scan
  order, not to a time-sorted stream** — a bare query with a low `--limit` can
  return an arbitrary subset that skips large chunks of the window entirely,
  not the earliest or latest N. Always check `grafana-hits`' `total` against
  your intended `--limit` before trusting a `grafana-logs` pull as complete
  (see qa-logs-agent Rule 8).
- Very long `_msg` values (HTTP header dumps, base64 payloads) are truncated
  by `workflow grafana-logs` with a `(+N chars, re-query narrower for full
  line)` suffix. **Calibrate the cutoff against your own data** — pull a
  sample of real `_msg` lengths for a busy entity and check the
  median/outlier split before trusting a default. A cutoff set too low can
  silently amputate the one field your conclusion needs, with no visible
  signal that it happened other than the field simply not being there.
- **Beat truncation on a specific field with `extract` + `keep`** — the
  correct fix when the line is long by nature (a webhook with a big header
  block), not just because the window is too wide, so "re-query narrower"
  doesn't help:
  ```
  "<entity_id>" AND "<marker>" | extract `"reason": "<reason>"` | keep _time, reason
  ```
  Backtick-quote the `extract` pattern — double quotes inside it are literal
  JSON to match, not the outer query's quoting.
