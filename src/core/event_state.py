"""Single-owner, atomically replaced JSON journal in application-owned storage."""

import copy
import fcntl
import json
import os
import tempfile
from collections.abc import MutableMapping
from contextlib import contextmanager
from pathlib import Path


class _State(MutableMapping):
    def __init__(self, path, commit):
        self.path, self.commit = path, commit
        self.data = json.loads(path.read_text()) if path.exists() else {}
        self.failed = False

    def _check(self):
        if self.failed:
            raise RuntimeError("journal commit failed; reopen and recover before continuing")

    def __getitem__(self, key):
        self._check()
        return copy.deepcopy(self.data[key])

    def __iter__(self):
        self._check()
        return iter(self.data)

    def __len__(self):
        self._check()
        return len(self.data)

    def __setitem__(self, key, value):
        self._check()
        candidate = {**self.data, key: copy.deepcopy(value)}
        self._write(candidate)

    def __delitem__(self, key):
        self._check()
        candidate = copy.deepcopy(self.data)
        del candidate[key]
        self._write(candidate)

    def _write(self, candidate):
        encoded = json.dumps(candidate, allow_nan=False, separators=(",", ":"))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as out:
                temporary = Path(out.name)
                out.write(encoded)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            if self.commit:
                self.commit()
            self.data = candidate
        except BaseException:
            self.failed = True
            raise
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)


@contextmanager
def locked_state(path, *, commit=None):
    """Hold exclusive ownership for the whole tick, not only individual writes.

    A mounted remote volume also needs a single-container scheduler and a
    reload before opening this journal. Its commit callback runs before any
    state assignment returns, so a failed remote commit cannot authorize ACK.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield _State(path, commit)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
