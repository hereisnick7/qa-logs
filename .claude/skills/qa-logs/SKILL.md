---
name: qa-logs
description: >
  Backend-aware log investigation via Grafana/VictoriaLogs, for QA and backend
  engineers alike: takes a symptom description (user/entity id, symptom,
  approximate time) and/or a Jira key, reconstructs the causal chain across
  services step by step, and ends with a conclusion plus either a concrete
  suggestion or a "dig here" pointer for the backend owner. When the logs
  alone can't resolve something a current DB snapshot could, offers to hand
  off to `qa-database-agent` (read-only Postgres, second-line escalation).
  Report only — filing a Jira bug is a separate explicit step.
  Use when: "разбери логи по юзеру X", "почему дублируется Y", "что сломалось
  в логах", "qa-logs DEV-XXXX", "найди причину по логам".
---

# /qa-logs

Entry point for log-based root-cause investigation. Coordinates `qa-logs-agent`
against Grafana/VictoriaLogs.

## Usage

```
/qa-logs "юзер 42, дублируются записи о проверке, с 6 утра"      # free text
/qa-logs DEV-XXXX                                                 # Jira key — agent reads the ticket
/qa-logs DEV-XXXX "уточнение: это только для одной геозоны"       # Jira key + extra context
```

## Algorithm

### 1. Parse input

- A Jira key (project prefix from `projects.yaml`, e.g. `DEV-\d+`) anywhere in
  the args → Jira-key mode (+ free text remainder, if any, as extra context).
- Otherwise → free-text mode, the whole argument is the symptom description.
- Neither a Jira key nor any text → ask: "Что искать? Опиши: юзер/сущность,
  симптом, примерное время — или дай ключ задачи."

### 2. Pick the environment, then check Grafana is configured

Each environment is a separate Grafana instance behind its own account
(`--account=<name>`; which one the bare keys are is in
`config/qa-logs-context.md` → Environments). Decide from the symptom
**before** launching the agent — a live customer's id/reference, an "on prod"
mention, or a Jira ticket describing a customer-facing incident means
production; a test-stand run means the test instance. Ambiguous → ask the
user; do not guess, because the wrong account answers "нет логов" instead of
failing.

```bash
workflow grafana-info                       # default (bare keys) account
workflow grafana-info --account=<name>      # a named account, e.g. prod
```

Failure (missing token / unreachable) → tell the user, point at
`config/grafana/.env` (`GRAFANA_URL` / `GRAFANA_TOKEN`, or
`GRAFANA__<ACCOUNT>__URL` / `__TOKEN` for a named account), do not launch the
agent.

State the chosen account in the agent prompt (step 3) so every query it runs
carries the same flag, and repeat it in your own status update to the user.

### 3. Launch qa-logs-agent

```python
Agent(subagent_type="qa-logs-agent", prompt="""
Investigate via Grafana/VictoriaLogs logs.

{Jira key mode: "Jira key: {KEY}. Read the task (rtk summary workflow jira-task {KEY})
and its comments (workflow jira-comments {KEY}) to extract the entity id, symptom,
and approximate time. Extra context from the user: {free text or 'none'}."}
{Free-text mode: "Free-text report from the user: {free text}."}

Grafana account for this investigation: {account chosen in step 2}. Pass
{--account=<name> | nothing, if it's the default account} on EVERY grafana-hits /
grafana-logs call, and name the environment in the report. Never query another
account for the same chain.

Start by reading: config/qa-logs-context.md

Then follow the full qa-logs-agent workflow:
  Step 0: Scope the query — entity id, symptom, time window (convert UI time to UTC
          per the confirmed offset in config/qa-logs-context.md)
  Step 1: grafana-hits to locate the window (skip only if the time is already precise)
  Step 2: grafana-logs scoped to that window
  Step 3: Correlate chronologically across services
  Step 4: Write the causal chain (## 🔗 Ход расследования) with [FACT]/[HYPOTHESIS]/[UNKNOWN]
          labels inline — every entry pairs evidence with what it caused, not a bare quote list
  Step 5: Scope check — isolated incident vs recurring pattern
  Step 6: Recommendations — a concrete suggestion or a "where to dig" pointer,
          anchored to a log-visible module/function, framed as a hypothesis (no repo access)

This agent is a bloodhound, not a curator — no Seed Mode step. Report ends at
Step 6.

Produce the full report in the standard output format.
""")
```

