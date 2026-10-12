"""The daily walk's reads, served from Reflex's own copy of each hub table.

soma's table-pull helper (`table_pull.py`, vendored) keeps the copy current:
one `/v1/cursor` request per run for every table read last time, then batched
pulls of only the rows that arrived since. Deleted rows stay in the copy with
their columns, as a hub scan returns them, because the planner reads them (a
deleted task is a canceled occurrence). The copy and its cursor live in one
file on the journal's volume, written once after the reads; losing it costs
one full pull. A purge is invisible to incremental pulls, so the copy is
pulled in full again once a week.
"""

import json
import os
from datetime import date, timedelta
from pathlib import Path

import httpx

from core.table_pull import HubError

FULL_EVERY = timedelta(days=7)


class TableMirror:
    def __init__(self, hub, path, *, today: date, commit=None):
        self.hub, self.path, self.today, self.commit = hub, Path(path), today, commit
        try:
            stored = json.loads(self.path.read_text())
        except (OSError, ValueError):
            stored = {}
        self.columns = stored.get("columns", {})
        self.rows = stored.get("rows", {})
        self.pull = stored.get("pull")
        self.full_on = stored.get("full_on")
        self.used = set()

    def __getattr__(self, name):
        return getattr(self.hub, name)  # writes and dry_run go to the hub itself

    def refresh(self):
        """One cursor round, plus pulls of what moved, for every table held."""
        if not self.full_on or self.today - date.fromisoformat(self.full_on) >= FULL_EVERY:
            self.pull, self.rows, self.full_on = None, {}, self.today.isoformat()
        if not self.columns:
            return
        try:
            self._pull(self.columns)
        except (httpx.HTTPStatusError, HubError) as exc:
            # A held table renamed or gone would fail every run: start cold instead.
            print(f"table mirror dropped: {exc}")
            self.columns, self.rows, self.pull = {}, {}, None

    def pull_rows(self, table, columns, *, where=None):
        """Live rows and tombstones with at least `columns`, filtered by equality."""
        need = {*columns, "id", "deleted_at"}
        if not need <= set(self.columns.get(table, ())):
            self._pull({table: sorted(need | set(self.columns.get(table, ())))})
        self.used.add(table)
        return [
            dict(row)
            for row in self.rows[table].values()
            if all(row.get(col) == value for col, value in (where or {}).items())
        ]

    def _pull(self, tables):
        out = self.hub.pull_tables(tables, self.pull)
        for table, change in out["changes"].items():
            held = {} if change["full"] else self.rows.get(table, {})
            held.update((row["id"], row) for row in change["rows"] + change["tombstones"])
            self.rows[table] = held
        self.columns.update(tables)
        self.pull = {
            **out["state"],
            "tables": {**(self.pull or {}).get("tables", {}), **out["state"]["tables"]},
        }

    def save(self):
        """Keep only the tables this run read; replace the file in one step."""
        keep = lambda held: {t: v for t, v in held.items() if t in self.used}  # noqa: E731
        stored = {
            "full_on": self.full_on,
            "columns": keep(self.columns),
            "rows": keep(self.rows),
            "pull": {**self.pull, "tables": keep(self.pull["tables"])} if self.pull else None,
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(stored, separators=(",", ":")))
        os.replace(temporary, self.path)
        if self.commit:
            self.commit()
