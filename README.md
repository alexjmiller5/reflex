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
