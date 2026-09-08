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
    R.BOOKS: [("Status", "Date Read", {"Finished"}, {"Not Started", "In Progress"})],
    R.MOVIES: [("Status", "Date Watched", {"Finished"}, {"Not Started", "In Progress"})],
    R.PODCASTS: [("Status", "Date Listened To", {"Finished"}, {"Not Started", "In Progress"})],
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
    R.BOOKS: "books",
    R.MOVIES: "movies",
    R.SYNAPSE: "synapse",
    R.PODCASTS: "podcasts",
}


def evaluate(data_source_id, page, now, created=False, place_tags=()):
    props, out = page["properties"], []
    pid, purl, ptitle = page["id"], page.get("url", ""), title_of(props)
    slug = _SLUGS.get(data_source_id, "db")

    def viol(rule, fix):
        out.append(Violation(rule, pid, ptitle, purl, fix))

    for status_prop, date_prop, set_on, clear_on in TIMESTAMP_RULES.get(data_source_id, []):
        s = _status(props, status_prop)
        if s in set_on and not _date_set(props, date_prop):
            viol(
                f"{slug}-{date_prop.lower().replace(' ', '-')}-set",
                {date_prop: {"date": {"start": now.astimezone(NY).isoformat()}}},
            )
        elif s in clear_on and _date_set(props, date_prop):
            viol(f"{slug}-{date_prop.lower().replace(' ', '-')}-clear", {date_prop: {"date": None}})

    if data_source_id == R.TASKS:
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
        if remedied and not _date_set(props, "Date Remedied"):
            viol(
                "synapse-date-remedied-set",
                {"Date Remedied": {"date": {"start": now.astimezone(NY).isoformat()}}},
            )
        elif not remedied and _date_set(props, "Date Remedied"):
            viol("synapse-date-remedied-clear", {"Date Remedied": {"date": None}})

        effective_outcome = _status(props, "Outcome")
        if created:
            exec_ok = _status(props, "Code Execution") == "Success"
            cat = ((props.get("Category") or {}).get("select") or {}).get("name", "")
            desired = "Successful Flow" if (exec_ok and cat == "bookmarks") else "To Review"
            if effective_outcome != desired:
                viol("synapse-outcome", {"Outcome": {"status": {"name": desired}}})
            effective_outcome = desired  # post-fix value, not the raw one

        if effective_outcome in SYNAPSE_REVIEWED_STATUSES and not _date_set(props, "Date Reviewed"):
            viol(
                f"{slug}-date-reviewed-set",
                {"Date Reviewed": {"date": {"start": now.astimezone(NY).isoformat()}}},
            )
        elif effective_outcome == "To Review" and _date_set(props, "Date Reviewed"):
            viol(f"{slug}-date-reviewed-clear", {"Date Reviewed": {"date": None}})

    return out
