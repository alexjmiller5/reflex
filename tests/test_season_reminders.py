"""Complete inventories drive one retained reminder per watched season."""

import copy
from datetime import date
from unittest.mock import Mock

import pytest

from core.season_reminders import dispatch_seasons


CONFIG = {
    "shows": {"table": "series", "title_column": "name"},
    "episodes": {
        "table": "episodes",
        "show_column": "series_id",
        "season_column": "season",
        "number_column": "number",
        "status_column": "status",
        "finished_value": "Watched",
    },
    "seasons": {
        "table": "season_totals",
        "show_column": "series_id",
        "season_column": "season",
        "total_column": "total",
    },
    "title_prefixes": ["Example Show"],
    "title_template": "Read discussion for {title}, season {season}",
    "task_values": {"status": "Open", "priority": "High"},
}
TASKS = {"table": "work_items", "columns": {"title": "title", "due": "due"}}
TODAY = date(2030, 1, 2)


class Hub:
    dry_run = False

    def __init__(self):
        self.tables = {
            "series": [
                {"id": "show-a", "name": "Example Show", "deleted_at": None},
                {"id": "show-b", "name": "Example Show: Regional", "deleted_at": None},
            ],
            "season_totals": [
                {"id": "season-a", "series_id": "show-a", "season": 1, "total": 1},
                {"id": "season-b", "series_id": "show-b", "season": 1, "total": 1},
            ],
            "episodes": [],
            "work_items": [],
        }
        self.writes = []
        self.fail = False

    def pull_rows(self, table, columns):
        return copy.deepcopy(self.tables[table])

    def insert_rows(self, table, rows):
        self.writes.append(copy.deepcopy(rows))
        existing = {r["id"] for r in self.tables[table]}
        self.tables[table] += [copy.deepcopy(r) for r in rows if r["id"] not in existing]
        if self.fail:
            raise TimeoutError("Committed, receipt lost")
        return {
            "inserted": [r["id"] for r in rows if r["id"] not in existing],
            "existing": [r["id"] for r in rows if r["id"] in existing],
            "rejected": [],
        }


def episode(key, number=1, status="Open", season=1, show="show-a"):
    return {
        "id": key,
        "series_id": show,
        "season": season,
        "number": number,
        "status": status,
        "deleted_at": None,
    }


def run(hub, state, config=CONFIG):
    return dispatch_seasons(hub, state, config, TASKS, TODAY)


def test_baseline_does_not_flood_old_finished_seasons():
    hub = Hub()
    hub.tables["episodes"] = [episode("old", status="Watched")]
    state = {}
    run(hub, state)
    run(hub, state)
    assert hub.writes == []
    assert state


def test_new_completion_includes_regional_editions_and_ignores_specials():
    hub = Hub()
    hub.tables["episodes"] = [
        episode("a"),
        episode("b", show="show-b"),
        episode("special", season=0),
    ]
    state = {}
    run(hub, state)
    for row in hub.tables["episodes"]:
        row["status"] = "Watched"
    run(hub, state)
    run(hub, state)
    assert len(hub.tables["work_items"]) == 2
    assert len({r["id"] for r in hub.tables["work_items"]}) == 2
    assert all(r["due"] == TODAY.isoformat() for r in hub.tables["work_items"])


def test_partial_season_gap_and_unwatched_episode_do_not_complete():
    hub = Hub()
    hub.tables["season_totals"][0]["total"] = 3
    state = {}
    run(hub, state)
    hub.tables["episodes"] = [
        episode("a", status="Watched"),
        episode("c", number=3, status="Watched"),
    ]
    run(hub, state)
    assert hub.writes == []
    hub.tables["episodes"].append(episode("b", number=2))
    run(hub, state)
    assert hub.writes == []
    hub.tables["episodes"][-1]["status"] = "Watched"
    run(hub, state)
    assert len(hub.writes) == 1


