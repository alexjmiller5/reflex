import pytest
from pydantic import ValidationError

from core.config import Settings

BASE = {
    "notion_api_token": "synthetic",
    "life_hub_url": "https://hub.example",
    "life_hub_token": "synthetic",
}


def test_default_keeps_notion_authority():
    settings = Settings(**BASE)
    assert settings.life_event_policy is None
    assert settings.retired_notion_sources == ()


def test_runtime_policy_selects_only_explicit_sources():
    settings = Settings(
        **BASE,
        life_event_policy={
            "subscription_id": "sub",
            "tables": {"items": {"creation_defaults": {"priority": "Urgent"}}},
        },
        notion_retired_sources="source-a, source-b",
    )
    assert settings.life_event_policy["tables"]["items"]["creation_defaults"] == {
        "priority": "Urgent"
    }
    assert settings.retired_notion_sources == ("source-a", "source-b")


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"subscription_id": "sub", "tables": {}},
        {"subscription_id": "sub", "tables": {"items": {"day_start_minutes": 1440}}},
    ],
)
def test_invalid_policy_fails_before_automation(policy):
    with pytest.raises(ValidationError):
        Settings(**BASE, life_event_policy=policy)
