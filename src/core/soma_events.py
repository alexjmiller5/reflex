"""Prospective event folding with durable-before-ACK state and guarded effects.

The injected state store must durably commit each assignment. Exactly one
consumer may own a subscription at a time. Seeding requires a reconciled
snapshot at its subscription checkpoint; a normal paginated scan is not one.
`listen` drains when the hub's wake socket names a watched table, instead of
polling the subscription.
"""

import copy
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
from websockets.exceptions import WebSocketException

from core.rules import evaluate_transition


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _columns(policy):
    columns = set(policy.get("creation_defaults", {}))
    for rule in policy.get("timestamps", []) + policy.get("checkbox_timestamps", []):
        columns.update((rule["trigger"], rule["date"]))
    if rule := policy.get("due_on_creation"):
        columns.update((rule["column"], rule["tags_column"]))
    if rule := policy.get("creation_outcome"):
        columns.add(rule["column"])
        columns.update(rule["when"])
    return sorted(columns)


def seed_projection(state, policy, tables, *, through_seq):
    key = "subscription:" + policy["subscription_id"]
    if key in state:
        raise ValueError("subscription already seeded")
    projection = {}
    for table, config in policy["tables"].items():
        projection[table] = {}
        for row in tables[table]:
            if row["id"] in projection[table]:
                raise ValueError("duplicate seed identity")
            projection[table][row["id"]] = {
                "observed": {col: row.get(col) for col in _columns(config)},
                "pending": {},
                "deleted": bool(row.get("deleted_at")),
            }
    state[key] = {
        "last_seq": int(through_seq),
        "policy": _digest(policy),
        "projection": projection,
        "delivery": None,
    }


def adopt_policy(state, policy, sources, renames):
    """Carry a seeded journal onto a changed policy for the SAME subscription.

    Only for a subscription whose watched columns the hub renamed (it follows a
    rename once drained): `sources` is the subscription's current status and
    `renames` maps `{table: {old: new}}`. Observed values and pending effects
    keep their data under the new names; nothing is re-derived. Anything the
    renames do not explain fails closed and leaves the journal untouched.
    """
    key = "subscription:" + policy["subscription_id"]
    if key not in state:
        raise ValueError("subscription requires a reconciled seed")
    expected = {table: _columns(config) for table, config in policy["tables"].items()}
    if {s["table"]: sorted(s["columns"]) for s in sources} != expected:
        raise ValueError("subscription sources do not match the new policy")
    data = copy.deepcopy(state[key])
    if set(data["projection"]) != set(expected):
        raise ValueError("policy tables differ from the seeded projection")
    for table, rows in data["projection"].items():
        moves = renames.get(table, {})
        for row in rows.values():
            for part in ("observed", "pending"):
                row[part] = {moves.get(col, col): value for col, value in row[part].items()}
            if sorted(row["observed"]) != expected[table]:
                raise ValueError("renames do not explain the new watched columns")
    data["policy"] = _digest(policy)
    state[key] = data


