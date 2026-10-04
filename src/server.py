from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from urllib.parse import parse_qs
from pathlib import Path
import base64
import json
import secrets
import threading
import main


# the one login that works
USERNAME = "admin"
PASSWORD = "admin"

# name of the cookie that holds the login
COOKIE_NAME = "session"

# logins that are currently valid (cleared when the server restarts)
sessions = set()

# the server itself
app = FastAPI()


# describes what an incoming alert looks like: one field called text
class Alert(BaseModel):
    text: str


# true when the username and password are right
def check_login(username, password):
    name_ok = secrets.compare_digest(username.encode(), USERNAME.encode())
    password_ok = secrets.compare_digest(password.encode(), PASSWORD.encode())
    return name_ok and password_ok


# lets a request through if it has a valid login cookie,
# or a username and password sent as basic auth (that is what the alert generator uses)
def require_login(request: Request):
    if request.cookies.get(COOKIE_NAME) in sessions:
        return

    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode()
            username, _, password = decoded.partition(":")
            if check_login(username, password):
                return
        except Exception:
            pass

    raise HTTPException(status_code=401, detail="Not logged in")


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Log in - Auto Alert Handler</title>
  <style>
    body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
           background: #171d2b; color: #e4e8f0; font: 15px/1.5 system-ui, sans-serif; }
    form { width: 300px; padding: 28px; background: #1e2636; border: 1px solid #2f3a50; border-radius: 4px; }
    h1 { margin: 0 0 20px; font-size: 20px; }
    label { display: block; margin-bottom: 4px; font-size: 13px; color: #8793a9; }
    input { width: 100%; box-sizing: border-box; margin-bottom: 14px; padding: 8px; font: inherit;
            color: inherit; background: #171d2b; border: 1px solid #2f3a50; border-radius: 3px; }
    button { width: 100%; padding: 9px; font: inherit; font-weight: 600; color: #171d2b;
             background: #5fb7a5; border: none; border-radius: 3px; cursor: pointer; }
    .error { margin-bottom: 14px; color: #e5645a; font-size: 14px; }
  </style>
</head>
<body>
  <form method="post" action="/login">
    <h1>Auto Alert Handler</h1>
    ERROR_MESSAGE
    <label for="username">Username</label>
    <input id="username" name="username" autofocus autocomplete="username">
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password">
    <button type="submit">Log in</button>
  </form>
</body>
</html>"""


# shows the login page
@app.get("/login")
def login_page():
    return HTMLResponse(LOGIN_PAGE.replace("ERROR_MESSAGE", ""))


# checks the username and password, then sets the login cookie
@app.post("/login")
async def login(request: Request):
    body = (await request.body()).decode()
    fields = parse_qs(body)
    username = fields.get("username", [""])[0]
    password = fields.get("password", [""])[0]

    if not check_login(username, password):
        error = '<div class="error">Wrong username or password.</div>'
        return HTMLResponse(LOGIN_PAGE.replace("ERROR_MESSAGE", error), status_code=401)

    token = secrets.token_urlsafe(32)
    sessions.add(token)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax")
    return response


# removes the login
@app.get("/logout")
def logout(request: Request):
    sessions.discard(request.cookies.get(COOKIE_NAME))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# runs when someone opens the main address in a browser
@app.get("/")
def dashboard(request: Request):
    # not logged in: go to the login page instead of showing an error
    try:
        require_login(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(Path(__file__).parent / "dashboard.html")

# runs when someone sends a GET request to /alerts
@app.get("/alerts", dependencies=[Depends(require_login)])
def list_alerts():
    return read_alerts()

# runs when someone sends a POST request to /alert
@app.post("/alert", dependencies=[Depends(require_login)])
def receive_alert(alert: Alert):
    result = main.handle_alert(alert.text)
    return result


# how many alerts /alerts sends back at most (the newest ones)
MAX_ALERTS = 200

# what the server remembers between calls, so each call only reads the new lines of the log
log_state = {"offset": 0, "alerts": {}, "order": []}
log_lock = threading.Lock()


# adds one log entry to the alert it belongs to
def apply_entry(entry):
    # lines written by older versions of the log have no alert_id, skip them
    if "alert_id" not in entry:
        return

    alert_id = entry["alert_id"]
    alerts = log_state["alerts"]

    if alert_id not in alerts:
        alerts[alert_id] = {
            "alert_id": alert_id,
            "time": entry.get("timestamp", ""),
            "alert_text": "",
            "alert_type": "",
            "decision": "",
            "reason": "",
            "action": "",
            "diagnostics": 0,
            "flags": [],
        }
        log_state["order"].append(alert_id)

    alert = alerts[alert_id]
    event_type = entry.get("event_type", "")
    data = entry.get("data", {})

    if event_type == "classification":
        alert["alert_text"] = data.get("alert_text", "")
        alert["alert_type"] = data.get("alert_type", "")
    elif event_type == "input_blocked":
        alert["alert_text"] = data.get("alert_text", "")
        alert["flags"].append("rejected at input")
    elif event_type == "diagnostic":
        alert["diagnostics"] = alert["diagnostics"] + 1
    elif event_type == "decision":
        alert["decision"] = data.get("decision", "")
        alert["reason"] = data.get("reason", "")
    elif event_type == "action":
        alert["action"] = data.get("tool_called", "")
        # alerts that never got a decision take the reason from the escalation
        result = data.get("result", {})
        if alert["reason"] == "" and isinstance(result, dict) and "reason" in result:
            alert["reason"] = result["reason"]
    elif event_type == "error":
        message = str(data.get("message", ""))[:100]
        if message not in alert["flags"]:
            alert["flags"].append(message)


# reads the new lines of the audit log and groups them by alert ID
# returns a list of alerts, newest first, at most MAX_ALERTS
def read_alerts():
    if not main.AUDIT_LOG_PATH.exists():
        return []

    with log_lock:
        # the file got smaller, so it was replaced: start again from the top
        if main.AUDIT_LOG_PATH.stat().st_size < log_state["offset"]:
            log_state["offset"] = 0
            log_state["alerts"] = {}
            log_state["order"] = []

        with open(main.AUDIT_LOG_PATH, "rb") as f:
            f.seek(log_state["offset"])
            chunk = f.read()

        # only use whole lines, a line still being written is picked up next time
        end = chunk.rfind(b"\n")
        if end == -1:
            chunk = b""
        else:
            chunk = chunk[:end + 1]
        log_state["offset"] = log_state["offset"] + len(chunk)

        for line in chunk.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line == "":
                continue

            # one bad line must never break the whole list
            try:
                apply_entry(json.loads(line))
            except Exception:
                continue

        result = []
        for alert_id in reversed(log_state["order"][-MAX_ALERTS:]):
            # copy, so the caller never changes what is remembered
            alert = dict(log_state["alerts"][alert_id])
            alert["flags"] = list(alert["flags"])
            result.append(alert)
        return result


# runs the server when you start this file directly: python src/server.py
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)