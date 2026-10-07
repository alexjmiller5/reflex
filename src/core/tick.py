"""One bounded event tick with the existing daily dispatch time retained."""

from datetime import timedelta, timezone


def run_tick(state, now, daily, *, consume=None, dry_run=False):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("tick requires an aware clock")
    if consume:
        consume()
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
