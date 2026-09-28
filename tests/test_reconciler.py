"""Compliance reconciler tests. reconcile() only ever flags violations as
remediation tasks in TASKS - it never writes fixes back to the source page
(a human backfills the true event timestamps; the reconciler can't know
them)."""

from datetime import datetime, timezone

from core import registry as R
from core.reconciler import reconcile

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


class FakeNotion:
    def __init__(self, pages_by_ds, pages_by_id=None):
        self.p, self.created = pages_by_ds, []
        self._by_id = pages_by_id or {}

    def query(self, ds, filter=None, **kw):
        return self.p.get(ds, [])

    def get_page(self, page_id):
        return self._by_id[page_id]

    def create_page(self, ds, props, icon=None):
        self.created.append((ds, props))
        return {"id": "t"}


def bad_project(pid="b1"):
    return {
        "id": pid,
        "url": f"https://notion.so/{pid}",
        "parent": {"data_source_id": R.PROJECTS},
        "properties": {
            "Status": {"status": {"name": "Completed"}},
            "Completed Date": {"date": None},
            "Name": {"title": [{"plain_text": "Bad Project"}]},
        },
    }


def test_violation_creates_remediation_task_not_fix():
    fake = FakeNotion({R.PROJECTS: [bad_project()]})
    logs, mark = reconcile(fake, frozenset({R.PROJECTS}), "2026-08-19T12:00:00+00:00", NOW)
    assert len(fake.created) == 1
    ds, props = fake.created[0]
    assert ds == R.TASKS
    title = props["Name"]["title"][0]["text"]["content"]
    assert "Bad Project" in title and "projects-completed-date-set" in title
    assert props["Priority"]["select"]["name"] == "High"
    assert props["Tags"]["multi_select"] == [{"name": "Chore"}]
    assert props["Links"]["rich_text"][0]["text"]["content"] == "https://notion.so/b1"
    assert mark == NOW.isoformat()


def test_compliant_pages_create_nothing():
    good = bad_project()
    good["properties"]["Completed Date"] = {"date": {"start": "2026-08-01"}}
    fake = FakeNotion({R.PROJECTS: [good]})
    logs, _ = reconcile(fake, frozenset({R.PROJECTS}), "2026-08-19T12:00:00+00:00", NOW)
    assert fake.created == []


def test_one_task_per_page_even_with_multiple_violations():
    p = bad_project()
    fake = FakeNotion({R.TASKS: [], R.PROJECTS: [p, p]})
    logs, _ = reconcile(fake, frozenset({R.PROJECTS}), "2026-08-19T12:00:00+00:00", NOW)
    assert len(fake.created) == 1  # dedupe by page id within a run


class FlakyNotion(FakeNotion):
    """One data source is unreachable - Notion answers 404 for DBs an
    integration isn't connected to (it hides their existence)."""

    def __init__(self, pages_by_ds, broken_ds, pages_by_id=None):
        super().__init__(pages_by_ds, pages_by_id)
        self.broken = broken_ds

    def query(self, ds, filter=None, **kw):
        if ds == self.broken:
            raise RuntimeError("Client error '404 Not Found'")
        return super().query(ds, filter=filter, **kw)


def test_unreachable_db_does_not_abort_the_other_sweeps():
    fake = FlakyNotion({R.PROJECTS: [bad_project()]}, broken_ds=R.TASKS)
    logs, mark = reconcile(fake, frozenset({R.TASKS, R.PROJECTS}), "2026-08-19T12:00:00+00:00", NOW)
    # Projects was still swept and flagged despite Tasks blowing up first
    assert len(fake.created) == 1
    assert any("SWEEP FAILED" in line and R.TASKS in line for line in logs)
    # ...and the window stays open so the missed DB is retried next run
    assert mark is None


def test_all_reachable_returns_a_mark():
    fake = FlakyNotion({R.PROJECTS: [bad_project()]}, broken_ds="not-a-real-ds")
    _, mark = reconcile(fake, frozenset({R.PROJECTS}), "2026-08-19T12:00:00+00:00", NOW)
    assert mark == NOW.isoformat()
