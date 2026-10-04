# Auto Alert Handler

An AI agent that reads an infrastructure alert, finds the matching runbook, and decides what to do: fix it itself or hand it to a human. A live dashboard shows what it did and why.

The point of this project is not the model call. It is the **code around the model**: checks that decide what the model is allowed to do, so a wrong or made-up answer ends in a safe escalation and never in a harmful action.

> **Status:** a working prototype. Every action is mocked. Nothing here restarts a real service or pages a real person.

## What it does

```
alert text ──► input check ──► classify ──► fetch runbook ──► tool loop ──► action
                  │                              (Qdrant)         │
                  │                                               ├─ check_service_status
                  ▼                                               ├─ record_decision
            escalate to human ◄── any failure, any blocked check ─┘  restart_service
                                                                      escalate_to_human
```

1. **Input check.** The alert is checked before any model sees it. An empty alert, one over 1000 characters, or one with a known injection phrase goes straight to a human.
2. **Classify.** The model labels the alert as one of five types.
3. **Retrieve.** The full runbook for that type is pulled from a Qdrant vector database.
4. **Tool loop.** The model gets up to 8 steps. It can check a service's status, record a decision (`auto_resolve` or `escalate`), and then call the action that matches.
5. **Checks in code.** Between the model's steps, plain code enforces the safety rules below. The model proposes. The code decides what runs.
6. **Fallback.** If anything goes wrong, the alert is escalated. It is never silently dropped.

## Dashboard

A small web page shows every alert, what the agent decided, and its reason. It updates every 5 seconds.

<img width="1600" height="799" alt="f23089bc-b9d3-40e4-bf59-5e39250b901b" src="https://github.com/user-attachments/assets/48e4d87e-88b2-4c82-9d4e-17c33b6d91e5" />

- Totals for restarted, escalated and no action recorded, with a bar showing how the alerts ended
- One row per alert, newest first, with the alert type, the decision, the number of status checks, and the full reason on click
- Dark and light mode, and a layout that works on a phone
- Login required

## Safeguards

| Safeguard | What it stops |
|---|---|
| **Input check** | Empty, oversized or injection-style alerts never reach the model. |
| **Argument check** | A `host` or `service` the model passes to a tool must appear in the alert text. An invented value is blocked, the model gets one retry, and a second one escalates. |
| **Approval gate** | The action the model takes must match the decision it recorded. A mismatch escalates. |
| **Inventory check** | `auto_resolve` only goes through if every service checked is in `inventory.yaml`, is stateless, and is not production-critical. Unknown services are never restarted. |
| **Grounding check** | For `auto_resolve` only, a second model call checks that the claims in the reason are backed by the alert, the runbook or the diagnostics. An unsupported reason forces an escalation. |
| **Failure fallback** | A failed model call, a missing tool call, an unexpected action, or running out of steps all end in an escalation. |

Escalating is always safe, so `escalate` decisions are not checked. Only the risky path is.

## Tech

- **Python**, with the OpenAI-compatible API on **Groq** (`openai/gpt-oss-20b`)
- **Qdrant** for runbook retrieval, **sentence-transformers** for embeddings
- **FastAPI** and **uvicorn** for the server, plain HTML, CSS and JavaScript for the dashboard (no build step)
- An append-only **JSON Lines audit log** of every step

## Quick start

You need Python 3.10+, Docker and a Groq API key.

```bash
git clone <repo-url>
cd <repo-folder>
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# start the vector database
docker run -d -p 6333:6333 -v qdrant_storage:/qdrant/storage --name qdrant qdrant/qdrant

# add your key
cp config.example.yaml config.yaml      # then set api_key in config.yaml

# load the runbooks into Qdrant (expected output: "stored 35 chunks")
python src/rag.py
```

Run the server and open <http://127.0.0.1:8000>:

```bash
python src/server.py
```

Log in with `admin` / `admin`. In a second terminal, send some fake alerts:

```bash
python src/alert_generator.py
```

The generator sends 5 alerts, 30 seconds apart. The pause is there so a free Groq daily token limit lasts. You can watch each one appear on the dashboard.

## Tests

`src/test_alerts.py` holds 13 alerts, each with the action the agent should end with. `src/run_tests.py` runs every alert 5 times, because the model gives different answers between runs, and prints a pass count per case.

```bash
python src/run_tests.py
```

It also counts the two failures that matter most:

- **unsafe restarts:** the agent restarted something when a human was needed
- **crashes:** an alert was lost

Test runs write to `test_audit_log.jsonl`, so they do not fill the real log.

## API

| Method | Path | What it does |
|---|---|---|
| `GET` | `/` | The dashboard |
| `GET` / `POST` | `/login` | The login page and the login form |
| `GET` | `/logout` | Ends the session |
| `GET` | `/alerts` | The newest 200 alerts as JSON, grouped by alert ID |
| `POST` | `/alert` | Sends one alert: `{"text": "..."}`. Returns what the agent did |

Everything except `/login` needs a login. A script can use HTTP basic auth:

```bash
curl -u admin:admin -H 'Content-Type: application/json' \
  -d '{"text": "ServiceDown: billing-worker health check failing on host prod-app-2, port 9200 not listening"}' \
  http://127.0.0.1:8000/alert
```

The login is `admin` / `admin` and is written in the code. That is fine on your own machine. Change it before the server is reachable from anywhere else.

## Project layout

```
src/
  main.py             the agent: checks, tool loop, mocked tools, audit log
  rag.py              loads the runbooks into Qdrant
  server.py           FastAPI server and login
  dashboard.html      the dashboard
  alert_generator.py  sends fake alerts to the server
  test_alerts.py      test alerts and their expected outcomes
  run_tests.py        runs the tests several times each
docs/                 five runbooks, one per alert type
inventory.yaml        the known services and facts about them
config.example.yaml   settings template
```

## Runbooks

Five markdown files in `docs/`: disk usage, service down, high CPU, high latency, certificate expiry. Each has the same sections:

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

`rag.py` splits each file on its `##` headers, embeds each section, and stores it in Qdrant with the alert type and section name. The agent gets the **whole** runbook for each alert, because the approval rules refer to conditions in other sections.

## Inventory

`inventory.yaml` lists each known service and the facts the runbooks depend on:

```yaml
services:
  internal-metrics-exporter:
    stateless: true
    environment: staging
    critical: false
```

A service that is not listed is never auto-restarted, because nothing is known about it.

## Audit log

Every step is appended to `audit_log.jsonl`, one JSON object per line, tagged with an alert ID. Event types: `classification`, `diagnostic`, `guardrail_check`, `decision`, `action`, `error`, `input_blocked`. The dashboard is built from this file. Writes are guarded by a lock so lines from different alerts never mix.

## Design choices

- **The model proposes, the code decides.** Anything that can cause harm is checked by plain code, not by asking the model to be careful.
- **Escalate by default.** Every failure path ends with a human, never with a dropped alert or a guessed action.
- **Checks that are lookups beat checks that are model calls.** The inventory check is a dictionary lookup and is always right. The grounding check is a second model call and is not always right.
- **Whole runbooks, not snippets.** Approval rules depend on other sections, so cutting the runbook into pieces at retrieval time loses them.

## Roadmap

- Wrap `classify_alert` and `get_full_runbook` in the same failure fallback
- Move the login out of the code and into config
- Alert format check as part of the input guardrail
- History of past actions per host
- Real diagnostics and actions behind the same safeguards
