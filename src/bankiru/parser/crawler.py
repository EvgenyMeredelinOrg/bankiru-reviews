"""Fully sequential crawler on top of `BankiruClient`.

Products are crawled one by one. Within each product, listing pages are
fetched one by one. For each listing page, detail pages are fetched one by
one. Every request goes through the same back-off client — there is no
concurrency anywhere.

A listing fetch or parse error ends that product. Other products still run,
and a partial collection is still POSTed. Connect retries stop at the
per-URL budget. The run itself stops at the deadline passed into the client.
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

from bankiru.config import get_settings
from bankiru.parser.challenge import is_challenge_page
from bankiru.parser.client import BankiruClient
from bankiru.parser.errors import (
    ChallengePageError,
    EmptyMarkupError,
    ListingFetchError,
    ListingParseError,
)
from bankiru.parser.settings import (
    BASE_URL,
    LOC_PATTERN,
    PAGE_URL,
    PRODUCTS,
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


def listing_records(html: str) -> list[ParsedReview]:
    if is_challenge_page(html):
        raise ChallengePageError("challenge page")
    reviews: list[dict[str, Any]] = []
    for match in re.finditer(
        r"""<script[^>]*type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise ListingParseError("json-ld") from exc
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
    return [row for row in parsed if row.rating in {"1", "2"}]


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


class BankiruCrawler:
    """Sequential crawler. One instance per run; pass it a fresh `BankiruClient`."""

    def __init__(self, client: BankiruClient) -> None:
        self.client = client
        self.records: list[dict[str, str]] = []
        self.seen_review = False
        self.saw_challenge = False

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
            for product in PRODUCTS:
                await self._crawl_product(product, start_date, end_date)
            if not self.seen_review:
                if self.saw_challenge:
                    raise ChallengePageError("challenge page")
                raise EmptyMarkupError("no review markup")
            return self._deduplicated()

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
        """Crawl listing pages for one product. Fetch and parse errors end it."""
        with logfire.span("crawl product={product}", product=product):
            for page in itertools.count(1):
                url = PAGE_URL.format(base=BASE_URL, product=product, page=page)
                try:
                    response = await self.client.get(url, product=product, kind="listing")
                    if response is None:
                        raise ListingFetchError(url)
                    records = listing_records(response.text)
                except (ListingFetchError, ListingParseError) as exc:
                    logger.error(
                        "parser product skipped: %s product=%s url=%s",
                        type(exc).__name__,
                        product,
                        url,
                    )
                    return
                except ChallengePageError:
                    self.saw_challenge = True
                    logger.error(
                        "parser product skipped: ChallengePageError product=%s url=%s",
                        product,
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
        review_url = BASE_URL + record.detail_path
        response = await self.client.get(
            review_url,
            product=product,
            extra_headers={"Referer": listing_url},
            kind="detail",
        )
        text = response.text if response is not None else None
        self.records.append(
            {
                "datePublished": record.date_published.strftime("%Y-%m-%d %H:%M:%S"),
                "reviewBody": clean_text_pipe(record.review_body),
                "bankName": record.bank_name,
                "url": review_url,
                "location": location_from_detail(text),
                "product": PRODUCTS[product],
            }
        )
