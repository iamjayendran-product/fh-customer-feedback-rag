from datetime import date

import pytest

from fh_feedback.app import rag

TODAY = date(2026, 10, 1)


@pytest.fixture(autouse=True)
def latest(monkeypatch):
    monkeypatch.setattr(rag, "_dataset_max_date", lambda: "2026-09-29T01:19:47")


@pytest.mark.parametrize(
    "q,since",
    [
        ("what are the pain points in last two weeks?", "2026-09-15"),
        ("pain points in the past 2 weeks", "2026-09-15"),
        ("issues in the last 30 days", "2026-08-30"),
        ("last week", "2026-09-22"),
        ("this week", "2026-09-28"),
        ("last 3 months", "2026-06-29"),
        ("this year", "2026-01-01"),
    ],
)
def test_relative_windows_anchor_to_dataset_end(q, since):
    s, u, label = rag._extract_date_range(q, TODAY)
    assert s == since
    assert u == "2026-09-29 23:59:59"
    assert "29 Sep 2026" in label


def test_months_still_work():
    assert rag._extract_date_range("pain points in September 2026 alone", TODAY)[:2] == (
        "2026-09-01",
        "2026-09-30 23:59:59",
    )
    assert rag._extract_date_range("last month", TODAY)[0] == "2026-09-01"


def test_no_period():
    assert rag._extract_date_range("what do people say about refunds?", TODAY) == (None, None, None)


def test_period_stats_scope_to_window():
    from fh_feedback import analyse

    window = analyse.run(since="2026-09-01", until="2026-09-30 23:59:59")
    assert window["total_reviews"] < analyse.run()["total_reviews"]
    text = rag._format_period_stats(window, "September 2026", analyse.run())
    assert "PERIOD STATS - September 2026" in text and "usual share" in text


def test_last_weeks_of_named_month_even_misspelled():
    for q in ("last two weeks of September", "pain points in last two weeks of Septemeber?"):
        s, u, _ = rag._extract_date_range(q, TODAY)
        assert (s, u) == ("2026-09-15", "2026-09-29 23:59:59")
    s, u, _ = rag._extract_date_range("last two weeks of August", TODAY)
    assert (s, u) == ("2026-08-17", "2026-08-31 23:59:59")
