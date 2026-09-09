# AGENTS.md

All of Alex's native Notion automations, codified as code and deployed on
Modal: a daily cron dispatcher (recurring tasks, compliance reconciler) + a
Notion webhook receiver (event-triggered rules).

## Architecture rule (the one that matters)

**Business logic lives in** **`src/core/`** **as plain Python.** Only `app.py`
imports `modal` - it is the deployment shim (image, secrets, endpoints,
schedules). Within `src/core/`, only `notion.py` (Notion API) and `hub.py`
(life-data hub) do network I/O - every other module (`registry.py`,
`planner.py`, `rules.py`, `reconciler.py`) is pure functions: dataclasses and
dicts in, decisions out. This keeps the logic trivially testable and
portable - no backend abstraction, no `TaskBackend` interface; a future
migration off Notion rewrites `notion.py` and the event rules, the planner
and the specs-as-intent carry over as-is.

* **The recurring specs are DATA, not code - they live in life-data**, in the
  `recurring_specs` and `cc_keepalive_cards` tables (see the `data` skill),
  pulled from the hub at each `daily()` run and validated by
  `registry.load_recurring`/`load_cards`. Changing a chore's cadence, title,
  or cards = `life sql UPDATE ...` - no commit, no deploy. Disable a spec by
  soft-deleting its row (`SET deleted_at = updated_at`); re-enable by
  clearing it. `registry.py` ships only the Notion data-source ids, the
  dataclasses, and the row loaders. Personal spec content (chores, finances,
  people) must NEVER be committed to this repo - that's why it moved out.
* The `DRY_RUN` env var (`Settings.dry_run`) gates all Notion writes -
  when true, automations run their full logic and log what they would have
  written instead of calling the API.
* Cron: Modal is the PREFERRED home for schedules - but the Starter plan
  allows **5 deployed crons across ALL apps**, so track the budget. This app
  uses one slot. Overflow goes to GHA cron or CF Cron Triggers (see the
  `infra` skill).
* Webhook endpoint is public (Notion can't send Modal proxy-auth headers) -
  authenticated instead by verifying the `X-Notion-Signature` HMAC.

## Stack

uv · pydantic-settings (env config) · httpx (Notion API) · fastapi (webhook
`Request`) · pytest · ruff.
Config comes from env vars only: Modal Secret in the cloud, `op run` locally.
`.env.tpl` is the canonical secrets manifest (op\:// refs, committed).
Instantiate `Settings()` inside functions, never at import time.

## Commands

Standard verb set (see global AGENTS.md) - the justfile is the interface,
not a script catalog; one-offs go in `scripts/` and run directly.

| Command                                 | Purpose                                                            |
| --------------------------------------- | ------------------------------------------------------------------ |
| `just dev`                              | Live-reload dev against real Modal infra (`modal serve`)           |
| `just test` / `just check` / `just fmt` | pytest / ruff read-only / ruff fix                                 |
| `just logs`                             | Stream deployed-app logs                                           |
| `just sync-secrets`                     | Push `.env.tpl` → Modal secret store                               |
| `just deploy`                           | test + sync-secrets + `modal deploy` - CI's job, not yours (below) |

**Deploying = commit + push to** **`main`.** The GHA deploy workflow runs tests,
syncs secrets, and deploys - never run `just deploy` locally unless there's a
legitimate stated reason. After pushing, verify the run with the gh CLI
(`gh run watch <id> --exit-status`; on failure `gh run view <id> --log-failed`);
never assume the deploy succeeded.

## TDD

Write the test in `tests/` first, then the `src/core/` code. `app.py` shim
functions stay thin enough to not need tests.

## Hardcoded owner assumptions

The code is generic, but the workflow is wired to Alex's setup for
convenience: secrets flow through his 1Password (`.env.tpl` with `op://`
references; `op-project-bootstrap` is his private bootstrap script) and
deploys target his Modal workspace.

## Credential provisioning

`op-project-bootstrap` calls `scripts/provision.py --batch modal-token` to
mint one dedicated CI token pair. Open its stderr URL in the configured
remote browser session and approve the displayed code. Both verified fields
are saved together in the project vault through JSON stdin; no plaintext
credential cache is written. Individual Modal field minting is refused.
