"""life-data hub client - the only module besides notion.py that does network I/O.

Pulls the automation catalog rows (recurring specs, keepalive cards) and the
people names the Christmas generator needs; pushes the gift rows it creates.
"""

import httpx


class HubClient:
    def __init__(self, url: str, token: str, dry_run: bool = False):
        self.url, self.token, self.dry_run = url.rstrip("/"), token, dry_run

    def _post(self, path, body):
        resp = httpx.post(
            f"{self.url}{path}",
            json=body,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def pull_rows(self, table: str, columns) -> list[dict]:
        return self._post("/v1/rows/pull", {"table": table, "columns": list(columns), "since": ""})[
            "rows"
        ]

    def push_rows(self, table: str, rows: list[dict]) -> dict:
        """Upsert rows; the hub validates them against the catalog and returns
        {"upserted", "rejected": [...]}. A rejection is surfaced, never retried."""
        if self.dry_run:
            print(f"DRY RUN push {table}: {rows}")
            return {"upserted": 0, "rejected": []}
        columns = sorted({k for r in rows for k in r})
        return self._post("/v1/rows/push", {"table": table, "columns": columns, "rows": rows})


def pull_rows(url: str, token: str, table: str, columns) -> list[dict]:
    return HubClient(url, token).pull_rows(table, columns)
