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


def handle_event(event, notion, now, bot_id, place_tags=()):
    if any(a.get("id") == bot_id for a in event.get("authors", [])):
        return ["skipped: self-authored"]
    if event.get("entity", {}).get("type") != "page":
        return ["skipped: non-page entity"]

    page = notion.get_page(event["entity"]["id"])
    ds = page["parent"].get("data_source_id")
    if not ds:
        return ["skipped: non-data-source parent"]
    if ds not in EVENT_DBS:
        return [f"skipped: unwatched db {ds}"]

    created = event["type"] == "page.created"
    log = []

    # 1. property rules (pure) - apply fixes
    for v in evaluate(ds, page, now, created=created, place_tags=place_tags):
        if v.fix:
            notion.update_page(page["id"], v.fix)
            log.append(f"applied {v.rule}")

    return log or ["compliant"]
