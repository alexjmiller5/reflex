import copy
from dataclasses import replace
from datetime import date

import httpx
import pytest

from core.life_dispatch import dispatch_life
from core.registry import RecurringSpec, TaskTemplate

CONFIG = {
    "table": "items",
    "time_zone": "UTC",
    "columns": {
        "title": "label",
        "status": "state",
        "due": "due",
        "completed": "finished",
        "tags": "tags",
        "priority": "priority",
        "notes": "body",
        "links": "links",
        "blocked_by": "blocked_by",
    },
    "defaults": {"status": "To Do"},
    "adoptions": [],
}


class Hub:
    dry_run = False

    def __init__(self):
        self.rows = {}
        self.inserts = []
        self.fail_id = None
        self.lose_reply = False

    def pull_rows(self, table, columns):
        assert table == "items"
        return copy.deepcopy(list(self.rows.values()))

    def insert_rows(self, table, rows):
        assert table == "items"
        row = copy.deepcopy(rows[0])
        self.inserts.append(row)
        if self.fail_id == len(self.inserts):
            self.fail_id = None
            raise httpx.TimeoutException("request failed")
        existed = row["id"] in self.rows
        self.rows.setdefault(row["id"], {**row, "deleted_at": None})
        if self.lose_reply:
            self.lose_reply = False
            raise httpx.TimeoutException("committed response lost")
        return {
            "inserted": [] if existed else [row["id"]],
            "existing": [row["id"]] if existed else [],
            "rejected": [],
        }


def spec():
    return RecurringSpec(
        key="series-1",
        mode="fixed",
        anchor=date(2026, 1, 1),
        interval_months=1,
        match_titles=("Prepare", "Finish"),
        templates=(
            TaskTemplate(title="Prepare", tags=("Routine",), priority="High", key="prepare"),
            TaskTemplate(
                title="Finish",
                tags=("Routine",),
                priority="High",
                key="finish",
                blocked_by_prev=True,
            ),
        ),
    )


def run(hub, state, recurring=None, config=None, today=None):
    return dispatch_life(
        None, today or date(2026, 1, 1), (recurring or spec(),), (), hub, state, config or CONFIG
    )


def test_partial_occurrence_recovers_blocker_and_preserves_edited_first_task():
    hub, state = Hub(), {}
    hub.fail_id = 2
    with pytest.raises(httpx.TimeoutException):
        run(hub, state)
    first = next(iter(hub.rows))
    hub.rows[first]["body"] = "user edit"
    run(hub, state)
    assert len(hub.rows) == 2
    second = next(row for row in hub.rows.values() if row["id"] != first)
    assert second["blocked_by"] == [first]
    assert hub.rows[first]["body"] == "user edit"


def test_ambiguous_insert_retry_keeps_first_intent_when_template_changes():
    hub, state = Hub(), {}
    hub.lose_reply = True
    with pytest.raises(httpx.TimeoutException):
        run(hub, state)
    changed = replace(
        spec(), templates=tuple(replace(t, title=t.title + " renamed") for t in spec().templates)
    )
    run(hub, state, changed)
    assert len(hub.rows) == 2
    assert sorted(r["label"] for r in hub.rows.values()) == ["Finish", "Prepare"]


def test_complete_and_tombstoned_tasks_are_never_recreated_after_rename():
    hub, state = Hub(), {}
    run(hub, state)
    first, second = hub.rows.values()
    first["state"] = "Completed"
    second["deleted_at"] = "2026-01-02T00:00:00.000Z"
    changed = replace(
        spec(), templates=tuple(replace(t, title="New " + t.title) for t in spec().templates)
    )
    run(hub, state, changed)
    assert len(hub.rows) == 2 and len(hub.inserts) == 2


def test_adopted_target_is_authoritative_including_renamed_title():
    hub, state = Hub(), {}
    hub.rows["legacy"] = {
        "id": "legacy",
        "label": "Old title",
        "state": "Completed",
        "due": "2026-01-01",
        "finished": None,
        "deleted_at": None,
    }
    config = {
        **CONFIG,
        "adoptions": [
            {
                "spec_key": "series-1",
                "occurrence": "2026-01-01",
                "template_key": "prepare",
                "target_id": "legacy",
            }
        ],
    }
    run(hub, state, config=config)
    assert len(hub.rows) == 2
    created = next(row for row in hub.rows.values() if row["id"] != "legacy")
    assert created["blocked_by"] == ["legacy"]
    assert hub.rows["legacy"]["label"] == "Old title"


def test_missing_adopted_target_fails_before_any_writes():
    hub, state = Hub(), {}
    config = {
        **CONFIG,
        "adoptions": [
            {
                "spec_key": "series-1",
                "occurrence": "2026-01-01",
                "template_key": "prepare",
                "target_id": "missing",
            }
        ],
    }
    with pytest.raises(ValueError, match="adopted"):
        run(hub, state, config=config)
    assert not hub.inserts


def test_next_month_has_distinct_stable_occurrence_identity():
    hub, state = Hub(), {}
    run(hub, state)
    run(hub, state, today=date(2026, 2, 1))
    run(hub, state, today=date(2026, 2, 1))
    assert len(hub.rows) == 4 and len(hub.inserts) == 4


def test_dry_run_makes_no_mutation_or_journal_commit():
    hub, state = Hub(), {}
    hub.dry_run = True
    run(hub, state)
    assert state == {} and hub.inserts == [] and hub.rows == {}


