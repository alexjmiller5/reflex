"""The daily walk's mirror: one cursor round per run, incremental pulls, kept tombstones."""

import json
from datetime import date

import httpx
import pytest

from core.hub import HubClient
from core.table_mirror import TableMirror

TODAY = date(2026, 10, 11)


def at(n):
    return f"2026-10-01T00:00:{n:02d}.000Z"


class Hub(HubClient):
    """Answers /v1/cursor and batched /v1/rows/pull from in-memory tables."""

    def __init__(self):
        super().__init__("https://hub.example", "test-token")
        self.tables, self.routes, self.inserted = {}, [], []

    def put(self, table, row_id, hub_at, **values):
        row = self.tables.setdefault(table, {}).setdefault(row_id, {"id": row_id})
        row.update(values, hub_at=hub_at)
        row.setdefault("deleted_at", None)

    def _post(self, path, body):
        self.routes.append(path)
        if path == "/v1/rows/insert":
            self.inserted.append(body)
            return {"inserted": [r["id"] for r in body["rows"]], "existing": [], "rejected": []}
        if path == "/v1/cursor":
            if "gone" in body["tables"]:
                raise httpx.HTTPStatusError(
                    "unknown table",
                    request=httpx.Request("POST", "https://hub.example/v1/cursor"),
                    response=httpx.Response(400),
                )
            marks = {
                t: max((r["hub_at"] for r in self.tables.get(t, {}).values()), default="")
                for t in body["tables"]
            }
            return {
                "tables": marks,
                "at_mark": {
                    t: sum(r["hub_at"] == m for r in self.tables.get(t, {}).values())
                    for t, m in marks.items()
                },
                "pull_batch": {"items": 50, "rows": 5000},
            }
        pages = []
        for item in body["batch"]:
            rows = sorted(
                (
                    {c: r.get(c) for c in item["columns"]}
                    for r in self.tables.get(item["table"], {}).values()
                    if r["hub_at"] >= item["since"] and r["id"] > item.get("after", "")
                ),
                key=lambda r: r["id"],
            )[: item["limit"]]
            pages.append(
                {
                    "rows": rows,
                    "next_cursor": rows[-1]["id"] if len(rows) == item["limit"] else None,
                }
            )
        return {"batch": pages}


def fresh_hub():
    hub = Hub()
    hub.put("tasks", "a", at(1), title="Trash", status="To Do")
    hub.put("tasks", "b", at(2), title="Laundry", status="To Do", deleted_at=at(2))
    hub.put("specs", "s", at(1), key="weekly")
    return hub


def mirror(hub, path, today=TODAY, commits=None):
    return TableMirror(
        hub, path, today=today, commit=(lambda: commits.append(1)) if commits is not None else None
    )


def run(hub, path, today=TODAY):
    """One daily run's reads: refresh, then the readers' pulls, then save."""
    m = mirror(hub, path, today)
    m.refresh()
    tasks = m.pull_rows("tasks", ("title", "status"))
    specs = m.pull_rows("specs", ("key",))
    m.save()
    return {r["id"]: r for r in tasks}, specs


def test_first_run_pulls_each_table_and_keeps_tombstones_with_their_columns(tmp_path):
    hub = fresh_hub()
    tasks, specs = run(hub, tmp_path / "mirror.json")
    assert tasks["a"]["title"] == "Trash" and not tasks["a"]["deleted_at"]
    # A deleted task still reads as one: the planner counts it as a canceled occurrence.
    assert (tasks["b"]["title"], tasks["b"]["deleted_at"]) == ("Laundry", at(2))
    assert [s["key"] for s in specs] == ["weekly"]


def test_a_quiet_run_is_one_cursor_request_for_every_table(tmp_path):
    hub = fresh_hub()
    run(hub, tmp_path / "mirror.json")
    hub.routes.clear()
    tasks, specs = run(hub, tmp_path / "mirror.json")
    assert hub.routes == ["/v1/cursor"]
    assert set(tasks) == {"a", "b"} and len(specs) == 1


def test_changes_arrive_in_one_batch_and_deletions_keep_the_row(tmp_path):
    hub = fresh_hub()
    run(hub, tmp_path / "mirror.json")
    hub.put("tasks", "a", at(5), status="Completed")
    hub.put("tasks", "c", at(5), title="Dishes", status="To Do")
    hub.put("tasks", "a", at(6), deleted_at=at(6))
    hub.put("specs", "s2", at(6), key="monthly")
    hub.routes.clear()
    tasks, specs = run(hub, tmp_path / "mirror.json")
    assert hub.routes == ["/v1/cursor", "/v1/rows/pull"]
    assert (tasks["a"]["status"], tasks["a"]["deleted_at"]) == ("Completed", at(6))
    assert tasks["c"]["title"] == "Dishes" and set(tasks) == {"a", "b", "c"}
    assert sorted(s["key"] for s in specs) == ["monthly", "weekly"]
    # A restored row is live again.
    hub.put("tasks", "a", at(7), deleted_at=None)
    tasks, _ = run(hub, tmp_path / "mirror.json")
    assert tasks["a"]["deleted_at"] is None


