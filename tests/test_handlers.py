"""Webhook handler tests. Event shape is pinned against the real Notion
webhook docs (2026-08-20 read of developers.notion.com/reference/webhooks*),
not the brief's guesses - see task-7-report.md for the full correction list.
Key corrections baked into these fixtures:
  - event.entity has both "id" and "type" (we only act on type == "page")
  - event.data.parent has {"id", "type"} - NEVER a "data_source_id". The
    data_source_id comes only from the fetched Page object's own "parent"
    field (page["parent"]["data_source_id"], per the 2025-09-03+ Pages API),
    so handle_event always fetches the page before routing.
  - event.data.updated_properties carries property IDs (short opaque
    strings), not names - handlers.py resolves names via each property's
    own "id" field on the fetched page.
  - one event per HTTP delivery (no "events" batch wrapper).
"""

import hashlib
import hmac
from datetime import datetime, timezone

import pytest

from core import registry as R
from core.handlers import handle_event, handshake_token, verify_signature

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def sig(body: bytes, secret="s"):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_roundtrip():
    body = b'{"x":1}'
    assert verify_signature(body, sig(body), "s")
    assert not verify_signature(body, sig(body), "wrong")
    assert not verify_signature(b"tampered", sig(body), "s")


class FakeNotion:
    def __init__(self, pages):
        self.pages, self.updated, self.created = pages, [], []

    def get_page(self, pid):
        return self.pages[pid]

    def update_page(self, pid, props):
        self.updated.append((pid, props))

    def create_page(self, ds, props, icon=None):
        self.created.append((ds, props))
        return {"id": "new-note"}


def event(
    page_id, etype="page.properties_updated", author="other-bot", entity_type="page", updated=()
):
    return {
        "timestamp": NOW.isoformat(),
        "type": etype,
        "entity": {"id": page_id, "type": entity_type},
        "data": {
            "parent": {"id": "irrelevant", "type": "data_source"},
            "updated_properties": list(updated),
        },
        "authors": [{"id": author}],
    }


def test_loop_guard_skips_own_events():
    fake = FakeNotion({})
    out = handle_event(event("p", author="me"), fake, NOW, bot_id="me")
    assert out == ["skipped: self-authored"] and fake.updated == []


def test_non_page_entity_skipped():
    fake = FakeNotion({})
    out = handle_event(event("c", entity_type="comment"), fake, NOW, bot_id="me")
    assert out == ["skipped: non-page entity"]


def test_non_data_source_parent_skipped():
    fake = FakeNotion(
        {
            "p": {
                "id": "p",
                "url": "u",
                "parent": {"type": "page_id", "page_id": "x"},
                "properties": {},
            }
        }
    )
    out = handle_event(event("p"), fake, NOW, bot_id="me")
    assert out == ["skipped: non-data-source parent"]
    assert fake.updated == [] and fake.created == []


def test_unwatched_db_skipped():
    fake = FakeNotion(
        {
            "p": {
                "id": "p",
                "url": "u",
                "parent": {"data_source_id": "not-watched"},
                "properties": {},
            }
        }
    )
    out = handle_event(event("p"), fake, NOW, bot_id="me")
    assert out == ["skipped: unwatched db not-watched"]
    assert fake.updated == []


def test_project_completion_gets_fix_applied():
    fake = FakeNotion(
        {
            "p": {
                "id": "p",
                "url": "u",
                "parent": {"data_source_id": R.PROJECTS},
                "properties": {
                    "Status": {"id": "status-id", "status": {"name": "Completed"}},
                    "Completed Date": {"date": None},
                    "Name": {"title": [{"plain_text": "P"}]},
                },
            }
        }
    )
    handle_event(event("p", updated=("status-id",)), fake, NOW, bot_id="me")
    assert fake.updated and "Completed Date" in fake.updated[0][1]


def historical_task(status="Completed", completed=None):
    return {
        "id": "old-task",
        "created_time": "2024-01-01T12:00:00Z",
        "parent": {"data_source_id": R.TASKS},
        "properties": {
            "Name": {"id": "title", "title": [{"plain_text": "Renamed task"}]},
            "Status": {"id": "s%40", "status": {"name": status}},
            "Completed Date": {"id": "completed", "date": completed},
            "Due Date": {"id": "due", "date": None},
            "Tags": {"id": "tags", "multi_select": []},
            "Priority": {"id": "priority", "select": None},
        },
    }


@pytest.mark.parametrize("status", ["Completed", "Canceled", "To Do", "In Progress"])
@pytest.mark.parametrize("updated", [("title",), ("due", "completed"), ()])
def test_unrelated_edits_leave_historical_dates_and_defaults_alone(status, updated):
    page = historical_task(status)
    fake = FakeNotion({page["id"]: page})
    handle_event(event(page["id"], updated=updated), fake, NOW, bot_id="me")
    assert fake.updated == []


