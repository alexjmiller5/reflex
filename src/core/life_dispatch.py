"""Recurring task creation with frozen occurrence intent and retained adoption.

Uses the existing planner and supported hub client. The injected journal belongs
to Reflex; identities never depend on editable titles or credentials.
"""

import copy
import json
from datetime import date, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from core.planner import TaskSnapshot, keepalive_due, next_occurrence
from core.registry import (
    KEEPALIVE_INACTIVE_DAYS,
    TRANSACTIONS,
    RecurringSpec,
    TaskTemplate,
    cc_keepalive_title,
)


def _identity(*parts):
    return uuid5(
        NAMESPACE_URL, json.dumps(["reflex-occurrence-v1", *parts], separators=(",", ":"))
    ).hex


def _day(value, zone):
    if not value:
        return None
    if len(value) == 10:
        return date.fromisoformat(value)
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("task instant requires a timezone")
    return instant.astimezone(zone).date()


def _write_plan(plan, hub, state, state_key, records, snapshot):
    """Persist intent first; every retry uses these exact IDs and bodies."""
    for item in plan["items"]:
        if item["adopted"] and item["row"]["id"] not in snapshot[item["table"]]:
            raise ValueError("adopted task target is missing; refusing a replacement")
    if hub.dry_run:
        return
    for item in plan["items"]:
        if item.get("delivered"):
            continue
        row_id = item["row"]["id"]
        if row_id not in snapshot[item["table"]]:
            hub.insert_rows(item["table"], [item["row"]])
            snapshot[item["table"]][row_id] = copy.deepcopy(item["row"])
        item["delivered"] = True
        state[state_key] = records
    plan["complete"] = True
    state[state_key] = records


def dispatch_life(notion, today, recurring, cards, hub, state, config):
    columns = config["columns"]
    zone = ZoneInfo(config["time_zone"])
    rows = hub.pull_rows(config["table"], sorted({"id", "deleted_at", *columns.values()}))
    snapshot = {row["id"]: row for row in rows}
    if len(rows) != len(snapshot):
        raise ValueError("task scan changed during pagination; retry a complete scan")
    inventory = {config["table"]: snapshot}
    if any(spec.gift_recipients for spec in recurring):
        binding = config["gifts"]
        gift_rows = hub.pull_rows(
            binding["table"], sorted({"id", "deleted_at", *binding["columns"].values()})
        )
        inventory[binding["table"]] = {row["id"]: row for row in gift_rows}
        if len(gift_rows) != len(inventory[binding["table"]]):
            raise ValueError("gift scan changed during pagination")
    retained_adoptions = state.get("recurrence_adoptions", [])
    adoptions = [*retained_adoptions, *config.get("adoptions", [])]
    adopted = {}
    for entry in adoptions:
        key = (entry["spec_key"], entry["occurrence"], entry["template_key"])
        if key in adopted and adopted[key] != entry["target_id"]:
            raise ValueError("retained adoption cannot be rebound")
        if entry["target_id"] not in snapshot:
            raise ValueError("missing adopted target")
        adopted[key] = entry["target_id"]
    merged_adoptions = [
        {"spec_key": key[0], "occurrence": key[1], "template_key": key[2], "target_id": target}
        for key, target in adopted.items()
    ]
    if not hub.dry_run and merged_adoptions != retained_adoptions:
        state["recurrence_adoptions"] = merged_adoptions
    log = []
    for spec in recurring:
        full_names = {}
        if spec.gift_recipients:
            from core.dispatcher import _hydrate_recipients

            spec, full_names = _hydrate_recipients(spec, hub)
        templates = {template.key: template for template in spec.templates}
        if "" in templates or len(templates) != len(spec.templates):
            raise ValueError("Life Data recurring templates require unique stable keys")
        state_key = "recurrence:" + spec.key
        records = copy.deepcopy(state.get(state_key, {"plans": {}}))
        # Finish interrupted work before planning another occurrence or reading
        # updated template values. Delayed retries preserve the first intent.
        for plan in records["plans"].values():
            if not plan["complete"]:
                _write_plan(plan, hub, state, state_key, records, inventory)
        known = {
            item["row"]["id"]: item["template_key"]
            for plan in records["plans"].values()
            for item in plan["items"]
        }
        known.update({target: key[2] for key, target in adopted.items() if key[0] == spec.key})
        existing = []
        for row in snapshot.values():
            template = templates.get(known.get(row["id"]))
            title = template.title if template else row.get(columns["title"])
            if not template and title not in spec.match_titles:
                continue
            existing.append(
                TaskSnapshot(
                    title=title,
                    status="Canceled" if row.get("deleted_at") else row.get(columns["status"]),
                    due=_day(row.get(columns["due"]), zone),
                    completed=_day(row.get(columns["completed"]), zone),
                )
            )
        occurrence = next_occurrence(spec, existing, today)
        if occurrence is None:
            log.append(f"{spec.key}: nothing due")
            continue
        occurrence_key = occurrence.due.isoformat()
        if occurrence_key in records["plans"]:
            log.append(f"{spec.key}: occurrence retained")
            continue
        items, previous = [], None
        for template in spec.templates:
            due = (occurrence.due + timedelta(days=template.due_offset_days)).isoformat()
            retained = adopted.get((spec.key, occurrence_key, template.key))
            matches = [
                r
                for r in rows
                if r.get(columns["title"]) == template.title
                and _day(r.get(columns["due"]), zone) == date.fromisoformat(due)
            ]
            if not retained and len(matches) > 1:
                raise ValueError("ambiguous historical occurrence requires an explicit adoption")
            if not retained and matches:
                retained = matches[0]["id"]
            row_id = retained or _identity(spec.key, occurrence_key, template.key)
            values = {
                **config.get("defaults", {}),
                "title": template.title,
                "due": due,
                "tags": list(template.tags),
                "priority": template.priority,
                "notes": template.notes,
                "links": template.links,
            }
            if template.blocked_by_prev:
                if previous is None:
                    raise ValueError("first template cannot depend on a previous task")
                values["blocked_by"] = [previous]
            body = {"id": row_id, **{columns[key]: value for key, value in values.items()}}
            items.append(
                {
                    "table": config["table"],
                    "row": body,
                    "template_key": template.key,
                    "adopted": bool(retained),
                    "delivered": False,
                }
            )
            previous = row_id
        if spec.gift_recipients:
            binding = config["gifts"]
            gift_columns = binding["columns"]
            for person_id in spec.gift_recipients:
                gift_day = binding["date_template"].format(year=occurrence.due.year)
                date.fromisoformat(gift_day)
                matches = []
                for row in inventory[binding["table"]].values():
                    recipients = row.get(gift_columns["recipient_ids"])
                    if isinstance(recipients, str):
                        recipients = json.loads(recipients)
                    if (
                        recipients == [person_id]
                        and row.get(gift_columns["gift_on"]) == gift_day
                        and row.get(gift_columns["occasion"]) == binding["defaults"]["occasion"]
                    ):
                        matches.append(row)
                if len(matches) > 1:
                    raise ValueError("ambiguous historical gift identity")
                target = (
                    matches[0]["id"]
                    if matches
                    else _identity(spec.key, occurrence_key, "gift", person_id)
                )
                values = {
                    **binding["defaults"],
                    "description": binding["description_template"].format(
                        name=full_names[person_id], year=occurrence.due.year
                    ),
                    "recipient_ids": [person_id],
                    "gift_on": gift_day,
                }
                if "task_ids" in gift_columns:
                    values["task_ids"] = [
                        item["row"]["id"]
                        for item in items
                        if item["template_key"] in {person_id + ":brainstorm", person_id + ":buy"}
                    ]
                items.append(
                    {
                        "table": binding["table"],
                        "row": {"id": target, **{gift_columns[k]: v for k, v in values.items()}},
                        "template_key": "gift:" + person_id,
                        "adopted": bool(matches),
                        "delivered": False,
                    }
                )
        plan = {"items": items, "complete": False}
        records["plans"][occurrence_key] = plan
        if not hub.dry_run:
            state[state_key] = records
        _write_plan(plan, hub, state, state_key, records, inventory)
        log.append(f"{spec.key}: occurrence {'previewed' if hub.dry_run else 'delivered'}")
    if cards:
        log.extend(_keepalive(notion, today, cards, hub, state, config, snapshot))
    return log


