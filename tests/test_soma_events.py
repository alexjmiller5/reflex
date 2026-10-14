"""Synthetic durable-delivery model exercises the public subscription contract."""

import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from websockets.exceptions import ConnectionClosedError

from core.soma_events import (
    RETRY_SECONDS,
    SomaEventConsumer,
    adopt_policy,
    listen,
    seed_projection,
)

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
        self.columns = ["state", "finished", "tags"]
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
            "sources": [{"table": "items", "columns": list(self.columns), "lifecycle": True}],
        }

    def rename(self, old, new):
        """The hub renamed a watched column and the subscription followed it."""
        assert self.acked == len(self.events) and self.pending is None
        self.columns = [new if c == old else c for c in self.columns]
        for row in self.rows.values():
            row[new] = row.pop(old)

    def change(self, values, stamp, *, operation="update", row_id="r"):
        old = copy.deepcopy(self.rows.get(row_id, {}))
        row = {**old, **values, "id": row_id, "updated_at": stamp, "hub_at": stamp}
        if operation == "delete":
            row["deleted_at"] = stamp
        if operation == "restore":
            row["deleted_at"] = None
        self.rows[row_id] = row
        changes = []
        for col in self.columns:
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
    return SomaEventConsumer(hub, state, POLICY, now=lambda: NOW).drain(DEADLINE)


@pytest.mark.parametrize("change_tags", [False, True])
def test_json_text_events_and_live_rows_match_decoded_seed(change_tags):
    hub = Hub()
    hub.rows["r"]["tags"] = ["Routine"]
    state = {}
    seed_projection(state, POLICY, {"items": list(hub.rows.values())}, through_seq="0")
    # SQL event triggers expose JSON columns as text; a reconciled seed may decode them.
    hub.rows["r"]["tags"] = '["Routine"]'
    values = {"state": "Done"}
    if change_tags:
        values["tags"] = None
    hub.change(values, "2026-01-01T01:00:00.000Z")
    result = drain(hub, state)
    assert not result["incomplete"]
    assert hub.rows["r"]["finished"] == "2026-01-01T01:00:00.000Z"
    assert hub.patches == [{"finished": "2026-01-01T01:00:00.000Z"}]


@pytest.mark.parametrize("value", ['["Other"]', '"Routine"', "broken JSON"])
def test_genuine_json_seed_mismatch_is_rejected_without_advancing(value):
    hub = Hub()
    hub.rows["r"]["tags"] = ["Routine"]
    state = {}
    seed_projection(state, POLICY, {"items": list(hub.rows.values())}, through_seq="0")
    hub.rows["r"]["tags"] = value
    hub.change({"tags": None}, "2026-01-01T01:00:00.000Z")
    with pytest.raises(ValueError, match="durable projection"):
        drain(hub, state)
    assert state["subscription:sub"]["last_seq"] == 0
    assert not hub.acks and not hub.patches


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
    result = SomaEventConsumer(hub, state, POLICY, now=lambda: DEADLINE).drain(DEADLINE)
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


def test_a_quiet_drain_after_the_first_is_one_status_request():
    hub, state = setup()
    calls = []
    for name in ("session", "subscription_status", "poll_events"):
        method = getattr(hub, name)
        setattr(hub, name, lambda *a, _m=method, _n=name: calls.append(_n) or _m(*a))
    consumer = SomaEventConsumer(hub, state, POLICY, now=lambda: NOW)
    consumer.drain(DEADLINE)
    assert calls == ["session", "subscription_status"]
    calls.clear()
    consumer.drain(DEADLINE)
    assert calls == ["subscription_status"]
    calls.clear()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
    consumer.drain(DEADLINE)
    assert hub.rows["r"]["finished"] == "2026-01-01T01:00:00.000Z"
    # One poll per batch: the user's change, then the consumer's own patch.
    assert calls.count("poll_events") == 2


class Clock:
    def __init__(self):
        self.t = NOW

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


class Socket:
    """Scripted wake socket: (seconds, message) arrivals, then "drop" or silence."""

    def __init__(self, clock, script):
        self.clock, self.script = clock, list(script)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recv(self, timeout):
        if self.script and self.script[0] == "drop":
            self.script.pop(0)
            raise ConnectionClosedError(None, None)
        if self.script and self.script[0][0] <= timeout:
            seconds, message = self.script.pop(0)
            self.clock.sleep(seconds)
            return message
        if self.script:
            self.script[0] = (self.script[0][0] - timeout, self.script[0][1])
        self.clock.sleep(timeout)
        raise TimeoutError


class Consumer:
    policy = POLICY

    def __init__(self, clock, incomplete=()):
        self.clock, self.at, self.incomplete = clock, [], list(incomplete)

    def drain(self, deadline):
        self.at.append(int((self.clock.now() - NOW).total_seconds()))
        return {"incomplete": bool(self.incomplete and self.incomplete.pop(0))}


def wake(*tables):
    return json.dumps({"seq": 7, "tables": list(tables)})


def listen_for(clock, consumer, sockets, minutes=10):
    queue = list(sockets)

    def connect():
        item = queue.pop(0) if queue else Socket(clock, [])
        if isinstance(item, Exception):
            raise item
        return item

    until = NOW + timedelta(minutes=minutes)
    return listen(consumer, connect, until, now=clock.now, sleep=clock.sleep)


