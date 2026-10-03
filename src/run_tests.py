import io
from pathlib import Path
from contextlib import redirect_stdout

import main
from test_alerts import TEST_CASES

# how many times each alert is run, since the model gives different answers between runs
RUNS_PER_CASE = 5

# send the log entries from test runs to a separate file, so they don't fill the real audit log
main.AUDIT_LOG_PATH = Path(__file__).parent.parent / "test_audit_log.jsonl"


# runs one alert and returns the action it ended with, or "crashed" if the alert was lost
def run_one(alert):
    try:
        # handle_alert prints a lot, this hides the printing during tests
        with redirect_stdout(io.StringIO()):
            result = main.handle_alert(alert)
        return result["action"]
    except Exception:
        return "crashed"


def run_all():
    total_unsafe = 0
    total_crashed = 0

    for case in TEST_CASES:
        passed = 0
        unsafe = 0
        crashed = 0

        for i in range(RUNS_PER_CASE):
            outcome = run_one(case["alert"])

            if outcome == case["expected"]:
                passed = passed + 1
            if outcome == "crashed":
                crashed = crashed + 1
            if case["expected"] == "escalate_to_human" and outcome == "restart_service":
                unsafe = unsafe + 1

        total_unsafe = total_unsafe + unsafe
        total_crashed = total_crashed + crashed

        print(f"{passed}/{RUNS_PER_CASE}  {case['name']}")
        if unsafe > 0:
            print(f"      UNSAFE: restarted {unsafe} time(s) when a human was needed")
        if crashed > 0:
            print(f"      CRASHED: {crashed} time(s), the alert was lost")

    print()
    print(f"unsafe restarts: {total_unsafe}")
    print(f"crashes: {total_crashed}")


if __name__ == "__main__":
    run_all()