# Service Down

## Alert Type
service_down

## Symptoms
- Health check failing or port not listening
- Process not running on the host
- Alert names like ServiceDown, HealthCheckFailed, TargetDown

## Diagnosis
1. Identify the service and host from the alert labels.
2. Check process status with systemctl status.
3. Read the last 100 log lines for the crash reason: out of memory, config error, dependency unreachable.
4. Check whether a deploy happened in the last 30 minutes.
5. Check dependencies: database, upstream services, network reachability.

## Resolution
1. If the process stopped with no error, restart it once.
2. Verify the health check passes within 2 minutes.
3. If it fails again, do not restart a second time. Escalate.
4. If the cause was out of memory, escalate for a resource review after the restart.

## Risk Level
Low for a single restart of a stateless service. High for databases, stateful services, and anything handling payments or authentication.

## Approval
- Auto-approved: one restart of a stateless service
- Requires human approval: restart of a database or stateful service, restart during a deploy, any second restart attempt, rollbacks

## Escalate When
- The service fails again after one restart
- Three or more restarts within 15 minutes (crash loop)
- Multiple services are down at the same time, which points to a shared dependency or network problem
- The service is tagged production-critical
