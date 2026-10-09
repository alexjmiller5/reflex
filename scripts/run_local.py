"""Run the daily dispatch logic locally, no Modal - for a dry-run sanity
check before deploying. Skips the reconciler's state persistence (no Modal
Dict outside the cloud); logs a fresh window each run.

Usage: PYTHONPATH=src DRY_RUN=true op run --env-file=.env.tpl -- uv run scripts/run_local.py
(also wired as `just run-local`, which sets PYTHONPATH=src automatically -
plain `uv run` doesn't apply pytest's pythonpath config, so `core` isn't
importable without it)
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.config import Settings
from core.dispatcher import dispatch
from core.handlers import EVENT_DBS
from core.hub import HubClient
from core.notion import NotionClient
from core.reconciler import reconcile
from core.registry import CARD_COLUMNS, SPEC_COLUMNS, load_cards, load_recurring
from core.season_reminders import dispatch_seasons


def main() -> None:
    s = Settings()
    notion = NotionClient(s.notion_api_token, dry_run=s.dry_run)
    hub = HubClient(s.soma_hub_url, s.soma_hub_token, dry_run=s.dry_run)
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo("America/New_York")).date()
    journal = {}  # throwaway: the deployed journal lives on the Modal Volume

    recurring = load_recurring(hub.pull_rows("recurring_specs", SPEC_COLUMNS))
    cards = load_cards(hub.pull_rows("cc_keepalive_cards", CARD_COLUMNS))
    for line in dispatch(
        notion, today, recurring, cards, hub, task_config=s.soma_tasks_config, state=journal
    ):
        print(line)
    if s.soma_season_reminders_config is not None:
        for line in dispatch_seasons(
            hub, journal, s.soma_season_reminders_config, s.soma_tasks_config, today
        ):
            print(line)

    since = (now - timedelta(days=1)).isoformat()
    sources = EVENT_DBS - set(s.retired_notion_sources)
    logs, _mark = reconcile(notion, sources, since, now, place_tags=s.place_tags)
    for line in logs:
        print(line)


if __name__ == "__main__":
    main()
