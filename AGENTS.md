# AGENTS.md

Alex's automations, codified as code and deployed on Modal: a daily cron
dispatcher (recurring tasks into Notion Tasks, Christmas gift rows into
soma `gifts`, the credit-card keepalive, the compliance reconciler) + a
Notion webhook receiver (event rules over the DBs still in Notion: Tasks,
Projects, Synapse Executions). Migrated DBs (Books, Movies, Podcasts,
Calendar, Trips, Gifts) are soma tables now - their catalog enforces
what the rules here used to, so no rule targets them.

## Architecture rule (the one that matters)

**Business logic lives in** **`src/core/`** **as plain Python.** Only `app.py`
imports `modal` - it is the deployment shim (image, secrets, endpoints,
schedules). Within `src/core/`, only `notion.py` (Notion API) and `hub.py`
(soma hub, including the vendored `table_pull.py` it drives) do network
I/O - every other module (`registry.py`,
`planner.py`, `rules.py`) is pure functions: dataclasses and dicts in, decisions
out. Orchestrators consume injected clients; `event_state.py` alone owns the
local durable journal. This keeps the logic trivially testable and
portable - no backend abstraction, no `TaskBackend` interface; a future
migration off Notion rewrites `notion.py` and the event rules, the planner
and the specs-as-intent carry over as-is.

* **The recurring specs are DATA, not code - they live in soma**, in the
  `recurring_specs` and `cc_keepalive_cards` tables (see the `data` skill),
  pulled from the hub at each `daily()` run and validated by
  `registry.load_recurring`/`load_cards`. Fixed monthly series calculate each
  occurrence from the original anchor; an anchor on day 31 means month-end
  and recovers after shorter months. Relative series follow completion dates.
  Changing a chore's cadence, title,
  or cards = `soma sql UPDATE ...` - no commit, no deploy. Disable a spec by
  soft-deleting its row (`SET deleted_at = updated_at`); re-enable by
  clearing it. Every template in a spec's `templates` JSON carries a `key`,
  unique within the spec and never renamed: it is the template's identity in
  retained plans and adoptions, so the loader rejects a spec whose templates
  lack one. Gift specs can override generated tasks with recipient-keyed templates
  (`<people-id>:brainstorm` / `<people-id>:buy`); their lanes and instructions
  live in the spec, with `{name}` and `{person_id}` resolved at dispatch.
  Card reminder lanes come from runtime `keepalive_defaults.lane`.
  `registry.py` ships only the Notion data-source ids, the
  dataclasses, and the row loaders. Personal spec content (chores, finances,
  people) must NEVER be committed to this repo - that's why it moved out.
* The `DRY_RUN` env var (`Settings.dry_run`) gates all Notion and hub writes -
  when true, automations run their full logic and log what they would have
  written instead of calling the API.
* The ten-minute `daily` tick serializes event delivery and owns the
  `reflex-events` Modal Volume. Volume commits precede hub acknowledgments;
  one function/container owns the journal. Stop and drain an active consumer
  before replacing its deployment. Daily dispatch remains due at 11:30 UTC
  with a durable success marker. Runtime policy and reconciled seed are
  operator state, never repository content. Notion sources are retired only
  by explicit runtime IDs; no event policy leaves that backend selected.
  Event and live-row comparisons accept JSON text matching a decoded array
  or object in the seed; genuine value mismatches still fail before ACK.
* Events are notify-then-pull, never a per-minute poll: each tick drains once,
  runs daily work when due, then `soma_events.listen` holds the hub's wake
  socket (`/v1/changes`, one `{seq, tables}` message per burst of commits)
  until a minute before the next tick. A message naming a watched table
  drains; a drain with nothing for Reflex is one subscription status read
  (hub capabilities are checked once per tick). Every (re)connect drains, and
  retained work or a down socket drains every 60 s instead. A backlog is not
  a failure: it defers daily work (which reads what drains stamp), and
  `tick.note_backlog` fails the run only after 30 minutes of pending work.
* Daily reads go through `table_mirror.TableMirror`, Reflex's copy of each
  table it reads in `/state/tables.json` on the same volume, kept current by
  soma's vendored table-pull helper (`table_pull.py`, copied verbatim from
  soma `src/soma/table_pull.py`; contract: soma `docs/consumer-access.md`):
  one `/v1/cursor` request when quiet, batched pulls of arrivals otherwise.
  Tombstones keep their columns, like a hub scan. Only tables a run reads are
  kept; the copy is pulled in full weekly (purges) and dropped when the hub
  refuses a held table. Writes and `read_row` go to the hub directly.
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
* `rules.evaluate_transition` is the pure Soma policy evaluator. Column
  names, status values, defaults, excluded tags, timezone and optional day
  boundary come from runtime policy. It uses event time, preserves supplied
  values and explicit date edits, and never applies creation defaults to a
  historical seed or an unrelated edit. The Notion evaluator remains separate.
* `soma_dispatch.py` owns the optional recurring task adapter, selected by
  `SOMA_TASKS_CONFIG`. Stable template keys and retained occurrence/adoption
  mappings preserve IDs across retries and title edits. Whole task/gift intent
  is journaled before inserts; partial recovery retains dependency edges.
  Missing adopted targets fail closed. Timed dues (UTC instants and floating
  `YYYY-MM-DDTHH:MM` local times) count on their logical day (local time minus
  the config's optional `day_start_minutes`); Reflex itself writes dates only.
* Card keepalive activity is soma finance: each `cc_keepalive_cards` row
  names its `accounts.id`, whose `source` selects the `txns_<source>` table
  (`dispatcher.recent_card_activity`, shared by both task writers). Synthetic
  and soft-deleted rows are not activity; an unknown account fails the run.
* `season_reminders.py` owns optional prospective season-completion reminders
  selected by `SOMA_SEASON_REMINDERS_CONFIG`; it stays staged (log only) until
  `SOMA_TASKS_CONFIG` selects the Tasks binding. Runtime bindings supply shows
  and episodes. A season completes when episodes 1..n are all aired and watched;
  an announced or unscheduled episode holds it.
  The first complete scan baselines existing completed seasons without backfill.
  Retained episode identities prevent disappearance from fabricating completion.
  Frozen plans precede insert-only delivery and preserve existing tombstones.
* Hub row scans exhaust bounded pages and retain tombstones for the caller
  to interpret. They are not frozen snapshots. Insert-only writes require
  stable caller-owned IDs and a complete inserted/existing receipt; a
  partial rejection fails the operation, naming each rejected id/col/rule.
  The hub requires `updated_at` on inserted rows, so `insert_rows` stamps it
  at send time; frozen plans never carry one. Conditional patches carry both
  `updated_at` and `hub_at`. A conflict requires rereading and recomputing,
  never falling back to an unconditional push. Dry-run inserts and patches
  return no committed receipt. The optional Soma consumer is
  selected by runtime policy; transport availability does not switch authority.
  Hub HTTP connections use HTTPX's two connection retries. Response timeouts
  and HTTP errors still propagate, so uncertain writes use journal recovery.

## Stack

uv · pydantic-settings (env config) · httpx (Notion API, hub) · websockets
(hub wake socket) · fastapi (webhook `Request`) · pytest · ruff.
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
