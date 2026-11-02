"""Async HTTP client for banki.ru.

Every request sleeps a random interval drawn from
``uniform(PARSER_SLEEP_MIN, PARSER_SLEEP_MAX)`` (default 10–20 s).

Retry policy
------------
- **Connect errors** (``ConnectTimeout``, ``ConnectError``) back off up to
  ``PARSER_BAN_PAUSE_MAX`` and keep retrying until
  ``PARSER_CONNECT_RETRY_BUDGET_SEC`` (default 900 s) on that URL. A listing
  URL then raises ``ListingFetchError``. A detail URL returns ``None``.
- **Other errors** give up after ``PARSER_MAX_RETRIES`` non-connect failures,
  with the same listing / detail split.
- The whole run stops when ``now`` passes the ``run_deadline`` handed in by
  ``run_once`` (``PARSER_RUN_DEADLINE_SEC``, default 21600 s).

Every request sends ``USER_AGENT`` and ``ACCEPT_LANGUAGE``. Warmup reads
``/robots.txt`` and ``get`` refuses a URL ``matching_disallow`` denies.

Keepalive is disabled (fresh TCP connection per request).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Literal
from urllib.parse import urlsplit

import httpx
import logfire

from bankiru.config import get_settings
from bankiru.parser.challenge import is_challenge_page
from bankiru.parser.errors import CrawlDeadlineError, ListingFetchError, RobotsDenied
from bankiru.parser.robots import RobotsGroup, matching_disallow, parse_robots
from bankiru.parser.settings import (
    ACCEPT_LANGUAGE,
    BASE_HEADERS,
    BASE_URL,
    USER_AGENT,
)

_ROBOTS_URL = "https://www.banki.ru/robots.txt"
_HOME_URL = "https://www.banki.ru/"
_ROBOTS_BODY_LIMIT = 262144


def _is_connect_error(exc: BaseException) -> bool:
    """Return True if the exception signals a TCP-level connect failure."""
    return isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError))


class BankiruClient:
    """Sequential async HTTP client with randomised pacing and bounded retries."""

    def __init__(
        self,
        *,
        run_deadline: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        """Initialise the HTTP client with settings from config.py."""
        s = get_settings()
        self._sleep_min = s.PARSER_SLEEP_MIN
        self._sleep_max = s.PARSER_SLEEP_MAX
        self._max_retries = s.PARSER_MAX_RETRIES
        self._ban_pause_max = s.PARSER_BAN_PAUSE_MAX
        self._run_deadline = run_deadline
        self._sleep = sleep or asyncio.sleep
        self._now = now or time.monotonic
        self._robots: tuple[RobotsGroup, ...] | None = None
        timeout = httpx.Timeout(
            connect=s.PARSER_CONNECT_TIMEOUT,
            read=s.PARSER_READ_TIMEOUT,
            write=10.0,
            pool=5.0,
        )
        limits = httpx.Limits(
            max_connections=1,
            max_keepalive_connections=0,
            keepalive_expiry=0,
        )
        hooks: dict[str, list] = {"response": [self._refuse_denied_redirect]}
        if transport is None:
            self._client = httpx.AsyncClient(
                http2=False,
                timeout=timeout,
                follow_redirects=True,
                limits=limits,
                headers=BASE_HEADERS,
                event_hooks=hooks,
            )
        else:
            self._client = httpx.AsyncClient(
                http2=False,
                timeout=timeout,
                follow_redirects=True,
                limits=limits,
                headers=BASE_HEADERS,
                transport=transport,
                event_hooks=hooks,
            )

    def _identity_headers(self) -> dict[str, str]:
        return {"User-Agent": USER_AGENT, "Accept-Language": ACCEPT_LANGUAGE}

    def _random_sleep(self) -> float:
        """Return a random sleep duration in [sleep_min, sleep_max]."""
        return random.uniform(self._sleep_min, self._sleep_max)

    async def __aenter__(self) -> BankiruClient:
        """Warm up the session, then load robots.txt, before crawling."""
        await self.warmup()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

    async def warmup(self) -> None:
        """Seed the cookie jar, then store a parsed robots.txt.

        Raises RuntimeError if the homepage is unreachable.
        Raises RobotsDenied if robots.txt is missing, a challenge page,
        or does not contain a User-agent line.
        """
        with logfire.span("warmup banki.ru session"):
            await self._sleep(self._random_sleep())
            try:
                resp = await self._client.get(
                    BASE_URL + "/",
                    headers=self._identity_headers(),
                )
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                raise RuntimeError(f"warmup failed — origin unreachable: {exc!r}") from exc
            if resp.status_code >= 400:
                logfire.warning("warmup status={status}", status=resp.status_code)

            await self._sleep(self._random_sleep())
            try:
                robots = await self._client.get(
                    _ROBOTS_URL,
                    headers=self._identity_headers(),
                )
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                raise RobotsDenied("unusable", "/robots.txt") from exc
            body = robots.text[:_ROBOTS_BODY_LIMIT]
            if (
                robots.status_code != 200
                or is_challenge_page(body)
                or "user-agent" not in body.lower()
            ):
                raise RobotsDenied("unusable", "/robots.txt")
            self._robots = parse_robots(body)

    def _check_robots(self, url: str) -> None:
        if url == _HOME_URL or url == _ROBOTS_URL:
            return
        path = urlsplit(url).path
        if urlsplit(url).query:
            path = path + "?" + urlsplit(url).query
        if self._robots is None:
            raise RobotsDenied("not-loaded", path)
        denied = matching_disallow(self._robots, url, USER_AGENT)
        if denied is not None:
            raise RobotsDenied(denied, path)

    async def _refuse_denied_redirect(self, response: httpx.Response) -> None:
        if not response.has_redirect_location:
            return
        location = response.headers["location"]
        target = str(response.url.join(location))
        self._check_robots(target)

    async def get(
        self,
        url: str,
        *,
        product: str = "",
        extra_headers: dict[str, str] | None = None,
        kind: Literal["listing", "detail"],
    ) -> httpx.Response | None:
        """Sleep, then GET. Listing failures raise; detail failures return None."""
        self._check_robots(url)
        url_deadline = self._now() + get_settings().PARSER_CONNECT_RETRY_BUDGET_SEC
        headers = extra_headers or {}
        non_connect_failures = 0
        ban_streak = 0

        attempt = 0
        while True:
            attempt += 1
            now = self._now()
            if self._run_deadline is not None and now >= self._run_deadline:
                raise CrawlDeadlineError(url)
            if ban_streak and now >= url_deadline:
                if kind == "listing":
                    raise ListingFetchError(url)
                return None

            if ban_streak > 0:
                base = min(
                    self._sleep_max * 2 ** (ban_streak - 1),
                    self._ban_pause_max,
                )
                delay = min(
                    base * (0.5 + random.random()),
                    self._ban_pause_max,
                )
            else:
                delay = self._random_sleep()

            await self._sleep(delay)

            try:
                response = await self._client.get(
                    url,
                    headers={**self._identity_headers(), **headers},
                )
                logfire.info(
                    "parse {product} {url} attempt={attempt} status={status}",
                    product=product, url=url, attempt=attempt, status=response.status_code,
                )
                if response.status_code == 200:
                    return response

                ban_streak = 0
                non_connect_failures += 1

            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                if _is_connect_error(exc):
                    ban_streak += 1
                    logfire.warning(
                        "parse {product} {url} attempt={attempt} connect_error={error} "
                        "ban_streak={streak} (backing off)",
                        product=product, url=url, attempt=attempt,
                        error=repr(exc), streak=ban_streak,
                    )
                    non_connect_failures = 0
                    continue

                ban_streak = 0
                logfire.info(
                    "parse {product} {url} attempt={attempt} error={error}",
                    product=product, url=url, attempt=attempt, error=repr(exc),
                )
                non_connect_failures += 1

            if non_connect_failures > self._max_retries:
                logfire.info(
                    "parse {product} {url}: giving up after {n} non-connect failures",
                    product=product, url=url, n=non_connect_failures,
                )
                if kind == "listing":
                    raise ListingFetchError(url)
                return None
