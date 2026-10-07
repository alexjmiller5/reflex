"""Synthetic durable-delivery model exercises the public subscription contract."""

import copy
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from core.life_events import LifeEventConsumer, seed_projection

NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
DEADLINE = NOW + timedelta(minutes=1)
POLICY = {
    "subscription_id": "sub",
    "tables": {
        "items": {
            "time_zone": "UTC",
            "timestamps": [
                {"trigger": "state", "date": "finished", "set_on": ["Done"], "clear_on": ["Open"]}
            ],
            "creation_defaults": {"tags": ["Routine"]},
        }
    },
}


class Hub:
    dry_run = False

    def __init__(self):
        self.rows = {
            "r": {
                "id": "r",
                "state": "Open",
                "finished": None,
                "tags": None,
                "deleted_at": None,
                "updated_at": "2026-01-01T00:00:00.000Z",
                "hub_at": "2026-01-01T00:00:00.000Z",
            }
        }
        self.events = []
        self.acked = 0
        self.pending = None
        self.acks = []
        self.patches = []
        self.on_read = None
        self.on_patch = None
        self.on_ack = None
        self.lose_patch_reply = False
        self.caps = {
            "conditional_patch": "revision-v1",
            "subscriptions": "durable-pull-v1",
            "subscription_features": "scalar-lifecycle-v1",
        }

    def session(self):
        return {"capabilities": self.caps}

    def subscription_status(self, subscription):
        return {
            "id": subscription,
            "protocol": "durable-pull-v1",
            "state": "active",
            "last_seq": str(len(self.events)),
            "acked_seq": str(self.acked),
            "sources": [
                {"table": "items", "columns": ["state", "finished", "tags"], "lifecycle": True}
            ],
        }

    def change(self, values, stamp, *, operation="update", row_id="r"):
        old = copy.deepcopy(self.rows.get(row_id, {}))
        row = {**old, **values, "id": row_id, "updated_at": stamp, "hub_at": stamp}
        if operation == "delete":
            row["deleted_at"] = stamp
        if operation == "restore":
            row["deleted_at"] = None
        self.rows[row_id] = row
        changes = []
        for col in ["state", "finished", "tags"]:
            before = None if operation == "insert" or old.get("deleted_at") else old.get(col)
            after = None if operation == "delete" else row.get(col)
            if before != after:
                changes.append({"column": col, "old_value": before, "new_value": after})
        seq = str(len(self.events) + 1)
        self.events.append(
            {
                "id": "e" + seq,
                "seq": seq,
                "recorded_at": stamp,
                "operation": operation,
                "source": {"table": "items", "row_id": row_id},
                "changes": changes,
            }
        )

    def poll_events(self, subscription):
        if self.pending:
            return copy.deepcopy(self.pending)
        batch = self.events[self.acked : self.acked + 100]
        self.pending = {
            "subscription_id": subscription,
            "delivery_id": f"d-{self.acked}-{self.acked + len(batch)}" if batch else None,
            "through_seq": str(self.acked + len(batch)),
            "events": copy.deepcopy(batch),
        }
        return copy.deepcopy(self.pending)

    def acknowledge(self, subscription, delivery_id):
        if self.on_ack:
            self.on_ack()
        assert self.pending and self.pending["delivery_id"] == delivery_id
        self.acked = int(self.pending["through_seq"])
        self.pending = None
        self.acks.append(delivery_id)
        return {"acked_seq": str(self.acked)}

    def read_row(self, table, row_id, columns):
        row = copy.deepcopy(self.rows.get(row_id))
        if self.on_read:
            fn, self.on_read = self.on_read, None
            fn()
        return row

    def patch_row(self, table, row_id, values, expected_revision):
        self.patches.append(copy.deepcopy(values))
        if self.on_patch:
            fn, self.on_patch = self.on_patch, None
            fn()
        row = self.rows[row_id]
        if expected_revision != {k: row.get(k) for k in ["updated_at", "hub_at"]}:
            raise httpx.HTTPStatusError(
                "conflict",
                request=httpx.Request("POST", "https://hub.example/v1/rows/patch"),
                response=httpx.Response(409),
            )
        stamp = f"2026-01-01T12:{len(self.patches):02d}:00.000Z"
        self.change(values, stamp, row_id=row_id)
        if self.lose_patch_reply:
            self.lose_patch_reply = False
            raise httpx.TimeoutException("committed, response lost")
        return {"id": row_id, "revision": {"updated_at": stamp, "hub_at": stamp}}


