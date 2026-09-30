import yaml
from openai import OpenAI
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
import json
from datetime import datetime, timezone
from pathlib import Path
import uuid


# ---------------- VARIABLES ----------------

# settings loaded from config.yaml (qdrant url, model names, api key, etc.)
with open(Path(__file__).parent.parent / "config.yaml") as f:
    config = yaml.safe_load(f)

# client used to send requests to the LLM (Groq, using the OpenAI-compatible API)
llm = OpenAI(
    api_key=config["api_key"],
    base_url=config["provider_base_url"],
)

# model used to turn text into vectors, for both runbooks and incoming alerts
embedder = SentenceTransformer(config["embedding_model"])

# client used to connect to the Qdrant vector database
qdrant = QdrantClient(url=config["qdrant_url"])

# the only alert types the classifier is allowed to choose from
ALERT_TYPES = [
    "service_down",
    "disk_usage_high",
    "cpu_usage_high",
    "latency_high",
    "certificate_expiring",
]

AUDIT_LOG_PATH = Path(__file__).parent.parent / "audit_log.jsonl"

# ---------------- TOOLS ----------------

tools = [
    {
        "type": "function",
        "function": {
            "name": "restart_service",
            "description": "Restart a service on a host. Only call this after confirming the runbook allows an automatic restart for this situation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "the hostname the service is running on"},
                    "service": {"type": "string", "description": "the name of the service to restart"},
                },
                "required": ["host", "service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": "Hand this alert off to a human instead of taking action. Use this whenever the runbook requires approval, or you are not confident the situation is safe to auto-resolve.",
            "parameters": {
                "type": "object",
                "properties": {
                    "alert_text": {"type": "string", "description": "the original alert text"},
                    "reason": {"type": "string", "description": "why this needs human approval"},
                },
                "required": ["alert_text", "reason"],
            },
        },
    },
        {
        "type": "function",
        "function": {
            "name": "record_decision",
            "description": "Record your decision for this alert before taking any action. Call this once you have enough information to decide whether the alert can be auto-resolved or needs a human.",
            "parameters": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "enum": ["auto_resolve", "escalate"]},
                    "reason": {"type": "string", "description": "why you made this decision, citing the runbook and any evidence you gathered"},
                },
                "required": ["decision", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_service_status",
            "description": "Check the current status of a service on a host: whether the process is running, when it was last deployed, and its most recent log line. Use this to gather evidence before deciding how to resolve an alert.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "the hostname to check"},
                    "service": {"type": "string", "description": "the service name to check"},
                },
                "required": ["host", "service"],
            },
        },
    },
]



# ------# ---------------- FUNCTIONS -------------------------- FUNCTIONS ----------------

# reads an alert and returns one label from ALERT_TYPES
def classify_alert(alert_text):
    system_prompt = (
        "You classify infrastructure alerts. Read the alert carefully, since the "
        "alert name and the actual cause don't always match.\n\n"
        "First, on one line, name the specific evidence in the alert text that "
        "points to the cause.\n"
        "Then, on a new line, reply with exactly one label from this list and "
        "nothing else on that line:\n"
        + "\n".join(ALERT_TYPES)
    )

    response = llm.chat.completions.create(
        model=config["classifier_model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": alert_text},
        ],
        temperature=0,
    )

    raw_reply = response.choices[0].message.content.strip()
    reply_lines = raw_reply.splitlines()
    label = reply_lines[-1].strip()

    if label not in ALERT_TYPES:
        raise ValueError(f"classifier returned an unknown label: {repr(label)}\nfull response:\n{raw_reply}")

    return label


# returns just the Approval section text for one alert type
def get_approval_rules(alert_type):
    results = qdrant.query_points(
        collection_name=config["collection"],
        query=embedder.encode(alert_type).tolist(),
        query_filter=Filter(must=[
            FieldCondition(key="alert_type", match=MatchValue(value=alert_type)),
            FieldCondition(key="section", match=MatchValue(value="Approval")),
        ]),
        limit=1,
    ).points

    if not results:
        raise ValueError(f"no Approval section found for alert_type: {repr(alert_type)}")

    approval_text = results[0].payload["text"]
    return approval_text


