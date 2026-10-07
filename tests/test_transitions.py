"""Prospective workflow rules consume runtime policy, never historical guesses."""

from datetime import datetime, timezone

import pytest

from core.rules import evaluate_transition


WHEN = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc)
STAMP = "2026-01-02T03:04:05.678Z"
POLICY = {
    "time_zone": "America/Los_Angeles",
    "timestamps": [
        {
            "trigger": "state",
            "date": "finished",
            "set_on": ["Done"],
            "clear_on": ["Open", "Working"],
        }
    ],
    "checkbox_timestamps": [{"trigger": "fixed", "date": "fixed_at"}],
    "creation_defaults": {"tags": ["Routine"], "priority": "Urgent"},
    "due_on_creation": {"column": "due", "tags_column": "tags", "excluded_tags": ["At store"]},
}


def run(before, after, changed=(), *, created=False, policy=POLICY, when=WHEN):
    return evaluate_transition(before, after, set(changed), when, policy, created)


def test_completion_records_event_time_and_reopen_clears_it():
    assert run({"state": "Open"}, {"state": "Done"}, ["state"]) == {"finished": STAMP}
    assert run(
        {"state": "Done", "finished": STAMP}, {"state": "Open", "finished": STAMP}, ["state"]
    ) == {"finished": None}


@pytest.mark.parametrize("state", ["Done", "Canceled", "Open"])
def test_unrelated_edit_never_backfills_or_clears_historical_dates(state):
    before = {"state": state, "finished": None, "title": "Old"}
    assert run(before, {**before, "title": "New"}, ["title"]) == {}


def test_canceled_does_not_invent_or_clear_a_completion_date():
    assert run({"state": "Open"}, {"state": "Canceled"}, ["state"]) == {}
    assert (
        run(
            {"state": "Done", "finished": STAMP},
            {"state": "Canceled", "finished": STAMP},
            ["state"],
        )
        == {}
    )


def test_existing_or_explicitly_edited_dates_are_preserved():
    assert run({"state": "Open"}, {"state": "Done", "finished": "2025-01-01"}, ["state"]) == {}
    assert (
        run(
            {"state": "Done", "finished": STAMP},
            {"state": "Open", "finished": "2025-01-01"},
            ["state", "finished"],
        )
        == {}
    )


def test_numeric_checkbox_set_and_clear_follow_their_own_transition():
    assert run({"fixed": 0}, {"fixed": 1}, ["fixed"]) == {"fixed_at": STAMP}
    assert run({"fixed": 1, "fixed_at": STAMP}, {"fixed": 0, "fixed_at": STAMP}, ["fixed"]) == {
        "fixed_at": None
    }
    assert run({"fixed": 1}, {"fixed": 1, "title": "Changed"}, ["title"]) == {}


def test_creation_defaults_use_configured_zone_without_changing_provided_values():
    assert run({}, {}, created=True) == {
        "tags": ["Routine"],
        "priority": "Urgent",
        "due": "2026-01-01",
    }
    assert (
        run({}, {"tags": ["Provided"], "priority": "Low", "due": "2026-03-01"}, created=True) == {}
    )


def test_place_tagged_creation_remains_undated_for_array_or_json_storage():
    for tags in [["At store"], '["At store"]']:
        assert run({}, {"tags": tags, "priority": "Low"}, created=True) == {}


def test_existing_empty_values_do_not_gain_creation_defaults():
    assert run({}, {"tags": [], "priority": None, "due": None}, ["tags"]) == {}


def test_opt_in_task_day_boundary_uses_local_wall_time_across_dst():
    policy = {
        "time_zone": "America/New_York",
        "day_start_minutes": 180,
        "due_on_creation": {"column": "due"},
    }
    assert run(
        {},
        {},
        created=True,
        policy=policy,
        when=datetime.fromisoformat("2026-03-08T06:59:00+00:00"),
    ) == {"due": "2026-03-07"}
    assert run(
        {},
        {},
        created=True,
        policy=policy,
        when=datetime.fromisoformat("2026-03-08T07:00:00+00:00"),
    ) == {"due": "2026-03-08"}
    assert run(
        {},
        {},
        created=True,
        policy=policy,
        when=datetime.fromisoformat("2026-11-01T07:59:00+00:00"),
    ) == {"due": "2026-10-31"}
    assert run(
        {},
        {},
        created=True,
        policy=policy,
        when=datetime.fromisoformat("2026-11-01T08:00:00+00:00"),
    ) == {"due": "2026-11-01"}


def test_successful_creation_review_rule_uses_effective_outcome_and_keeps_manual_review():
    policy = {
        "time_zone": "UTC",
        "creation_outcome": {
            "column": "outcome",
            "default": "Pending",
            "replaceable": [None, "Pending"],
            "when": {"result": "Success", "category": "bookmark"},
            "value": "Accepted",
        },
        "timestamps": [
            {
                "trigger": "outcome",
                "date": "reviewed",
                "set_on": ["Accepted", "Bug"],
                "clear_on": ["Pending"],
            }
        ],
    }
    assert run({}, {"result": "Success", "category": "bookmark"}, created=True, policy=policy) == {
        "outcome": "Accepted",
        "reviewed": STAMP,
    }
    assert run({}, {"result": "Error", "category": "bookmark"}, created=True, policy=policy) == {
        "outcome": "Pending"
    }
    assert (
        run(
            {},
            {
                "result": "Success",
                "category": "bookmark",
                "outcome": "Bug",
                "reviewed": "2025-01-01",
            },
            created=True,
            policy=policy,
        )
        == {}
    )


def test_invalid_timezone_or_naive_time_fails_instead_of_guessing():
    with pytest.raises(ValueError):
        run({}, {}, created=True, when=WHEN.replace(tzinfo=None))
    with pytest.raises(Exception):
        run({}, {}, created=True, policy={"time_zone": "not/a/zone"})


def test_complete_reopen_complete_uses_the_second_completion_event():
    row = {"state": "Open", "finished": None}
    stamps = []
    for state, when in [
        ("Done", "2026-01-01T01:00:00+00:00"),
        ("Open", "2026-01-01T02:00:00+00:00"),
        ("Done", "2026-01-01T03:00:00+00:00"),
    ]:
        after = {**row, "state": state}
        after.update(run(row, after, ["state"], when=datetime.fromisoformat(when)))
        row = after
        stamps.append(row["finished"])
    assert stamps == ["2026-01-01T01:00:00.000Z", None, "2026-01-01T03:00:00.000Z"]
