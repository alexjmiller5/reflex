"""Prospective season reminders from complete, caller-selected inventories.

The inventory must contain the whole season, not only currently aired episodes.
Keep this optional consumer disabled until that source contract is established.
"""

import copy
import json

from core.life_dispatch import _identity, _write_plan


def _inventory(hub, binding, columns):
    rows = hub.pull_rows(binding["table"], sorted({"id", "deleted_at", *columns}))
    result = {}
    for row in rows:
        key = row.get("id")
        if not isinstance(key, str) or not key or key in result:
            raise ValueError("inventory requires unique string IDs")
        result[key] = row
    return result


def dispatch_seasons(hub, state, config, task_config, today):
    """Freeze one occurrence before insertion; preserve every existing target."""
    shows, episodes = config["shows"], config["episodes"]
    columns = task_config["columns"]
    prefixes = config["title_prefixes"]
    if not prefixes or any(not isinstance(p, str) or not p for p in prefixes):
        raise ValueError("nonempty title prefixes are required")
    if set(config.get("task_values", {})) & {
        "id",
        "title",
        "due",
        "deleted_at",
        "updated_at",
        "hub_at",
        columns["title"],
        columns["due"],
    }:
        raise ValueError("task values cannot override occurrence identity or content")
    show_rows = _inventory(hub, shows, [shows["title_column"]])
    episode_rows = _inventory(
        hub,
        episodes,
        [episodes[k] for k in ("show_column", "season_column", "number_column", "status_column")],
    )
    seasons = config["seasons"]
    season_rows = _inventory(
        hub, seasons, [seasons[k] for k in ("show_column", "season_column", "total_column")]
    )
    totals = {}
    for row in season_rows.values():
        if row.get("deleted_at"):
            continue
        season, total = row.get(seasons["season_column"]), row.get(seasons["total_column"])
        if type(season) is not int or season < 1 or type(total) is not int or total < 1:
            raise ValueError("season totals require positive integers")
        key = json.dumps([row[seasons["show_column"]], season], separators=(",", ":"))
        if key in totals:
            raise ValueError("duplicate season total")
        totals[key] = total
    tasks = _inventory(hub, task_config, columns.values())
    selected = {}
    for key, row in show_rows.items():
        title = row.get(shows["title_column"])
        if (
            not row.get("deleted_at")
            and isinstance(title, str)
            and title.startswith(tuple(prefixes))
        ):
            selected[key] = title
    groups = {}
    for row in episode_rows.values():
        show_id = row.get(episodes["show_column"])
        if show_id not in selected:
            continue
        season, number = row.get(episodes["season_column"]), row.get(episodes["number_column"])
        if type(season) is not int or season < 0 or type(number) is not int or number < 1:
            raise ValueError("episodes require integer season and positive episode numbers")
        if season == 0:
            continue
        key = json.dumps([show_id, season], separators=(",", ":"))
        group = groups.setdefault(key, {})
        if number in group:
            raise ValueError("duplicate season episode number")
        group[number] = row
    records = copy.deepcopy(state.get("season_reminders", {"known": {}, "plans": {}}))
    baseline = "season_reminders" not in state
    finished = []
    for key, group in groups.items():
        known = set(records["known"].get(key, []))
        current = {r["id"] for r in group.values()}
        complete = (
            known <= current
            and key in totals
            and sorted(group) == list(range(1, totals[key] + 1))
            and all(
                not r.get("deleted_at")
                and r.get(episodes["status_column"]) == episodes["finished_value"]
                for r in group.values()
            )
        )
        records["known"][key] = sorted(known | current)
        if complete and key not in records["plans"]:
            if baseline:
                records["plans"][key] = {"items": [], "complete": True}
            else:
                show_id, season = json.loads(key)
                values = {
                    **config.get("task_values", {}),
                    "title": config["title_template"].format(
                        title=selected[show_id], season=season
                    ),
                    "due": today.isoformat(),
                }
                row = {
                    "id": _identity("season-finish", show_id, season),
                    **config.get("task_values", {}),
                    columns["title"]: values["title"],
                    columns["due"]: values["due"],
                }
                records["plans"][key] = {
                    "items": [
                        {
                            "table": task_config["table"],
                            "row": row,
                            "adopted": False,
                            "delivered": False,
                        }
                    ],
                    "complete": False,
                }
                finished.append(key)
    if hub.dry_run:
        return [f"season reminders: {len(finished)} previewed"]
    # All inventories and planned bodies validate before the durable intent.
    state["season_reminders"] = records
    for plan in records["plans"].values():
        if not plan["complete"]:
            _write_plan(
                plan, hub, state, "season_reminders", records, {task_config["table"]: tasks}
            )
    return [f"season reminders: {len(finished)} planned"]
