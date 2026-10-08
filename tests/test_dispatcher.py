from dataclasses import replace
from datetime import date

import pytest

from core.dispatcher import _hydrate_recipients, dispatch
from core.planner import TaskSnapshot
from core.registry import TASKS, KeepaliveCard, RecurringSpec, TaskTemplate, cc_keepalive_title

RECIPIENTS = ("r1", "r2", "r3", "r4", "r5")
CARDS = (KeepaliveCard("Card A", "acct-a"), KeepaliveCard("Card B", "acct-b"))
ACCOUNTS = [
    {"id": "acct-a", "source": "bank", "deleted_at": None},
    {"id": "acct-b", "source": "bank", "deleted_at": None},
]


def txn(account, day, **extra):
    return {
        "id": f"{account}-{day}",
        "account_id": account,
        "date": day,
        "synthetic": None,
        "deleted_at": None,
    } | extra


# Recipient names live in life-data, not the repo, so the fake hub serves the
# people rows and records the gift rows it is asked to push.
FAKE_PEOPLE = [
    {"id": pid, "name": f"Person{i} Surname", "deleted_at": None}
    for i, pid in enumerate(RECIPIENTS)
]


class FakeHub:
    """Serves people for the gift generator and the life-data finance tables
    (accounts + txns_<source>) the keepalive reads; every card is active
    unless a test replaces `txns`."""

    def __init__(self, reject=None, txns=None):
        self.pushed, self.reject = [], reject
        self.txns = (
            txns if txns is not None else [txn("acct-a", "2026-08-01"), txn("acct-b", "2026-08-01")]
        )
        self.pulls = []

    def pull_rows(self, table, columns, *, since="", where=None):
        self.pulls.append((table, where))
        if table == "people":
            return FAKE_PEOPLE
        if table == "accounts":
            return ACCOUNTS
        assert table == "txns_bank" and set(where) == {"account_id"}
        return [r for r in self.txns if r["account_id"] == where["account_id"]]

    def push_rows(self, table, rows):
        self.pushed.append((table, rows[0]))
        return {"upserted": 1, "rejected": self.reject or []}


PLANTS = RecurringSpec(
    key="water-plants",
    mode="relative",
    interval_months=3,
    anchor=date(2026, 8, 2),
    match_titles=("Water the office plants",),
    templates=(TaskTemplate(title="Water the office plants", tags=("Chore",), priority="High"),),
)
CHRISTMAS = RecurringSpec(
    key="christmas",
    mode="fixed",
    interval_months=12,
    anchor=date(2026, 10, 15),
    match_titles=(),
    templates=(),
    gift_recipients=RECIPIENTS,
)
SPECS = (PLANTS, CHRISTMAS)


class FakeNotion:
    """Tasks only: card activity comes from life-data, so this fake has no
    query or schema methods - a Notion Transactions read would raise."""

    def __init__(self, snaps=None):
        self._snaps = snaps or {}
        self.created = []

    def snapshots(self, ds, titles):
        return self._snaps.get(titles, [])

    def create_page(self, ds, properties, icon=None):
        self.created.append((ds, properties))
        return {"id": f"page-{len(self.created)}", "url": "u"}


def test_dispatch_creates_due_relative_task():
    fake = FakeNotion()  # no history -> every relative spec fires from anchor
    dispatch(fake, date(2026, 8, 20), SPECS, CARDS, FakeHub())
    titles = [p["Name"]["title"][0]["text"]["content"] for _, p in fake.created]
    assert "Water the office plants" in titles


def test_dispatch_open_task_suppresses():
    fake = FakeNotion()
    specs = [_hydrate_recipients(s, FakeHub())[0] if s.gift_recipients else s for s in SPECS]
    fake._snaps = {
        s.match_titles: [TaskSnapshot(t, "To Do", None, None) for t in s.match_titles]
        for s in specs
    }
    dispatch(fake, date(2026, 8, 20), SPECS, CARDS, FakeHub())
    assert fake.created == []


def test_christmas_creates_gifts_and_blocked_chain():
    fake, hub = FakeNotion(), FakeHub()
    dispatch(fake, date(2026, 10, 15), SPECS, CARDS, hub)
    buys = [p for _, p in fake.created if "Blocked by" in p]
    assert len(hub.pushed) == 5 and len(buys) == 5
    table, row = hub.pushed[0]
    assert table == "gifts" and row["gift_on"] == "2026-12-25"  # computed year
    assert row["description"] == "Person0 Surname's Christmas Gift 2026"
    assert row["recipient_ids"] == ["r1"] and row["status"] == "To Do"
    assert len(row["id"]) == 32 and row["updated_at"].endswith("Z")


