import httpx
import pytest

from bankiru.config import get_settings
from bankiru.parser.client import BankiruClient
from bankiru.parser.crawler import location_from_detail
from bankiru.parser.errors import ListingFetchError


def _budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PARSER_CONNECT_RETRY_BUDGET_SEC", "1")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_listing_connect_errors_stop_inside_a_short_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _budget(monkeypatch)
    calls = {"n": 0}
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    async def sleep(delay: float) -> None:
        clock["t"] += delay

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("down", request=request)

    client = BankiruClient(transport=httpx.MockTransport(handler), sleep=sleep, now=now)
    client._sleep_min = 10.0
    client._sleep_max = 10.0
    client._ban_pause_max = 300.0
    client._robots = ()
    with pytest.raises(ListingFetchError):
        await client.get(
            "https://www.banki.ru/services/responses/list/product/creditcards/?page=1",
            kind="listing",
        )
    assert calls["n"] < 20
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_detail_connect_budget_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _budget(monkeypatch)
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    async def sleep(delay: float) -> None:
        clock["t"] += delay

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = BankiruClient(transport=httpx.MockTransport(handler), sleep=sleep, now=now)
    client._sleep_min = 10.0
    client._sleep_max = 10.0
    client._robots = ()
    assert (
        await client.get(
            "https://www.banki.ru/services/responses/bank/response/1/",
            kind="detail",
        )
        is None
    )
    get_settings.cache_clear()


def test_missing_detail_keeps_empty_location() -> None:
    assert location_from_detail(None) == ""
    assert location_from_detail("__jhash_") == ""