def test_lost_ack_freezes_body_and_identity_and_preserves_user_completion():
    hub = Hub()
    hub.tables["episodes"] = [episode("a")]
    state = {}
    run(hub, state)
    hub.tables["episodes"][0]["status"] = "Watched"
    hub.fail = True
    with pytest.raises(TimeoutError):
        run(hub, state)
    original = copy.deepcopy(hub.writes[0])
    hub.fail = False
    hub.tables["work_items"][0].update(status="Completed", deleted_at="2030-01-03T00:00:00.000Z")
    changed = {**CONFIG, "title_template": "Changed {title}"}
    run(hub, state, changed)
    assert len(hub.tables["work_items"]) == 1
    assert hub.tables["work_items"][0]["status"] == "Completed"
    assert hub.tables["work_items"][0]["deleted_at"]
    assert hub.writes[0] == original


def test_inventory_failure_and_duplicate_identity_precede_any_state_or_write():
    hub = Hub()
    state = {}
    hub.tables["episodes"] = [episode("a"), episode("a")]
    with pytest.raises(ValueError):
        run(hub, state)
    assert state == {} and hub.writes == []
    hub.pull_rows = Mock(side_effect=RuntimeError("Incomplete page chain"))
    with pytest.raises(RuntimeError):
        run(hub, state)
    assert state == {} and hub.writes == []


def test_deleted_or_disappeared_episode_cannot_fake_completion():
    hub = Hub()
    hub.tables["episodes"] = [episode("a", status="Watched"), episode("b", number=2)]
    state = {}
    run(hub, state)
    hub.tables["episodes"] = [hub.tables["episodes"][0]]
    run(hub, state)
    assert hub.writes == []


def test_dry_run_does_not_save_baseline_or_plan():
    hub = Hub()
    hub.dry_run = True
    state = {}
    run(hub, state)
    assert state == {} and hub.writes == []


def test_invalid_late_plan_does_not_publish_partial_intent():
    hub = Hub()
    hub.tables["episodes"] = [episode("a")]
    state = {}
    run(hub, state)
    before = copy.deepcopy(state)
    hub.tables["episodes"][0]["status"] = "Watched"
    with pytest.raises(KeyError):
        run(hub, state, {**CONFIG, "title_template": "{unknown}"})
    assert state == before and hub.writes == []


def test_duplicate_episode_number_fails_before_baseline():
    hub = Hub()
    hub.tables["episodes"] = [episode("a"), episode("b")]
    state = {}
    with pytest.raises(ValueError):
        run(hub, state)
    assert state == {} and hub.writes == []


def test_short_released_inventory_waits_for_declared_season_total():
    hub = Hub()
    hub.tables["episodes"] = [episode("a")]
    hub.tables["season_totals"] = [
        {"id": "season-a", "series_id": "show-a", "season": 1, "total": 2}
    ]
    config = {
        **CONFIG,
        "seasons": {
            "table": "season_totals",
            "show_column": "series_id",
            "season_column": "season",
            "total_column": "total",
        },
    }
    state = {}
    run(hub, state, config)
    hub.tables["episodes"][0]["status"] = "Watched"
    run(hub, state, config)
    assert hub.writes == []
    hub.tables["episodes"].append(episode("b", number=2, status="Watched"))
    run(hub, state, config)
    assert len(hub.writes) == 1


def test_missing_total_never_infers_finished_season():
    hub = Hub()
    hub.tables["season_totals"] = []
    hub.tables["episodes"] = [episode("a")]
    state = {}
    run(hub, state)
    hub.tables["episodes"][0]["status"] = "Watched"
    run(hub, state)
    assert hub.writes == []


def test_failed_task_inventory_prevents_baseline():
    hub = Hub()
    original = hub.pull_rows

    def pull(table, columns):
        if table == "work_items":
            raise RuntimeError("incomplete tasks")
        return original(table, columns)

    hub.pull_rows = pull
    state = {}
    with pytest.raises(RuntimeError):
        run(hub, state)
    assert state == {} and hub.writes == []
