# Task: a `~` in a task's `cwd` is not expanded

Role: dev_backend (agent: claude). Repository: this checkout (the distributed-infra worker/orchestrator code).

## Bug

Task payloads sometimes give the working directory with a tilde, for example `"cwd": "~/Projects/some-repo"`.
The worker passes that string straight to the subprocess it starts, and nothing turns `~` into the home directory.
A `run_script` task with that `cwd` fails with a "No such file or directory" error instead of running the script
in the directory the operator meant. Absolute paths and tasks without a `cwd` work fine.

The same shared helper is used by the other worker handlers that run shell commands (git pull, builds, npm),
so a tilde `cwd` fails there too.

## What to do

1. Find where the worker starts subprocesses with a task-supplied `cwd` and make a tilde `cwd` resolve to the
   user's home directory before the subprocess starts.
2. Keep the behaviour for absolute paths and for a missing/empty `cwd` unchanged.
3. Add tests that fail without your change and pass with it, and make sure the whole test suite passes.

Keep the change small and in the style of the surrounding code. Do not modify unrelated files.
