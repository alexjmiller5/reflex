"""load_recurring/load_cards: soma rows in, validated specs out."""

import json
from datetime import date

import pytest

from core.registry import KeepaliveCard, cc_keepalive_title, load_cards, load_recurring


def row(**overrides):
    base = {
        "key": "water-plants",
        "mode": "relative",
        "anchor": "2026-08-02",
        "interval_months": 3,
        "interval_days": 0,
        "match_titles": json.dumps(["Water the office plants"]),
        "templates": json.dumps(
            [
                {
                    "title": "Water the office plants",
                    "tags": ["Chore"],
                    "priority": "High",
                    "due_offset_days": 0,
                    "links": "",
                    "notes": "",
                    "blocked_by_prev": False,
                }
            ]
        ),
        "gift_recipients": json.dumps([]),
        "deleted_at": None,
    }
    return base | overrides


def test_row_becomes_spec():
    (spec,) = load_recurring([row()])
    assert spec.key == "water-plants"
    assert spec.anchor == date(2026, 8, 2)
    assert spec.match_titles == ("Water the office plants",)
    t = spec.templates[0]
    assert t.tags == ("Chore",) and t.priority == "High" and t.blocked_by_prev is False


def test_soft_deleted_row_is_skipped():
    assert load_recurring([row(deleted_at="2026-09-02T00:00:00")]) == ()


def test_gift_recipient_spec_needs_no_templates():
    (spec,) = load_recurring(
        [
            row(
                key="gifts",
                mode="fixed",
                interval_months=12,
                match_titles=json.dumps([]),
                templates=json.dumps([]),
                gift_recipients=json.dumps(["r1", "r2"]),
            )
        ]
    )
    assert spec.gift_recipients == ("r1", "r2") and spec.templates == ()


def test_blocked_by_prev_and_offset_survive():
    (spec,) = load_recurring(
        [
            row(
                templates=json.dumps(
                    [
                        {
                            "title": "Buy it",
                            "tags": ["Gifts"],
                            "priority": "High",
                            "due_offset_days": 30,
                            "links": "",
                            "notes": "",
                            "blocked_by_prev": True,
                        }
                    ]
                ),
                match_titles=json.dumps(["Buy it"]),
            )
        ]
    )
    t = spec.templates[0]
    assert t.due_offset_days == 30 and t.blocked_by_prev is True


def test_bad_mode_fails_loudly():
    with pytest.raises(ValueError, match="water-plants"):
        load_recurring([row(mode="sometimes")])


def test_missing_interval_fails_loudly():
    with pytest.raises(ValueError, match="water-plants"):
        load_recurring([row(interval_months=0, interval_days=0)])


def test_templateless_spec_without_recipients_fails_loudly():
    with pytest.raises(ValueError, match="water-plants"):
        load_recurring([row(templates=json.dumps([]), match_titles=json.dumps([]))])


def test_load_cards_skips_deleted():
    rows = [
        {"name": "Card A", "account_id": "acct-a", "deleted_at": None},
        {"name": "Card B", "account_id": "acct-b", "deleted_at": "2026-09-02T00:00:00"},
    ]
    assert load_cards(rows) == (KeepaliveCard("Card A", "acct-a"),)


def test_load_cards_requires_an_account_id():
    # without the accounts ref there is no transaction table to read, and a
    # silent skip would hide the card from the keepalive forever
    with pytest.raises(ValueError, match="Card A"):
        load_cards([{"name": "Card A", "account_id": None, "deleted_at": None}])


def test_cc_keepalive_title_names_card():
    t = cc_keepalive_title("Card A")
    assert "Card A" in t and "$5" in t
