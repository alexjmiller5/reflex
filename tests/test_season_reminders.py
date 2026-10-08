"""Complete inventories drive one retained reminder per watched season.

A season is finished when every episode in it has aired and is watched: an
announced or unscheduled episode holds the reminder until it airs and is
watched, so a weekly-drop season never fires after its first batch."""

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
        "air_date_column": "aired",
        "status_column": "status",
        "finished_value": "Watched",
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


def episode(key, number=1, status="Open", season=1, show="show-a", aired="2029-12-01"):
    return {
        "id": key,
        "series_id": show,
        "season": season,
        "number": number,
        "aired": aired,
        "status": status,
        "deleted_at": None,
    }


def run(hub, state, config=CONFIG, today=TODAY):
    return dispatch_seasons(hub, state, config, TASKS, today)


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


def test_announced_episode_holds_the_season_until_it_airs_and_is_watched():
    # weekly drop: the first batch is watched, the finale is announced
    hub = Hub()
    hub.tables["episodes"] = [episode("a"), episode("b", number=2, aired="2030-01-09")]
    state = {}
    run(hub, state)
    hub.tables["episodes"][0]["status"] = "Watched"
    run(hub, state)
    run(hub, state, today=date(2030, 1, 9))
    assert hub.writes == []
    hub.tables["episodes"][1]["status"] = "Watched"
    run(hub, state, today=date(2030, 1, 9))
    assert len(hub.writes) == 1
    assert hub.tables["work_items"][0]["due"] == "2030-01-09"


def test_watched_but_unaired_episode_still_holds_the_season():
    # every AIRED episode is watched, but one is dated after today
    hub = Hub()
    hub.tables["episodes"] = [episode("a"), episode("b", number=2, aired="2030-01-09")]
    state = {}
    run(hub, state)
    for row in hub.tables["episodes"]:
        row["status"] = "Watched"
    run(hub, state)
    assert hub.writes == []
    run(hub, state, today=date(2030, 1, 9))
    assert len(hub.writes) == 1


def test_unscheduled_episode_never_infers_finished_season():
    hub = Hub()
    hub.tables["episodes"] = [episode("a"), episode("b", number=2, aired=None)]
    state = {}
    run(hub, state)
    hub.tables["episodes"][0]["status"] = "Watched"
    run(hub, state)
    assert hub.writes == []


def test_title_prefix_ignores_case():
    hub = Hub()
    hub.tables["series"].append(
        {"id": "show-c", "name": "example show: Denmark", "deleted_at": None}
    )
    hub.tables["episodes"] = [episode("c", show="show-c")]
    state = {}
    run(hub, state)
    hub.tables["episodes"][0]["status"] = "Watched"
    run(hub, state)
    assert [r["title"] for r in hub.tables["work_items"]] == [
        "Read discussion for example show: Denmark, season 1"
    ]


def test_unrelated_show_is_ignored():
    hub = Hub()
    hub.tables["series"].append({"id": "show-z", "name": "Other Show", "deleted_at": None})
    hub.tables["episodes"] = [episode("z", show="show-z")]
    state = {}
    run(hub, state)
    hub.tables["episodes"][0]["status"] = "Watched"
    run(hub, state)
    assert hub.writes == []


def test_staged_config_waits_for_the_tasks_binding():
    # the config can ship before the Tasks cutover; nothing is read or saved
    hub = Mock()
    state = {}
    assert dispatch_seasons(hub, state, CONFIG, None, TODAY) == [
        "season reminders: staged until LIFE_TASKS_CONFIG selects the Life Data Tasks binding"
    ]
    assert state == {} and not hub.mock_calls


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
