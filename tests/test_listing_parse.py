import json
from datetime import datetime
from pathlib import Path

import pytest

from bankiru.parser.crawler import (
    ParsedReview,
    assert_rate,
    is_challenge_page,
    listing_records,
    loads_listing_json,
    parse_listing,
    repair_json_escapes,
    select_page,
)
from bankiru.parser.errors import ChallengePageError, ListingParseError, RatingFilterMissed
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


def _listing(rows: list[tuple[str, str, str, str]]) -> str:
    blocks: list[str] = []
    anchors: list[str] = []
    for body, stamp, rating, rid in rows:
        blocks.append(
            "{"
            '"@type":"Review",'
            f'"datePublished":"{stamp}",'
            f'"reviewBody":"{body}",'
            f'"reviewRating":{{"ratingValue":"{rating}"}},'
            '"itemReviewed":{"name":"Bank"}'
            "}"
        )
        anchors.append(
            f'<a href="/services/responses/bank/response/{rid}/" data-x="1">{rid}</a>'
        )
    return (
        '<script type="application/ld+json">['
        + ",".join(blocks)
        + "]</script>"
        + "".join(anchors)
    )


def test_backslash_before_cyrillic() -> None:
    raw = '{"a":"\\д"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    assert loads_listing_json(raw) == {"a": "\\д"}


def test_valid_pair_then_bad_escape() -> None:
    # Source text is {"a":"\\д\д"}: a valid pair, then a bad one.
    raw = '{"a":"\\\\д\\д"}'
    assert loads_listing_json(raw) == {"a": "\\д\\д"}


def test_three_backslashes_before_cyrillic() -> None:
    raw = '{"a":"\\\\\\д"}'
    assert loads_listing_json(raw) == {"a": "\\\\д"}


def test_short_unicode_escape_is_doubled() -> None:
    raw = '{"a":"\\u12"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    assert loads_listing_json(raw) == {"a": "\\u12"}


def test_four_hex_unicode_escape_is_kept() -> None:
    raw = '{"a":"\\u0041"}'
    assert repair_json_escapes(raw) == raw
    assert loads_listing_json(raw) == {"a": "A"}


def test_valid_json_is_unchanged() -> None:
    raw = '{"a":"line\\n\\"quote\\" \\\\ slash\\/ \\u0041"}'
    assert repair_json_escapes(raw) == raw
    assert loads_listing_json(raw) == {"a": "line\n\"quote\" \\ slash/ A"}


def test_raw_newline_parses() -> None:
    raw = '{"reviewBody":"one\ntwo"}'
    assert repair_json_escapes(raw) == raw
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    assert loads_listing_json(raw) == {"reviewBody": "one\ntwo"}


def test_trailing_backslash_is_doubled_and_still_invalid() -> None:
    raw = '{"a":"abc\\'
    assert repair_json_escapes(raw) == '{"a":"abc\\\\'
    with pytest.raises(ListingParseError):
        loads_listing_json(raw)


def test_requested_rating_does_not_raise() -> None:
    rows = [
        ("body", "2026-11-01 12:00:00", "1", str(i))
        for i in range(25)
    ]
    parsed = parse_listing(_listing(rows))
    assert len(parsed) == 25
    assert_rate(parsed, "1")


def test_other_rating_raises_rating_filter_missed() -> None:
    five = parse_listing(_listing([("body", "2026-11-01 12:00:00", "5", "1")]))
    other = parse_listing(_listing([("body", "2026-11-01 12:00:00", "2", "1")]))
    with pytest.raises(RatingFilterMissed):
        assert_rate(five, "1")
    with pytest.raises(RatingFilterMissed):
        assert_rate(other, "1")
    dotted = ParsedReview("b", datetime(2026, 11, 1), "Bank", "/p", "1.0")
    with pytest.raises(RatingFilterMissed):
        assert_rate([dotted], "1")


def test_aggregate_rating_is_not_a_review() -> None:
    rows = [("body", "2026-11-01 12:00:00", "1", str(i)) for i in range(25)]
    html = (
        '<script type="application/ld+json">'
        '{"@type":"AggregateRating","ratingValue":5}'
        "</script>"
        + _listing(rows)
    )
    parsed = parse_listing(html)
    assert len(parsed) == 25
    assert_rate(parsed, "1")
