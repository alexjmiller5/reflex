"""Settings from env vars - Modal Secret in the cloud, `op run` locally.

One field per line in .env.tpl. Instantiate Settings() inside functions,
not at import time, so tests can run without secrets.
"""

from datetime import datetime, timezone

from pydantic import field_validator
from pydantic_settings import BaseSettings

from core.life_events import _columns
from core.rules import evaluate_transition


class Settings(BaseSettings):
    notion_api_token: str
    notion_webhook_secret: str = ""  # empty until subscription created
    life_hub_url: str  # life-data hub serving the recurring_specs/cc_keepalive_cards tables
    life_hub_token: str
    notion_tasks_place_tags: str = ""  # comma-separated Tags exempt from the default due date
    dry_run: bool = False
    life_event_policy: dict | None = None
    life_tasks_config: dict | None = None
    notion_retired_sources: str = ""

    @field_validator("life_event_policy")
    @classmethod
    def validate_event_policy(cls, policy):
        if policy is None:
            return None
        try:
            if not isinstance(policy["subscription_id"], str) or not policy["subscription_id"]:
                raise ValueError("subscription_id is required")
            if not isinstance(policy["tables"], dict) or not policy["tables"]:
                raise ValueError("tables must contain runtime policies")
            for table, rules in policy["tables"].items():
                if not isinstance(table, str) or not table or not isinstance(rules, dict):
                    raise ValueError("invalid table policy")
                columns = _columns(rules)
                if not columns or any(not isinstance(c, str) or not c for c in columns):
                    raise ValueError("policy must watch named columns")
                if set(columns) & {"id", "updated_at", "hub_at", "deleted_at"}:
                    raise ValueError("policy cannot modify identity or lifecycle columns")
                evaluate_transition({}, {}, set(), datetime.now(timezone.utc), rules)
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid event policy structure") from exc
        return policy

    @property
    def retired_notion_sources(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.notion_retired_sources.split(",") if s.strip())

    @property
    def place_tags(self) -> tuple[str, ...]:
        return tuple(t.strip() for t in self.notion_tasks_place_tags.split(",") if t.strip())
