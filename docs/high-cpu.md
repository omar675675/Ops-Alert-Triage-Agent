# High CPU Usage

## Alert Type
cpu_usage_high

## Symptoms
- CPU above 90% for more than 10 minutes
- High load average and slow responses
- Alert names like HighCPU, CPUSaturation

## Diagnosis
1. Identify the host and how long the alert has been firing.
2. List the top processes by CPU.
3. Decide whether the process is expected (backup, batch job, scheduled task) or runaway.
4. Check for a recent deploy or a traffic spike.
5. Check whether one host is affected or many. Many hosts points to load, not a local bug.

## Resolution
1. If it is a known scheduled job, wait for it to finish and note it on the ticket. Do not intervene.
2. If a single process is runaway, recommend restarting that process.
3. If several hosts are affected, escalate for a scaling decision.
4. Never kill an unrecognized process.

## Risk Level
Low for read-only diagnostics. High for killing or restarting processes on production hosts, and for any scaling action.

## Approval
- Auto-approved: read-only diagnostics, re-checking CPU after 10 minutes
- Requires human approval: killing or restarting any process on a production host, scaling, changing resource limits

## Escalate When
- CPU is still above 90% 30 minutes after action
- Multiple hosts are affected
- The top process is not recognized
- The host runs a database
