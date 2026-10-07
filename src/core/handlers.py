"""Webhook orchestration: verify, route, apply. The only module that both
reads AND writes pages in reaction to events.

Event shape is pinned against the real Notion webhook docs (2026-08-20 read
of developers.notion.com/reference/webhooks and the events-delivery page),
not guessed - see task-7-report.md for the full correction list. The two
that shape this module:
  - event["data"]["parent"] is {"id", "type"} - it never carries a
    data_source_id. The data_source_id only exists on the fetched Page
    object (page["parent"]["data_source_id"]), so every event needs a
    get_page() before it can be routed.
  - event["data"]["updated_properties"] holds property IDs, not names -
    resolved back to names via each property's own "id" field on the page.
"""

import hashlib
import hmac
from datetime import datetime
from urllib.parse import unquote

from core import registry as R
from core.rules import evaluate

EVENT_DBS = frozenset({R.TASKS, R.PROJECTS, R.SYNAPSE})


def handshake_token(payload, secret):
    """The one-time subscription handshake token, or None.

    Notion posts this UNSIGNED (it is the secret being handed over, so there is
    nothing to sign with yet), which makes it the only unauthenticated path in
    the endpoint. It is therefore honored only while no secret is configured:
    once one is, an unsigned handshake must be refused, or anyone could spray
    plausible "verification token" lines into the logs and get one adopted as
    the signing secret during a later re-subscription.

    Re-subscribing on purpose (e.g. the endpoint URL changed) means clearing
    NOTION_WEBHOOK_SECRET and redeploying first - see the README.
    """
    if secret:
        return None
    return payload.get("verification_token")


def verify_signature(body, header, secret):
    if not (header and secret):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def handle_event(event, notion, now, bot_id, place_tags=(), retired_sources=()):
    authors = event.get("authors", [])
    # Aggregation may combine our writes with a real user/integration change.
    if authors and all(a.get("id") == bot_id for a in authors):
        return ["skipped: self-authored"]
    if event.get("entity", {}).get("type") != "page":
        return ["skipped: non-page entity"]
    if event["type"] not in {"page.created", "page.properties_updated"}:
        return ["skipped: unrelated event"]

    page = notion.get_page(event["entity"]["id"])
    if page.get("in_trash") or page.get("archived"):
        return ["skipped: trashed page"]
    ds = page["parent"].get("data_source_id")
    if not ds:
        return ["skipped: non-data-source parent"]
    if ds in retired_sources:
        return ["skipped: migrated source"]
    if ds not in EVENT_DBS:
        return [f"skipped: unwatched db {ds}"]

    created = event["type"] == "page.created"
    updated_ids = {unquote(pid) for pid in event.get("data", {}).get("updated_properties", [])}
    changed = {
        name
        for name, prop in page["properties"].items()
        if prop.get("id") and unquote(prop["id"]) in updated_ids
    }
    # Delayed deliveries must use the event's date, not the delivery day's date.
    occurred = datetime.fromisoformat(event["timestamp"]) if event.get("timestamp") else now
    # The payload has no old/new values. Never pair an older event with a
    # newer fetched state. Notion's minute precision limits this comparison.
    edited = page.get("last_edited_time")
    if edited and datetime.fromisoformat(edited) > occurred:
        return ["skipped: superseded event"]
    log = []

    for v in evaluate(
        ds, page, occurred, created=created, place_tags=place_tags, changed_properties=changed
    ):
        if v.fix:
            notion.update_page(page["id"], v.fix)
            log.append(f"applied {v.rule} page={page['id']}")

    return log or ["compliant"]
