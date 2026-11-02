from datetime import datetime
from pathlib import Path

import httpx
import pytest

from bankiru.parser.client import BankiruClient
from bankiru.parser.crawler import (
    BankiruCrawler,
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


@pytest.mark.asyncio
async def test_high_ratings_do_not_hide_a_later_low_rating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Page 1 is only a 5. Page 2 has a rating 1 inside the window, then an older review.

    The rating 1 is stored. The review dated before start_date is not.
    The run does not raise EmptyMarkupError.
    """
    monkeypatch.setattr(
        "bankiru.parser.crawler.PRODUCTS",
        {"creditcards": "Кредитная карта"},
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if "page=1" in url:
            return httpx.Response(
                200,
                text=_listing([("high only", "2026-10-31 18:00:00", "5", "501")]),
            )
        if "page=2" in url:
            return httpx.Response(
                200,
                text=_listing(
                    [
                        ("low in window", "2026-10-31 08:00:00", "1", "101"),
                        ("too old", "2026-10-30 12:00:00", "3", "301"),
                    ]
                ),
            )
        if "/response/101" in url:
            return httpx.Response(200, text='<span class="l3a372298">Moscow</span>')
        return httpx.Response(200, text="<html></html>")

    async def sleep(_delay: float) -> None:
        return None

    client = BankiruClient(
        transport=httpx.MockTransport(handler),
        sleep=sleep,
        now=lambda: 0.0,
    )
    client._robots = ()
    rows = await BankiruCrawler(client).crawl_all(
        start_date=datetime(2026, 10, 31),
        end_date=datetime(2026, 11, 1),
    )
    assert [row["reviewBody"] for row in rows] == ["low in window"]
    assert rows[0]["location"] == "Moscow"
    assert rows[0]["product"] == "Кредитная карта"
    assert any("page=2" in url for url in seen)
    assert not any("page=3" in url for url in seen)
    assert not any("/response/501" in url for url in seen)
    assert not any("/response/301" in url for url in seen)