def setup():
    hub = Hub()
    state = {}
    seed_projection(state, POLICY, {"items": list(hub.rows.values())}, through_seq="0")
    return hub, state


def drain(hub, state):
    return LifeEventConsumer(hub, state, POLICY, now=lambda: NOW).drain(DEADLINE)


def test_event_is_persisted_before_ack_and_fix_uses_event_time():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")

    def durable():
        assert state["subscription:sub"]["last_seq"] >= 1

    hub.on_ack = durable
    result = drain(hub, state)
    assert hub.rows["r"]["finished"] == "2026-01-01T01:00:00.000Z"
    assert len(hub.patches) == 1 and result["pending"] == 0


def test_complete_open_complete_after_more_than_one_page_uses_final_event():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
    for n in range(110):
        hub.change({"tags": f'["tag-{n % 2}"]'}, f"2026-01-01T02:{n // 60:02d}:{n % 60:02d}.000Z")
    hub.change({"state": "Open"}, "2026-01-01T03:00:00.000Z")
    hub.change({"state": "Done"}, "2026-01-01T04:00:00.000Z")
    drain(hub, state)
    assert hub.rows["r"]["finished"] == "2026-01-01T04:00:00.000Z"
    assert hub.patches == [{"finished": "2026-01-01T04:00:00.000Z"}]
    assert len(hub.acks) >= 2


def test_later_manual_date_wins_over_queued_automation():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
    hub.change({"finished": "2025-12-01"}, "2026-01-01T02:00:00.000Z")
    drain(hub, state)
    assert hub.patches == [] and hub.rows["r"]["finished"] == "2025-12-01"


def test_same_status_cycle_between_row_read_and_high_water_is_not_missed():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")

    def race():
        hub.change({"state": "Open"}, "2026-01-01T02:00:00.000Z")
        hub.change({"state": "Done"}, "2026-01-01T03:00:00.000Z")

    hub.on_read = race
    drain(hub, state)
    assert hub.rows["r"]["finished"] == "2026-01-01T03:00:00.000Z"


def test_patch_conflict_rereads_events_and_preserves_new_manual_value():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
    hub.on_patch = lambda: hub.change({"finished": "2025-12-02"}, "2026-01-01T02:00:00.000Z")
    drain(hub, state)
    assert len(hub.patches) == 1 and hub.rows["r"]["finished"] == "2025-12-02"


def test_timeout_after_patch_commit_recovers_without_second_effect():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
    hub.lose_patch_reply = True
    with pytest.raises(httpx.TimeoutException):
        drain(hub, state)
    drain(hub, state)
    assert len(hub.patches) == 1 and hub.rows["r"]["finished"] == "2026-01-01T01:00:00.000Z"


def test_ack_failure_redelivery_keeps_effect_and_does_not_refold_it():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")

    def lose():
        raise httpx.TimeoutException("ack unavailable")

    hub.on_ack = lose
    with pytest.raises(httpx.TimeoutException):
        drain(hub, state)
    assert hub.patches == []
    hub.on_ack = None
    drain(hub, state)
    assert hub.patches == [{"finished": "2026-01-01T01:00:00.000Z"}]


def test_failed_local_commit_never_acknowledges_the_delivery():
    hub, state = setup()

    class Broken(dict):
        def __setitem__(self, key, value):
            raise OSError("disk full")

    state = Broken(state)
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
    with pytest.raises(OSError):
        drain(hub, state)
    assert not hub.acks and not hub.patches


