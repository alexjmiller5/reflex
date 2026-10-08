# AGENTS.md

Alex's automations, codified as code and deployed on Modal: a daily cron
dispatcher (recurring tasks into Notion Tasks, Christmas gift rows into
life-data `gifts`, the credit-card keepalive, the compliance reconciler) + a
Notion webhook receiver (event rules over the DBs still in Notion: Tasks,
Projects, Synapse Executions). Migrated DBs (Books, Movies, Podcasts,
Calendar, Trips, Gifts) are life-data tables now - their catalog enforces
what the rules here used to, so no rule targets them.

## Architecture rule (the one that matters)

**Business logic lives in** **`src/core/`** **as plain Python.** Only `app.py`
imports `modal` - it is the deployment shim (image, secrets, endpoints,
schedules). Within `src/core/`, only `notion.py` (Notion API) and `hub.py`
(life-data hub) do network I/O - every other module (`registry.py`,
`planner.py`, `rules.py`) is pure functions: dataclasses and dicts in, decisions
out. Orchestrators consume injected clients; `event_state.py` alone owns the
local durable journal. This keeps the logic trivially testable and
portable - no backend abstraction, no `TaskBackend` interface; a future
migration off Notion rewrites `notion.py` and the event rules, the planner
and the specs-as-intent carry over as-is.

* **The recurring specs are DATA, not code - they live in life-data**, in the
  `recurring_specs` and `cc_keepalive_cards` tables (see the `data` skill),
  pulled from the hub at each `daily()` run and validated by
  `registry.load_recurring`/`load_cards`. Fixed monthly series calculate each
  occurrence from the original anchor; an anchor on day 31 means month-end
  and recovers after shorter months. Relative series follow completion dates.
  Changing a chore's cadence, title,
  or cards = `life sql UPDATE ...` - no commit, no deploy. Disable a spec by
  soft-deleting its row (`SET deleted_at = updated_at`); re-enable by
  clearing it. `registry.py` ships only the Notion data-source ids, the
  dataclasses, and the row loaders. Personal spec content (chores, finances,
  people) must NEVER be committed to this repo - that's why it moved out.
* The `DRY_RUN` env var (`Settings.dry_run`) gates all Notion and hub writes -
  when true, automations run their full logic and log what they would have
  written instead of calling the API.
* The one-minute `daily` tick serializes event delivery and owns the
  `reflex-events` Modal Volume. Volume commits precede hub acknowledgments;
  one function/container owns the journal. Stop and drain an active consumer
  before replacing its deployment. Daily dispatch remains due at 11:30 UTC
  with a durable success marker. Runtime policy and reconciled seed are
  operator state, never repository content. Notion sources are retired only
  by explicit runtime IDs; no event policy leaves that backend selected.
* Cron: Modal is the PREFERRED home for schedules - but the Starter plan
  allows **5 deployed crons across ALL apps**, so track the budget. This app
  uses one slot. Overflow goes to GHA cron or CF Cron Triggers (see the
  `infra` skill).
* Webhook endpoint is public (Notion can't send Modal proxy-auth headers) -
  authenticated instead by verifying the `X-Notion-Signature` HMAC.
* Webhook rules are scoped to the event: defaults run only on `page.created`;
  timestamps follow changes to their own status/checkbox property, resolved
  from `updated_properties` IDs. Use the event timestamp, preserve existing
  nonempty dates, and ignore unrelated edits and trashed pages. The daily
  reconciler reports contradictory dates, but missing historical dates are
  unknown, not permission to backfill dates or creation defaults.
  Skip self-authored events only when every author is Reflex; aggregated
  events containing another author still need their property rules evaluated.
* `rules.evaluate_transition` is the pure Life Data policy evaluator. Column
  names, status values, defaults, excluded tags, timezone and optional day
  boundary come from runtime policy. It uses event time, preserves supplied
  values and explicit date edits, and never applies creation defaults to a
  historical seed or an unrelated edit. The Notion evaluator remains separate.
* `life_dispatch.py` owns the optional recurring task adapter, selected by
  `LIFE_TASKS_CONFIG`. Stable template keys and retained occurrence/adoption
  mappings preserve IDs across retries and title edits. Whole task/gift intent
  is journaled before inserts; partial recovery retains dependency edges.
  Missing adopted targets fail closed.
* Card keepalive activity is life-data finance: each `cc_keepalive_cards` row
  names its `accounts.id`, whose `source` selects the `txns_<source>` table
  (`dispatcher.recent_card_activity`, shared by both task writers). Synthetic
  and soft-deleted rows are not activity; an unknown account fails the run.
* `season_reminders.py` owns optional prospective season-completion reminders
  selected by `LIFE_SEASON_REMINDERS_CONFIG`; it stays staged (log only) until
  `LIFE_TASKS_CONFIG` selects the Tasks binding. Runtime bindings supply shows
  and episodes. A season completes when episodes 1..n are all aired and watched;
  an announced or unscheduled episode holds it.
  The first complete scan baselines existing completed seasons without backfill.
  Retained episode identities prevent disappearance from fabricating completion.
  Frozen plans precede insert-only delivery and preserve existing tombstones.
* Hub row scans exhaust bounded pages and retain tombstones for the caller
  to interpret. They are not frozen snapshots. Insert-only writes require
  stable caller-owned IDs and a complete inserted/existing receipt; a
  partial rejection fails the operation. Conditional patches carry both
  `updated_at` and `hub_at`. A conflict requires rereading and recomputing,
  never falling back to an unconditional push. Dry-run inserts and patches
  return no committed receipt. The optional Life Data consumer is
  selected by runtime policy; transport availability does not switch authority.

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
