"""Pure event rules: page JSON in, violations/fixes out. Mirrors the native
Notion automations captured in the inventory page (3c203953a8af818b998ff5a152078c8a).

Status/property names below are pinned against scripts/pin_schema.py output
(see task-3-report.md), not guessed - see task-6-report.md for the full diff
against the original brief guesses.
"""

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from core import registry as R

NY = ZoneInfo("America/New_York")

# (status property, date property, statuses that SET, statuses that CLEAR)
TIMESTAMP_RULES = {
    R.TASKS: [("Status", "Completed Date", {"Completed"}, {"To Do", "In Progress"})],
    R.PROJECTS: [("Status", "Completed Date", {"Completed"}, {"To Do", "In progress"})],
    # Synapse's Outcome -> Date Reviewed rule is handled in the SYNAPSE-specific
    # block in evaluate() instead of here: on page.created it must judge the
    # EFFECTIVE post-fix Outcome (after the auto-approve fix below), not the
    # raw one, so it can't be a simple raw-property lookup like the rest.
}

# Outcome has no "Complete"/"To-do" options; "reviewed" = triaged away from
# "To Review" into any terminal category (see task-6-report.md).
SYNAPSE_REVIEWED_STATUSES = {
    "User Error",
    "Test Execution",
    "Bug",
    "Failed Extraction",
    "Successful Flow",
}


@dataclass(frozen=True)
class Violation:
    rule: str
    page_id: str
    page_title: str
    page_url: str
    fix: dict | None


def title_of(props):
    for p in props.values():
        if "title" in p:
            return "".join(t.get("plain_text", "") for t in p["title"])
    return "(untitled)"


def _status(props, prop):  # tolerate select-typed status props
    v = props.get(prop) or {}
    inner = v.get("status") or v.get("select") or {}
    return (inner or {}).get("name", "")


def _date_set(props, prop):
    return bool((props.get(prop) or {}).get("date"))


_SLUGS = {
    R.TASKS: "tasks",
    R.PROJECTS: "projects",
    R.SYNAPSE: "synapse",
}


def evaluate(data_source_id, page, now, created=False, place_tags=(), changed_properties=None):
    """Evaluate a creation, specific property changes, or a read-only audit.

    None means an audit: report contradictory dates, but never infer missing
    historical timestamps or creation defaults from a page's current state.
    Webhooks pass an explicit set, including an empty set for unrelated edits.
    """
    props, out = page["properties"], []
    pid, purl, ptitle = page["id"], page.get("url", ""), title_of(props)
    slug = _SLUGS.get(data_source_id, "db")

    def viol(rule, fix):
        out.append(Violation(rule, pid, ptitle, purl, fix))

    def triggered(prop):
        return created or prop in (changed_properties or ())

    for status_prop, date_prop, set_on, clear_on in TIMESTAMP_RULES.get(data_source_id, []):
        s = _status(props, status_prop)
        if s in set_on and not _date_set(props, date_prop) and triggered(status_prop):
            viol(
                f"{slug}-{date_prop.lower().replace(' ', '-')}-set",
                {date_prop: {"date": {"start": now.astimezone(NY).isoformat()}}},
            )
        elif (
            s in clear_on
            and _date_set(props, date_prop)
            and (changed_properties is None or triggered(status_prop))
        ):
            viol(f"{slug}-{date_prop.lower().replace(' ', '-')}-clear", {date_prop: {"date": None}})

    if data_source_id == R.TASKS and created:
        due_today = now.astimezone(NY).date().isoformat()
        existing_tags = [t["name"] for t in (props.get("Tags") or {}).get("multi_select", [])]
        # Place-tagged tasks (NOTION_TASKS_PLACE_TAGS) are done whenever the
        # user is next at that place - dateless by design, no due date forced.
        if not _date_set(props, "Due Date") and not set(existing_tags) & set(place_tags):
            viol("tasks-default-due", {"Due Date": {"date": {"start": due_today}}})
        if not existing_tags:
            viol("tasks-default-tags", {"Tags": {"multi_select": [{"name": "Chore"}]}})
        if not (props.get("Priority") or {}).get("select"):
            viol("tasks-default-priority", {"Priority": {"select": {"name": "High"}}})

    if data_source_id == R.SYNAPSE:
        remedied = (props.get("Remedied?") or {}).get("checkbox", False)
        if remedied and not _date_set(props, "Date Remedied") and triggered("Remedied?"):
            viol(
                "synapse-date-remedied-set",
                {"Date Remedied": {"date": {"start": now.astimezone(NY).isoformat()}}},
            )
        elif (
            not remedied
            and _date_set(props, "Date Remedied")
            and (changed_properties is None or triggered("Remedied?"))
        ):
            viol("synapse-date-remedied-clear", {"Date Remedied": {"date": None}})

        effective_outcome = _status(props, "Outcome")
        if created:
            exec_ok = _status(props, "Code Execution") == "Success"
            cat = ((props.get("Category") or {}).get("select") or {}).get("name", "")
            desired = "Successful Flow" if (exec_ok and cat == "bookmarks") else "To Review"
            if effective_outcome != desired:
                viol("synapse-outcome", {"Outcome": {"status": {"name": desired}}})
            effective_outcome = desired  # post-fix value, not the raw one

        if (
            effective_outcome in SYNAPSE_REVIEWED_STATUSES
            and not _date_set(props, "Date Reviewed")
            and triggered("Outcome")
        ):
            viol(
                f"{slug}-date-reviewed-set",
                {"Date Reviewed": {"date": {"start": now.astimezone(NY).isoformat()}}},
            )
        elif (
            effective_outcome == "To Review"
            and _date_set(props, "Date Reviewed")
            and (changed_properties is None or triggered("Outcome"))
        ):
            viol(f"{slug}-date-reviewed-clear", {"Date Reviewed": {"date": None}})

    return out


