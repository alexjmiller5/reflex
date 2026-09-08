#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Print exact property names / status options / People ids used by the registry."""

import os
import httpx

H = {"Authorization": f"Bearer {os.environ['NOTION_API_TOKEN']}", "Notion-Version": "2026-03-11"}
DBS = {  # name -> data_source_id (copy the Global Constraints block)
    "Tasks": "77ef5074-aa23-468a-b5fb-2692e78184db",
    "Projects": "30703953-a8af-8041-94b9-000b187b5b36",
    "Trips": "19603953-a8af-80af-8803-000be09834a6",
    "Calendar": "24c03953-a8af-8036-8b1b-000bb8d77b03",
    "Synapse Executions": "2b103953-a8af-8062-971a-000b0e200122",
    "Books": "331618e2-6245-4819-983f-6f7e9b06401d",
    "Movies": "4eb907d5-1be3-41e3-be31-9afd33510a1f",
    "Podcast Episodes": "cebfc967-c37d-4dbf-a3d0-795b8e971ab7",
    "Gifts": "0c39fffe-c8c2-43a5-af03-0a378c682c1c",
}
for name, ds in DBS.items():
    s = httpx.get(f"https://api.notion.com/v1/data_sources/{ds}", headers=H).json()["properties"]
    print(f"\n== {name} ==")
    for pname, p in sorted(s.items()):
        extra = ""
        if p["type"] == "status":
            extra = " options=" + ",".join(o["name"] for o in p["status"]["options"])
        print(f"  {pname} ({p['type']}){extra}")
