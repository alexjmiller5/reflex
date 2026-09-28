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
                    "Status": {"status": {"name": "Completed"}},
                    "Completed Date": {"date": None},
                    "Name": {"title": [{"plain_text": "P"}]},
                },
            }
        }
    )
    handle_event(event("p"), fake, NOW, bot_id="me")
    assert fake.updated and "Completed Date" in fake.updated[0][1]


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
