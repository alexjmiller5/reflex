import json
from datetime import datetime, timezone

import pytest

from core.event_state import locked_state
from core.tick import run_tick


def test_state_survives_reopen_and_flush_precedes_return(tmp_path):
    path = tmp_path / "journal.json"
    flushed = []
    with locked_state(path, commit=lambda: flushed.append(json.loads(path.read_text()))) as state:
        state["checkpoint"] = {"seq": 3, "pending": ["r"]}
        assert flushed == [{"checkpoint": {"seq": 3, "pending": ["r"]}}]
        value = state["checkpoint"]
        value["seq"] = 4
        assert state["checkpoint"]["seq"] == 3
    with locked_state(path) as reopened:
        assert reopened["checkpoint"] == {"seq": 3, "pending": ["r"]}


def test_overlapping_invocations_cannot_open_the_journal(tmp_path):
    path = tmp_path / "journal.json"
    with locked_state(path):
        with pytest.raises(BlockingIOError):
            with locked_state(path):
                pytest.fail("concurrent owner admitted")
    with locked_state(path) as state:
        state["ok"] = True


def test_failed_serialization_preserves_previous_checkpoint(tmp_path):
    path = tmp_path / "journal.json"
    with locked_state(path) as state:
        state["checkpoint"] = 3
        with pytest.raises(ValueError):
            state["checkpoint"] = float("nan")
    assert json.loads(path.read_text()) == {"checkpoint": 3}


def test_remote_commit_failure_is_not_reported_as_success(tmp_path):
    def fail():
        raise OSError("volume commit failed")

    with locked_state(tmp_path / "journal.json", commit=fail) as state:
        with pytest.raises(OSError):
            state["checkpoint"] = 3
        with pytest.raises(RuntimeError, match="reopen"):
            _ = state["checkpoint"]


def at(day, hour, minute=0):
    return datetime(2026, 1, day, hour, minute, tzinfo=timezone.utc)


def test_single_daily_slot_preserves_1130_utc_and_survives_restart(tmp_path):
    path = tmp_path / "journal.json"
    calls = []
    for now in [at(2, 11, 29), at(2, 11, 30), at(2, 12), at(3, 9), at(3, 13)]:
        with locked_state(path) as state:
            run_tick(state, now, lambda: calls.append(now), consume=lambda: None)
    assert calls == [at(2, 11, 30), at(3, 13)]


def test_delayed_tick_runs_latest_due_day_once_without_cron_backlog():
    state = {"daily_success": "2026-01-01"}
    calls = []
    run_tick(state, at(5, 3), lambda: calls.append("daily"))
    run_tick(state, at(5, 4), lambda: calls.append("daily"))
    assert calls == ["daily"] and state["daily_success"] == "2026-01-04"


@pytest.mark.parametrize("failed_phase", ["consume", "daily"])
def test_failed_tick_preserves_daily_marker(failed_phase):
    state = {"daily_success": "2026-01-01"}

    def fail():
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError):
        run_tick(
            state,
            at(2, 12),
            fail if failed_phase == "daily" else lambda: None,
            consume=fail if failed_phase == "consume" else lambda: None,
        )
    assert state["daily_success"] == "2026-01-01"


def test_dry_run_never_advances_daily_success():
    state = {}
    calls = []
    run_tick(state, at(2, 12), lambda: calls.append("preview"), dry_run=True)
    assert calls == ["preview"] and state == {}
