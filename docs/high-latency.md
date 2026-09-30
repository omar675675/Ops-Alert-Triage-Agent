# High Latency

## Alert Type
latency_high

## Symptoms
- p95 or p99 response time above threshold
- Request timeouts and user complaints
- Alert names like HighLatency, SlowResponses, P99Breach

## Diagnosis
1. Identify the service and the slow endpoints.
2. Check whether latency started after a deploy or config change.
3. Check downstream dependencies: database query times, upstream APIs.
4. Check saturation: CPU, memory, connection pool, disk I/O.
5. Check the network path: packet loss, DNS resolution time, link utilization between tiers.

## Resolution
1. If latency started within 60 minutes of a deploy, escalate with the change details so a human can decide on rollback.
2. If a connection pool is exhausted, escalate. Do not resize pools automatically.
3. If there is packet loss on a link, open a ticket with the network team.
4. If nothing is found, attach the collected metrics and escalate.

## Risk Level
Low for diagnostics. High for any change, because latency has ambiguous causes and a wrong fix can make it worse.

## Approval
- Auto-approved: read-only diagnostics, collecting a metrics and traces snapshot
- Requires human approval: every remediation, including rollback, config change, scaling, and restart

## Escalate When
- Always, after diagnostics. This runbook does not authorize automated remediation.
