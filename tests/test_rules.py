from datetime import datetime, timezone

from core import registry as R
from core.rules import evaluate

# 2026-08-20 12:00 UTC == 2026-08-20 08:00 America/New_York (EDT, UTC-4)
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def page(ds, props, page_id="p1", title="Some Page"):
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "parent": {"data_source_id": ds},
        "properties": props,
    }


def status(name):
    return {"status": {"name": name}}


def dateval(iso):
    return {"date": {"start": iso} if iso else None}


# --- Tasks ---


def test_tasks_complete_without_completed_date():
    p = page(
        R.TASKS,
        {
            "Status": status("Completed"),
            "Completed Date": dateval(None),
            "Due Date": dateval("2026-08-01"),
            "Tags": {"multi_select": [{"name": "Chore"}]},
            "Priority": {"select": {"name": "High"}},
            "Name": {"title": [{"plain_text": "T"}]},
        },
    )
    v = evaluate(R.TASKS, p, NOW)
    assert [x.rule for x in v] == ["tasks-completed-date-set"]
    assert v[0].fix["Completed Date"]["date"]["start"].startswith("2026-08-20")


def test_tasks_open_with_completed_date_cleared():
    p = page(
        R.TASKS,
        {
            "Status": status("To Do"),
            "Completed Date": dateval("2026-08-01"),
            "Due Date": dateval("2026-08-01"),
            "Tags": {"multi_select": [{"name": "Chore"}]},
            "Priority": {"select": {"name": "High"}},
            "Name": {"title": [{"plain_text": "T"}]},
        },
    )
    v = evaluate(R.TASKS, p, NOW)
    assert v[0].rule == "tasks-completed-date-clear" and v[0].fix["Completed Date"]["date"] is None


def test_tasks_defaults_filled_only_if_empty():
    p = page(
        R.TASKS,
        {
            "Status": status("To Do"),
            "Completed Date": dateval(None),
            "Due Date": dateval(None),
            "Tags": {"multi_select": []},
            "Priority": {"select": None},
            "Name": {"title": [{"plain_text": "T"}]},
        },
    )
    rules = {x.rule: x for x in evaluate(R.TASKS, p, NOW)}
    assert rules["tasks-default-due"].fix["Due Date"]["date"]["start"] == "2026-08-20"
    assert rules["tasks-default-tags"].fix["Tags"]["multi_select"] == [{"name": "Chore"}]
    assert rules["tasks-default-priority"].fix["Priority"]["select"]["name"] == "High"


def test_tasks_place_tag_exempt_from_default_due():
    p = page(
        R.TASKS,
        {
            "Status": status("To Do"),
            "Completed Date": dateval(None),
            "Due Date": dateval(None),
            "Tags": {"multi_select": [{"name": "Lake House"}]},
            "Priority": {"select": {"name": "High"}},
            "Name": {"title": [{"plain_text": "T"}]},
        },
    )
    assert evaluate(R.TASKS, p, NOW, place_tags=("Lake House",)) == []
    # not configured as a place tag -> the default-due rule still applies
    rules = {v.rule for v in evaluate(R.TASKS, p, NOW)}
    assert "tasks-default-due" in rules


# --- Books ---


def test_books_complete_sets_date_read():
    p = page(
        R.BOOKS,
        {
            "Status": status("Finished"),
            "Date Read": dateval(None),
            "Title": {"title": [{"plain_text": "B"}]},
        },
    )
    v = evaluate(R.BOOKS, p, NOW)
    assert v[0].rule == "books-date-read-set"


def test_books_open_clears_date_read():
    p = page(
        R.BOOKS,
        {
            "Status": status("Not Started"),
            "Date Read": dateval("2026-08-01"),
            "Title": {"title": [{"plain_text": "B"}]},
        },
    )
    v = evaluate(R.BOOKS, p, NOW)
    assert v[0].rule == "books-date-read-clear"


def test_compliant_page_yields_nothing():
    p = page(
        R.BOOKS,
        {
            "Status": status("Finished"),
            "Date Read": dateval("2026-08-01"),
            "Title": {"title": [{"plain_text": "B"}]},
        },
    )
    assert evaluate(R.BOOKS, p, NOW) == []


# --- Movies ---


def test_movies_only_finished_sets_date_watched():
    p = page(
        R.MOVIES,
        {
            "Status": status("In Progress"),
            "Date Watched": dateval(None),
            "Title": {"title": [{"plain_text": "M"}]},
        },
    )
    assert evaluate(R.MOVIES, p, NOW) == []


def test_movies_finished_sets_date_watched():
    p = page(
        R.MOVIES,
        {
            "Status": status("Finished"),
            "Date Watched": dateval(None),
            "Title": {"title": [{"plain_text": "M"}]},
        },
    )
    v = evaluate(R.MOVIES, p, NOW)
    assert v[0].rule == "movies-date-watched-set"


def test_movies_not_started_clears_date_watched():
    p = page(
        R.MOVIES,
        {
            "Status": status("Not Started"),
            "Date Watched": dateval("2026-08-01"),
            "Title": {"title": [{"plain_text": "M"}]},
        },
    )
    v = evaluate(R.MOVIES, p, NOW)
    assert v[0].rule == "movies-date-watched-clear"


# --- Articles ---


