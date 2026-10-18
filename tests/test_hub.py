import json
import re
from datetime import datetime, timezone

import httpx
import httpcore
import pytest

from core.hub import HubClient
from core.tick import run_tick


@pytest.mark.parametrize("operation", ["session", "acknowledge"])
@pytest.mark.parametrize("failure", [httpcore.ConnectError, httpcore.ConnectTimeout])
@pytest.mark.parametrize("recover", [True, False])
def test_hub_retries_only_connection_establishment(monkeypatch, operation, failure, recover):
    from httpcore._backends.sync import SyncBackend

    attempts = []

    def connect(*args, **kwargs):
        attempts.append(kwargs)
        if not recover or len(attempts) < 3:
            raise failure("[Errno 101] Network is unreachable")
        return httpcore.MockStream([b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}'])

    monkeypatch.setattr(SyncBackend, "connect_tcp", connect)
    monkeypatch.setattr(SyncBackend, "sleep", lambda *args: None)
    client = HubClient("https://hub.example", "test-token")
    call = client.session if operation == "session" else lambda: client.acknowledge("s", "d")
    if recover and operation == "session":
        state = {}
        assert run_tick(
            state,
            datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            lambda: None,
            consume=lambda: not call()["ok"],
        ) == {"daily": True}
        assert state["daily_success"] == "2026-01-01"
    elif recover:
        assert call() == {"ok": True}
    else:
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            call()
    assert len(attempts) == 3


@pytest.mark.parametrize("failure", [httpcore.ReadTimeout, httpcore.WriteError])
def test_native_transport_never_replays_an_uncertain_write(monkeypatch, failure):
    from httpcore._backends.sync import SyncBackend

    attempts = []

    class LostResponse(httpcore.MockStream):
        def read(self, *args, **kwargs):
            raise httpcore.ReadTimeout("write outcome unknown")

        def write(self, *args, **kwargs):
            if failure is httpcore.WriteError:
                raise failure("write outcome unknown")

    def connect(*args, **kwargs):
        attempts.append(kwargs)
        return LostResponse([])

    monkeypatch.setattr(SyncBackend, "connect_tcp", connect)
    with pytest.raises((httpx.ReadTimeout, httpx.WriteError)):
        HubClient("https://hub.example", "test-token").acknowledge("s", "d")
    assert len(attempts) == 1


@pytest.fixture
def hub(monkeypatch):
    def make(handler, *, dry_run=False):
        monkeypatch.setattr(httpx, "HTTPTransport", lambda **kwargs: httpx.MockTransport(handler))
        return HubClient("https://hub.example/", "test-token", dry_run=dry_run)

    return make


def test_pull_exhausts_pages_and_retains_tombstones(hub):
    calls = []

    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.url.path == "/v1/rows/pull"
        body = json.loads(request.content)
        calls.append(body)
        assert body["columns"] == ["id", "deleted_at"]
        assert body["limit"] == 200
        if "after" not in body:
            return httpx.Response(
                200,
                json={
                    "rows": [{"id": f"r{i:03}", "deleted_at": None} for i in range(200)],
                    "next_cursor": "r199",
                },
            )
        assert body["after"] == "r199"
        return httpx.Response(
            200,
            json={
                "rows": [{"id": "r200", "deleted_at": "2026-01-02T00:00:00.000Z"}],
                "next_cursor": None,
            },
        )

    rows = hub(handler).pull_rows("records", ("id", "deleted_at"))
    assert len(rows) == 201
    assert rows[-1] == {"id": "r200", "deleted_at": "2026-01-02T00:00:00.000Z"}
    assert len(calls) == 2


def test_pull_failure_on_later_page_never_returns_partial_data(hub):
    def handler(request):
        body = json.loads(request.content)
        if "after" not in body:
            return httpx.Response(200, json={"rows": [{"id": "a"}], "next_cursor": "a"})
        return httpx.Response(503, json={"error": "unavailable"})

    with pytest.raises(httpx.HTTPStatusError):
        hub(handler).pull_rows("records", ("id",))


@pytest.mark.parametrize(
    "page",
    [
        {"rows": [{"id": "a"}], "next_cursor": "a"},
        {"rows": [{"id": "a"}]},
        {"rows": [{"id": "a"}], "next_cursor": 5},
    ],
)
def test_pull_refuses_broken_or_repeated_cursor(hub, page):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        assert calls <= 2, "a broken cursor must not loop forever"
        return httpx.Response(200, json=page)

    with pytest.raises(RuntimeError, match="cursor"):
        hub(handler).pull_rows("records", ("id",))


def test_pull_preserves_filter_and_since_across_pages(hub):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["where"] == {"kind": "pending"}
        assert body["since"] == "2026-01-01T00:00:00.000Z"
        return httpx.Response(
            200,
            json={
                "rows": [{"id": "a"}] if len(calls) == 1 else [],
                "next_cursor": "a" if len(calls) == 1 else None,
            },
        )

    assert hub(handler).pull_rows(
        "records", ("id",), since="2026-01-01T00:00:00.000Z", where={"kind": "pending"}
    ) == [{"id": "a"}]
    assert len(calls) == 2


def test_insert_retry_reports_existing_without_upserting(hub):
    stored = {}

    def handler(request):
        assert request.url.path == "/v1/rows/insert"
        body = json.loads(request.content)
        assert body["columns"] == ["id", "label", "updated_at"]
        row = body["rows"][0]
        if row["id"] in stored:
            return httpx.Response(
                200, json={"inserted": [], "existing": [row["id"]], "rejected": []}
            )
        stored[row["id"]] = row.copy()
        raise httpx.ReadTimeout("response lost after commit", request=request)

    client = hub(handler)
    with pytest.raises(httpx.ReadTimeout):
        client.insert_rows("records", [{"id": "stable", "label": "created"}])
    stored["stable"]["label"] = "human edit"
    result = client.insert_rows("records", [{"id": "stable", "label": "created"}])
    assert result == {"inserted": [], "existing": ["stable"], "rejected": []}
    assert stored["stable"]["label"] == "human edit"


def test_insert_stamps_the_edit_timestamp_the_hub_requires(hub):
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(
            200, json={"inserted": [r["id"] for r in body["rows"]], "existing": [], "rejected": []}
        )

    rows = [{"id": "a", "label": "new"}, {"id": "b", "updated_at": "2026-01-01T00:00:00.000Z"}]
    hub(handler).insert_rows("records", rows)
    stamped, kept = sent[0]["rows"]
    assert "updated_at" in sent[0]["columns"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", stamped["updated_at"])
    assert kept["updated_at"] == "2026-01-01T00:00:00.000Z"
    assert "updated_at" not in rows[0]  # the caller's frozen intent is untouched


def test_insert_rejection_names_the_rule_without_row_values(hub):
    rejection = {
        "id": "row-7",
        "col": "updated_at",
        "rule": "required",
        "message": "updated_at must be a valid UTC millisecond timestamp.",
    }
    receipt = {"inserted": [], "existing": [], "rejected": [rejection]}
    with pytest.raises(RuntimeError) as err:
        hub(lambda _: httpx.Response(200, json=receipt)).insert_rows(
            "records", [{"id": "row-7", "label": "private text"}]
        )
    text = str(err.value)
    for part in rejection.values():
        assert part in text
    assert "private text" not in text


@pytest.mark.parametrize(
    "receipt",
    [
        {"inserted": ["a"], "existing": [], "rejected": [{"id": "b", "rule": "required"}]},
        {"inserted": ["a"], "existing": [], "rejected": []},
        {"inserted": ["a", "b"], "existing": ["a"], "rejected": []},
        {"inserted": ["a", "unexpected"], "existing": [], "rejected": []},
    ],
)
def test_insert_rejects_partial_or_ambiguous_receipt(hub, receipt):
    with pytest.raises(RuntimeError):
        hub(lambda _: httpx.Response(200, json=receipt)).insert_rows(
            "records", [{"id": "a"}, {"id": "b"}]
        )


def test_insert_returns_every_successful_identity(hub):
    result = hub(
        lambda _: httpx.Response(
            200,
            json={
                "inserted": ["b"],
                "existing": ["a"],
                "rejected": [],
            },
        )
    ).insert_rows("records", [{"id": "a"}, {"id": "b"}])
    assert result == {"inserted": ["b"], "existing": ["a"], "rejected": []}


def test_patch_sends_both_revision_fields_and_returns_committed_revision(hub):
    expected = {"updated_at": "2026-01-01T00:00:00.000Z", "hub_at": None}
    committed = {"updated_at": "2026-01-02T00:00:00.000Z", "hub_at": "2026-01-02T00:00:00.001Z"}

    def handler(request):
        assert request.url.path == "/v1/rows/patch"
        assert json.loads(request.content) == {
            "table": "records",
            "id": "a",
            "values": {"label": "changed"},
            "expected_revision": expected,
        }
        return httpx.Response(200, json={"id": "a", "revision": committed})

    assert hub(handler).patch_row("records", "a", {"label": "changed"}, expected) == {
        "id": "a",
        "revision": committed,
    }


@pytest.mark.parametrize("status", [403, 404, 409, 422, 503])
def test_patch_never_retries_or_falls_back_to_unconditional_write(hub, status):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, json={"error": "refused"})

    with pytest.raises(httpx.HTTPStatusError):
        hub(handler).patch_row(
            "records",
            "a",
            {"label": "changed"},
            {
                "updated_at": "2026-01-01T00:00:00.000Z",
                "hub_at": None,
            },
        )
    assert calls == ["/v1/rows/patch"]


def test_dry_run_makes_no_writes_and_claims_no_committed_receipt(hub):
    def handler(request):
        raise AssertionError("dry-run must not mutate the hub")

    client = hub(handler, dry_run=True)
    assert client.insert_rows("records", [{"id": "a"}]) is None
    assert (
        client.patch_row(
            "records",
            "a",
            {"label": "changed"},
            {
                "updated_at": "2026-01-01T00:00:00.000Z",
                "hub_at": None,
            },
        )
        is None
    )


def test_subscription_reads_and_ack_use_supported_endpoints(hub):
    seen = []

    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        seen.append((request.method, request.url.path, request.url.query))
        if request.method == "POST":
            assert json.loads(request.content) == {"delivery_id": "delivery"}
        return httpx.Response(200, json={"ok": True})

    client = hub(handler)
    assert client.session() == {"ok": True}
    assert client.subscription_status("sub") == {"ok": True}
    assert client.poll_events("sub") == {"ok": True}
    assert client.acknowledge("sub", "delivery") == {"ok": True}
    assert seen == [
        ("GET", "/v1/session", b""),
        ("GET", "/v1/subscriptions/sub", b""),
        ("GET", "/v1/subscriptions/sub/events", b"wait=0"),
        ("POST", "/v1/subscriptions/sub/ack", b""),
    ]


def test_dry_run_never_acknowledges(hub):
    def forbidden(request):
        raise AssertionError("no acknowledgment in dry-run")

    assert hub(forbidden, dry_run=True).acknowledge("sub", "delivery") is None


@pytest.mark.parametrize(
    "rows", [[], [{"id": "wanted", "updated_at": "v", "hub_at": "h", "deleted_at": None}]]
)
def test_read_row_uses_exact_identity_and_keeps_revision(hub, rows):
    def handler(request):
        body = json.loads(request.content)
        assert body["where"] == {"id": "wanted"}
        assert body["columns"] == ["id", "updated_at", "hub_at", "deleted_at"]
        return httpx.Response(200, json={"rows": rows, "next_cursor": None})

    assert hub(handler).read_row(
        "records", "wanted", ["id", "updated_at", "hub_at", "deleted_at"]
    ) == (rows[0] if rows else None)


@pytest.mark.parametrize("rows", [[{"id": "wrong"}], [{"id": "wanted"}, {"id": "wanted"}]])
def test_read_row_refuses_ambiguous_identity(hub, rows):
    with pytest.raises(RuntimeError):
        hub(lambda _: httpx.Response(200, json={"rows": rows, "next_cursor": None})).read_row(
            "records", "wanted", ["id"]
        )


@pytest.mark.parametrize(
    "receipt",
    [
        {},
        {"id": "wrong", "revision": {"updated_at": "v", "hub_at": "h"}},
        {"id": "a", "revision": {"updated_at": "v"}},
    ],
)
def test_patch_rejects_missing_or_wrong_commit_identity(hub, receipt):
    with pytest.raises(RuntimeError):
        hub(lambda _: httpx.Response(200, json=receipt)).patch_row(
            "records", "a", {"label": "value"}, {"updated_at": "v", "hub_at": None}
        )