# returns the full runbook text (all sections, in the right order) for one alert type
def get_full_runbook(alert_type):
    results = qdrant.query_points(
        collection_name=config["collection"],
        query=embedder.encode(alert_type).tolist(),
        query_filter=Filter(must=[
            FieldCondition(key="alert_type", match=MatchValue(value=alert_type)),
        ]),
        limit=10,
    ).points

    if not results:
        raise ValueError(f"no runbook found for alert_type: {repr(alert_type)}")

    # the order the sections should be read in
    section_order = ["Alert Type", "Symptoms", "Diagnosis", "Resolution", "Risk Level", "Approval", "Escalate When"]

    # build a lookup from section name to its text
    section_to_text = {}
    for r in results:
        section_name = r.payload["section"]
        section_text = r.payload["text"]
        section_to_text[section_name] = section_text

    # walk through the sections in the right order and collect their text
    ordered_parts = []
    for section_name in section_order:
        if section_name in section_to_text:
            ordered_parts.append(section_to_text[section_name])

    full_runbook_text = "\n\n".join(ordered_parts)
    return full_runbook_text

def extract_host_and_service(alert_text):
    system_prompt = (
        "Extract the host and service name from this infrastructure alert.\n\n"
        "Reply in exactly this format, nothing else:\n"
        "host: <hostname>\n"
        "service: <service name>"
    )

    response = llm.chat.completions.create(
        model=config["classifier_model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": alert_text},
        ],
        temperature=0,
    )

    raw_reply = response.choices[0].message.content.strip()
    reply_lines = raw_reply.splitlines()

    parsed = {}
    for line in reply_lines:
        if ": " in line:
            parts = line.split(": ", 1)
            key = parts[0].strip()
            value = parts[1].strip()
            parsed[key] = value

    if "host" not in parsed or "service" not in parsed:
        raise ValueError(f"could not extract host and service:\n{raw_reply}")

    return parsed

# decides auto_resolve vs escalate for one alert, using the full runbook as context
def decide_action(alert_text, runbook):
    system_prompt = (
        "You are an on-call decision assistant. You are given an alert and the "
        "matching runbook. Decide whether this specific alert can be auto-resolved "
        "under the runbook's Approval rules, or whether it requires human approval.\n\n"
        "Read the whole runbook, not just the Approval section, since the approval "
        "rules can depend on conditions described in Diagnosis or Resolution, such "
        "as whether the host is a database or marked production.\n\n"
        "Reply in exactly this format, nothing else:\n"
        "decision: auto_resolve or escalate\n"
        "reason: one sentence citing the specific rule and evidence"
    )

    user_message = f"RUNBOOK:\n{runbook}\n\nALERT:\n{alert_text}"

    response = llm.chat.completions.create(
        model=config["classifier_model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0,
    )

    raw_reply = response.choices[0].message.content.strip()
    reply_lines = raw_reply.splitlines()

    # turn the two labeled lines ("decision: ..." and "reason: ...") into a dict
    parsed = {}
    for line in reply_lines:
        if ": " in line:
            parts = line.split(": ", 1)
            key = parts[0].strip()
            value = parts[1].strip()
            parsed[key] = value

    decision = parsed.get("decision")
    reason = parsed.get("reason", "")

    if decision not in ("auto_resolve", "escalate"):
        raise ValueError(f"unexpected decision format:\n{raw_reply}")

    return {"decision": decision, "reason": reason}


def execute_decision(alert_text, decision_result):
    if decision_result["decision"] == "auto_resolve":
        host_and_service = extract_host_and_service(alert_text)
        return restart_service(host_and_service["host"], host_and_service["service"])
    else:
        return escalate_to_human(alert_text, decision_result["reason"])


# mocked action: pretends to restart a service, only logs what it would do
def restart_service(host, service):
    action_record = {
        "action": "restart_service",
        "host": host,
        "service": service,
        "timestamp": datetime.now(timezone.utc).strftime("%H:%M"),
    }
    print(f"[MOCK ACTION] restarting {service} on {host}")
    return action_record


# mocked action: pretends to notify a human, only logs what it would do
def escalate_to_human(alert_text, reason):
    action_record = {
        "action": "escalate_to_human",
        "alert": alert_text,
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).strftime("%H:%M"),
    }
    print(f"[MOCK ACTION] escalating to human: {reason}")
    return action_record

