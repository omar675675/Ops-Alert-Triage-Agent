# Disk Usage Critical

## Alert Type
disk_usage_high

## Symptoms
- Filesystem usage above 90% on a host
- Services failing to write logs or temp files
- Alert names like DiskSpaceCritical, FilesystemAlmostFull

## Diagnosis
1. Identify the affected mount point from the alert labels.
2. Check what is consuming space: large log files, old backups, temp directories.
3. Confirm growth rate. A slow fill means cleanup is enough. A fast fill means something is misbehaving.

## Resolution
1. Rotate or compress old logs in /var/log.
2. Delete temp files older than 7 days.
3. If usage is still above 85%, escalate for a disk expansion.

## Risk Level
Low for log rotation and temp cleanup. High for deleting anything outside /var/log and /tmp.

## Approval
- Auto-approved: log rotation, temp file cleanup
- Requires human approval: deleting backups, resizing volumes, any action on a database host

## Escalate When
- Usage keeps growing after cleanup
- The host runs a database or is marked production-critical