def test_rejected_gift_row_fails_the_run():
    with pytest.raises(RuntimeError, match="rejected"):
        dispatch(
            FakeNotion(), date(2026, 10, 15), (CHRISTMAS,), CARDS, FakeHub(reject=[{"col": "x"}])
        )


def test_christmas_partial_batch_skips_existing_template_but_finishes_the_rest():
    # simulates a prior run that crashed after creating just the first
    # person's Brainstorm task: only that one template + due already exists,
    # everything else (9 tasks + 5 gifts) is still missing and must be
    # created on retry, without duplicating the one that's already there
    spec, _ = _hydrate_recipients(CHRISTMAS, FakeHub())
    existing_template = spec.templates[0]
    existing_due = spec.anchor  # due_offset_days=0 for the first (Brainstorm) template
    snaps = {
        spec.match_titles: [TaskSnapshot(existing_template.title, "To Do", existing_due, None)]
    }
    fake, hub = FakeNotion(snaps=snaps), FakeHub()
    dispatch(fake, spec.anchor, (CHRISTMAS,), CARDS, hub)
    christmas_titles = [
        p["Name"]["title"][0]["text"]["content"]
        for _, p in fake.created
        if "Name" in p and p["Name"]["title"][0]["text"]["content"] in spec.match_titles
    ]
    assert christmas_titles.count(existing_template.title) == 0  # not duplicated
    assert len(christmas_titles) == len(spec.templates) - 1  # every other task created
    assert len(hub.pushed) == 5  # gifts still created in full


def test_hydration_builds_the_brainstorm_then_buy_pairs():
    spec, full_names = _hydrate_recipients(CHRISTMAS, FakeHub())
    assert len(spec.templates) == 10 and len(full_names) == 5
    buys = [t for t in spec.templates if t.blocked_by_prev]
    assert len(buys) == 5 and all(t.due_offset_days == 30 for t in buys)
    # first names only in task titles, full names for the Gifts page titles
    assert (
        spec.templates[0].title
        == "Brainstorm and come up with an idea for Person0's Christmas Gift"
    )
    assert full_names[RECIPIENTS[0]] == "Person0 Surname"
    assert spec.match_titles == tuple(t.title for t in spec.templates)


def keepalive_titles(fake):
    return [
        p["Name"]["title"][0]["text"]["content"]
        for _, p in fake.created
        if "no transactions in the past year" in p["Name"]["title"][0]["text"]["content"]
    ]


def test_keepalive_creates_task_for_inactive_card_only():
    # Card B's last life-data transaction is older than a year
    fake = FakeNotion()
    hub = FakeHub(txns=[txn("acct-a", "2026-08-01"), txn("acct-b", "2025-08-24")])
    dispatch(fake, date(2026, 8, 25), SPECS, CARDS, hub)
    keepalive = [
        (ds, p)
        for ds, p in fake.created
        if "no transactions in the past year" in p["Name"]["title"][0]["text"]["content"]
    ]
    assert len(keepalive) == 1
    ds, p = keepalive[0]
    assert ds == TASKS
    assert p["Name"]["title"][0]["text"]["content"] == cc_keepalive_title("Card B")
    assert p["Due Date"]["date"]["start"] == "2026-08-25"
    assert p["Tags"]["multi_select"] == [{"name": "Finances"}]


def test_keepalive_reads_each_card_from_its_accounts_source_table():
    hub = FakeHub()
    dispatch(FakeNotion(), date(2026, 8, 25), (), CARDS, hub)
    assert ("accounts", None) in hub.pulls
    assert ("txns_bank", {"account_id": "acct-a"}) in hub.pulls
    assert ("txns_bank", {"account_id": "acct-b"}) in hub.pulls


def test_keepalive_counts_a_transaction_exactly_one_year_old():
    hub = FakeHub(txns=[txn("acct-a", "2025-08-25"), txn("acct-b", "2025-08-25")])
    fake = FakeNotion()
    dispatch(fake, date(2026, 8, 25), (), CARDS, hub)
    assert keepalive_titles(fake) == []


