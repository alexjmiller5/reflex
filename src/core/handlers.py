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
from core.rules import evaluate, title_of  # title_of shared deliberately (renamed from _title)

EVENT_DBS = frozenset(
    {
        R.TASKS,
        R.PROJECTS,
        R.TRIPS,
        R.CALENDAR,
        R.SYNAPSE,
        R.BOOKS,
        R.MOVIES,
        R.PODCASTS,
    }
)


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
    props, log = page["properties"], []

    # 1. property rules (pure) - apply fixes
    for v in evaluate(ds, page, now, created=created, place_tags=place_tags):
        if v.fix:
            notion.update_page(page["id"], v.fix)
            log.append(f"applied {v.rule}")

    # 2. Calendar: companion note on creation (native Create Calendar Item Notes)
    if ds == R.CALENDAR and created and not (props.get("Notes") or {}).get("relation"):
        note = notion.create_page(
            R.NOTES, {"Title": {"title": [{"text": {"content": f"{title_of(props)} Notes"}}]}}
        )
        notion.update_page(page["id"], {"Notes": {"relation": [{"id": note["id"]}]}})
        log.append("created companion note")

    # 3. Trips: sync linked note titles (native "Alex Miller's automation")
    if ds == R.TRIPS:
        desired = f"{title_of(props)} Notes"
        for rel in (props.get("Notes") or {}).get("relation", []):
            note = notion.get_page(rel["id"])
            if title_of(note["properties"]) != desired:
                notion.update_page(
                    rel["id"], {"Title": {"title": [{"text": {"content": desired}}]}}
                )
                log.append(f"synced trip note title -> {desired}")

    return log or ["compliant"]