def evaluate_transition(before, after, changed, occurred_at, policy, created=False):
    """Compute prospective fixes from runtime column/value policy.

    Historical snapshots are seeds, not creation events. Explicit date edits
    win over simultaneous status automation, and supplied creation values are
    retained. The caller folds events in order and guards the eventual patch.
    """
    import copy
    import json
    from datetime import timedelta, timezone

    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("An aware event timestamp is required")
    zone = ZoneInfo(policy.get("time_zone", "UTC"))
    boundary = policy.get("day_start_minutes", 0)
    if isinstance(boundary, bool) or not isinstance(boundary, int) or not 0 <= boundary <= 1439:
        raise ValueError("day_start_minutes must be an integer from 0 through 1439")
    stamp = (
        occurred_at.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    effective, fixes = copy.deepcopy(after), {}

    def missing(value):
        return value is None or value == "" or value == []

    def assign(column, value):
        if effective.get(column) != value:
            effective[column] = copy.deepcopy(value)
            fixes[column] = copy.deepcopy(value)

    def triggered(column):
        return created or (column in changed and before.get(column) != after.get(column))

    def tags_of(value):
        if isinstance(value, str):
            value = json.loads(value)
        if value is None:
            return []
        if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
            raise ValueError("Tag values must be an array of strings")
        return value

    if created:
        for column, default in policy.get("creation_defaults", {}).items():
            existing = effective.get(column)
            if isinstance(default, list):
                existing = tags_of(existing)
            if missing(existing):
                assign(column, default)
        if rule := policy.get("due_on_creation"):
            if missing(effective.get(rule["column"])):
                tags = tags_of(effective.get(rule.get("tags_column")))
                if not set(tags) & set(rule.get("excluded_tags", [])):
                    local = occurred_at.astimezone(zone)
                    day = local.date()
                    if local.hour * 60 + local.minute < boundary:
                        day -= timedelta(days=1)
                    assign(rule["column"], day.isoformat())
        if rule := policy.get("creation_outcome"):
            if effective.get(rule["column"]) in rule["replaceable"]:
                desired = (
                    rule["value"]
                    if all(effective.get(k) == v for k, v in rule["when"].items())
                    else rule["default"]
                )
                assign(rule["column"], desired)

    for rule in policy.get("timestamps", []):
        trigger, date = rule["trigger"], rule["date"]
        if not triggered(trigger) or (date in changed and not created):
            continue
        if effective.get(trigger) in rule["set_on"] and missing(effective.get(date)):
            assign(date, stamp)
        elif (
            effective.get(trigger) in rule["clear_on"]
            and not missing(effective.get(date))
            and not created
        ):
            assign(date, None)

    for rule in policy.get("checkbox_timestamps", []):
        trigger, date = rule["trigger"], rule["date"]
        if not triggered(trigger) or (date in changed and not created):
            continue
        value = effective.get(trigger)
        if value not in (None, 0, 1, False, True):
            raise ValueError("Checkbox value must be boolean or numeric zero/one")
        if value in (1, True) and missing(effective.get(date)):
            assign(date, stamp)
        elif value in (None, 0, False) and not missing(effective.get(date)) and not created:
            assign(date, None)
    return fixes