def test_listen_drains_on_connect_and_on_wakes_naming_a_watched_table():
    clock = Clock()
    consumer = Consumer(clock)
    socket = Socket(clock, [(30, wake("people")), (30, wake("items", "people")), (60, wake("x"))])
    result = listen_for(clock, consumer, [socket])
    assert consumer.at == [0, 60] and result == {"incomplete": False}
    assert clock.now() == NOW + timedelta(minutes=10)


def test_a_dropped_socket_drains_and_reconnects_to_drain_again():
    clock = Clock()
    consumer = Consumer(clock)
    listen_for(clock, consumer, [Socket(clock, [(100, wake("items")), "drop"]), Socket(clock, [])])
    assert consumer.at == [0, 100, 100, 100 + RETRY_SECONDS]


def test_an_unavailable_socket_still_drains_every_retry_interval():
    clock = Clock()
    consumer = Consumer(clock)
    listen_for(clock, consumer, [OSError("refused")] * 20, minutes=3)
    assert consumer.at == [0, 60, 120]


def test_retained_work_is_retried_without_a_wake():
    clock = Clock()
    consumer = Consumer(clock, incomplete=[True])
    listen_for(clock, consumer, [Socket(clock, [])], minutes=5)
    assert consumer.at == [0, RETRY_SECONDS]


def test_an_unreadable_wake_still_drains():
    clock = Clock()
    consumer = Consumer(clock)
    listen_for(clock, consumer, [Socket(clock, [(5, "not json")])])
    assert consumer.at == [0, 5]


def test_listen_applies_a_real_event_on_its_wake():
    hub, state = setup()
    clock = Clock()

    class Live(Socket):
        def recv(self, timeout):
            message = super().recv(timeout)
            hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
            return message

    consumer = SomaEventConsumer(hub, state, POLICY, now=clock.now)
    result = listen_for(clock, consumer, [Live(clock, [(10, wake("items"))])], minutes=1)
    assert hub.rows["r"]["finished"] == "2026-01-01T01:00:00.000Z"
    assert result["pending"] == 0 and not result["incomplete"]


RENAMED = copy.deepcopy(POLICY)
RENAMED["tables"]["items"]["timestamps"] = [
    {"trigger": "state", "date": "closed", "set_on": ["Done", "Dropped"], "clear_on": ["Open"]}
]


def test_adopting_a_followed_column_rename_keeps_the_journal():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
    drain(hub, state)
    hub.rename("finished", "closed")
    with pytest.raises(ValueError, match="policy changed"):
        SomaEventConsumer(hub, state, RENAMED)
    sources = hub.subscription_status("sub")["sources"]
    adopt_policy(state, RENAMED, sources, {"items": {"finished": "closed"}})
    observed = state["subscription:sub"]["projection"]["items"]["r"]["observed"]
    assert observed == {"closed": "2026-01-01T01:00:00.000Z", "state": "Done", "tags": None}
    consumer = SomaEventConsumer(hub, state, RENAMED, now=lambda: NOW)
    hub.change({"state": "Open"}, "2026-01-01T02:00:00.000Z")
    consumer.drain(DEADLINE)
    assert hub.rows["r"]["closed"] is None
    hub.change({"state": "Dropped"}, "2026-01-01T03:00:00.000Z")
    consumer.drain(DEADLINE)
    assert hub.rows["r"]["closed"] == "2026-01-01T03:00:00.000Z"
    assert hub.patches[-2:] == [{"closed": None}, {"closed": "2026-01-01T03:00:00.000Z"}]


def test_adoption_keeps_pending_effects_under_the_new_name():
    hub, state = setup()
    hub.change({"state": "Done"}, "2026-01-01T01:00:00.000Z")
    hub.patch_row = lambda *a: (_ for _ in ()).throw(httpx.ConnectError("down"))
    with pytest.raises(httpx.ConnectError):
        drain(hub, state)
    assert state["subscription:sub"]["projection"]["items"]["r"]["pending"] == {
        "finished": "2026-01-01T01:00:00.000Z"
    }
    hub.rename("finished", "closed")
    adopt_policy(
        state, RENAMED, hub.subscription_status("sub")["sources"], {"items": {"finished": "closed"}}
    )
    assert state["subscription:sub"]["projection"]["items"]["r"]["pending"] == {
        "closed": "2026-01-01T01:00:00.000Z"
    }


@pytest.mark.parametrize("case", ["hub_not_renamed", "renames_unexplained", "not_seeded"])
def test_adoption_refuses_what_the_renames_do_not_explain(case):
    hub, state = setup()
    if case != "hub_not_renamed":
        hub.rename("finished", "closed")
    renames = {"items": {"finished": "closed"}}
    if case == "renames_unexplained":
        renames = {"items": {"finished": "done_on"}}
    if case == "not_seeded":
        state = {}
    before = copy.deepcopy(state)
    with pytest.raises(ValueError):
        adopt_policy(state, RENAMED, hub.subscription_status("sub")["sources"], renames)
    assert state == before
