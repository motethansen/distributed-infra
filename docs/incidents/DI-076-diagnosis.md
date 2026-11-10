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

## S-8: empty done

Question: S-8 `integrate:t2` (`5fb668eb-5e95-44d7-9241-7490e913d861`) finished `done` at 04:09:32, but nothing was applied: no commit, no push, no PR. Read-only; measured with a throwaway python command against the saved `GET /tasks/{id}` record of S-8 t2 (`01a8f701-…`, `/tmp/di076_s8_t2_result.txt`, byte-identical to `result.response`) and the integrator's own record (`payload.prompt`, `result.response`).

### How the integrator result becomes `done`

- `integrate_prompt()` (`shared/scrum_flow.py:405-413`) builds the prompt. Each DeepSeek answer is cut with `text[:12000]` (`:406`) with no marker, no warning and no check on the length. The prompt tells the integrator to "Apply ALL of it" (`:408`).
- The integrator is queued and awaited by `run()` (`shared/scrum_flow.py:539-549`). It raises `PlanError` only when `done["status"] != "done"` (`:547`). `done` here is the status the worker set for the Claude task, i.e. "the claude CLI exited without error and returned text". Nothing checks that a commit, branch, push or PR exists afterwards.
- The caller (`:577-583`) then does `pr_material.append(answer(d))` and `authors.add(...)`. `answer()` (`:290-292`) is just `result.response`. So an integrator reply that says "I applied nothing" is stored as PR material and the wave continues.
- `agents/deepseek_agent.py` does not help on the input side. The text-only check (`:253`, `:265`, `_split` `:66-83`) only fires for printed tool-call markup. A well-formed reply of any length passes. There is no length cap and no `finish_reason` check, so a reply that is merely long or cut by the model is returned `ok: true`.

### Measured facts: DeepSeek S-8 t2 answer

| Fact | Value |
|---|---|
| Length | 19135 characters (19159 bytes) |
| `FILE:` blocks | 7 (all at line start; 8 fences in total: 7 labelled `FILE:`, the eighth an unlabelled `json` example inside the trailing API-contract prose at offset 18400) |
| Tool-call imitation detected | **No.** `_split(text)` = `(imitation=False, real_code=True)`; `looks_like_tool_call` = False; `_is_text_only_failure` = False. No retry was made. |
| Over the 12000-character cut-off | **Yes.** 7135 characters (37%) were dropped. |

Actual format: the code is in `FILE: path` lines each followed by a plain fenced block (` ```ts ` / ` ```js `), exactly what `SYSTEM_PROMPT` (`agents/deepseek_agent.py:32-38`) asks for. It starts with a prose preamble (1498 chars) saying the class-code direct join does not exist and only the invite path is delivered. The last file block ends at offset 16907; after it comes API-contract prose (no more code to apply except that example). Block start offsets: `scripts/migrate-at-258-course-join-route-trust.ts` 1498, `join-errors.js` 3887, `student-invites.js` 5303, `rate-limit.js` 10173, `course-links.js` 11074, `join-routes.js` 13350, `scripts/migrate-at-258-join-attempts.ts` 15553.

Effect of the cut-off at char 12000: it falls inside `course-links.js` (block 11074-13345), inside a template literal in `removeCodeLink`. The integrator therefore got 5 of the 7 files, the fifth cut mid-statement; `join-routes.js` and `migrate-at-258-join-attempts.ts` never reached it. The integrator prompt (14872 chars) ends `…async removeCodeLink({ courseId, teacherId, accountId }) {\n await this.assertTeaches(courseId, teacherId);\n\n    con`. (The edge of the code section differs by a few characters from 12000 because of the `--- task … ---` header line.)

### What integrate:t2 reported and did

Task record: agent claude / sonnet, mac-mini, status `done`, notes `integrate S-8`, started 04:07:40, finished 04:09:32 (112 s), `tokens_out` 9036, `payload.cwd` absent, `payload.timeout` 1800. Its `result.response` opens:

> "I haven't applied any of the returned code, so there's no commit, push or PR yet. It can't be applied as written, and the class-code part needs a decision from you first."

It closes:

> "I created a local worktree at `~/wt/academictool-S-8` on branch `dis/academictool-4-S-8`, cut from `origin/main`. It has no commits and nothing is pushed. I left your existing clone alone because it has 31 uncommitted changes on the S-1 branch. Tell me how to handle the class code, or whether to ship the invite part first."

Its reasons, quoted in short: the code is **"Truncated: `course-links.js` ends mid-statement at `con`, and the reply cuts off there"** (this is the 12000 cut-off, but the integrator blamed the author, not the pipeline); the migration duplicates columns that exist; `student-invites.js` would overwrite a 268-line real file; the code assumes APIs (`db.one/db.tx`, `redeemInviteTx`, `course_teachers`) that the repo does not have; and the class-code path was left out. Several of those objections are about DeepSeek guessing the repo layout ("I can't inspect the existing repo", same behaviour as in S-7), so the truncation was not the only reason for refusing.

### Could it report `done` with nothing applied?

Yes, and it did. The task is `done` because the CLI finished normally, while the content is a request for a decision. `run()` (`shared/scrum_flow.py:547`) treats that as success, and the only trace is the prose in `result.response`, which flows into `pr_material` (`:583`). Nothing in the flow distinguishes "applied and pushed" from "decided not to apply". There is no `needs_human` here: that status is set only when the claude result carries `needs_human` (`worker/poller.py:170-173`), which this reply did not.

Working directory (DI-020 `~`): not the cause here. The task has no `cwd`, so it ran in the worker default. It also did not work in `~`: it located the existing academictool clone and made a worktree at `~/wt/academictool-S-8`. Nothing was written to a wrong directory. The worktree has no commits, so the "nothing applied" state is real, not hidden in another directory.

### Implications for fixes (for S-2)

1. Fail loudly instead of silently truncating: in `integrate_prompt()` reject, or pass a marker for, an answer over the limit, and do not cut inside a `FILE:` block. Better: split by `FILE:` block and send whole blocks only.
2. Detect an incomplete reply at the source: record `finish_reason` in `deepseek_agent.run` and treat `length` as failure. (This reply was 5109 completion tokens, so it was probably not cut by the model.)
3. After the integrator task, verify the outcome (branch has a new commit, or a PR URL is in the reply) before the wave continues; map "applied nothing" to `needs_human`.
