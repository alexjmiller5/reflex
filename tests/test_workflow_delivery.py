"""The actual deployment manifest delivers configured and disabled adapters."""

import json
import runpy
from pathlib import Path

from core.config import Settings


def test_deployment_manifest_preserves_optional_workflow_settings(monkeypatch):
    runtime = {
        "NOTION_API_TOKEN": "fixture",
        "NOTION_WEBHOOK_SECRET": "",
        "LIFE_HUB_URL": "https://example.invalid",
        "LIFE_HUB_TOKEN": "fixture",
        "NOTION_TASKS_PLACE_TAGS": "",
        "LIFE_EVENT_POLICY": json.dumps(
            {
                "subscription_id": "fixture",
                "tables": {"work_items": {"creation_defaults": {"priority": "High"}}},
            }
        ),
        "LIFE_TASKS_CONFIG": json.dumps({"table": "work_items"}),
        "LIFE_SEASON_REMINDERS_CONFIG": "null",
        "NOTION_RETIRED_SOURCES": "old-source",
    }
    # Simulate injection at the owning ENV boundary, then use the real deployed parser.
    rendered = []
    for line in Path(".env.tpl").read_text().splitlines():
        if line and not line.startswith("#"):
            key, reference = line.split("=", 1)
            rendered.append(key + "=" + runtime[reference.rsplit("/", 1)[-1]])
    env = runpy.run_path("scripts/sync_secrets.py")["parse_dotenv"]("\n".join(rendered))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert settings.life_tasks_config == {"table": "work_items"}
    assert settings.life_event_policy["subscription_id"] == "fixture"
    assert settings.retired_notion_sources == ("old-source",)
    assert settings.life_season_reminders_config is None
