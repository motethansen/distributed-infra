# DI-076 diagnosis: task records for academictool sprint 4, S-7 / S-8

Read-only investigation of four academictool sprint-4 tasks dated 2026-11-10 (stories S-7 and S-8): the DeepSeek `t1`/`t2` backend tasks and their Claude `integrate:t1`/`integrate:t2` follow-ups. Values below are copied from `GET /tasks/{id}`; nothing was modified.

## Task records

**Source:** live mac-mini orchestrator (`GET /tasks?limit=500` and `GET /tasks/{id}` on port 8000, read-only, `x-secret-key` header). The records were present, so no Litestream/rqlite fallback was needed.

| Label | Orchestrator task id | Agent | Machine | Final status | Started | Finished |
|---|---|---|---|---|---|---|
| S-7 t1 | `73bab341-a4c4-44a2-966e-65869475339e` | deepseek | thinkpad-x1 | done | 2026-11-10T03:15:37.693022 | 2026-11-10T03:16:16.293985 |
| S-7 integrate:t1 | `7ef9b913-6c5f-444f-823c-fc2ec93d012d` | claude | mac-mini | needs_human | 2026-11-10T03:16:22.974528 | 2026-11-10T03:46:23.238126 |
| S-8 t2 | `01a8f701-2077-403e-afbe-e2f6e8433cef` | deepseek | thinkpad-x1 | done | 2026-11-10T04:07:08.732565 | 2026-11-10T04:07:32.010512 |
| S-8 integrate:t2 | `5fb668eb-5e95-44d7-9241-7490e913d861` | claude | mac-mini | done | 2026-11-10T04:07:40.378106 | 2026-11-10T04:09:32.435746 |

Identification: tasks matched on `payload.project=academictool`, `sprint=4`, `story`, and `flow_key` (`t1`, `integrate:t1`, `t2`, `integrate:t2`). Note that the queue also holds later same-day tasks with `flow_key` t1/t2 for these stories (S-7 t1 `ce1119cf-…`, claude on thinkpad-x1, started 2026-11-10T10:49:53; S-8 t2 `13eac51a-…`, claude on thinkpad-x1, started 2026-11-10T12:05:56). The rows above are the earlier DeepSeek `t1`/`t2` tasks that pair with the `integrate:` tasks.

## Notes and results

### S-7 t1 (`73bab341-a4c4-44a2-966e-65869475339e`)

- notes: `dev_backend S-7 t1`
- result `response`: 30315 chars total (truncated to first 1500)

```text
I can't inspect the existing repo (no file access), so the paths below follow a conventional layout for a Cloudflare Worker + Durable Object "auth" function; where I had to guess a path I say so in one line. All code is plain text; nothing was run.

Guessed layout (adjust to your repo): migrations live in `migrations/` and are numbered sequentially; the auth DO lives in `src/auth/`; the Worker entry is `src/index.ts`; shared DB helpers in `src/db.ts`; the verify endpoint lives in `src/auth/verify.ts`; the course-link request in `src/course/link.ts`.

Assumption noted in code: the sign-up form uses Turnstile (per constraint "If the sign-up form already uses Turnstile, require it"). If it does not, the `requireTurnstile` calls are commented and a TODO comment marks the spot.

FILE: migrations/00XX_email_verification_recovery.sql
```sql
-- Run as academictool_migrator on :25060.
-- Idempotent-safe; safe to re-run.
-- Adds resend bookkeeping columns to the accounts table.
-- NOTE: table name assumed to be public.accounts (the users/accounts table used by auth).

BEGIN;

ALTER TABLE public.accounts
  ADD COLUMN IF NOT EXISTS verification_last_sent_at   timestamptz,
  ADD COLUMN IF NOT EXISTS verification_send_count     integer     NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS verification_window_start   timestamptz;

-- Invariant: counter is only meaningful inside the window; keep it non-negative.
ALTER TABLE public.accounts
  DROP CONSTRAINT IF EXISTS accounts_verification_send_
```

### S-7 integrate:t1 (`7ef9b913-6c5f-444f-823c-fc2ec93d012d`)

- notes: `claude agent failed: claude CLI timed out after 1800s`
- result: no `response` text; result keys: failover, answered_by, answered_model, requested, agent; `answered_by`=None, `failover`=[].

### S-8 t2 (`01a8f701-2077-403e-afbe-e2f6e8433cef`)

- notes: `dev_backend S-8 t2`
- result `response`: 19135 chars total (truncated to first 1500)

```text
**A class-code mechanism exists, but it is an approval-request code, not a direct join code.** Per the recon report, `courses.student_request_code` only names a course and creates a pending `student_link_requests` row that a teacher must approve. There is no expiry, rotation, seat limit, or direct link-on-redeem behaviour for it. Therefore, per your instruction, I stop on the class-code direct-join code path and deliver only the invite path.

