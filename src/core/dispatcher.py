"""Daily dispatcher: evaluate recurring specs, create what's due."""

import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from core.notion import task_properties
from core.planner import keepalive_due, next_occurrence
from core.registry import (
    KEEPALIVE_INACTIVE_DAYS,
    TASKS,
    TRANSACTIONS,
    TaskTemplate,
    cc_keepalive_title,
)


def _hydrate_recipients(spec, hub):
    """Build the Christmas templates from live people rows.

    People's names are personal data and are not stored in this repo - the
    spec row holds people ids, so the names come from the life-data `people`
    table at run time. Returns the spec with templates/match_titles filled in,
    plus each recipient's full name for the gift row.
    """
    names = {
        r["id"]: r["name"]
        for r in hub.pull_rows("people", ("id", "name", "deleted_at"))
        if not r.get("deleted_at")
    }
    templates, full_names = [], {}
    for person_id in spec.gift_recipients:
        if person_id not in names:
            raise RuntimeError(
                f"{spec.key}: no live life-data people row for recipient id {person_id} - "
                f"fix gift_recipients on the recurring_specs row (people ids are dashless)"
            )
        full = names[person_id]
        full_names[person_id] = full
        name = full.split()[0]
        templates.append(
            TaskTemplate(
                title=f"Brainstorm and come up with an idea for {name}'s Christmas Gift",
                key=person_id + ":brainstorm",
                tags=("Gifts",),
                priority="High",
            )
        )
        templates.append(
            TaskTemplate(
                title=f"Buy {name}'s Christmas Gift",
                key=person_id + ":buy",
                tags=("Gifts",),
                priority="High",
                due_offset_days=30,
                blocked_by_prev=True,
            )
        )
    hydrated = replace(
        spec,
        templates=tuple(templates),
        match_titles=tuple(t.title for t in templates),
    )
    return hydrated, full_names


def dispatch(notion, today, recurring, cards, hub, *, task_config=None, state=None):
    """recurring/cards come from the life-data tables (see registry loaders);
    `hub` (core.hub.HubClient) reads people and writes gift rows."""
    if task_config is not None:
        from core.life_dispatch import dispatch_life

        if state is None:
            raise ValueError("Life Data task dispatch requires a durable journal")
        return dispatch_life(notion, today, recurring, cards, hub, state, task_config)
    log = []
    for spec in recurring:
        full_names = {}
        if spec.gift_recipients:
            spec, full_names = _hydrate_recipients(spec, hub)
        existing = notion.snapshots(TASKS, spec.match_titles)
        occ = next_occurrence(spec, existing, today)
        if not occ:
            log.append(f"{spec.key}: nothing to do")
            continue
        prev_id = ""
        for t in spec.templates:
            due = occ.due + timedelta(days=t.due_offset_days)
            matches = [s for s in existing if s.title == t.title and s.due == due]
            if matches:
                if len(matches) > 1:
                    raise ValueError("ambiguous recurring task identity")
                prev_id = matches[0].id
                log.append(f"{spec.key}: '{t.title}' already exists due {due}, skipping")
                continue
            props = task_properties(
                t.title,
                due,
                t.tags,
                t.priority,
                links=t.links,
                notes=t.notes,
                blocked_by=prev_id if t.blocked_by_prev else "",
            )
            prev_id = notion.create_page(TASKS, props)["id"]
            log.append(f"{spec.key}: created '{t.title}' due {due}")
        for person_id in spec.gift_recipients:
            name = full_names[person_id]
            row = {
                "id": secrets.token_hex(16),
                "description": f"{name}'s Christmas Gift {occ.due.year}",
                "recipient_ids": [person_id],
                "status": "To Do",
                "occasion": "Christmas",
                "gift_on": f"{occ.due.year}-12-25",
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                + "Z",
            }
            rejected = hub.push_rows("gifts", [row]).get("rejected") or []
            if rejected:
                raise RuntimeError(
                    f"{spec.key}: life-data rejected the gift row for {person_id}: {rejected[0]}"
                )
            log.append(f"{spec.key}: created gifts row for {person_id}")
    # Fail hard (Modal emails on a failed schedule) if a Transactions-DB
    # overhaul renames a card option - a filter on a gone option matches
    # nothing, which would read as inactivity and create bogus tasks.
    schema = notion.get_data_source(TRANSACTIONS)
    options = {
        o["name"] for o in schema["properties"]["Credit Card / Account"]["select"]["options"]
    }
    missing = [c for c in cards if c not in options]
    if missing:
        raise RuntimeError(
            f"cc-keepalive: cards missing from Transactions DB 'Credit Card / Account' "
            f"options: {missing} - update the cc_keepalive_cards table in life-data"
        )
    cutoff = (today - timedelta(days=KEEPALIVE_INACTIVE_DAYS)).isoformat()
    for account in cards:
        title = cc_keepalive_title(account)
        existing = notion.snapshots(TASKS, (title,))
        has_recent_txn = notion.any_match(
            TRANSACTIONS,
            {
                "and": [
                    {"property": "Credit Card / Account", "select": {"equals": account}},
                    {"property": "Transaction Date", "date": {"on_or_after": cutoff}},
                ]
            },
        )
        if not keepalive_due(existing, has_recent_txn, today, KEEPALIVE_INACTIVE_DAYS):
            log.append(f"cc-keepalive {account}: nothing to do")
            continue
        notion.create_page(TASKS, task_properties(title, today, ("Finances",), "Medium"))
        log.append(f"cc-keepalive {account}: created task")
    return log
