# Ops Alert Triage Agent

A prototype agent that reads an infrastructure alert, looks up the matching runbook, decides whether the alert can be handled automatically or needs a human, and carries out that decision with a tool.

## Status

Early-stage prototype, built as a learning project. Every action is mocked: nothing here restarts a real service or pages a real person. The diagnostic tool returns fixed fake data. Not production-ready.

## How it works

1. **Input check.** The alert is checked before any model sees it. An empty alert, one longer than 1000 characters, or one containing a known injection phrase is escalated to a human immediately.
2. **Classify.** `classify_alert` labels the alert as one of five types.
3. **Retrieve.** `get_full_runbook` pulls every section of the matching runbook from Qdrant, filtered by alert type.
4. **Tool loop.** The model gets the alert and the runbook and can take up to 8 steps. It can call `check_service_status` to gather evidence, then `record_decision` with `auto_resolve` or `escalate`, then the action tool that matches that decision.
5. **Checks in code.** Between the model's steps, plain code enforces the rules listed below. The model proposes, the code decides what is allowed to run.
6. **Fallback.** If anything goes wrong, the alert is escalated to a human. It is never silently dropped.

## Safeguards

- **Input check.** Rejects empty, oversized, or suspicious alerts before the model is called.
- **Argument check.** Any `host` or `service` the model passes to a tool must appear in the alert text. An invented value is blocked, the model is told why and gets one retry, and a second invented value escalates to a human.
- **Approval gate.** The action the model takes must match the decision it recorded. A mismatch escalates.
- **Inventory check.** An `auto_resolve` only goes through if every service the model checked is listed in `inventory.yaml`, is stateless, and is not production-critical. Otherwise it is forced to escalate.
- **Grounding check.** For `auto_resolve` decisions only, a second model call checks whether the claims in the model's reason are supported by the alert, the runbook, or the diagnostic results. An unsupported reason forces an escalation. Escalating is always safe, so `escalate` decisions are not checked.
- **Failure fallback.** A failed model call, a missing tool call, an unexpected action, or running out of steps all end in an escalation to a human.

## Tools

All tools are mocked.

- `check_service_status(host, service)`: returns fake process and log status, plus what the inventory says about the service.
- `record_decision(decision, reason)`: the model states `auto_resolve` or `escalate`. It has no real-world effect. Your code reads the arguments directly.
- `restart_service(host, service)`: logs what a restart would do.
- `escalate_to_human(alert_text, reason)`: logs what a handoff to a human would look like.

## Inventory

`inventory.yaml` lists each known service and the facts the runbooks depend on:

```yaml
services:
  internal-metrics-exporter:
    stateless: true
    environment: staging
    critical: false
```

A service that is not listed is never auto-restarted, because nothing is known about it. Add services here to make them eligible.

## Runbooks

Five markdown files in `docs/`, one per alert type: disk usage, service down, high CPU, high latency, certificate expiry. Each follows the same structure:

```
# <Title>
## Alert Type
## Symptoms
## Diagnosis
## Resolution
## Risk Level
## Approval
## Escalate When
```

`rag.py` splits each file on its `##` headers, so every section becomes one chunk, embeds the chunks, and stores them in Qdrant with the alert type and section name as payload fields. The full runbook is retrieved for each alert, because the approval rules refer to conditions described in other sections.

## Audit log

Every step is appended to `audit_log.jsonl`, one JSON object per line, tagged with an alert ID. Event types: `classification`, `diagnostic`, `guardrail_check`, `decision`, `action`, `error`, `input_blocked`.

## Setup

Prerequisites: Python 3.10+, Docker, a Groq API key.

```bash
git clone <repo-url>
cd <repo-folder>
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Start Qdrant:

```bash
docker run -d -p 6333:6333 -v qdrant_storage:/qdrant/storage --name qdrant qdrant/qdrant
```

Copy the config template and add your key:

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` and set `api_key`. `config.yaml` and `audit_log.jsonl` are in `.gitignore` and should not be committed.

Load the runbooks into Qdrant:

```bash
python src/rag.py
```

Expected output: `stored 35 chunks`.

Run the agent. Testing is manual: edit the alerts in the `__main__` block at the bottom of `src/main.py`, then run:

```bash
python src/main.py
```

## Models

All steps currently use `classifier_model` (`openai/gpt-oss-20b` on Groq). `writer_model` (`openai/gpt-oss-120b`) is in the config but not used yet.

## Known limitations

- The model gives different answers on the same alert from run to run. There is no test set yet, so changes are checked by reading output by hand.
- The grounding check is itself a model call and is inconsistent. It sometimes flags harmless conclusions and sometimes lets an unsupported claim through. The inventory check does not have this problem, because it is a plain lookup.
- `classify_alert` and `get_full_runbook` run before the tool loop, and a failure in either still crashes the alert instead of escalating it.
- The input check is a short phrase list plus length limits. It stops obvious attempts only, since any fixed list can be reworded around.
- The inventory is written by hand and covers a handful of services.
- Diagnostics return fixed fake data.
- Alerts are entered by editing the code. There is no queue, API, or UI.
- No memory across alerts. The agent does not know that a host was restarted three times this week.
- The audit log is append-only JSON lines. It is built to be read by a program, not by eye.

## Roadmap

- Automated test set with known-correct outcomes for each kind of alert
- Wrap `classify_alert` and `get_full_runbook` in the same failure fallback
- Mock ticket generator sending alerts to a running `main.py` through an API
- Dashboard or desktop UI reading `audit_log.jsonl`
- Alert format check as part of the input guardrail
- History of past actions per host