**Consequence:** Requirements (b), (c) and (e) are **not implemented** here, because they depend on a class-code join that does not exist. There is no code to build them on without inventing a new mechanism (expiry, rotation, seat checks), which the report says is absent. Requirement (a) is implemented on top of S-1's transactional redemption. Requirement (d) is implemented for the invite path only. Requirement (e)'s roster exposure is included for `route`, `trust` and `email_verified`. Requirement (f) is included, and existing rows are backfilled as the invite route.

**Assumption to check before merge:** S-1's transactional redemption is assumed to expose a single function, `redeemInviteTx(tx, { inviteId, accountId })`, which performs the invite state change and the course/student link insert inside a caller-supplied transaction. If S-1 named it differently, only the call site inside `accept` changes; the logic stays the same. The report describes S-1 as a findings-only commit, so this helper may need to be taken from S-1's branch.

FI
```

Full text saved outside the repo at `/tmp/di076_s8_t2_result.txt`.

### S-8 integrate:t2 (`5fb668eb-5e95-44d7-9241-7490e913d861`)

- notes: `integrate S-8`
- result `response`: 3917 chars total (truncated to first 1500)

```text
I haven't applied any of the returned code, so there's no commit, push or PR yet. It can't be applied as written, and the class-code part needs a decision from you first.

**Problems with the returned code**
- **Truncated:** `course-links.js` ends mid-statement at `con`, and the reply cuts off there. There are no tests, no Playwright spec, no S-7 panel change and no roster UI.
- **Migration is redundant:** `student_course_links.route` and `trust` already exist in `scripts/migrate-student-links.ts`. The returned migration's `CHECK (route IN ('invite','code','legacy'))` would also drop the existing `'scan'` and `'request'` routes. Its backfill would relabel every row as `'invite'`.
- **`student-invites.js` would be overwritten:** the real file (268 lines) is the teacher-side invite service (`ensure`, `prepare`, `mailMerge`, `overview`, and the HMAC-derived tokens). The returned file replaces all of that with a different class.
- **Wrong assumptions:**
  - `redeemInviteTx` doesn't exist, and S-1 didn't add it either.
  - The code uses a `db.one/db.tx` API, but the repo uses postgres.js (`sql`, `sql.begin`).
  - It selects no `target_email` column, and `student_invites` has none; the address lives in `students.email`.
  - `course_teachers` and `join_attempts` don't exist.
- **The class-code path is missing:** the author left it out, which is the part most of the criteria depend on.

