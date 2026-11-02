import json
import logging
from datetime import datetime

import httpx
import pytest

from bankiru.parser.crawler import (
    BankiruCrawler,
    ParsedReview,
    collapse_locations,
    load_known_locations,
)
from bankiru.parser.errors import RatingFilterMissed

START = datetime(2026, 11, 1)
END = datetime(2026, 11, 2)
IN_WINDOW = "2026-11-01 12:00:00"
BEFORE = "2026-10-31 12:00:00"
CITY = '<span class="l3a372298">Город</span>'
LISTING_URL = "https://www.banki.ru/services/responses/list/"


class FakeClient:
    def __init__(self, texts: list[str | None]) -> None:
        self._texts = list(texts)
        self.urls: list[str] = []

    async def get(
        self,
        url: str,
        *,
        product: str = "",
        extra_headers: dict[str, str] | None = None,
        kind: str,
    ) -> httpx.Response | None:
        self.urls.append(url)
        text = self._texts.pop(0)
        if text is None:
            return None
        return httpx.Response(200, text=text)


def listing_html(reviews: list[tuple[str, str, str]]) -> str:
    nodes = []
    hrefs = []
    for rid, rating, stamp in reviews:
        nodes.append(
            {
                "@type": "Review",
                "reviewBody": "body " + rid,
                "datePublished": stamp,
                "itemReviewed": {"name": "Bank"},
                "reviewRating": {"ratingValue": rating},
            }
        )
        hrefs.append(
            f'<a href="/services/responses/bank/response/{rid}/" data>'
        )
    blob = json.dumps(nodes, ensure_ascii=False)
    return '<script type="application/ld+json">' + blob + "</script> " + " ".join(hrefs)


def _crawler(texts: list[str | None]) -> tuple[BankiruCrawler, FakeClient]:
    client = FakeClient(texts)
    return BankiruCrawler(client), client  # type: ignore[arg-type]


def _record(rid: str = "1") -> ParsedReview:
    return ParsedReview(
        "body",
        datetime(2026, 11, 1, 12, 0, 0),
        "Bank",
        f"/services/responses/bank/response/{rid}",
        "1",
    )


@pytest.mark.asyncio
async def test_second_product_reuses_location() -> None:
    crawler, client = _crawler([CITY])
    await crawler._enrich_one(_record(), "debitcards", LISTING_URL)
    await crawler._enrich_one(_record(), "hypothec", LISTING_URL)
    assert client.urls == [
        "https://www.banki.ru/services/responses/bank/response/1/"
    ]
    assert client.urls[0].endswith("/")
    assert all(not row["url"].endswith("/") for row in crawler.records)
    assert {row["location"] for row in crawler.records} == {"Город"}
    assert {row["product"] for row in crawler.records} == {
        "Дебетовая карта",
        "Ипотека",
    }
    assert crawler.cache_hits == 1
    assert crawler.cache_misses == 1


@pytest.mark.asyncio
async def test_none_detail_is_not_cached() -> None:
    crawler, client = _crawler([None, CITY])
    stored = "https://www.banki.ru/services/responses/bank/response/1"
    await crawler._enrich_one(_record(), "debitcards", LISTING_URL)
    assert stored not in crawler.location_cache
    await crawler._enrich_one(_record(), "hypothec", LISTING_URL)
    assert len(client.urls) == 2
    assert crawler.records[0]["location"] == ""
    assert crawler.records[1]["location"] == "Город"


@pytest.mark.asyncio
async def test_challenge_detail_is_fetched_again() -> None:
    crawler, client = _crawler(["__jhash_", CITY])
    await crawler._enrich_one(_record(), "debitcards", LISTING_URL)
    await crawler._enrich_one(_record(), "hypothec", LISTING_URL)
    assert len(client.urls) == 2
    assert crawler.records[0]["location"] == ""
    assert crawler.records[1]["location"] == "Город"


@pytest.mark.asyncio
async def test_missing_span_is_cached_as_empty() -> None:
    crawler, client = _crawler(["<html>no city</html>", CITY])
    await crawler._enrich_one(_record(), "debitcards", LISTING_URL)
    await crawler._enrich_one(_record(), "hypothec", LISTING_URL)
    assert len(client.urls) == 1
    assert crawler.records[0]["location"] == ""
    assert crawler.records[1]["location"] == ""


@pytest.mark.asyncio
async def test_known_locations_skip_the_get_and_still_store_rows() -> None:
    crawler, client = _crawler([CITY])
    stored = "https://www.banki.ru/services/responses/bank/response/1"
    crawler.known_locations[stored] = "Город"
    await crawler._enrich_one(_record(), "debitcards", LISTING_URL)
    await crawler._enrich_one(_record(), "hypothec", LISTING_URL)
    assert client.urls == []
    assert crawler.db_hits == 2
    assert crawler.cache_misses == 0
    assert len(crawler.records) == 2
    assert {row["product"] for row in crawler.records} == {
        "Дебетовая карта",
        "Ипотека",
    }


