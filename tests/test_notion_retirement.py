"""Soma-only deployments cannot accidentally revive retired Notion traffic."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from core.config import Settings
from core.handlers import EVENT_DBS


def soma_settings(**changes):
    return {
        "notion_api_token": "",
        "soma_hub_url": "https://example.invalid",
        "soma_hub_token": "synthetic",
        "soma_tasks_config": {"table": "work_items"},
        "notion_retired_sources": ",".join(sorted(EVENT_DBS)),
        **changes,
    }


def test_soma_only_can_remove_notion_credential(monkeypatch):
    monkeypatch.delenv("NOTION_API_TOKEN", raising=False)
    values = soma_settings()
    del values["notion_api_token"]
    settings = Settings(**values)
    assert settings.notion_api_token == ""


@pytest.mark.parametrize(
    "changes",
    [{"soma_tasks_config": None}, {"notion_retired_sources": ""}],
)
def test_missing_credential_rejects_still_selected_notion_backends(changes):
    with pytest.raises(ValidationError, match="Notion"):
        Settings(**soma_settings(**changes))


def test_disabled_webhook_never_parses_payload_or_calls_notion(monkeypatch):
    import app
    import core.config
    import core.notion

    monkeypatch.setattr(core.config, "Settings", lambda: Settings(**soma_settings()))
    monkeypatch.setattr(
        core.notion,
        "NotionClient",
        lambda *a, **k: pytest.fail("Disabled backend must not create a Notion client"),
    )
    request = SimpleNamespace(body=AsyncMock(), headers={})
    result = asyncio.run(app.notion_webhook.get_raw_f()(request))
    assert result == {"ok": True, "ignored": "notion_disabled"}
    request.body.assert_not_awaited()
