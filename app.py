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


@app.function(image=image, secrets=secrets, schedule=modal.Cron("30 11 * * *"), timeout=600)
def daily():
    """Recurring-task dispatch, then the compliance sweep over everything
    edited since the last run (state carries the high-water mark so a
    missed/failed webhook delivery still gets caught)."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from core.config import Settings
    from core.dispatcher import dispatch
    from core.handlers import EVENT_DBS
    from core.hub import pull_rows
    from core.notion import NotionClient
    from core.reconciler import reconcile
    from core.registry import CARD_COLUMNS, SPEC_COLUMNS, load_cards, load_recurring

    s = Settings()
    notion = NotionClient(s.notion_api_token, dry_run=s.dry_run)
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo("America/New_York")).date()

    recurring = load_recurring(
        pull_rows(s.life_hub_url, s.life_hub_token, "recurring_specs", SPEC_COLUMNS)
    )
    cards = load_cards(
        pull_rows(s.life_hub_url, s.life_hub_token, "cc_keepalive_cards", CARD_COLUMNS)
    )
    for line in dispatch(notion, today, recurring, cards):
        print(line)

    since = state.get("high_water") or (now - timedelta(days=1)).isoformat()
    logs, mark = reconcile(notion, EVENT_DBS, since, now, place_tags=s.place_tags)
    for line in logs:
        print(line)
    if mark is None:
        # Some DB was unreachable: keep the window open so the next run
        # re-sweeps it, and fail loudly (Modal emails on a failed schedule)
        # rather than silently under-reporting compliance.
        raise RuntimeError("reconciler could not sweep every database - see SWEEP FAILED above")
    if not s.dry_run:
        state["high_water"] = mark


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
    logs = handle_event(payload, notion, now, notion.me(), place_tags=s.place_tags)
    print(logs)
    return {"ok": True, "handled": len(logs)}
