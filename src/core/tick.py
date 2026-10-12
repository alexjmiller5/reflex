"""One bounded event tick with the existing daily dispatch time retained."""

from datetime import datetime, timedelta, timezone

STUCK_AFTER = timedelta(minutes=30)


def run_tick(state, now, daily, *, consume=None, dry_run=False):
    """`consume` drains events and returns True while work remains retained.

    A backlog defers daily work, which reads what the drain stamps, to a later
    tick instead of failing this one.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("tick requires an aware clock")
    if consume and consume():
        return {"daily": False}
    utc = now.astimezone(timezone.utc)
    before_due = (utc.hour, utc.minute) < (11, 30)
    previous = state.get("daily_success")
    if before_due and previous is None:
        return {"daily": False}
    due = (utc.date() - timedelta(days=int(before_due))).isoformat()
    if previous is not None and previous >= due:
        return {"daily": False}
    daily()
    if not dry_run:
        state["daily_success"] = due
    return {"daily": True}


def note_backlog(state, incomplete, now, *, limit=STUCK_AFTER):
    """Retained event work is normal after a burst; work pending past `limit` is stuck.

    Raising fails the scheduled run, which is what alerts the owner.
    """
    since = state.get("backlog_since")
    if not incomplete:
        if since is not None:
            del state["backlog_since"]
    elif since is None:
        state["backlog_since"] = now.isoformat()
    elif now - datetime.fromisoformat(since) >= limit:
        raise RuntimeError(f"event work pending since {since}; see the drain results above")