**What's real in the repo (on `main` at 1604f144)**
- `StudentLinkService.linkProved` (`student-li
```

## S-7: which timer ended the run

**Verdict: the task itself was ended by the worker-side agent timeout (certain). Which timer ended the *run* (the dispatcher process) is `unknown`: the dispatcher's `Client.wait` deadline expired about 4.4 s before the worker's agent timeout, so it very probably raised first, but no run log or stored `PlanError` text is in the records.**

### The two timers in code

| Timer | Where | Default / value used | Starts at | Message |
|---|---|---|---|---|
| Dispatcher `Client.wait` | `shared/scrum_flow.py:274` (`timeout: float = 1800`), deadline set at `:276`, checked and raised at `:284-285` | 1800 s; `run()` (`:539-545`) and `batch()` (`:551-566`) call `c.wait(...)` without a timeout, so the default applies | When `wait()` is entered, i.e. right after `c.queue()` returns, so at queue time rather than run start | `PlanError("timed out waiting for: <task ids>")` (`:285`) |
| Worker agent timeout | `agents/claude_agent.py:34` (`CLAUDE_AGENT_TIMEOUT_SECS`, default 1800), `:107-117` (`asyncio.wait_for(proc.communicate(), timeout=effective_timeout)`), message at `:120`; `agents/agy_agent.py:32,62-65` is the same for agy | The task payload `timeout` (the flow sends `timeout=1800` at `scrum_flow.py:541` and `:556`) is passed `agent_run.py:38` -> `agents/runner.py:130` -> `claude_agent.run(timeout=...)`, so 1800 s | When the claude CLI subprocess starts, i.e. at claim/start time | `{"error": "claude CLI timed out after 1800s"}`; `worker/handlers/agent_run.py` turns that into `needs_human` with notes `"claude agent failed: claude CLI timed out after 1800s"` |

`worker/poller.py` has no task-level timeout (only HTTP timeouts of 3-30 s). `run_plan` also does not cancel the queued task when `wait()` times out.

The two `PlanError` texts are raised in different places:
- `"timed out waiting for: ..."`: `scrum_flow.py:285`, from `Client.wait`. It means the task was still non-terminal when the dispatcher's 1800 s expired.
- `"task <key> ended <status>: <notes> — stopped; fix and re-run"`: `scrum_flow.py:547` (`run()`) and `:564` (`batch()`). It means the task reached a terminal status (`done`/`failed`/`needs_human`) that is not `done`. For this run it would read `task integrate:t1 ended needs_human: claude agent failed: claude CLI timed out after 1800s — stopped; fix and re-run`.

The integrator is queued through `run("integrator", ...)` at `scrum_flow.py:581-582`, inside the wave loop (after the parallel `batch()` of text-only DeepSeek tasks).

### Evidence: S-7 `integrate:t1` (`7ef9b913-6c5f-444f-823c-fc2ec93d012d`)

Read-only `GET /tasks/{id}` on the mac-mini orchestrator:

| Field | Value |
|---|---|
| payload `timeout` | `1800` |
| `created_at` (queued) | 2026-11-10T03:16:18.605891 |
| `started_at` | 2026-11-10T03:16:22.974528 |
| `finished_at` | 2026-11-10T03:46:23.238126 |
| status | `needs_human` |
| notes | `claude agent failed: claude CLI timed out after 1800s` |
| result | no `response`; keys `failover`, `answered_by`=None, `failover`=[], `requested`, `agent` |

Arithmetic:
- started to finished = 1800.26 s. This matches the worker agent timeout (1800 s plus a fraction of a second to kill the CLI). It is not a coincidence of the dispatcher, which never writes task status.
- queued to finished = 1804.63 s. The dispatcher's deadline (`wait()` entered at or just after `created_at`, plus 1800 s) was therefore about 03:46:18.6, which is roughly 4.4 s before `finished_at`. The queue wait of 4.37 s (queued 03:16:18.6, started 03:16:22.97) is exactly why the dispatcher's timer, which starts at queue time, expires before the worker's, which starts at run start.
- The notes text names the worker-side message (`agents/claude_agent.py:120`), so the worker agent timeout is what put the task into `needs_human`.

### What this does and does not establish

- Certain: the task died from the worker-side agent timeout (`claude CLI timed out after 1800s`), not from anything the dispatcher did.
- Probable but not provable: because both timers were 1800 s and the dispatcher's started about 4.4 s earlier, a dispatcher polling every 3 s (`poll=3`, `scrum_flow.py:274`) would have seen the task still `in_progress` after its deadline and raised `PlanError("timed out waiting for: 7ef9b913-...")` at `:285` before the task became `needs_human`. If no poll fell inside that 4.4 s window the dispatcher instead saw `needs_human` and raised the `ended needs_human: claude agent failed...` message at `:547`.
- Missing: the dispatcher's own output (the `say()` lines and the final `PlanError`) for this run is not in the task records, and I did not find it stored anywhere. The orchestrator only holds task rows. So which `PlanError` text the run produced is **unknown**.
- Consequence for S-2: the two timers must produce distinguishable messages and should not race. The dispatcher's `"timed out waiting for:"` message carries no timer name, elapsed time, or task status, and the worker's `needs_human` carries no hint of the dispatcher's timer. S-2 should (a) make the dispatcher message say it is the dispatcher wait and include the task's last known status and elapsed time since queue, (b) include the queue-to-start delay, and (c) set the dispatcher wait comfortably above the task timeout (for example task timeout + queue grace) so the worker's own error is the one reported, and the dispatcher cancels or marks the task when it does give up.

### Finding: integrator working directory (DI-020 `~` issue) for `integrate:t1`

No, the DI-020 `~` problem did not occur for `integrate:t1`.
- The task payload has **no `cwd` key** (`run_plan` was called with `cwd=None`, so `tags()` at `scrum_flow.py:304-305` omits it). `agent_run.py:36` passes `cwd=None`, and `claude_agent.py:107` yields `work_dir = None`, so the claude CLI ran in the worker's own working directory (mac-mini worker), not in a `~`-prefixed path.
- Even if a `~` cwd had been sent, the DI-020 fix is present: `agents/claude_agent.py:107` expands it with `os.path.expanduser`, and `worker/handlers/common.py:87` does the same for scripts (fix commit e9e23f8).
- A related observation, not a DI-020 bug: the integrator's prompt (15505 chars) tells the agent to find or clone the repo itself with `find ~/Projects ...`, because no cwd is set. That is the intended design, so the agent was in the worker's default directory when it started and relied on its own `find`/clone. I cannot tell from the records how much of the 1800 s went on that; the result has no response text.