def test_where_filters_on_equality_without_another_request(tmp_path):
    hub = fresh_hub()
    hub.put("txns", "t1", at(1), account_id="card-1", date="2026-10-01")
    hub.put("txns", "t2", at(1), account_id="card-2", date="2026-10-02")
    m = mirror(hub, tmp_path / "mirror.json")
    m.pull_rows("txns", ("account_id", "date"))
    hub.routes.clear()
    rows = m.pull_rows("txns", ("account_id", "date"), where={"account_id": "card-2"})
    assert [r["id"] for r in rows] == ["t2"] and hub.routes == []


def test_a_wider_column_set_is_pulled_once_and_kept(tmp_path):
    hub = fresh_hub()
    path = tmp_path / "mirror.json"
    run(hub, path)
    m = mirror(hub, path)
    m.refresh()
    m.pull_rows("tasks", ("title", "status"))
    m.pull_rows("specs", ("key",))
    hub.routes.clear()
    rows = m.pull_rows("tasks", ("title", "priority"))
    assert hub.routes == ["/v1/cursor", "/v1/rows/pull"]
    assert {"title", "status", "priority"} <= set(rows[0])
    m.save()
    hub.routes.clear()
    # Widening one table keeps every other table's cursor.
    m = mirror(hub, path)
    m.refresh()
    m.pull_rows("tasks", ("priority",))
    m.pull_rows("specs", ("key",))
    assert hub.routes == ["/v1/cursor"]


def test_save_drops_tables_this_run_did_not_read_and_commits(tmp_path):
    hub = fresh_hub()
    path, commits = tmp_path / "mirror.json", []
    run(hub, path)
    m = mirror(hub, path, commits=commits)
    m.refresh()
    m.pull_rows("tasks", ("title", "status"))
    m.save()
    assert commits and set(json.loads(path.read_text())["rows"]) == {"tasks"}


def test_a_week_old_mirror_is_pulled_again_in_full_in_one_round(tmp_path):
    hub = fresh_hub()
    path = tmp_path / "mirror.json"
    run(hub, path, today=date(2026, 10, 1))
    hub.tables["tasks"].pop("a")  # an older row purged: invisible to incremental pulls
    hub.routes.clear()
    tasks, _ = run(hub, path, today=date(2026, 10, 7))
    assert hub.routes == ["/v1/cursor"] and "a" in tasks
    tasks, _ = run(hub, path, today=date(2026, 10, 8))
    assert hub.routes[-2:] == ["/v1/cursor", "/v1/rows/pull"] and set(tasks) == {"b"}


def test_a_held_table_the_hub_refuses_drops_the_mirror_instead_of_failing(tmp_path):
    hub = fresh_hub()
    path = tmp_path / "mirror.json"
    m = mirror(hub, path)
    m.pull_rows("tasks", ("title", "status"))
    m.save()
    stored = json.loads(path.read_text())
    stored["columns"]["gone"] = ["id", "title"]
    path.write_text(json.dumps(stored))
    tasks, _ = run(hub, path)
    assert set(tasks) == {"a", "b"}
    assert "gone" not in json.loads(path.read_text())["columns"]


def test_an_unreadable_mirror_file_starts_cold(tmp_path):
    hub = fresh_hub()
    path = tmp_path / "mirror.json"
    path.write_text("{not json")
    tasks, _ = run(hub, path)
    assert set(tasks) == {"a", "b"}


def test_writes_pass_through_to_the_hub(tmp_path):
    hub = fresh_hub()
    m = mirror(hub, tmp_path / "mirror.json")
    m.insert_rows("tasks", [{"id": "n", "title": "New"}])
    assert hub.inserted and m.dry_run is False


def test_failed_pull_leaves_the_saved_mirror_untouched(tmp_path):
    hub = fresh_hub()
    path = tmp_path / "mirror.json"
    run(hub, path)
    before = path.read_text()
    hub.put("tasks", "a", at(9), status="Completed")

    def broken(path_, body):
        raise httpx.ConnectError("hub unreachable")

    hub._post = broken
    with pytest.raises(httpx.ConnectError):
        m = mirror(hub, path)
        m.refresh()
        m.pull_rows("tasks", ("title", "status"))
    assert path.read_text() == before
