"""Modal deployment shim - ALL infrastructure lives here, as code.

Business logic stays in src/core/ (plain Python, no Modal imports) so the
same package runs on the mac mini, in tests, or anywhere else. This file
only maps that logic onto Modal: image, secrets, endpoints, schedules.
"""

import json

import modal
from fastapi import HTTPException, Request

APP_NAME = "reflex"  # also the Modal secret name (see justfile sync-secrets)

app = modal.App(APP_NAME)

image = (
    modal.Image.debian_slim(python_version="3.13")
    .uv_sync(extra_options="--no-dev")  # reads pyproject.toml + uv.lock; skip dev group
    # add_local_dir, NOT add_local_python_source: the latter can't resolve
    # packages under src/ layout, and this also carries non-.py data files.
    .add_local_dir("src/core", remote_path="/root/core", ignore=["**/__pycache__"])
)

secrets = [modal.Secret.from_name(APP_NAME)]

# High-water mark for the reconciler's "since" window, persisted across runs.
state = modal.Dict.from_name(f"{APP_NAME}-state", create_if_missing=True)
event_volume = modal.Volume.from_name(f"{APP_NAME}-events", create_if_missing=True)


# Retries absorb transient Notion/hub 5xx blips (one 500 used to cost the whole
# day). Safe because a rerun is idempotent: dispatch skips tasks that already
# exist and the high-water mark only advances on a clean finish.
@app.function(
    image=image,
    secrets=secrets,
    schedule=modal.Cron("* * * * *"),
    volumes={"/state": event_volume},
    max_containers=1,
    timeout=600,
    retries=modal.Retries(max_retries=2, initial_delay=60.0),
)
def daily(seed: dict | None = None):
    """Single serialized tick; daily work retains its 11:30 UTC due time.

    The optional seed is an operator-supplied reconciled subscription baseline,
    installed through this same serialized function before consumer activation.
    """
    from datetime import datetime, timedelta, timezone

    from core.config import Settings
    from core.event_state import locked_state
    from core.hub import HubClient
    from core.soma_events import SomaEventConsumer, seed_projection
    from core.tick import run_tick

    s = Settings()
    now = datetime.now(timezone.utc)
    event_volume.reload()  # No open volume handles until after this call.
    with locked_state("/state/events.json", commit=event_volume.commit) as journal:
        hub = HubClient(s.soma_hub_url, s.soma_hub_token, dry_run=s.dry_run)
        if seed is not None:
            if s.dry_run or s.soma_event_policy is None:
                raise ValueError("seeding requires a configured, non-dry-run subscription")
            status = hub.subscription_status(s.soma_event_policy["subscription_id"])
            if int(status["acked_seq"]) != int(seed["through_seq"]):
                raise ValueError("seed checkpoint must equal the acknowledged subscription cursor")
            seed_projection(
                journal, s.soma_event_policy, seed["tables"], through_seq=seed["through_seq"]
            )
            return {"seeded": True, "through_seq": seed["through_seq"]}

        def consume():
            if s.soma_event_policy is not None:
                result = SomaEventConsumer(hub, journal, s.soma_event_policy).drain(
                    now + timedelta(seconds=40)
                )
                print(result)
                if result["incomplete"] and not s.dry_run:
                    raise RuntimeError("event work remains pending; retained for next tick")

        return run_tick(
            journal, now, lambda: _daily(s, journal, now), consume=consume, dry_run=s.dry_run
        )


def _daily(s, journal, now):
    """Recurring dispatch and remaining Notion compliance, once per due day."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from core.dispatcher import dispatch
    from core.handlers import EVENT_DBS
    from core.hub import HubClient
    from core.notion import NotionClient
    from core.reconciler import reconcile
    from core.registry import CARD_COLUMNS, SPEC_COLUMNS, load_cards, load_recurring

    notion = NotionClient(s.notion_api_token, dry_run=s.dry_run)
    today = now.astimezone(ZoneInfo("America/New_York")).date()

    hub = HubClient(s.soma_hub_url, s.soma_hub_token, dry_run=s.dry_run)
    recurring = load_recurring(hub.pull_rows("recurring_specs", SPEC_COLUMNS))
    cards = load_cards(hub.pull_rows("cc_keepalive_cards", CARD_COLUMNS))
    for line in dispatch(
        notion, today, recurring, cards, hub, task_config=s.soma_tasks_config, state=journal
    ):
        print(line)

    if s.soma_season_reminders_config is not None:
        from core.season_reminders import dispatch_seasons

        # Stays staged (logs only) until SOMA_TASKS_CONFIG selects the Tasks binding.
        for line in dispatch_seasons(
            hub, journal, s.soma_season_reminders_config, s.soma_tasks_config, today
        ):
            print(line)

    since = (
        journal.get("high_water")
        or state.get("high_water")
        or (now - timedelta(days=1)).isoformat()
    )
    sources = EVENT_DBS - set(s.retired_notion_sources)
    logs, mark = reconcile(notion, sources, since, now, place_tags=s.place_tags)
    for line in logs:
        print(line)
    if mark is None:
        # Some DB was unreachable: keep the window open so the next run
        # re-sweeps it, and fail loudly (Modal emails on a failed schedule)
        # rather than silently under-reporting compliance.
        raise RuntimeError("reconciler could not sweep every database - see SWEEP FAILED above")
    if not s.dry_run:
        journal["high_water"] = mark


@app.local_entrypoint()
def seed_events(seed_file: str):
    """Install a private reconciled baseline via the serialized deployed worker."""
    from pathlib import Path

    seed = json.loads(Path(seed_file).read_text())
    # Resolve the deployed function, not a second ephemeral writer deployment.
    worker = modal.Function.from_name(APP_NAME, "daily")
    print(worker.remote(seed=seed))


# max_containers bounds the cost of a flood: signature checking happens in
# our container (Notion cannot send Modal proxy-auth headers), so unlike
# synapse's proxy-authed endpoints, junk requests are not rejected at the edge.
@app.function(image=image, secrets=secrets, min_containers=0, max_containers=8)
@modal.fastapi_endpoint(method="POST")
async def notion_webhook(request: Request):
    from datetime import datetime, timezone

    from core.config import Settings
    from core.handlers import handle_event, handshake_token, verify_signature
    from core.notion import NotionClient

    body = await request.body()
    payload = json.loads(body)
    s = Settings()
    token = handshake_token(payload, s.notion_webhook_secret)
    if token:  # one-time subscription handshake: surface it in logs
        print(f"NOTION VERIFICATION TOKEN: {token}")
        return {"ok": True}
    if not verify_signature(
        body, request.headers.get("X-Notion-Signature", ""), s.notion_webhook_secret
    ):
        raise HTTPException(status_code=401)
    notion = NotionClient(s.notion_api_token, dry_run=s.dry_run)
    now = datetime.now(timezone.utc)
    # Notion sends one event per delivery (not a batch under "events" -
    # pinned against the docs 2026-08-20; "batched" in their docs means
    # multiple rapid edits get coalesced into fewer events upstream, not
    # multiple events per HTTP request).
    logs = handle_event(
        payload,
        notion,
        now,
        notion.me(),
        place_tags=s.place_tags,
        retired_sources=s.retired_notion_sources,
    )
    print(logs)
    return {"ok": True, "handled": len(logs)}
