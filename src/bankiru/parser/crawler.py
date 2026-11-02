"""Fully sequential crawler on top of `BankiruClient`.

Products are crawled one by one. Each product has two listing passes,
``rate=1`` then ``rate=2``. Pages and detail pages are fetched one by one.
Every request goes through the same back-off client.

A listing fetch or parse error ends that rate pass. The other rate still
runs. A challenge page ends both passes of that product. Later products
still run, and a partial collection is still POSTed. A page whose reviews
are not the requested rate raises ``RatingFilterMissed`` and the run does
not POST. A detail URL already resolved in this run, or already stored for
this date window with one non-empty location, reuses that location and
still stores the row. Connect retries stop at the per-URL budget. The run
itself stops at the deadline passed into the client.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import logfire
import pandas as pd
from dateutil.relativedelta import relativedelta
from dateutil.utils import today
from sqlalchemy import select

from bankiru.config import get_settings
from bankiru.db import get_session_maker
from bankiru.models import Review
from bankiru.parser.challenge import is_challenge_page
from bankiru.parser.client import BankiruClient
from bankiru.parser.errors import (
    ChallengePageError,
    EmptyMarkupError,
    ListingFetchError,
    ListingParseError,
    RatingFilterMissed,
)
from bankiru.parser.settings import (
    BASE_URL,
    LOC_PATTERN,
    PAGE_URL,
    PRODUCTS,
    RATES,
    REVIEW_URL_PATTERN,
)
from bankiru.parser.tools import clean_text_pipe

logger = logging.getLogger("bankiru.parser.crawler")


@dataclass(frozen=True)
class ParsedReview:
    review_body: str
    date_published: dt.datetime
    bank_name: str
    detail_path: str
    rating: str = "1"


def _walk_reviews(node: object) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        raw_type = node.get("@type")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        if "Review" in types:
            found.append(node)
        for value in node.values():
            found.extend(_walk_reviews(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_reviews(item))
    return found


_VALID_ESCAPE = re.compile(r'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})')


def repair_json_escapes(raw: str) -> str:
    """Keep valid JSON escapes intact and double every other backslash.

    Valid pairs are a backslash plus one of " \\ / b f n r t, or a backslash
    u and four hex digits. A valid pair is copied as two (or six) characters.
    Any other backslash, including a short \\u12 and a trailing backslash,
    becomes two backslashes. The v6 regex did not consume the valid pair, so
    a later bad backslash in the same string survived.
    """
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        if raw[i] != "\\":
            out.append(raw[i])
            i += 1
            continue
        match = _VALID_ESCAPE.match(raw, i)
        if match:
            out.append(match.group(0))
            i = match.end()
        else:
            out.append("\\\\")
            i += 1
    return "".join(out)


def loads_listing_json(raw: str) -> object:
    try:
        return json.loads(repair_json_escapes(raw), strict=False)
    except json.JSONDecodeError as exc:
        raise ListingParseError("json-ld") from exc


def parse_listing(html: str) -> list[ParsedReview]:
    """Return every Review on the page, in listing order, any rating."""
    if is_challenge_page(html):
        raise ChallengePageError("challenge page")
    reviews: list[dict[str, Any]] = []
    for match in re.finditer(
        r"""<script[^>]*type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        payload = loads_listing_json(match.group(1))
        reviews.extend(_walk_reviews(payload))
    page_text = " ".join(html.split())
    paths = [item.group(1) for item in REVIEW_URL_PATTERN.finditer(page_text)]
    if len(reviews) != len(paths):
        raise ListingParseError(f"{len(reviews)} reviews vs {len(paths)} urls")
    parsed: list[ParsedReview] = []
    for review, path in zip(reviews, paths):
        try:
            body = review["reviewBody"]
            stamp = dt.datetime.strptime(review["datePublished"], "%Y-%m-%d %H:%M:%S")
            bank = review["itemReviewed"]["name"].strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ListingParseError("review fields") from exc
        rating_node = review.get("reviewRating")
        rating = ""
        if isinstance(rating_node, dict):
            rating = str(rating_node.get("ratingValue", ""))
        parsed.append(ParsedReview(body, stamp, bank, path, rating))
    return parsed


def assert_rate(records: list[ParsedReview], rate: str) -> None:
    for record in records:
        if record.rating != rate:
            raise RatingFilterMissed(rate)


def listing_records(html: str) -> list[ParsedReview]:
    """Reviews with ratingValue 1 or 2. Pagination uses `parse_listing`."""
    return [row for row in parse_listing(html) if row.rating in {"1", "2"}]


def select_page(
    records: list[ParsedReview],
    start_date: dt.datetime,
    end_date: dt.datetime,
) -> tuple[list[ParsedReview], bool]:
    kept: list[ParsedReview] = []
    for record in records:
        if record.date_published >= end_date:
            continue
        if record.date_published < start_date:
            return kept, True
        kept.append(record)
    return kept, False


def location_from_detail(response_text: str | None) -> str:
    if response_text is None or is_challenge_page(response_text):
        return ""
    match = LOC_PATTERN.search(response_text)
    return match.group(1) if match else ""


def detail_request_url(stored_url: str) -> str:
    if stored_url.endswith("/"):
        return stored_url
    return stored_url + "/"


