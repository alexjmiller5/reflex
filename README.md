# reflex

All of Alex's native Notion automations, codified as code and deployed on
[Modal](https://modal.com): a daily cron dispatcher (recurring tasks,
compliance reconciler) plus a Notion webhook receiver (event-triggered
rules). No Dockerfile, no Terraform - all infrastructure lives in `app.py`
as code.

## Layout

```
app.py                 Modal shim - image, secrets, endpoints, schedule
src/core/               business logic (plain Python, portable)
src/core/registry.py   Notion ids + spec model/loaders (spec DATA lives in life-data)
src/core/notion.py     Notion API client
src/core/hub.py        life-data hub client - pulls the spec tables at run time
scripts/run_local.py   dry-run the dispatch logic with no Modal at all
tests/                  pytest
.env.tpl                secrets manifest (1Password op:// refs, committed)
justfile                dev / test / run / sync-secrets / deploy
```

See `AGENTS.md` for the architecture rule and stack.

## Event rules

Task defaults (due date, tag and priority) apply only at creation. Completion,
review and remedy timestamps react only to changes to their corresponding
status or checkbox, using the event's timestamp. Editing a title, note or
date does not infer when a historical task was completed. Existing nonempty
dates are preserved, and reopening a task clears its completion date.

The daily sweep reports contradictory dates (for example, an open task with
a completion date). Unknown historical dates stay blank without generating
backfill tasks.

## Bootstrap (one-time, manual)

1. `op-project-bootstrap .env.tpl --repo alexjmiller5/reflex` -
   creates the `Reflex` vault, the `Reflex ENV` item
   (one field per `.env.tpl` line - `NOTION_API_TOKEN` prompted,
   `NOTION_WEBHOOK_SECRET` left `CHANGEME` until the webhook step below fills
   it in), AND the `Reflex CI Modal Token` deploy-token item (bootstrap
   scans `.github/workflows/*.yml` for `op://` refs and mints a dedicated
   token pair via `scripts/provision.py` and Modal's browser approval flow;
   no plaintext credentials touch disk), plus the read-only
   `reflex-ci` service account and the repo's
   `OP_SERVICE_ACCOUNT_TOKEN` GitHub secret.
   (Local `just dev` / `just run` need no `~/.modal.toml` either - the
   machine-wide `modal` wrapper injects the same 1P-held token.)
2. Create the webhook subscription (Notion has no API for this - integration
   settings only):
   - Deploy first (push to `main`) so `notion_webhook`'s URL exists.
   - **Re-subscribing later** (new endpoint URL, new integration): clear
     `NOTION_WEBHOOK_SECRET` first and redeploy. Notion posts the handshake
     token unsigned, so the endpoint only accepts a handshake while no
     secret is set - otherwise anyone could spray fake tokens into the logs
     and have one adopted as the signing secret. A 401 on the handshake
     means the old secret is still configured.
   - `notion.so/profile/integrations` -> the integration -> **Webhooks**
     tab -> paste the deployed endpoint URL -> subscribe to
     `page.created` and `page.properties_updated`.
   - Notion POSTs a one-time verification payload to the endpoint; it's
     logged (`just logs`) and also shown directly in the integration UI -
     copy the token either place.
   - Update the project's ENV item by its stable vault/item IDs, preserving
     all existing tags: `op-personal item edit <env-item-id> --vault <vault-id>
     --tags "<existing-tags>" "NOTION_WEBHOOK_SECRET=<token>"`.
   - `just sync-secrets` to push it to the deployed Modal secret, then
     make a small test edit on any watched DB and confirm `just logs`
     shows the event handled (not a 401).

## Local runs

- `just run-local` - dry run: same dispatch logic with zero Modal involvement,
  `DRY_RUN=true` forced so nothing writes to Notion (`scripts/run_local.py`).
- `just run` - **not a dry run** - triggers `daily()` once against real Modal
  infra (ephemeral container, not the deployed schedule) and performs real
  writes to Notion.

## Life Data workflow events

The scheduled `daily` function is a serialized one-minute tick. Recurring dispatch
and the remaining Notion compliance sweep still become due at 11:30 UTC and run
once per due day. Failed ticks do not advance the durable success marker. The
existing schedule slot is reused.

`LIFE_EVENT_POLICY` is optional JSON runtime configuration containing a
`subscription_id` and `tables` object. Each table policy names its columns,
transition values, creation defaults, timezone and optional day boundary. No
policy means no Life Data event writes. `NOTION_RETIRED_SOURCES` is an explicit
comma-separated list of migrated Notion data source IDs; it disables their old
webhook rules and compliance sweeps without disabling unrelated sources.