def _keepalive(notion, today, cards, hub, state, config, snapshot):
    schema = notion.get_data_source(TRANSACTIONS)
    options = {
        o["name"] for o in schema["properties"]["Credit Card / Account"]["select"]["options"]
    }
    if any(account not in options or account not in config["card_keys"] for account in cards):
        raise ValueError("keepalive requires a current transaction option and stable card key")
    columns, zone = config["columns"], ZoneInfo(config["time_zone"])
    log = []
    for account in cards:
        key = "keepalive:" + config["card_keys"][account]
        title = cc_keepalive_title(account)
        records = state.get("recurrence:" + key, {"plans": {}})
        known = {item["row"]["id"] for plan in records["plans"].values() for item in plan["items"]}
        existing = [
            TaskSnapshot(
                title,
                "Canceled" if row.get("deleted_at") else row.get(columns["status"]),
                _day(row.get(columns["due"]), zone),
                _day(row.get(columns["completed"]), zone),
            )
            for row in snapshot.values()
            if row["id"] in known or row.get(columns["title"]) == title
        ]
        cutoff = (today - timedelta(days=KEEPALIVE_INACTIVE_DAYS)).isoformat()
        recent = notion.any_match(
            TRANSACTIONS,
            {
                "and": [
                    {"property": "Credit Card / Account", "select": {"equals": account}},
                    {"property": "Transaction Date", "date": {"on_or_after": cutoff}},
                ]
            },
        )
        if not keepalive_due(existing, recent, today, KEEPALIVE_INACTIVE_DAYS):
            continue
        defaults = config["keepalive_defaults"]
        template = TaskTemplate(
            title=title, tags=tuple(defaults["tags"]), priority=defaults["priority"], key="reminder"
        )
        recurring = RecurringSpec(
            key=key,
            mode="fixed",
            anchor=today,
            interval_months=12,
            match_titles=(title,),
            templates=(template,),
        )
        log.extend(dispatch_life(notion, today, (recurring,), (), hub, state, config))
    return log
