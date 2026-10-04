import random
import time
import requests

# where the server is listening
SERVER_URL = "http://localhost:8000/alert"

# seconds to wait between alerts, so the Groq daily token limit lasts
SECONDS_BETWEEN_ALERTS = 30

# login the server asks for
USERNAME = "admin"
PASSWORD = "admin"

# services with the host and port they run on
SERVICES = [
    ("internal-metrics-exporter", "staging-2", 9100),
    ("billing-worker", "prod-app-2", 9200),
    ("auth-service", "prod-app-7", 8080),
    ("payment-api", "prod-web-3", 8080),
    ("some-new-service", "prod-x1", 8080),
]

# hosts for alerts that are about a machine and not a service
HOSTS = ["prod-app-4", "prod-db-1", "staging-2", "prod-web-3"]

# domains for certificate alerts
DOMAINS = ["staging.example.com", "api.example.com", "www.example.com"]


# builds one fake alert as a piece of text
def make_alert():
    # service_down is listed three times so it comes up more often than the others
    kind = random.choice(["service_down", "service_down", "service_down", "disk", "cpu", "latency", "certificate"])

    if kind == "service_down":
        service, host, port = random.choice(SERVICES)
        return f"ServiceDown: {service} health check failing on host {host}, port {port} not listening"

    if kind == "disk":
        host = random.choice(HOSTS)
        percent = random.randint(90, 99)
        return f"DiskSpaceCritical: /var is at {percent}% on host {host}"

    if kind == "cpu":
        host = random.choice(HOSTS)
        load = random.randint(10, 30)
        process = random.choice(["a nightly backup job", "java", "python", "an unknown process"])
        return f"HighCPU: load average {load} on host {host}, top process is {process}"

    if kind == "latency":
        service, host, port = random.choice(SERVICES)
        seconds = random.randint(2, 6)
        return f"HighLatency: p99 response time above {seconds}s on {service}, host {host}"

    domain = random.choice(DOMAINS)
    days = random.randint(2, 14)
    return f"CertExpiringSoon: TLS certificate for {domain} expires in {days} days"

# sends one alert to the server and returns the server's reply as a dict
def send_alert(alert_text):
    response = requests.post(SERVER_URL, json={"text": alert_text}, auth=(USERNAME, PASSWORD), timeout=120)
    return response.json()


# how many alerts to send in one run
ALERTS_TO_SEND = 5


if __name__ == "__main__":
    for i in range(ALERTS_TO_SEND):
        alert_text = make_alert()
        print(f"\n[{i + 1}/{ALERTS_TO_SEND}] sending: {alert_text}")

        try:
            result = send_alert(alert_text)
            print("  action:", result["action"])
        except Exception as error:
            print("  failed:", str(error)[:150])

        if i < ALERTS_TO_SEND - 1:
            time.sleep(SECONDS_BETWEEN_ALERTS)