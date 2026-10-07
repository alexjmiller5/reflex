import httpx
import json
from core.notion import NotionClient, task_properties
from datetime import date


def make_client(handler, dry_run=False):
    return NotionClient(token="t", dry_run=dry_run, transport=httpx.MockTransport(handler))


def test_query_paginates():
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(
                200, json={"results": [{"id": "1"}], "has_more": True, "next_cursor": "c"}
            )
        return httpx.Response(200, json={"results": [{"id": "2"}], "has_more": False})

    out = make_client(handler).query("ds")
    assert [r["id"] for r in out] == ["1", "2"]
    assert calls[1]["start_cursor"] == "c"


def test_create_page_dry_run_skips_http():
    def handler(req):
        raise AssertionError("no HTTP in dry run")

    out = make_client(handler, dry_run=True).create_page("ds", {"x": 1})
    assert out["id"] == "dry-run"


def test_task_properties_payload():
    p = task_properties("T", date(2026, 11, 2), ("Chore",), "High", blocked_by="abc")
    assert p["Name"]["title"][0]["text"]["content"] == "T"
    assert p["Due Date"]["date"]["start"] == "2026-11-02"
    assert p["Status"]["status"]["name"] == "To Do"
    assert p["Priority"]["select"]["name"] == "High"
    assert p["Tags"]["multi_select"] == [{"name": "Chore"}]
    assert p["Blocked by"]["relation"] == [{"id": "abc"}]


def test_headers():
    def handler(req):
        assert req.headers["Authorization"] == "Bearer t"
        assert req.headers["Notion-Version"] == "2026-03-11"
        return httpx.Response(200, json={"results": [], "has_more": False})

    make_client(handler).query("ds")


def test_any_match_single_request():
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"results": [{"id": "1"}], "has_more": True})

    assert make_client(handler).any_match("ds", {"f": 1}) is True
    assert calls == [{"page_size": 1, "filter": {"f": 1}}]  # no pagination follow-up


def test_snapshot_preserves_source_page_identity_for_partial_recovery():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "has_more": False,
                "results": [
                    {
                        "id": "existing-task",
                        "properties": {
                            "Due Date": {"date": {"start": "2026-01-01"}},
                            "Completed Date": {"date": None},
                            "Status": {"status": {"name": "To Do"}},
                        },
                    }
                ],
            },
        )

    snapshot = make_client(handler).snapshots("source", ("Task",))[0]
    assert snapshot.id == "existing-task"
