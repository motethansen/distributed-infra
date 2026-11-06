# Security and secrets

This repository is **public**. Anything committed here, including old commits, is readable by anyone and
can be copied within minutes. The rules below keep secrets and private details out of it, and the checks
catch mistakes before they reach GitHub.

## What may never be committed

- API keys, tokens, passwords, private keys, certificates, `.env` files, session or cookie values
- Your machines' real Tailscale addresses, tailnet name, phone numbers, e-mail addresses, home paths
- Databases and state (`data/`, `*.db`), logs, agent transcripts, scrum plans and run logs
- Live launchd/systemd units that contain real values (the `*.example` templates are fine: they hold placeholders)

Use placeholders in docs (`<mac-mini-ip>`) and the documentation range `100.64.0.x` in tests.

## Where secrets live instead

Every script and service reads configuration from the **environment**. Provide it in whichever way fits:

| Where | How | Good for |
|---|---|---|
| A `.env` file in the checkout | `cp .env.example .env && chmod 600 .env` | a machine you own; the default |
| The process environment | `export SECRET_KEY=…`, or `Environment=` in a unit | containers, one-off runs, anything a secret manager injects |
| `EnvironmentFile=` (systemd) | points at the same `.env` | Linux services |
| `scripts/run-with-env.sh` | loads `.env` into a launchd job or any command | macOS services |
| GitHub Actions secrets | `${{ secrets.NAME }}` in a workflow | CI and automated deploys |
| A password manager | Bitwarden, 1Password, … holds the originals | the source of truth; copy values into `.env` when setting up a machine |

`run-with-env.sh` parses the file instead of sourcing it (a value can never run as code), never overrides a variable that is
already set, refuses a `.env` that other users can read, and never prints values.

**Adding a new secret:** add its *name* (empty or a placeholder) to `.env.example`, read it with `os.environ` in code, set the real
value in each machine's `.env`, and keep the original in your password manager. Never give a secret a default value in code.

## What is protected by design

- `.gitignore` excludes `.env`, `data/`, logs, keys and certificates, `config/machines.yaml`, `config/family.json`, local plists and
  `.claude/settings.local.json` (allowlists saved there can contain a pasted secret verbatim).
- The fleet authenticates with one shared `SECRET_KEY` sent as the `x-secret-key` header. Use `openssl rand -hex 32`, the same value
  on every machine, and rotate it by changing every `.env` and restarting the services.
- All traffic runs over Tailscale. Bind services to the tailnet address where you can, and never publish the orchestrator,
  worker or rqlite ports (4001/4002 have no authentication) on a public interface.
- Agents run unattended on the workers with their permission prompts off, so a worker is as trusted as the person logged in
  to it. Keep the tailnet closed to people you do not trust.
- The privacy guard in `config/routing.yaml` keeps mail, finance, calendar and personal data away from DeepSeek.

## The checks

| Check | Where | What it does |
|---|---|---|
| `scripts/check-secrets.sh` | your machine | scans the files git would commit, with [gitleaks](https://github.com/gitleaks/gitleaks) and `.gitleaks.toml` |
| `scripts/install-git-hooks.sh` | once per clone | runs that scan before every commit |
| `secret-scan` job | `.github/workflows/ci.yml` | scans the commits of every push and pull request; the build fails on a finding |
| `.gitleaks.toml` | repo root | built-in rules plus: Tailscale auth keys, real tailnet addresses, `SECRET_KEY=<value>`-style assignments |
| `.gitleaks-baseline.json` | repo root | findings already in old commits (redacted), so only *new* ones fail |

Also enable, in the repository's **Settings → Code security**: *Secret scanning* and *Push protection*. They block a push that
contains a known provider token before it lands. The alternative local hook [`git-secrets`](https://github.com/awslabs/git-secrets) works too;
use one hook, not both.

## If a secret was committed

1. **Rotate it first.** Treat it as stolen the moment it was pushed: a deleted commit is still served by GitHub through pull-request
   refs and caches, and may already have been scraped.
2. Remove it from the current tree and commit.
3. Only then consider rewriting history (`git filter-repo --replace-text`), force-pushing, and asking GitHub support to purge cached views.
   A rewrite does not undo step 1.
4. Add a rule or an allowlist entry to `.gitleaks.toml` if the pattern was one the scan missed.

## Reporting a problem

Open a private security advisory on the repository (Security tab) rather than a public issue.