def test_keepalive_ignores_synthetic_deleted_and_other_account_rows():
    # an opening-balance row, a soft-deleted row and a row the hub returned
    # for a different account are not activity on Card A
    hub = FakeHub(
        txns=[
            txn("acct-a", "2026-08-01", synthetic=1),
            txn("acct-a", "2026-08-02", deleted_at="2026-08-03T00:00:00.000Z"),
            txn("acct-b", "2026-08-01"),
        ]
    )
    original = hub.pull_rows

    def leaky(table, columns, *, since="", where=None):
        rows = original(table, columns, since=since, where=where)
        return rows + [txn("acct-b", "2026-08-04")] if table == "txns_bank" else rows

    hub.pull_rows = leaky
    fake = FakeNotion()
    dispatch(fake, date(2026, 8, 25), (), CARDS, hub)
    assert keepalive_titles(fake) == [cc_keepalive_title("Card A")]


def test_keepalive_card_with_no_transactions_is_inactive():
    hub = FakeHub(txns=[txn("acct-a", "2026-08-01")])
    fake = FakeNotion()
    dispatch(fake, date(2026, 8, 25), (), CARDS, hub)
    assert keepalive_titles(fake) == [cc_keepalive_title("Card B")]


def test_keepalive_open_task_suppresses():
    title = cc_keepalive_title("Card B")
    fake = FakeNotion(snaps={(title,): [TaskSnapshot(title, "To Do", date(2026, 8, 1), None)]})
    hub = FakeHub(txns=[txn("acct-a", "2026-08-01")])
    dispatch(fake, date(2026, 8, 25), SPECS, CARDS, hub)
    titles = [p["Name"]["title"][0]["text"]["content"] for _, p in fake.created]
    assert title not in titles


def test_keepalive_fails_hard_on_unknown_account():
    # a card pointing at an account that no longer exists must fail the run
    # (Modal emails on schedule failure), never read as inactivity
    cards = (*CARDS, KeepaliveCard("Card C", "acct-gone"))
    fake = FakeNotion()
    with pytest.raises(RuntimeError, match="acct-gone"):
        dispatch(fake, date(2026, 8, 25), (), cards, FakeHub())
    assert fake.created == []


def test_no_cards_reads_no_finance_tables():
    hub = FakeHub()
    dispatch(FakeNotion(), date(2026, 8, 25), (), (), hub)
    assert hub.pulls == []


def test_hydration_names_the_recipient_id_missing_from_life_data():
    # a gift_recipients id that no people row matches (wrong id format, a
    # hard-deleted person) used to surface as a bare KeyError that killed
    # the whole daily run - it has to say which id and which table to fix
    spec = replace(CHRISTMAS, gift_recipients=("r1", "nope"))
    with pytest.raises(RuntimeError, match="nope"):
        _hydrate_recipients(spec, FakeHub())


def test_dispatch_month_end_after_short_month():
    spec = RecurringSpec(
        key="monthly-review",
        mode="fixed",
        anchor=date(2025, 1, 31),
        interval_months=1,
        match_titles=("Monthly review",),
        templates=(TaskTemplate(title="Monthly review", tags=(), priority="Medium"),),
    )
    fake = FakeNotion(
        snaps={
            spec.match_titles: [
                TaskSnapshot("Monthly review", "Completed", date(2025, 2, 28), None)
            ]
        }
    )
    dispatch(fake, date(2025, 3, 28), (spec,), (), FakeHub())
    assert fake.created == []
    dispatch(fake, date(2025, 3, 31), (spec,), (), FakeHub())
    assert len(fake.created) == 1
    assert fake.created[0][1]["Due Date"]["date"]["start"] == "2025-03-31"


def test_notion_partial_occurrence_restores_existing_blocker_identity():
    spec, _ = _hydrate_recipients(CHRISTMAS, FakeHub())
    template = spec.templates[0]
    snapshots = {
        spec.match_titles: [
            TaskSnapshot(template.title, "To Do", spec.anchor, None, id="existing-preparation")
        ]
    }
    notion = FakeNotion(snaps=snapshots)
    dispatch(notion, spec.anchor, (CHRISTMAS,), (), FakeHub())
    first_buy = next(
        props
        for _, props in notion.created
        if props["Name"]["title"][0]["text"]["content"] == spec.templates[1].title
    )
    assert first_buy["Blocked by"]["relation"] == [{"id": "existing-preparation"}]
