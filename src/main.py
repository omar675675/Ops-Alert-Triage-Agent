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

AUDIT_LOG_PATH = Path(__file__).parent.parent / "audit_log.jsonl"
INVENTORY_PATH = Path(__file__).parent.parent / "inventory.yaml"


# settings loaded from config.yaml (qdrant url, model names, api key, etc.)
with open(Path(__file__).parent.parent / "config.yaml") as f:
    config = yaml.safe_load(f)

# the list of known services and the facts the runbooks depend on
with open(INVENTORY_PATH) as f:
    inventory_data = yaml.safe_load(f)

INVENTORY = inventory_data["services"]

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

# alerts longer than this are rejected, a normal alert is a few hundred characters at most
MAX_ALERT_LENGTH = 1000

# phrases that look like instructions to the AI, a real alert should never contain these
SUSPICIOUS_PHRASES = [
    "ignore previous instructions",
    "ignore all previous instructions",
    "ignore the runbook",
    "disregard the runbook",
    "skip the runbook",
    "skip approval",
    "do not escalate",
    "restart everything",
    "you are now",
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



# ---------------- FUNCTIONS ------------------

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

# fake diagnostic results, keyed by service name, so different alerts can be tested
FAKE_STATUS_BY_SERVICE = {
    "internal-metrics-exporter": {
        "process_running": False,
        "last_deploy_minutes_ago": 600,
        "recent_log_line": "process exited with code 0, no errors logged",
    },
}

# used for any service not listed above
DEFAULT_FAKE_STATUS = {
    "process_running": False,
    "last_deploy_minutes_ago": 12,
    "recent_log_line": "connection refused: upstream database unreachable",
}


def check_service_status(host, service):
    if service in FAKE_STATUS_BY_SERVICE:
        fake_status = FAKE_STATUS_BY_SERVICE[service]
    else:
        fake_status = DEFAULT_FAKE_STATUS

    status_record = {
        "action": "check_service_status",
        "host": host,
        "service": service,
        "process_running": fake_status["process_running"],
        "last_deploy_minutes_ago": fake_status["last_deploy_minutes_ago"],
        "recent_log_line": fake_status["recent_log_line"],
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
        "Conclusions and recommendations, such as 'therefore escalate', are not "
        "factual claims, so do not check them. Only check statements about the "
        "alert, the host, the service, or its state. A reason that approves an "
        "automatic restart must show the runbook's conditions are met. For "
        "example, the runbook only allows restarting a stateless service, so if "
        "the reason relies on that, it must be supported by the alert or the "
        "diagnostic results.\n\n"
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

# checks that the host and service the model asked about actually appear in the alert
# returns a problem message if something is wrong, or None if everything is fine
def find_bad_argument(alert_text, arguments):
    for field in ["host", "service"]:
        if field in arguments:
            value = arguments[field]
            if value.lower() not in alert_text.lower():
                return f"{field} '{value}' does not appear in the alert"
    return None

# checks the alert before the AI sees it
# returns a problem message if something is wrong, or None if the alert looks fine
def find_bad_alert(alert_text):
    if alert_text.strip() == "":
        return "the alert is empty"

    if len(alert_text) > MAX_ALERT_LENGTH:
        return f"the alert is too long ({len(alert_text)} characters, the limit is {MAX_ALERT_LENGTH})"

    lowered = alert_text.lower()
    cleaned = " ".join(lowered.split())

    for phrase in SUSPICIOUS_PHRASES:
        if phrase in cleaned:
            return f"the alert contains the phrase '{phrase}'"

    return None

# looks up one service in the inventory
# returns its facts as a dict, or None if the service is not listed
def get_service_info(service_name):
    name = service_name.lower()
    if name in INVENTORY:
        return INVENTORY[name]
    return None

# checks whether the services the model looked at are safe to restart automatically
# returns a problem message if not, or None if everything looks fine
def find_unsafe_to_restart(diagnostics_gathered):
    if len(diagnostics_gathered) == 0:
        return "no diagnostic check was run, so nothing is known about the service"

    for diagnostic_result in diagnostics_gathered:
        service = diagnostic_result["service"]
        service_info = get_service_info(service)

        if service_info is None:
            return f"{service} is not in the inventory"
        if service_info["stateless"] is False:
            return f"{service} is not stateless"
        if service_info["critical"] is True:
            return f"{service} is marked production-critical"

    return None

def handle_alert(alert_text):

    alert_id = str(uuid.uuid4())[:8]

    problem = find_bad_alert(alert_text)
    if problem is not None:
        log_event(alert_id, "input_blocked", {"alert_text": alert_text[:200], "problem": problem})
        return escalate_after_failure(alert_id, alert_text[:200], f"Alert rejected before processing: {problem}")


    try:
        alert_type = classify_alert(alert_text)
    except Exception as error:
        error_text = str(error)[:200]
        return escalate_after_failure(alert_id, alert_text, f"Automatic handling stopped: classification failed ({error_text})")
    log_event(alert_id, "classification", {"alert_text": alert_text, "alert_type": alert_type})
    print("alert type:", alert_type)

    try:
        runbook = get_full_runbook(alert_type)
    except Exception as error:
        error_text = str(error)[:200]
        return escalate_after_failure(alert_id, alert_text, f"Automatic handling stopped: runbook lookup failed ({error_text})")

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
    blocked_count = 0
    recorded_reason = None

    for i in range(8):  # allow the model to take up to 8 steps
        try:
            response = llm.chat.completions.create(
                model=config["classifier_model"],
                messages=messages,
                tools=tools,
                temperature=0,
            )
        except Exception as error:
            error_text = str(error)[:200]
            return escalate_after_failure(alert_id, alert_text, f"Automatic handling stopped: the model call failed ({error_text})")

        message = response.choices[0].message

        if not message.tool_calls:
            return escalate_after_failure(alert_id, alert_text, "Automatic handling stopped: the model did not call a tool")

        tool_call = message.tool_calls[0]
        tool_name = tool_call.function.name
        arguments = json.loads(tool_call.function.arguments)

        if "alert_text" in arguments:
            arguments["alert_text"] = alert_text

        # block the call if the model invented a host or service
        problem = find_bad_argument(alert_text, arguments)
        if problem is not None:
            blocked_count = blocked_count + 1
            log_event(alert_id, "error", {"message": "blocked tool call", "tool_called": tool_name, "problem": problem})

            # second time: stop asking the model and hand off to a human ourselves
            if blocked_count >= 2:
                reason = f"Automatic handling stopped: the model kept requesting tools with values that are not in the alert ({problem})"
                action_result = escalate_to_human(alert_text, reason)
                log_event(alert_id, "action", {"tool_called": "escalate_to_human", "result": action_result})
                return action_result

            # first time: tell the model what went wrong and let it try again
            messages.append({"role": "assistant", "tool_calls": [tool_call]})
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": f"Blocked: {problem}. Do not invent values. Use only what is in the alert. If you cannot proceed, call escalate_to_human."})
            continue

        # diagnostic step: run it, log it, feed the result back, keep going
        if tool_name == "check_service_status":
            function_to_call = AVAILABLE_FUNCTIONS[tool_name]
            tool_result = function_to_call(**arguments)

            # add what the inventory says about this service, so the model does not have to guess
            service_info = get_service_info(arguments["service"])
            if service_info is None:
                tool_result["inventory"] = "this service is not in the inventory, nothing is known about it"
            else:
                tool_result["inventory"] = service_info

            diagnostics_gathered.append(tool_result)
            log_event(alert_id, "diagnostic", {"tool_called": tool_name, "result": tool_result})

            messages.append({"role": "assistant", "tool_calls": [tool_call]})
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(tool_result)})
            continue

        # the model recording its decision: save it, feed it back, keep going
        if tool_name == "record_decision":
            recorded_decision = arguments["decision"]
            recorded_reason = arguments["reason"]
            
            # only auto_resolve can cause harm, escalating is always safe, so only check that one
            if recorded_decision == "auto_resolve":
                problem = find_unsafe_to_restart(diagnostics_gathered)
                if problem is not None:
                    reason = f"Forced escalation: {problem}"
                    log_event(alert_id, "decision", {"decision": "escalate", "reason": reason})
                    print("decision: escalate (forced)")
                    print("reason:", reason)
                    return escalate_after_failure(alert_id, alert_text, reason)

                grounding_check = check_reason_is_grounded(alert_text, runbook, arguments["reason"], diagnostics_gathered)
                log_event(alert_id, "guardrail_check", grounding_check)

                if not grounding_check["grounded"]:
                    reason = f"Forced escalation: the original reasoning was not supported ({grounding_check['explanation']})"
                    log_event(alert_id, "decision", {"decision": "escalate", "reason": reason})
                    print("decision: escalate (forced)")
                    print("reason:", reason)
                    return escalate_after_failure(alert_id, alert_text, reason)

            log_event(alert_id, "decision", {"decision": recorded_decision, "reason": arguments["reason"]})
            print("decision:", recorded_decision)
            print("reason:", arguments["reason"])

            messages.append({"role": "assistant", "tool_calls": [tool_call]})
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": "decision recorded"})
            continue

        # anything else is a real action: this is where we check it against the recorded decision
        if recorded_decision is None:
            return escalate_after_failure(alert_id, alert_text, f"Automatic handling stopped: the model called {tool_name} before recording a decision")

        if recorded_decision == "auto_resolve" and tool_name != "restart_service":
            return escalate_after_failure(alert_id, alert_text, f"Automatic handling stopped: the decision was auto_resolve but the model called {tool_name}")

        if recorded_decision == "escalate" and tool_name != "escalate_to_human":
            return escalate_after_failure(alert_id, alert_text, f"Automatic handling stopped: the decision was escalate but the model called {tool_name}")

        function_to_call = AVAILABLE_FUNCTIONS[tool_name]

        if "alert_text" in arguments:
            arguments["alert_text"] = alert_text

        if tool_name == "escalate_to_human" and recorded_reason is not None:
            arguments["reason"] = recorded_reason

        action_result = function_to_call(**arguments)
        log_event(alert_id, "action", {"tool_called": tool_name, "result": action_result})

        return action_result
    
    return escalate_after_failure(alert_id, alert_text, "Automatic handling stopped: the model took too many steps")

# hands the alert to a human when automatic handling fails, so no alert is silently dropped
def escalate_after_failure(alert_id, alert_text, reason):
    log_event(alert_id, "error", {"message": reason})
    action_result = escalate_to_human(alert_text, reason)
    log_event(alert_id, "action", {"tool_called": "escalate_to_human", "result": action_result})
    return action_result


if __name__ == "__main__":
    for _ in range(4):
        handle_alert("ServiceDown: billing-worker health check failing on host prod-app-2, port 9200 not listening")