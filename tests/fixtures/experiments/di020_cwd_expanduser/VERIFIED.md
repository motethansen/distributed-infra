# Verification of di020_cwd_expanduser

Run on 2026-11-06 in fresh detached git worktrees under a temp dir (absolute paths), using the repo `.venv` python.
The acceptance test is copied into the repo root as `test_di020_acceptance.py`, as the harness does.

```
git worktree add --detach /tmp/verify.XXXX/pinned 15e15fd2ed39a391456c10f63f0580c72489b660
git worktree add --detach /tmp/verify.XXXX/good   e9e23f874711e11ad2152148dc08ec5715f788a5
cp tests/fixtures/experiments/di020_cwd_expanduser/acceptance_test.py <worktree>/test_di020_acceptance.py
cd <worktree> && <repo>/.venv/bin/python -m pytest test_di020_acceptance.py -q
```

| Commit | Result |
|---|---|
| `pinned_commit` 15e15fd | FAILS: 2 failed, 1 passed (`test_run_helper_expands_tilde_cwd`, `test_run_script_handler_expands_tilde_cwd`: FileNotFoundError) |
| `known_good_commit` e9e23f8 | PASSES: 3 passed |

Worktrees were removed afterwards. No claude calls were made.

Re-verified after the PR #53 review with the hardened harness (`check_fixture_fails_before`, independent `git archive`
repos, repo `.venv` interpreter, allowlisted env, trusted pytest config): pinned commit exits 1 (assertion failure),
known_good_commit exits 0.