The subscription must advertise `durable-pull-v1` and `scalar-lifecycle-v1`, watch
exactly the configured policy columns, and enable `lifecycle: true` for every
source. The caller needs only the selected table reads/writes and the exact
subscription consume grant. Conditional effects require `revision-v1`.

Reflex owns the `reflex-events` Modal Volume. Its journal stores the watched
projection, pending effects, delivery receipts and daily success marker. Each
assignment commits to the volume before delivery acknowledgment. Only the
serialized `daily` function writes this journal. Do not mount it into another
writer or run overlapping deployment versions. Pause event execution and drain
the old invocation before replacing an active consumer deployment; resume after
the new version has loaded the same checkpoint. Local file locking protects
same-container invocations, not distributed writers.

Before activation, reconcile a frozen baseline against the subscription cursor.
Do not use an ordinary paginated scan as a frozen snapshot. Keep the resulting
JSON outside the repository:

```json
{"through_seq":"0","tables":{"records":[{"id":"record-1","state":"Open","finished":null,"deleted_at":null}]}}
```

Install it through the supported serialized operator entrypoint:

```sh
modal run app.py --seed-file /path/to/private-baseline.json
```

The entrypoint calls the deployed worker and refuses an existing seed or a
checkpoint different from the hub's acknowledged cursor. A policy change
requires deliberate reconciliation and a new subscription; it cannot silently
reinterpret the old journal. Seeding does not infer completion dates or apply
creation defaults to historical rows. `DRY_RUN` never persists consumer state,
acknowledges deliveries, patches rows or advances daily success; a preview that
cannot read beyond the outstanding batch is explicitly incomplete.

Retries fold actual event order and retain manual date edits. Deletes discard
queued effects; restores are not new creations. Before each conditional patch,
the consumer reads the live row, drains through a subsequently captured
subscription high-water mark, then uses both row revision fields. Lost replies
are resolved by replaying subscription events rather than resending an
unconditional write.

### Recurring task writer

`LIFE_TASKS_CONFIG` independently selects the Life Data task adapter. It contains
`table`, `time_zone`, semantic-to-catalog `columns` (title, status, due, completed,
tags, priority, notes, links, blocked_by), creation `defaults`, and optional
`adoptions`. Each recurring template has a stable `key` separate from its title.
An adoption names `spec_key`, `occurrence` (date label), `template_key` and
`target_id`. Accepted mappings are retained in the journal even when no new task
is due. Removing configuration cannot erase or rebind them; a missing adopted
target fails closed, and a tombstoned target remains handled.

The writer saves the entire occurrence before its first insert, including IDs,
field values and dependency links. Recovery completes that saved intent before
planning later occurrences. Existing identities are never overwritten. Gift
bindings add their own table/columns, defaults, description/date templates and
optional task links to the same retained occurrence. Failed gift creation is
recoverable even after all related tasks were created.

Card keepalive checks still read the existing Notion Transactions source. Their
task output uses the selected Life Data binding, stable runtime `card_keys` and
`keepalive_defaults`; migrating Tasks does not silently change the financial
activity reader. An absent task binding preserves the existing Notion writer.

## Season-completion reminders

`LIFE_SEASON_REMINDERS_CONFIG` optionally adds prospective reminders to the
existing daily dispatch. It requires `LIFE_TASKS_CONFIG`; neither setting is
enabled by deployment alone. Supply these runtime bindings:

- `shows`: `table`, `title_column`.
- `episodes`: `table`, `show_column`, `season_column`, `number_column`,
  `status_column`, `finished_value`.
- `seasons`: `table`, `show_column`, `season_column`, `total_column`.
  Totals must describe the entire season, including unreleased episodes.
- `title_prefixes`: selected edition title prefixes.
- `title_template`: template with `{title}` and `{season}` placeholders.
- `task_values`: initial values keyed by actual task column names. These cannot
  override identity, title or due date; task bindings supply the latter columns.

Positive seasons require exactly episodes 1 through the declared total, all
watched and live. Specials are excluded. Missing totals do not mean finished.
The initial scan retains already-completed seasons without creating reminders;
review that baseline before activation. Later completions create one stable
occurrence per show ID and season. Frozen intent survives ambiguous responses,
and existing tasks, including completed or deleted ones, remain unchanged.
Previously observed episode IDs remain required even if a later scan omits them.
All inventories must finish successfully before a journal update or task insert.
