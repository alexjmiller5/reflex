"""Data-source ids + the recurring-spec model and row loaders.

The specs themselves are DATA, not code: they live in the life-data tables
`recurring_specs` and `cc_keepalive_cards` (one row per spec/card, edited via
`life sql`, served by the hub) and are loaded at dispatch time. Disabling a
spec = soft-deleting its row (`SET deleted_at = updated_at`); re-enabling =
clearing `deleted_at`. This file only ships the shape and validation.
"""

import json
from dataclasses import dataclass
from datetime import date

TASKS = "77ef5074-aa23-468a-b5fb-2692e78184db"
PROJECTS = "30703953-a8af-8041-94b9-000b187b5b36"
NOTES = "28a03953-a8af-806c-8d14-000b51ab2a51"
TRIPS = "19603953-a8af-80af-8803-000be09834a6"
CALENDAR = "24c03953-a8af-8036-8b1b-000bb8d77b03"
SYNAPSE = "2b103953-a8af-8062-971a-000b0e200122"
BOOKS = "331618e2-6245-4819-983f-6f7e9b06401d"
MOVIES = "4eb907d5-1be3-41e3-be31-9afd33510a1f"
ARTICLES = "1c703953-a8af-8062-a379-000b8e250413"
PODCASTS = "cebfc967-c37d-4dbf-a3d0-795b8e971ab7"
GIFTS = "0c39fffe-c8c2-43a5-af03-0a378c682c1c"
TRANSACTIONS = "34603953-a8af-806e-bd83-000b5b921780"

KEEPALIVE_INACTIVE_DAYS = 365


@dataclass(frozen=True)
class TaskTemplate:
    title: str
    tags: tuple[str, ...]
    priority: str
    due_offset_days: int = 0
    links: str = ""
    notes: str = ""
    blocked_by_prev: bool = False


@dataclass(frozen=True)
class RecurringSpec:
    key: str
    mode: str  # "relative" | "fixed"
    anchor: date
    match_titles: tuple[str, ...]
    templates: tuple[TaskTemplate, ...]
    interval_months: int = 0
    interval_days: int = 0
    gift_recipients: tuple[str, ...] = ()


def cc_keepalive_title(account):
    return (
        f"My {account} card has no transactions in the past year (per my Transactions "
        "DB) - verify in the bank app, then make a small ~$5 charge (reload Amazon "
        "balance or put a bill on it) so it doesn't get closed for inactivity"
    )


SPEC_COLUMNS = (
    "key",
    "mode",
    "anchor",
    "interval_months",
    "interval_days",
    "match_titles",
    "templates",
    "gift_recipients",
    "deleted_at",
)
CARD_COLUMNS = ("name", "deleted_at")


def load_recurring(rows) -> tuple[RecurringSpec, ...]:
    """life-data `recurring_specs` rows -> validated specs.

    Raises on any malformed row so the daily run fails loudly (Modal emails
    on a failed schedule) instead of silently skipping an automation.
    """
    specs = []
    for r in rows:
        if r.get("deleted_at"):
            continue
        spec = RecurringSpec(
            key=r["key"],
            mode=r["mode"],
            anchor=date.fromisoformat(r["anchor"]),
            match_titles=tuple(json.loads(r["match_titles"])),
            templates=tuple(
                TaskTemplate(**t | {"tags": tuple(t["tags"])}) for t in json.loads(r["templates"])
            ),
            interval_months=r["interval_months"] or 0,
            interval_days=r["interval_days"] or 0,
            gift_recipients=tuple(json.loads(r["gift_recipients"])),
        )
        valid = (
            spec.mode in ("relative", "fixed")
            and (spec.interval_months or spec.interval_days)
            and ((spec.templates and spec.match_titles) or spec.gift_recipients)
        )
        if not valid:
            raise ValueError(f"invalid recurring_specs row {spec.key!r}")
        specs.append(spec)
    return tuple(specs)


def load_cards(rows) -> tuple[str, ...]:
    """life-data `cc_keepalive_cards` rows -> card option names."""
    return tuple(r["name"] for r in rows if not r.get("deleted_at"))
