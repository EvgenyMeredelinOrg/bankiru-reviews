from datetime import datetime
from pathlib import Path

import pytest

from bankiru.parser.crawler import (
    ParsedReview,
    is_challenge_page,
    listing_records,
    select_page,
)
from bankiru.parser.errors import ChallengePageError, ListingParseError
from bankiru.parser.smoke import smoke_missing_stored

FIXTURES = Path(__file__).parent / "fixtures"


def test_quote_and_newline_round_trip() -> None:
    html = (FIXTURES / "listing-snippet.html").read_text()
    rows = listing_records(html)
    assert [row.review_body for row in rows] == ['line one "quoted"\nline two', "plain"]
    assert [row.bank_name for row in rows] == ["Bank One", "Bank Two"]
    assert [row.detail_path for row in rows] == [
        "/services/responses/bank/response/111",
        "/services/responses/bank/response/222",
    ]


def test_challenge_raises() -> None:
    html = (FIXTURES / "challenge-snippet.html").read_text()
    assert is_challenge_page(html)
    with pytest.raises(ChallengePageError):
        listing_records(html)


def test_zero_reviews_returns_empty() -> None:
    html = (FIXTURES / "empty-snippet.html").read_text()
    assert listing_records(html) == []


def test_unequal_counts_raise() -> None:
    html = (FIXTURES / "listing-snippet.html").read_text().replace(
        'href="/services/responses/bank/response/222/" data-x="1"',
        'href="/services/responses/bank/response/222/"',
    )
    with pytest.raises(ListingParseError):
        listing_records(html)


def test_rating_above_two_is_dropped() -> None:
    html = (FIXTURES / "listing-snippet.html").read_text()
    rows = listing_records(html)
    assert [row.review_body for row in rows] == ['line one "quoted"\nline two', "plain"]
    assert [row.rating for row in rows] == ["1", "2"]


def _review(stamp: str) -> ParsedReview:
    return ParsedReview(
        "body",
        datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S"),
        "Bank",
        "/p",
    )


def test_page_newer_than_window_continues() -> None:
    start = datetime(2026, 10, 31)
    end = datetime(2026, 11, 1)
    kept, hit_left = select_page([_review("2026-11-01 00:00:00")], start, end)
    assert kept == []
    assert hit_left is False


def test_page_stops_at_older_review() -> None:
    start = datetime(2026, 10, 31)
    end = datetime(2026, 11, 1)
    kept, hit_left = select_page(
        [_review("2026-10-31 12:00:00"), _review("2026-10-30 12:00:00")],
        start,
        end,
    )
    assert [row.date_published for row in kept] == [datetime(2026, 10, 31, 12, 0, 0)]
    assert hit_left is True


def test_smoke_missing_stored_ignores_new_rows() -> None:
    collected = [
        {"url": "https://www.banki.ru/a", "product": "Кредитная карта", "bankName": "Bank One"},
        {"url": "https://www.banki.ru/new", "product": "Кредитная карта", "bankName": "Bank Two"},
    ]
    stored = {
        ("https://www.banki.ru/a", "Кредитная карта"),
        ("https://www.banki.ru/lost", "Кредитная карта"),
    }
    assert smoke_missing_stored(collected, stored) == 1