def check_service_status(host, service):
    status_record = {
        "action": "check_service_status",
        "host": host,
        "service": service,
        "process_running": False,
        "last_deploy_minutes_ago": 12,
        "recent_log_line": "connection refused: upstream database unreachable",
        "timestamp": datetime.now(timezone.utc).strftime("%H:%M"),
    }
    print(f"[MOCK DIAGNOSTIC] checked {service} on {host}")
    return status_record


# maps a tool name the model can request to the real Python function that runs it
AVAILABLE_FUNCTIONS = {
    "restart_service": restart_service,
    "escalate_to_human": escalate_to_human,
    "check_service_status": check_service_status,
}

def log_event(alert_id, event_type, data):
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "alert_id": alert_id,
        "event_type": event_type,
        "data": data,
    }

    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")

def check_reason_is_grounded(alert_text, runbook, reason, diagnostics_gathered):
    diagnostics_text = ""
    for diagnostic_result in diagnostics_gathered:
        diagnostics_text = diagnostics_text + json.dumps(diagnostic_result) + "\n"

    system_prompt = (
        "You are a fact-checker. You will be given an alert, a runbook, results "
        "from any diagnostic checks that were run, and a reason someone gave for "
        "a decision. Check whether every factual claim in the reason is actually "
        "supported by the alert, the runbook, or the diagnostic results.\n\n"
        "A claim does not need to be an exact quote. If any of these sources "
        "states something, restating it in different words still counts as "
        "grounded. Only flag a claim if it states something that none of these "
        "sources said at all, or that contradicts them.\n\n"
        "Reply in exactly this format, nothing else:\n"
        "grounded: yes or no\n"
        "explanation: one sentence, quoting the unsupported claim if grounded is no"
    )

    user_message = (
        f"ALERT:\n{alert_text}\n\n"
        f"RUNBOOK:\n{runbook}\n\n"
        f"DIAGNOSTIC RESULTS:\n{diagnostics_text}\n\n"
        f"REASON TO CHECK:\n{reason}"
    )

    response = llm.chat.completions.create(
        model=config["classifier_model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0,
    )

    raw_reply = response.choices[0].message.content.strip()
    reply_lines = raw_reply.splitlines()

    parsed = {}
    for line in reply_lines:
        if ": " in line:
            parts = line.split(": ", 1)
            key = parts[0].strip()
            value = parts[1].strip()
            parsed[key] = value

    grounded = parsed.get("grounded")
    explanation = parsed.get("explanation", "")

    if grounded not in ("yes", "no"):
        raise ValueError(f"unexpected grounded-check format:\n{raw_reply}")

    return {"grounded": grounded == "yes", "explanation": explanation}

def handle_alert(alert_text):
    alert_id = str(uuid.uuid4())[:8]

    alert_type = classify_alert(alert_text)
    log_event(alert_id, "classification", {"alert_text": alert_text, "alert_type": alert_type})
    print("alert type:", alert_type)

    runbook = get_full_runbook(alert_type)

    system_prompt = (
        "You resolve infrastructure alerts. You are given an alert and its runbook. "
        "Do not guess facts that are not stated in the alert or the runbook. If you "
        "are not sure whether a condition applies (for example, whether a host is "
        "stateful, a database, or production), call check_service_status first.\n\n"
        "Once you have enough information, call record_decision with auto_resolve "
        "or escalate and your reason.\n\n"
        "After that, call restart_service if you decided auto_resolve, or "
        "escalate_to_human if you decided escalate. Only call the action that "
        "matches your recorded decision."
    )

    user_message = f"ALERT:\n{alert_text}\n\nRUNBOOK:\n{runbook}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    recorded_decision = None
    diagnostics_gathered = []


    while True:
        response = llm.chat.completions.create(
            model=config["classifier_model"],
            messages=messages,
            tools=tools,
            temperature=0,
        )

        message = response.choices[0].message

        response = llm.chat.completions.create(
            model=config["classifier_model"],
            messages=messages,
            tools=tools,
            temperature=0,
        )

        message = response.choices[0].message
        print("raw content:", repr(message.content))
        print("tool_calls:", message.tool_calls)

        if not message.tool_calls:
            log_event(alert_id, "error", {"message": "model did not call a tool", "raw_reply": message.content})
            raise ValueError(f"model did not call a tool:\n{message.content}")

        tool_call = message.tool_calls[0]
        tool_name = tool_call.function.name
        arguments = json.loads(tool_call.function.arguments)

        if "alert_text" in arguments:
            arguments["alert_text"] = alert_text

        # diagnostic step: run it, log it, feed the result back, keep going
        if tool_name == "check_service_status":
            function_to_call = AVAILABLE_FUNCTIONS[tool_name]
            tool_result = function_to_call(**arguments)
            diagnostics_gathered.append(tool_result)
            log_event(alert_id, "diagnostic", {"tool_called": tool_name, "result": tool_result})

            messages.append({"role": "assistant", "tool_calls": [tool_call]})
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(tool_result)})
            continue

        # the model recording its decision: save it, feed it back, keep going
        if tool_name == "record_decision":
            recorded_decision = arguments["decision"]

            grounding_check = check_reason_is_grounded(alert_text, runbook, arguments["reason"], diagnostics_gathered)
            log_event(alert_id, "guardrail_check", grounding_check)

            if not grounding_check["grounded"]:
                log_event(alert_id, "error", {"message": "decision reason not grounded, forcing escalate", "explanation": grounding_check["explanation"]})
                recorded_decision = "escalate"
                arguments["reason"] = f"Forced escalation: original reasoning was not grounded in the alert or runbook ({grounding_check['explanation']})"

            log_event(alert_id, "decision", {"decision": recorded_decision, "reason": arguments["reason"]})
            print("decision:", recorded_decision)
            print("reason:", arguments["reason"])

            messages.append({"role": "assistant", "tool_calls": [tool_call]})
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": "decision recorded"})
            continue

        # anything else is a real action: this is where we check it against the recorded decision
        if recorded_decision is None:
            log_event(alert_id, "error", {"message": f"model called {tool_name} before recording a decision"})
            raise ValueError(f"model called {tool_name} before calling record_decision")

        if recorded_decision == "auto_resolve" and tool_name != "restart_service":
            log_event(alert_id, "error", {"message": f"decision was auto_resolve but model called {tool_name}"})
            raise ValueError(f"decision was auto_resolve but model called {tool_name}")

        if recorded_decision == "escalate" and tool_name != "escalate_to_human":
            log_event(alert_id, "error", {"message": f"decision was escalate but model called {tool_name}"})
            raise ValueError(f"decision was escalate but model called {tool_name}")

        function_to_call = AVAILABLE_FUNCTIONS[tool_name]

        if "alert_text" in arguments:
            arguments["alert_text"] = alert_text

        action_result = function_to_call(**arguments)
        log_event(alert_id, "action", {"tool_called": tool_name, "result": action_result})

        print("final alert stored:", action_result["alert"])
        return action_result



if __name__ == "__main__":
    handle_alert("ServiceDown: some-new-service health check failing on host prod-x1, port 8080 not listening")