def test_gift_retry_finishes_gifts_after_all_tasks_already_exist():
    class GiftHub(Hub):
        def __init__(self):
            super().__init__()
            self.gifts = {}
            self.fail_gift = True

        def pull_rows(self, table, columns):
            if table == "people":
                return [{"id": "person-1", "name": "Example Person", "deleted_at": None}]
            if table == "presents":
                return list(self.gifts.values())
            return super().pull_rows(table, columns)

        def insert_rows(self, table, rows):
            if table == "presents":
                if self.fail_gift:
                    self.fail_gift = False
                    raise httpx.TimeoutException("gift request failed")
                self.gifts.setdefault(rows[0]["id"], copy.deepcopy(rows[0]))
                return {"inserted": [rows[0]["id"]], "existing": [], "rejected": []}
            return super().insert_rows(table, rows)

    hub, state = GiftHub(), {}
    recurring = replace(spec(), templates=(), match_titles=(), gift_recipients=("person-1",))
    config = {
        **CONFIG,
        "gifts": {
            "table": "presents",
            "columns": {
                "description": "label",
                "recipient_ids": "people",
                "status": "state",
                "occasion": "occasion",
                "gift_on": "day",
                "task_ids": "tasks",
            },
            "defaults": {"status": "Open", "occasion": "Holiday"},
            "description_template": "Gift {name} {year}",
            "date_template": "{year}-12-25",
        },
    }
    with pytest.raises(httpx.TimeoutException):
        run(hub, state, recurring, config)
    assert len(hub.rows) == 2 and not hub.gifts
    run(hub, state, recurring, config)
    run(hub, state, recurring, config)
    assert len(hub.rows) == 2 and len(hub.gifts) == 1
    gift = next(iter(hub.gifts.values()))
    assert set(gift["tasks"]) == set(hub.rows)
    assert gift["people"] == ["person-1"] and gift["day"] == "2026-12-25"


def test_keepalive_retains_notion_transaction_reader_but_creates_life_task():
    from core.registry import TRANSACTIONS

    class Notion:
        def get_data_source(self, source):
            assert source == TRANSACTIONS
            return {
                "properties": {
                    "Credit Card / Account": {"select": {"options": [{"name": "Card A"}]}}
                }
            }

        def any_match(self, source, filters):
            assert source == TRANSACTIONS
            return False

        def create_page(self, *args):
            pytest.fail("no Notion task writes after selected cutover")

    hub, state = Hub(), {}
    config = {
        **CONFIG,
        "card_keys": {"Card A": "card-1"},
        "keepalive_defaults": {"tags": ["Finance"], "priority": "Medium"},
    }
    for _ in range(2):
        dispatch_life(Notion(), date(2026, 1, 1), (), ("Card A",), hub, state, config)
    assert len(hub.rows) == 1
    assert next(iter(hub.rows.values()))["tags"] == ["Finance"]


def test_dispatch_selection_never_calls_notion_task_writer():
    from core.dispatcher import dispatch

    hub, state = Hub(), {}
    dispatch(None, date(2026, 1, 1), (spec(),), (), hub, task_config=CONFIG, state=state)
    assert len(hub.rows) == 2


def test_selected_writer_requires_a_durable_journal():
    from core.dispatcher import dispatch

    hub = Hub()
    with pytest.raises(ValueError, match="journal"):
        dispatch(None, date(2026, 1, 1), (spec(),), (), hub, task_config=CONFIG)
    assert not hub.inserts


def test_retained_adoption_survives_later_configuration_removal():
    hub, state = Hub(), {}
    hub.rows["legacy"] = {
        "id": "legacy",
        "label": "Previous title",
        "state": "Completed",
        "due": "2026-01-01",
        "finished": None,
        "deleted_at": None,
    }
    config = {
        **CONFIG,
        "adoptions": [
            {
                "spec_key": "series-1",
                "occurrence": "2026-01-01",
                "template_key": "prepare",
                "target_id": "legacy",
            }
        ],
    }
    run(hub, state, config=config)
    del hub.rows["legacy"]
    # The stored occurrence already handled this target. Removing input config
    # never authorizes synthesizing a replacement ID or recreating its row.
    with pytest.raises(ValueError, match="adopted"):
        run(hub, state)
    assert len(hub.rows) == 1 and len(hub.inserts) == 1


def test_fully_imported_occurrence_retains_adoptions_without_new_creation():
    hub, state = Hub(), {}
    adoptions = []
    for key, title in [("prepare", "Original preparation"), ("finish", "Original finish")]:
        target = "legacy-" + key
        hub.rows[target] = {
            "id": target,
            "label": title,
            "state": "Completed",
            "due": "2026-01-01",
            "finished": None,
            "deleted_at": None,
        }
        adoptions.append(
            {
                "spec_key": "series-1",
                "occurrence": "2026-01-01",
                "template_key": key,
                "target_id": target,
            }
        )
    run(hub, state, config={**CONFIG, "adoptions": adoptions})
    changed = replace(
        spec(), templates=tuple(replace(t, title="Renamed " + t.title) for t in spec().templates)
    )
    run(hub, state, changed)
    assert len(hub.rows) == 2 and not hub.inserts


def test_adoption_target_cannot_be_rebound_by_later_config():
    hub, state = Hub(), {}
    for target in ["original", "replacement"]:
        hub.rows[target] = {
            "id": target,
            "label": "Old",
            "state": "Completed",
            "due": "2026-01-01",
            "finished": None,
            "deleted_at": None,
        }
    adoption = {
        "spec_key": "series-1",
        "occurrence": "2026-01-01",
        "template_key": "prepare",
        "target_id": "original",
    }
    run(hub, state, config={**CONFIG, "adoptions": [adoption]})
    before = len(hub.inserts)
    with pytest.raises(ValueError, match="adoption"):
        run(hub, state, config={**CONFIG, "adoptions": [{**adoption, "target_id": "replacement"}]})
    assert len(hub.inserts) == before