def test_collapse_locations_keeps_only_one_non_empty() -> None:
    assert collapse_locations([("u", "A"), ("u", "B")]) == {}
    assert collapse_locations([("u", ""), ("u", "A")]) == {}
    assert collapse_locations([("u", "A"), ("u", "A")]) == {"u": "A"}


@pytest.mark.asyncio
async def test_load_known_locations_swallows_session_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom() -> None:
        raise RuntimeError("postgresql://user:secret@host/db")

    monkeypatch.setattr("bankiru.parser.crawler.get_session_maker", boom)
    with caplog.at_level(logging.ERROR):
        found = await load_known_locations(START, END)
    assert found == {}
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_rate_one_stops_on_start_date_and_rate_two_still_runs() -> None:
    texts = [
        listing_html([("101", "1", IN_WINDOW), ("102", "1", BEFORE)]),
        CITY,
        listing_html([("201", "2", IN_WINDOW), ("202", "2", BEFORE)]),
        CITY,
    ]
    crawler, client = _crawler(texts)
    await crawler._crawl_product("debitcards", START, END)
    assert any("rate=2" in url for url in client.urls)
    bodies = [row["reviewBody"] for row in crawler.records]
    assert "body 101" in bodies
    assert "body 102" not in bodies


@pytest.mark.asyncio
async def test_empty_rate_two_page_leaves_rate_one_rows() -> None:
    texts = [
        listing_html([("101", "1", IN_WINDOW), ("102", "1", BEFORE)]),
        CITY,
        listing_html([]),
    ]
    crawler, client = _crawler(texts)
    await crawler._crawl_product("debitcards", START, END)
    assert any("rate=2" in url for url in client.urls)
    assert not any("page=2" in url and "rate=2" in url for url in client.urls)
    assert [row["reviewBody"] for row in crawler.records] == ["body 101"]


@pytest.mark.asyncio
async def test_listing_parse_error_on_rate_one_does_not_skip_rate_two() -> None:
    broken = '<script type="application/ld+json">{not json</script>'
    texts = [
        broken,
        listing_html([("201", "2", IN_WINDOW), ("202", "2", BEFORE)]),
        CITY,
    ]
    crawler, client = _crawler(texts)
    await crawler._crawl_product("debitcards", START, END)
    assert any("rate=1" in url for url in client.urls)
    assert any("rate=2" in url for url in client.urls)
    assert [row["reviewBody"] for row in crawler.records] == ["body 201"]


@pytest.mark.asyncio
async def test_rating_filter_missed_is_not_swallowed() -> None:
    crawler, client = _crawler([listing_html([("101", "2", IN_WINDOW)])])
    with pytest.raises(RatingFilterMissed):
        await crawler._crawl_product("debitcards", START, END)
    assert any("rate=1" in url for url in client.urls)
    assert not any("rate=2" in url for url in client.urls)


@pytest.mark.asyncio
async def test_challenge_on_rate_one_skips_rate_two() -> None:
    crawler, client = _crawler(["<html>__jhash_</html>"])
    await crawler._crawl_product("debitcards", START, END)
    assert crawler.saw_challenge is True
    assert not any("rate=2" in url for url in client.urls)


@pytest.mark.asyncio
async def test_challenge_on_one_product_still_fetches_both_rates_of_the_next() -> None:
    texts = [
        "<html>__jhash_</html>",
        listing_html([("201", "1", IN_WINDOW), ("202", "1", BEFORE)]),
        CITY,
        listing_html([("301", "2", IN_WINDOW), ("302", "2", BEFORE)]),
        CITY,
    ]
    crawler, client = _crawler(texts)
    await crawler._crawl_product("debitcards", START, END)
    await crawler._crawl_product("hypothec", START, END)
    debit = [url for url in client.urls if "/product/debitcards/" in url]
    hypo = [url for url in client.urls if "/product/hypothec/" in url]
    assert crawler.saw_challenge is True
    assert debit and all("rate=1" in url for url in debit)
    assert not any("rate=2" in url for url in debit)
    assert any("rate=1" in url for url in hypo)
    assert any("rate=2" in url for url in hypo)


@pytest.mark.asyncio
async def test_cache_line_is_logged_when_rating_filter_missed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        "bankiru.parser.crawler.PRODUCTS",
        {"debitcards": "Дебетовая карта"},
    )

    async def empty_map(start: datetime, end: datetime) -> dict[str, str]:
        return {}

    monkeypatch.setattr("bankiru.parser.crawler.load_known_locations", empty_map)
    crawler, client = _crawler([listing_html([("101", "5", IN_WINDOW)])])
    with caplog.at_level(logging.INFO, logger="bankiru.parser.crawler"):
        with pytest.raises(RatingFilterMissed):
            await crawler.crawl_all(start_date=START, end_date=END)
    assert "detail cache hits=0 misses=0 db_hits=0" in caplog.text
    assert not any("rate=2" in url for url in client.urls)