class SomaEventConsumer:
    def __init__(self, hub, state, policy, *, now=None):
        self.hub, self.state, self.policy = hub, state, policy
        self.subscription = policy["subscription_id"]
        self.key = "subscription:" + self.subscription
        self.now = now or (lambda: datetime.now(timezone.utc))
        if self.key not in state:
            raise ValueError("subscription requires a reconciled seed")
        self.data = copy.deepcopy(state[self.key])
        if self.data["policy"] != _digest(policy):
            raise ValueError("policy changed: reconcile and seed a new subscription")
        self.capable = False  # hub capabilities are checked once per consumer
        self.acked = None

    def _status(self):
        status = self.hub.subscription_status(self.subscription)
        if (
            status["id"] != self.subscription
            or status["state"] != "active"
            or status["protocol"] != "durable-pull-v1"
        ):
            raise ValueError("subscription is not active or compatible")
        self.acked = int(status["acked_seq"])
        if self.acked > self.data["last_seq"]:
            raise ValueError("subscription advanced beyond durable local state")
        sources = status.get("sources", [])
        expected = {table: _columns(config) for table, config in self.policy["tables"].items()}
        actual = {s["table"]: sorted(s["columns"]) for s in sources}
        if (
            len(sources) != len(expected)
            or actual != expected
            or any(s.get("lifecycle") is not True for s in sources)
        ):
            raise ValueError("subscription sources do not match the runtime policy")
        return int(status["last_seq"])

    def _fold(self, data, event):
        if int(event["seq"]) != data["last_seq"] + 1:
            raise ValueError("subscription sequence gap")
        table, row_id = event["source"]["table"], event["source"]["row_id"]
        config = self.policy["tables"][table]
        rows = data["projection"][table]
        operation = event["operation"]
        if operation not in {"insert", "update", "delete", "restore"}:
            raise ValueError("unknown lifecycle operation")
        if operation == "insert":
            if row_id in rows:
                raise ValueError("insert reused a seeded identity")
            rows[row_id] = {
                "observed": dict.fromkeys(_columns(config)),
                "pending": {},
                "deleted": False,
            }
        if row_id not in rows:
            raise ValueError("event row missing from reconciled seed")
        row = rows[row_id]
        before = {**row["observed"], **row["pending"]}
        changed = set()
        for change in event["changes"]:
            col = change["column"]
            if col not in row["observed"] or col in changed:
                raise ValueError("unexpected or repeated watched column")
            expected = None if row["deleted"] else row["observed"][col]
            if change["old_value"] != expected:
                raise ValueError("event does not match durable projection")
            changed.add(col)
            row["observed"][col] = change["new_value"]
            row["pending"].pop(col, None)
        row["deleted"] = operation == "delete"
        if operation in {"delete", "restore"}:
            row["pending"] = {}
        else:
            after = {**row["observed"], **row["pending"]}
            stamp = datetime.fromisoformat(event["recorded_at"].replace("Z", "+00:00"))
            row["pending"].update(
                evaluate_transition(
                    before, after, changed, stamp, config, created=operation == "insert"
                )
            )
            row["pending"] = {
                col: value
                for col, value in row["pending"].items()
                if value != row["observed"].get(col)
            }
        data["last_seq"] = int(event["seq"])

    def _delivery(self, *, dry_run=False):
        envelope = self.hub.poll_events(self.subscription)
        if envelope["subscription_id"] != self.subscription:
            raise ValueError("wrong subscription delivery")
        events = envelope["events"]
        if not events:
            if (
                envelope["delivery_id"] is not None
                or int(envelope["through_seq"]) != self.data["last_seq"]
            ):
                raise ValueError("invalid empty delivery")
            return False
        receipt = {
            "id": envelope["delivery_id"],
            "digest": _digest(envelope),
            "through": int(envelope["through_seq"]),
        }
        if not receipt["id"] or receipt["through"] != int(events[-1]["seq"]):
            raise ValueError("invalid delivery receipt")
        if receipt["through"] <= self.data["last_seq"]:
            if self.data["delivery"] != receipt:
                raise ValueError("changed or obsolete redelivery")
        else:
            candidate = copy.deepcopy(self.data)
            for event in events:
                self._fold(candidate, event)
            candidate["delivery"] = receipt
            if not dry_run:
                self.state[self.key] = candidate
            self.data = candidate
        if not dry_run:
            ack = self.hub.acknowledge(self.subscription, receipt["id"])
            if int(ack["acked_seq"]) != receipt["through"]:
                raise ValueError("unexpected acknowledgment checkpoint")
            self.acked = receipt["through"]
        return True

    def _catch_up(self, target, deadline):
        while self.data["last_seq"] < target:
            if self.now() >= deadline:
                return False
            if not self._delivery():
                raise ValueError("subscription delivery missing advertised events")
        return True

    def _pending(self):
        return [
            (table, row_id)
            for table, rows in self.data["projection"].items()
            for row_id, row in rows.items()
            if row["pending"] and not row["deleted"]
        ]

    def drain(self, deadline):
        expected = {
            "conditional_patch": "revision-v1",
            "subscriptions": "durable-pull-v1",
            "subscription_features": "scalar-lifecycle-v1",
        }
        if not self.capable:
            caps = self.hub.session()["capabilities"]
            if any(caps.get(key) != value for key, value in expected.items()):
                raise ValueError("required hub capabilities unavailable")
            self.capable = True
        target = self._status()
        if self.hub.dry_run:
            self._delivery(dry_run=True)
            return {
                "dry_run": True,
                "pending": len(self._pending()),
                "incomplete": self.data["last_seq"] < target,
            }
        # A bounded pass leaves durable pending work for the next invocation.
        for _ in range(1000):
            if self.now() >= deadline or not self._catch_up(target, deadline):
                break
            # ACK may have failed after the local commit. Reoffer before effects.
            # Every status read and ACK updates `acked`, so a quiet drain is one read.
            if self.acked < self.data["last_seq"]:
                self._delivery()
            pending = self._pending()
            if not pending:
                break
            table, row_id = pending[0]
            columns = _columns(self.policy["tables"][table])
            live = self.hub.read_row(
                table, row_id, columns + ["id", "updated_at", "hub_at", "deleted_at"]
            )
            target = self._status()  # Must follow the row read, including same-value cycles.
            if not self._catch_up(target, deadline):
                break
            row = self.data["projection"][table][row_id]
            if not row["pending"] or row["deleted"]:
                continue
            if (
                not live
                or live.get("deleted_at")
                or any(live.get(col) != row["observed"][col] for col in columns)
            ):
                continue
            revision = {key: live[key] for key in ("updated_at", "hub_at")}
            try:
                self.hub.patch_row(table, row_id, copy.deepcopy(row["pending"]), revision)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    raise
            target = self._status()
        return {
            "dry_run": False,
            "pending": len(self._pending()),
            "last_seq": self.data["last_seq"],
            "incomplete": self.data["last_seq"] < target or bool(self._pending()),
        }