def test_delete_cancels_pending_effect_and_restore_is_not_creation():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
    hub.change({}, "2026-01-01T02:00:00.000Z", operation="delete")
    drain(hub, state)
    assert not hub.patches
    hub.change({}, "2026-01-01T03:00:00.000Z", operation="restore")
    drain(hub, state)
    assert not hub.patches and hub.rows["r"]["tags"] is None and hub.rows["r"]["finished"] is None


def test_only_new_inserts_receive_creation_defaults():
    hub, state = setup()
    hub.change(
        {"state": "Open", "finished": None, "tags": None, "deleted_at": None},
        "2026-01-01T01:00:00.000Z",
        operation="insert",
        row_id="new",
    )
    drain(hub, state)
    assert hub.rows["new"]["tags"] == ["Routine"] and hub.rows["r"]["tags"] is None


def test_missing_baseline_fails_closed():
    with pytest.raises(ValueError, match="seed"):
        drain(Hub(), {})


def test_sequence_gap_or_changed_redelivery_is_never_acknowledged():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
    hub.events[0]["seq"] = "2"
    with pytest.raises(ValueError):
        drain(hub, state)
    assert not hub.acks


def test_missing_capability_prevents_polling_and_writes():
    hub, state = setup()
    del hub.caps["subscription_features"]
    with pytest.raises(ValueError):
        drain(hub, state)
    assert hub.pending is None and not hub.patches


def test_dry_run_has_no_ack_patch_or_local_commit():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
    hub.dry_run = True
    before = copy.deepcopy(state)
    result = drain(hub, state)
    assert result["dry_run"] and state == before and not hub.acks and not hub.patches


def test_deadline_with_unfolded_events_reports_incomplete():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
    result = LifeEventConsumer(hub, state, POLICY, now=lambda: DEADLINE).drain(DEADLINE)
    assert result["incomplete"] and result["pending"] == 0
    assert not hub.acks and not hub.patches


def test_changed_redelivery_is_rejected_without_ack_or_patch():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")

    def fail():
        raise httpx.TimeoutException("lost ACK")

    hub.on_ack = fail
    with pytest.raises(httpx.TimeoutException):
        drain(hub, state)
    hub.on_ack = None
    hub.pending["events"][0]["recorded_at"] = "2026-01-01T02:00:00.000Z"
    with pytest.raises(ValueError, match="redelivery"):
        drain(hub, state)
    assert not hub.acks and not hub.patches


def test_disk_restart_after_committed_write_retains_exactly_one_effect(tmp_path):
    from core.event_state import locked_state

    hub = Hub()
    path = tmp_path / "events.json"
    with locked_state(path) as state:
        seed_projection(state, POLICY, {"items": list(hub.rows.values())}, through_seq="0")
        hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
        hub.lose_patch_reply = True
        with pytest.raises(httpx.TimeoutException):
            drain(hub, state)
    with locked_state(path) as state:
        assert drain(hub, state)["pending"] == 0
    assert hub.patches == [{"finished": "2026-01-01T01:00:00.000Z"}]


@pytest.mark.parametrize("mutation", ["missing_date", "lifecycle_off", "extra_source"])
def test_subscription_must_watch_exact_policy_fields_and_lifecycle(mutation):
    hub, state = setup()
    original = hub.subscription_status

    def status(subscription):
        result = original(subscription)
        if mutation == "missing_date":
            result["sources"][0]["columns"].remove("finished")
        elif mutation == "lifecycle_off":
            result["sources"][0]["lifecycle"] = False
        else:
            result["sources"].append({"table": "other", "columns": ["state"], "lifecycle": True})
        return result

    hub.subscription_status = status
    with pytest.raises(ValueError, match="sources"):
        drain(hub, state)
    assert hub.pending is None and not hub.acks and not hub.patches
