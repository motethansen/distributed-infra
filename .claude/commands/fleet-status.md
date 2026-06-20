---
description: Show fleet health — all machines online/offline, active tasks, and queue summary.
---

Fetch and display fleet status from the distributed-infra orchestrator.

Steps:
1. GET $ORCHESTRATOR_URL/machines (default: http://100.97.176.37:8000) with INFRA_SECRET_KEY header
2. GET $ORCHESTRATOR_URL/tasks?limit=20
3. Display:
   - Each machine: name, role, online status, last-seen if offline
   - Active/done/failed task counts per machine
   - Any tasks in needs_human or failed state (list them with notes)
4. If orchestrator is unreachable, say so clearly.

Use INFRA_SECRET_KEY in x-secret-key header.