RETRY_SECONDS = 60  # retained work, and polling while the wake socket is down
DRAIN_SECONDS = 40


def listen(consumer, connect, until, *, now, sleep=time.sleep):
    """Drain whenever the hub's wake socket names a watched table, until `until`.

    The change signal keeps no per-client state and never retries, so every
    (re)connect drains once to cover what a dropped socket missed. Retained
    work has no wake of its own and is retried every RETRY_SECONDS; while the
    socket is unavailable that is also the polling interval.
    """
    result = None
    watched = set(consumer.policy["tables"])
    for _ in _wakes(
        connect, watched, until, now, sleep, lambda: bool(result and result["incomplete"])
    ):
        result = consumer.drain(now() + timedelta(seconds=DRAIN_SECONDS))
    return result


def _wakes(connect, watched, until, now, sleep, retry):
    left = lambda: (until - now()).total_seconds()  # noqa: E731
    while left() > 0:
        try:
            with connect() as socket:
                yield
                while left() > 0:
                    try:
                        message = socket.recv(
                            timeout=min(left(), RETRY_SECONDS) if retry() else left()
                        )
                    except TimeoutError:
                        if retry():
                            yield
                        continue
                    if _names_watched(message, watched):
                        yield
        except (OSError, WebSocketException) as exc:
            print(f"wake socket unavailable ({exc!r}); polling every {RETRY_SECONDS}s")
            yield
            sleep(max(0.0, min(RETRY_SECONDS, left())))


def _names_watched(message, watched):
    """A wake lists the tables its commits changed; an unreadable one wakes anyway."""
    try:
        return bool(watched & set(json.loads(message)["tables"]))
    except (ValueError, KeyError, TypeError):
        return True
