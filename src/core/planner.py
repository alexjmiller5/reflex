"""Pure recurrence engine. No Notion, no I/O - dates in, decisions out."""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

OPEN_STATUSES = frozenset({"To Do", "In Progress"})


@dataclass(frozen=True)
class TaskSnapshot:
    title: str
    status: str
    due: date | None
    completed: date | None
    id: str = ""


@dataclass(frozen=True)
class Occurrence:
    due: date


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _step(d: date, spec) -> date:
    return add_months(d, spec.interval_months) + timedelta(days=spec.interval_days)


def keepalive_due(existing, has_recent_txn, today, grace_days=365):
    """Card keep-alive: fire when the Transactions DB shows no recent activity.

    Quiet if a transaction exists in the window, a task is already open, or a
    prior task was completed within grace_days - completing the task counts as
    activity, which also covers cards whose scraper isn't live yet.
    """
    if has_recent_txn or any(t.status in OPEN_STATUSES for t in existing):
        return False
    completions = [t.completed for t in existing if t.completed]
    return not (completions and (today - max(completions)).days < grace_days)


def next_occurrence(spec, existing, today):
    if spec.mode == "relative":
        if any(t.status in OPEN_STATUSES for t in existing):
            return None
        completions = [t.completed for t in existing if t.completed]
        last = max(completions + [spec.anchor]) if completions else spec.anchor
        return Occurrence(due=_step(last, spec))
    # fixed: series anchor + n*interval; materialize the latest series date <= today
    # unless the occurrence is already fully handled (any status).
    d = spec.anchor
    latest_due = None
    occurrence = 0
    while d <= today:
        latest_due = d
        occurrence += 1
        d = add_months(spec.anchor, spec.interval_months * occurrence) + timedelta(
            days=spec.interval_days * occurrence
        )
    if latest_due is None:
        return None
    if spec.templates:
        # per-template due (offset applied) must all already exist, else the
        # occurrence still has missing tasks - fire so dispatch() can finish
        # them (per-template dedupe there skips whichever already exist)
        handled = all(
            any(
                s.title == t.title and s.due == latest_due + timedelta(days=t.due_offset_days)
                for s in existing
            )
            for t in spec.templates
        )
    else:
        handled = any(t.due == latest_due for t in existing)
    if handled:
        return None
    return Occurrence(due=latest_due)
