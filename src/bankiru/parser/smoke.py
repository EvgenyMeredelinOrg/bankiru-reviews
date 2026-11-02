from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
import time

from sqlalchemy import select

from bankiru.config import get_settings
from bankiru.db import get_session_maker
from bankiru.models import Review
from bankiru.parser.client import BankiruClient
from bankiru.parser.crawler import BankiruCrawler


def smoke_missing_stored(
    collected: list[dict],
    stored: set[tuple[str, str]],
) -> int:
    have = {(row["url"], row["product"]) for row in collected}
    return sum(1 for url, product in stored if (url, product) not in have)


async def _stored(start: dt.datetime, end: dt.datetime) -> set[tuple[str, str]]:
    async with get_session_maker()() as session:
        rows = (
            await session.execute(
                select(Review.url, Review.product).where(
                    Review.datePublished >= start,
                    Review.datePublished < end,
                )
            )
        ).all()
    return {(url, product) for url, product in rows}


async def _run(day: str) -> int:
    start = dt.datetime.strptime(day, "%Y-%m-%d")
    end = start + dt.timedelta(days=1)
    run_deadline = time.monotonic() + get_settings().PARSER_RUN_DEADLINE_SEC
    async with BankiruClient(run_deadline=run_deadline) as client:
        collected = await BankiruCrawler(client).crawl_all(
            start_date=start,
            end_date=end,
        )
    stored = await _stored(start, end)
    missing = smoke_missing_stored(collected, stored)
    print(len(collected), len(stored), missing)
    if len(stored) == 0 or missing != 0:
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.date)))


if __name__ == "__main__":
    main()
