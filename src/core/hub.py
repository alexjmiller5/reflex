"""life-data hub client - the only module besides notion.py that does network I/O.

Pulls the automation catalog rows (recurring specs, keepalive cards) and the
people names the Christmas generator needs; pushes the gift rows it creates.
"""

import httpx
from urllib.parse import quote


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

    def _get(self, path):
        resp = httpx.get(
            f"{self.url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def session(self):
        return self._get("/v1/session")

    def subscription_status(self, subscription_id):
        return self._get(f"/v1/subscriptions/{quote(subscription_id, safe='')}")

    def poll_events(self, subscription_id):
        return self._get(f"/v1/subscriptions/{quote(subscription_id, safe='')}/events?wait=0")

    def acknowledge(self, subscription_id, delivery_id):
        if self.dry_run:
            return None
        return self._post(
            f"/v1/subscriptions/{quote(subscription_id, safe='')}/ack",
            {"delivery_id": delivery_id},
        )

    def read_row(self, table, row_id, columns):
        rows = self.pull_rows(table, columns, where={"id": row_id})
        if len(rows) > 1 or (rows and rows[0].get("id") != row_id):
            raise RuntimeError("hub returned an ambiguous row identity")
        return rows[0] if rows else None

    def pull_rows(self, table: str, columns, *, since="", where=None) -> list[dict]:
        """Exhaust bounded pages, including tombstones; a failed page raises.

        This is a scan, not a frozen snapshot. Consumers choose whether to
        filter tombstones and must reconcile writes made during the scan.
        """
        body = {"table": table, "columns": list(columns), "since": since, "limit": 200}
        if where is not None:
            body["where"] = where
        rows, seen = [], set()
        while True:
            page = self._post("/v1/rows/pull", body)
            if "next_cursor" not in page:
                raise RuntimeError("hub omitted the pagination cursor")
            rows.extend(page["rows"])
            cursor = page["next_cursor"]
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise RuntimeError("hub returned an invalid or repeated cursor")
            seen.add(cursor)
            body["after"] = cursor

    def insert_rows(self, table: str, rows: list[dict]) -> dict | None:
        """Create stable identities without overwriting an existing row.

        A retry must reuse the same IDs. Partial rejection or an incomplete
        receipt raises; HTTP success alone does not prove batch success.
        Dry-run returns no committed receipt.
        """
        if self.dry_run:
            print(f"DRY RUN insert {len(rows)} rows into {table}")
            return None
        columns = sorted({k for row in rows for k in row})
        out = self._post("/v1/rows/insert", {"table": table, "columns": columns, "rows": rows})
        if not all(isinstance(out.get(key), list) for key in ("inserted", "existing", "rejected")):
            raise RuntimeError("hub returned an invalid insert receipt")
        if out["rejected"]:
            raise RuntimeError(f"hub rejected {len(out['rejected'])} rows from {table}")
        accepted = out["inserted"] + out["existing"]
        expected = [row["id"] for row in rows]
        if (
            len(accepted) != len(expected)
            or len(set(accepted)) != len(accepted)
            or set(accepted) != set(expected)
        ):
            raise RuntimeError("hub returned an incomplete or ambiguous insert receipt")
        return out

    def patch_row(
        self, table: str, row_id: str, values: dict, expected_revision: dict
    ) -> dict | None:
        """Conditionally edit one live row; never fall back to an upsert.

        On conflict the caller must reread and recompute its effect before
        trying again. Dry-run returns no committed receipt.
        """
        if self.dry_run:
            print(f"DRY RUN patch {table}/{row_id}: {sorted(values)}")
            return None
        receipt = self._post(
            "/v1/rows/patch",
            {
                "table": table,
                "id": row_id,
                "values": values,
                "expected_revision": expected_revision,
            },
        )
        revision = receipt.get("revision")
        if (
            receipt.get("id") != row_id
            or not isinstance(revision, dict)
            or not isinstance(revision.get("updated_at"), str)
            or not isinstance(revision.get("hub_at"), str)
        ):
            raise RuntimeError("hub returned an invalid patch receipt")
        return receipt

    def push_rows(self, table: str, rows: list[dict]) -> dict:
        """Upsert rows; the hub validates them against the catalog and returns
        {"upserted", "rejected": [...]}. A rejection is surfaced, never retried."""
        if self.dry_run:
            print(f"DRY RUN push {table}: {rows}")
            return {"upserted": 0, "rejected": []}
        columns = sorted({k for r in rows for k in r})
        return self._post("/v1/rows/push", {"table": table, "columns": columns, "rows": rows})
