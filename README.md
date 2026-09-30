# Ops Alert Triage Agent

A prototype agent that reads an infrastructure alert, looks up the matching runbook, decides whether the situation can be auto-resolved or needs a human, and calls a tool to act on that decision.

## Status

Early-stage prototype, built as a learning project. Every action is mocked: nothing here restarts a real service or pages a real person. Not production-ready.

## How it works

1. An alert comes in as text, e.g. `"ServiceDown: payment-api health check failing on host prod-web-3"`.
2. `classify_alert` reads it and labels it as one of five known alert types.
3. `get_full_runbook` retrieves the matching runbook from a vector database.
4. `decide_action` reads the alert against the full runbook and returns a decision (`auto_resolve` or `escalate`) with a one-sentence reason.
5. Based on that decision, the code restricts which tool the model is allowed to call next. This is the approval gate, enforced in code, not left to the model to police itself.
6. The model calls a tool with arguments it extracts from the conversation. The code checks the tool called matches what the gate allows, then runs it.

## Tools

- `restart_service(host, service)` — mocked, logs what a restart would do
- `escalate_to_human(alert_text, reason)` — mocked, logs what a handoff to a human would look like
- `check_service_status(host, service)` — mocked diagnostic, returns fake process/log status

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

The Approval section is what the agent's decision is based on, and it can reference conditions described elsewhere in the runbook (for example, "requires approval if the host is a database"). Because of that, the full runbook is retrieved for a decision, not just the Approval section alone.

## Setup

Prerequisites: Python 3.10+, Docker, a Groq API key.

```bash
git clone <repo-url>
cd agentic
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Start Qdrant:

```bash
docker run -d -p 6333:6333 -v qdrant_storage:/qdrant/storage --name qdrant qdrant/qdrant
```

Copy the config template and add your API key:

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` and set `groq_api_key` to your own key.

Ingest the runbooks into the vector database:

```bash
python rag.py
```

Expected output: `stored 35 chunks`.

Run the agent. Testing is currently manual: edit the alert text in the `__main__` block of `main.py`, then run:

```bash
python main.py
```

## Models

Two models on Groq, chosen for cost and latency:

- `classifier_model` (`gpt-oss-20b`): used for classification and decision steps, which are bounded, short-answer tasks.
- `writer_model` (`gpt-oss-120b`): reserved for generating longer human-readable summaries. Not yet used in the pipeline.

## Known limitations

- No audit log. Actions are printed to the terminal and returned as dicts; nothing is persisted.
- No automated eval suite. Testing so far is a handful of specific alerts run by hand, output checked by reading it.
- No input or output guardrails. Nothing validates that an alert looks legitimate before acting on it, and nothing double-checks a tool call's arguments beyond the approval gate matching the decision.
- No persistent memory. The agent has no record of a host's or service's history across separate alerts.
- The tool-calling loop handles one tool call per alert and stops. It doesn't yet support a multi-step sequence, such as checking status, then deciding, then acting.
- Alerts are entered manually for testing. No queue, no API endpoint, no dashboard yet.
- One specific model behavior, inferring that a service like `payment-api` is stateful and high-risk from its name alone, without that being stated in the runbook or the alert, has only been checked by hand a handful of times. Not validated against a real test set.

## Roadmap

- Audit log for every decision and action
- Automated eval suite with known-correct alert and decision pairs
- Input and output guardrails
- Multi-step tool-calling loop (diagnose, then decide, then act)
- A mock ticket generator feeding alerts to a running `main.py`, instead of manual test calls
- A simple dashboard or desktop UI for visibility, instead of reading terminal output