### 4. After agent output — validate required sections

Required (match on the core text after `## `, ignore emoji/formatting around
it — an agent using a different emoji or dropping it is a cosmetic slip, not a
missing section): a section whose heading contains "Ход расследования", one
containing "ИТОГ", one containing "Масштаб", one containing "Рекомендации".
**Structural check, not just presence**: if the output instead has the old
separate `Таймлайн` + `Root Cause` sections with `ИТОГ`/`Рекомендации` merely
appended at the end (rather than merged into one causal-chain section), treat
it as non-compliant even though all four keywords are technically present,
and re-launch with the literal Step list above (not a paraphrase — a
paraphrased step list is what tends to trigger the drift).
Missing/non-compliant → "⚠️ Отчёт не в нужном формате: {что не так}. Повторить?"

### 5. Check the meta footer — offer escalation to qa-database

Read `<!-- qa-logs-meta -->` → `escalate_db:`. If `yes`, the report's
`## ❓ Вопросы` → `→ **Эскалация:**` line names a specific question the
agent couldn't resolve from logs alone but flagged as checkable via a
current-DB-state snapshot. Ask the user:

> "Логов недостаточно, чтобы закрыть вопрос: «{вопрос из Эскалация}».
> Подключить `qa-database` для сверки текущего состояния в БД? (да / нет)"

If yes, launch `qa-database-agent` (directly, or via `/qa-database` if that
skill is invoked instead — same effect) with the handoff context:

```python
Agent(subagent_type="qa-database-agent", prompt="""
Handoff from a qa-logs-agent investigation.

Entity: {entity from qa-logs-meta}
Question to confirm/refute (current DB state): {the Эскалация question}

Read config/qa-database-context.md → ## Database first, then run your normal
Step 0-4 workflow to answer this specific question.
""")
```

Relay `qa-database-agent`'s report to the user as a follow-up to the
original investigation — don't merge the two reports into one, they answer
different questions (why it happened vs. what's true right now).

If `escalate_db: no` (or absent), skip this step entirely.

### 6. After the report(s) — offer to file a bug

> "Завести баг в Jira по этому расследованию? (да / нет)"

If yes, hand off to `/qa-report {KEY} --result=bug` (qa-analyze package; without it — to
whatever ticket-creation flow your orchestrator has) with the investigation
report(s) as structured context (root cause, timeline evidence, entity id, DB
cross-check if one ran) — this agent never files tickets itself. If the investigation
started from an existing Jira key, offer to comment on that ticket instead of
creating a new one.

## Rules

- Never launch the agent without either a Jira key or a usable free-text symptom
- Never auto-file a Jira bug — always ask first
- Never launch `qa-database-agent` without asking first, even when
  `escalate_db: yes` — the offer in step 5 is not an auto-escalation
- No Seed Mode — this agent doesn't propose knowledge-base changes; keeping
  `config/qa-logs-context.md` current is a manual/orchestrator task
- One `/qa-logs` call = one investigation (plus, optionally, one
  `qa-database-agent` follow-up if escalation was offered and accepted)

## Output Contract

The agent produces the complete report. Relay it directly to the user.

Required (by core text, see step 4): "Ход расследования" merged causal chain,
"ИТОГ", "Масштаб", "Рекомендации".
Optional: `## ❓ Вопросы`, `escalate_db: yes|no` in the meta footer.

## Fail-fast

```
BLOCKED_NO_INPUT
Neither a Jira key nor a usable symptom description was given
Needed from user: entity id + symptom + approximate time, or a Jira key
```