def test_articles_done_sets_read_date():
    p = page(
        R.ARTICLES,
        {
            "Status": status("Done"),
            "Read Date": dateval(None),
            "Name": {"title": [{"plain_text": "A"}]},
        },
    )
    v = evaluate(R.ARTICLES, p, NOW)
    assert v[0].rule == "articles-read-date-set"


def test_articles_not_started_clears_read_date():
    p = page(
        R.ARTICLES,
        {
            "Status": status("Not started"),
            "Read Date": dateval("2026-08-01"),
            "Name": {"title": [{"plain_text": "A"}]},
        },
    )
    v = evaluate(R.ARTICLES, p, NOW)
    assert v[0].rule == "articles-read-date-clear"


# --- Projects ---


def test_projects_complete_sets_completed_date():
    p = page(
        R.PROJECTS,
        {
            "Status": status("Completed"),
            "Completed Date": dateval(None),
            "Title": {"title": [{"plain_text": "P"}]},
        },
    )
    v = evaluate(R.PROJECTS, p, NOW)
    assert v[0].rule == "projects-completed-date-set"


def test_projects_in_progress_clears_completed_date():
    p = page(
        R.PROJECTS,
        {
            "Status": status("In progress"),
            "Completed Date": dateval("2026-08-01"),
            "Title": {"title": [{"plain_text": "P"}]},
        },
    )
    v = evaluate(R.PROJECTS, p, NOW)
    assert v[0].rule == "projects-completed-date-clear"


# --- Synapse ---


def _synapse_page(**overrides):
    props = {
        "Outcome": status("Successful Flow"),
        "Code Execution": status("Success"),
        "Category": {"select": {"name": "bookmarks"}},
        "Remedied?": {"checkbox": False},
        "Date Remedied": dateval(None),
        "Date Reviewed": dateval(None),
        "Raw Input": {"title": [{"plain_text": "S"}]},
    }
    props.update(overrides)
    return page(R.SYNAPSE, props)


def test_synapse_outcome_autoapprove():
    p = _synapse_page(Outcome=status("To Review"))
    # outcome-autoapprove only applies on page.created events; evaluate() takes created flag
    v = evaluate(R.SYNAPSE, p, NOW, created=True)
    assert any(
        x.rule == "synapse-outcome" and x.fix["Outcome"]["status"]["name"] == "Successful Flow"
        for x in v
    )


def test_synapse_remedied_checked_sets_date_remedied():
    p = _synapse_page(**{"Remedied?": {"checkbox": True}, "Date Remedied": dateval(None)})
    v = evaluate(R.SYNAPSE, p, NOW)
    assert any(x.rule == "synapse-date-remedied-set" for x in v)


def test_synapse_remedied_unchecked_clears_date_remedied():
    p = _synapse_page(**{"Remedied?": {"checkbox": False}, "Date Remedied": dateval("2026-08-01")})
    v = evaluate(R.SYNAPSE, p, NOW)
    assert any(x.rule == "synapse-date-remedied-clear" for x in v)


def test_synapse_outcome_reviewed_sets_date_reviewed():
    p = _synapse_page(Outcome=status("Successful Flow"), **{"Date Reviewed": dateval(None)})
    v = evaluate(R.SYNAPSE, p, NOW)
    rules = {x.rule: x for x in v}
    assert (
        rules["synapse-date-reviewed-set"]
        .fix["Date Reviewed"]["date"]["start"]
        .startswith("2026-08-20")
    )


def test_synapse_created_autoapprove_also_sets_date_reviewed():
    # on created=True, the Date Reviewed rule must evaluate the EFFECTIVE
    # post-fix Outcome (Successful Flow), not the raw "To Review" - otherwise
    # the auto-approved page is born without a Date Reviewed timestamp
    p = _synapse_page(Outcome=status("To Review"), **{"Date Reviewed": dateval(None)})
    v = evaluate(R.SYNAPSE, p, NOW, created=True)
    rules = {x.rule: x for x in v}
    assert rules["synapse-outcome"].fix["Outcome"]["status"]["name"] == "Successful Flow"
    assert (
        rules["synapse-date-reviewed-set"]
        .fix["Date Reviewed"]["date"]["start"]
        .startswith("2026-08-20")
    )


def test_synapse_to_review_clears_date_reviewed():
    p = _synapse_page(Outcome=status("To Review"), **{"Date Reviewed": dateval("2026-08-01")})
    v = evaluate(R.SYNAPSE, p, NOW)
    assert any(x.rule == "synapse-date-reviewed-clear" for x in v)


def test_podcasts_finished_sets_date_listened_to():
    p = page(
        R.PODCASTS,
        {
            "Status": status("Finished"),
            "Date Listened To": dateval(None),
            "Episode Title": {"title": [{"plain_text": "Ep 1"}]},
        },
    )
    v = evaluate(R.PODCASTS, p, NOW)
    assert [x.rule for x in v] == ["podcasts-date-listened-to-set"]
    assert v[0].fix["Date Listened To"]["date"]["start"].startswith("2026-08-20")


def test_podcasts_reopened_clears_date_listened_to():
    p = page(
        R.PODCASTS,
        {
            "Status": status("In Progress"),
            "Date Listened To": dateval("2026-08-01"),
            "Episode Title": {"title": [{"plain_text": "Ep 1"}]},
        },
    )
    v = evaluate(R.PODCASTS, p, NOW)
    assert [x.rule for x in v] == ["podcasts-date-listened-to-clear"]
    assert v[0].fix["Date Listened To"]["date"] is None
