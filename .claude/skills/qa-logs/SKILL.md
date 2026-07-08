---
name: qa-logs
description: >
  Backend-aware log investigation via Grafana/VictoriaLogs, for QA and backend
  engineers alike: takes a symptom description (user/entity id, symptom,
  approximate time) and/or a Jira key, reconstructs the causal chain across
  services step by step, and ends with a conclusion plus either a concrete
  suggestion or a "dig here" pointer for the backend owner. Report only —
  filing a Jira bug is a separate explicit step.
  Use when: "разбери логи по юзеру X", "почему дублируется Y", "что сломалось
  в логах", "qa-logs DEV-XXXX", "найди причину по логам".
---

# /qa-logs

Entry point for log-based root-cause investigation. Coordinates `qa-logs-agent`
against Grafana/VictoriaLogs.

## Usage

```
/qa-logs "юзер 42, дублируются записи о проверке, с 6 утра"      # free text
/qa-logs DEV-3102                                                 # Jira key — agent reads the ticket
/qa-logs DEV-3102 "уточнение: это только для одной геозоны"       # Jira key + extra context
```

## Algorithm

### 1. Parse input

- A Jira key (project prefix from `projects.yaml`, e.g. `DEV-\d+`) anywhere in
  the args → Jira-key mode (+ free text remainder, if any, as extra context).
- Otherwise → free-text mode, the whole argument is the symptom description.
- Neither a Jira key nor any text → ask: "Что искать? Опиши: юзер/сущность,
  симптом, примерное время — или дай ключ задачи."

### 2. Check Grafana is configured

```bash
workflow grafana-info
```

Failure (missing token / unreachable) → tell the user, point at
`config/grafana/.env` (`GRAFANA_URL` / `GRAFANA_TOKEN`), do not launch the agent.

### 3. Launch qa-logs-agent

```python
Agent(subagent_type="qa-logs-agent", prompt="""
Investigate via Grafana/VictoriaLogs logs.

{Jira key mode: "Jira key: {KEY}. Read the task (rtk summary workflow jira-task {KEY})
and its comments (workflow jira-comments {KEY}) to extract the entity id, symptom,
and approximate time. Extra context from the user: {free text or 'none'}."}
{Free-text mode: "Free-text report from the user: {free text}."}

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

### 5. After the report — offer to file a bug

> "Завести баг в Jira по этому расследованию? (да / нет)"

If yes, hand the investigation report (root cause, timeline evidence, entity
id) to whatever ticket-creation flow your orchestrator has — this agent never
files tickets itself. If the investigation started from an existing Jira key,
offer to comment on that ticket instead of creating a new one.

## Rules

- Never launch the agent without either a Jira key or a usable free-text symptom
- Never auto-file a Jira bug — always ask first
- No Seed Mode — this agent doesn't propose knowledge-base changes; keeping
  `config/qa-logs-context.md` current is a manual/orchestrator task
- One `/qa-logs` call = one investigation

## Output Contract

The agent produces the complete report. Relay it directly to the user.

Required (by core text, see step 4): "Ход расследования" merged causal chain,
"ИТОГ", "Масштаб", "Рекомендации".
Optional: `## ❓ Вопросы`

## Fail-fast

```
BLOCKED_NO_INPUT
Neither a Jira key nor a usable symptom description was given
Needed from user: entity id + symptom + approximate time, or a Jira key
```