@pytest.mark.parametrize("property_id", ["s%40", "s@"])
def test_status_change_only_stamps_completion_at_event_time(property_id):
    page = historical_task()
    fake = FakeNotion({page["id"]: page})
    e = event(page["id"], updated=(property_id,))
    e["timestamp"] = "2026-08-19T23:30:00Z"
    handle_event(e, fake, NOW, bot_id="me")
    assert fake.updated == [
        (page["id"], {"Completed Date": {"date": {"start": "2026-08-19T19:30:00-04:00"}}})
    ]


def test_mixed_authors_do_not_hide_a_real_status_change():
    page = historical_task("In Progress", {"start": "2026-01-01"})
    fake = FakeNotion({page["id"]: page})
    e = event(page["id"], updated=("s%40", "completed"))
    e["authors"].append({"id": "me"})
    handle_event(e, fake, NOW, bot_id="me")
    assert fake.updated == [(page["id"], {"Completed Date": {"date": None}})]


def test_reopening_only_clears_completion_when_status_changes():
    page = historical_task("To Do", {"start": "2024-01-01"})
    fake = FakeNotion({page["id"]: page})
    handle_event(event(page["id"], updated=("title",)), fake, NOW, bot_id="me")
    assert fake.updated == []
    handle_event(event(page["id"], updated=("s%40",)), fake, NOW, bot_id="me")
    assert fake.updated == [(page["id"], {"Completed Date": {"date": None}})]


@pytest.mark.parametrize("etype", ["page.properties_updated", "page.created"])
def test_delayed_event_cannot_stamp_a_newer_page_state(etype):
    page = historical_task()
    page["last_edited_time"] = NOW.isoformat()
    fake = FakeNotion({page["id"]: page})
    stale = event(page["id"], etype=etype, updated=("s%40",))
    stale["timestamp"] = "2026-08-19T12:00:00Z"
    handle_event(stale, fake, NOW, bot_id="me")
    assert fake.updated == []
    handle_event(event(page["id"], updated=("s%40",)), fake, NOW, bot_id="me")
    assert fake.updated == [
        (page["id"], {"Completed Date": {"date": {"start": "2026-08-20T08:00:00-04:00"}}})
    ]


def test_new_task_receives_defaults_and_preserves_supplied_values():
    page = historical_task("To Do")
    page["properties"]["Priority"]["select"] = {"name": "Low"}
    fake = FakeNotion({page["id"]: page})
    handle_event(event(page["id"], etype="page.created"), fake, NOW, bot_id="me")
    assert dict((k, v) for _, patch in fake.updated for k, v in patch.items()) == {
        "Due Date": {"date": {"start": "2026-08-20"}},
        "Tags": {"multi_select": [{"name": "Chore"}]},
    }


@pytest.mark.parametrize("etype", ["page.content_updated", "page.undeleted", "page.deleted"])
def test_other_page_events_cannot_fill_historical_dates(etype):
    page = historical_task()
    fake = FakeNotion({page["id"]: page})
    handle_event(event(page["id"], etype=etype), fake, NOW, bot_id="me")
    assert fake.updated == []


def test_trashed_page_is_not_modified_by_delayed_event():
    page = historical_task()
    page["in_trash"] = True
    fake = FakeNotion({page["id"]: page})
    handle_event(event(page["id"], updated=("s%40",)), fake, NOW, bot_id="me")
    assert fake.updated == []


@pytest.mark.parametrize("updated", [("title",), ("outcome",), ("remedied",)])
def test_synapse_timestamps_only_follow_their_own_trigger(updated):
    page = {
        "id": "capture",
        "parent": {"data_source_id": R.SYNAPSE},
        "properties": {
            "Raw Input": {"id": "title", "title": [{"plain_text": "Redacted capture"}]},
            "Outcome": {"id": "outcome", "status": {"name": "Successful Flow"}},
            "Date Reviewed": {"date": None},
            "Remedied?": {"id": "remedied", "checkbox": True},
            "Date Remedied": {"date": None},
        },
    }
    fake = FakeNotion({page["id"]: page})
    handle_event(event(page["id"], updated=updated), fake, NOW, bot_id="me")
    assert [key for _, patch in fake.updated for key in patch] == {
        ("title",): [],
        ("outcome",): ["Date Reviewed"],
        ("remedied",): ["Date Remedied"],
    }[updated]


class TestHandshakeToken:
    """The handshake is the only unsigned path; it closes once a secret exists."""

    def test_returns_token_during_setup(self):
        assert handshake_token({"verification_token": "tok"}, "") == "tok"

    def test_refused_once_a_secret_is_configured(self):
        # log-poisoning guard: a stranger must not be able to write plausible
        # "verification token" lines that a later re-subscription might adopt
        assert handshake_token({"verification_token": "attacker"}, "real-secret") is None

    def test_ordinary_event_is_never_a_handshake(self):
        assert handshake_token({"type": "page.created"}, "") is None
