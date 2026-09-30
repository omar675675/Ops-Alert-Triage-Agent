import yaml
from openai import OpenAI
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
import json
from datetime import datetime, timezone

# ---------------- VARIABLES ----------------

# settings loaded from config.yaml (qdrant url, model names, api key, etc.)
with open("../config.yaml") as f:
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


def handle_alert(alert_text):
    alert_type = classify_alert(alert_text)
    runbook = get_full_runbook(alert_type)
    decision_result = decide_action(alert_text, runbook)

    print("alert type:", alert_type)
    print("decision:", decision_result["decision"])
    print("reason:", decision_result["reason"])

    # the approval gate: this is decided in code, not left to the model to choose
    if decision_result["decision"] == "auto_resolve":
        allowed_tool = "restart_service"
    else:
        allowed_tool = "escalate_to_human"

    system_prompt = (
        f"You resolve infrastructure alerts. A decision has already been made: "
        f"{decision_result['decision']}. You must call the {allowed_tool} tool "
        f"to carry it out. Do not call any other tool."
    )

    user_message = f"ALERT:\n{alert_text}\n\nRUNBOOK:\n{runbook}\n\nREASON FOR DECISION:\n{decision_result['reason']}"

    response = llm.chat.completions.create(
        model=config["classifier_model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        tools=tools,
        temperature=0,
    )

    message = response.choices[0].message

    if not message.tool_calls:
        raise ValueError(f"model did not call a tool:\n{message.content}")

    tool_call = message.tool_calls[0]
    tool_name = tool_call.function.name

    # the actual gate: even if the model tries to call the wrong tool, this stops it
    if tool_name != allowed_tool:
        raise ValueError(f"model called {tool_name} but only {allowed_tool} is allowed for this decision")

    arguments = json.loads(tool_call.function.arguments)

    # we already have the real alert text, don't trust the model's copy of it
    if "alert_text" in arguments:
        arguments["alert_text"] = alert_text

    function_to_call = AVAILABLE_FUNCTIONS[tool_name]
    action_result = function_to_call(**arguments)

    return action_result


if __name__ == "__main__":
    test_alert = "ServiceDown: payment-api health check failing on host prod-web-3, port 8080 not listening"
    result = handle_alert(test_alert)
    print("action result:", result)