def collapse_locations(rows: list[tuple[str, str]]) -> dict[str, str]:
    """Keep a url only when every location in the window agrees and is non-empty."""
    grouped: dict[str, set[str]] = {}
    for url, location in rows:
        grouped.setdefault(url, set()).add(location)
    known: dict[str, str] = {}
    for url, locations in grouped.items():
        if len(locations) != 1:
            continue
        only = next(iter(locations))
        if only:
            known[url] = only
    return known


async def load_known_locations(
    start_date: dt.datetime,
    end_date: dt.datetime,
) -> dict[str, str]:
    try:
        async with get_session_maker()() as session:
            rows = (
                await session.execute(
                    select(Review.url, Review.location).where(
                        Review.datePublished >= start_date,
                        Review.datePublished < end_date,
                    )
                )
            ).all()
    except Exception as exc:
        logger.error("location prefetch failed: %s", type(exc).__name__)
        return {}
    return collapse_locations([(url, location) for url, location in rows])


class BankiruCrawler:
    """Sequential crawler. One instance per run; pass it a fresh `BankiruClient`."""

    def __init__(self, client: BankiruClient) -> None:
        self.client = client
        self.records: list[dict[str, str]] = []
        self.seen_review = False
        self.saw_challenge = False
        self.location_cache: dict[str, str] = {}
        self.known_locations: dict[str, str] = {}
        self.cache_hits = 0
        self.cache_misses = 0
        self.db_hits = 0

    async def crawl_all(
        self,
        *,
        start_date: dt.datetime | None = None,
        end_date: dt.datetime | None = None,
        days: int = 1,
    ) -> list[dict[str, str]]:
        """Crawl every product sequentially; return deduplicated records."""
        tz = ZoneInfo(get_settings().PARSER_TIMEZONE)
        start_date = start_date or (today(tz) - relativedelta(days=days))
        end_date = end_date or today(tz)

        # banki.ru stamps are naive and implicitly in PARSER_TIMEZONE.
        start_date = start_date.replace(tzinfo=None)
        end_date = end_date.replace(tzinfo=None)

        with logfire.span(
            "crawl_all start={start} end={end}",
            start=start_date.isoformat(), end=end_date.isoformat(),
        ):
            self.seen_review = False
            self.known_locations = await load_known_locations(start_date, end_date)
            try:
                for product in PRODUCTS:
                    await self._crawl_product(product, start_date, end_date)
                if not self.seen_review:
                    if self.saw_challenge:
                        raise ChallengePageError("challenge page")
                    raise EmptyMarkupError("no review markup")
                return self._deduplicated()
            finally:
                logger.info(
                    "detail cache hits=%s misses=%s db_hits=%s",
                    self.cache_hits,
                    self.cache_misses,
                    self.db_hits,
                )

    def _deduplicated(self) -> list[dict[str, str]]:
        """Remove duplicate reviews by (reviewBody, product) combination."""
        if not self.records:
            return []
        rows: list[dict[str, str]] = (
            pd.DataFrame.from_records(self.records)
            .drop_duplicates(subset=["reviewBody", "product"])
            .to_dict(orient="records")
        )
        return rows

    async def _crawl_product(
        self,
        product: str,
        start_date: dt.datetime,
        end_date: dt.datetime,
    ) -> None:
        with logfire.span("crawl product={product}", product=product):
            for rate in RATES:
                await self._crawl_rate(product, rate, start_date, end_date)
                if self.saw_challenge:
                    return

    async def _crawl_rate(
        self,
        product: str,
        rate: str,
        start_date: dt.datetime,
        end_date: dt.datetime,
    ) -> None:
        for page in itertools.count(1):
            url = PAGE_URL.format(
                base=BASE_URL, product=product, page=page, rate=rate,
            )
            try:
                response = await self.client.get(url, product=product, kind="listing")
                if response is None:
                    raise ListingFetchError(url)
                records = parse_listing(response.text)
                assert_rate(records, rate)
            except RatingFilterMissed:
                raise
            except (ListingFetchError, ListingParseError) as exc:
                logger.error(
                    "parser product skipped: %s product=%s rate=%s url=%s",
                    type(exc).__name__,
                    product,
                    rate,
                    url,
                )
                return
            except ChallengePageError:
                self.saw_challenge = True
                logger.error(
                    "parser product skipped: ChallengePageError product=%s rate=%s url=%s",
                    product,
                    rate,
                    url,
                )
                return
            if page == 1 and records:
                self.seen_review = True
            if not records:
                return
            kept, hit_left = select_page(records, start_date, end_date)
            for record in kept:
                await self._enrich_one(record, product, url)
            if hit_left:
                return

    async def _enrich_one(
        self,
        record: ParsedReview,
        product: str,
        listing_url: str,
    ) -> None:
        stored_url = BASE_URL + record.detail_path
        if stored_url in self.known_locations:
            self.db_hits += 1
            location = self.known_locations[stored_url]
        elif stored_url in self.location_cache:
            self.cache_hits += 1
            location = self.location_cache[stored_url]
        else:
            self.cache_misses += 1
            response = await self.client.get(
                detail_request_url(stored_url),
                product=product,
                extra_headers={"Referer": listing_url},
                kind="detail",
            )
            if response is None or is_challenge_page(response.text):
                location = ""
            else:
                location = location_from_detail(response.text)
                self.location_cache[stored_url] = location
        self.records.append(
            {
                "datePublished": record.date_published.strftime("%Y-%m-%d %H:%M:%S"),
                "reviewBody": clean_text_pipe(record.review_body),
                "bankName": record.bank_name,
                "url": stored_url,
                "location": location,
                "product": PRODUCTS[product],
            }
        )
