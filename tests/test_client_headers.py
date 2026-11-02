import httpx
import pytest

from bankiru.parser.client import BankiruClient
from bankiru.parser.errors import RobotsDenied
from bankiru.parser.robots import RobotsGroup, RobotsRule
from bankiru.parser.settings import ACCEPT_LANGUAGE, USER_AGENT

LISTING = (
    "https://www.banki.ru/services/responses/list/product/creditcards/"
    "?page=1&type=all&rate[]=1&rate[]=2"
)


def test_identity_headers() -> None:
    client = BankiruClient()
    assert client._identity_headers() == {
        "User-Agent": USER_AGENT,
        "Accept-Language": ACCEPT_LANGUAGE,
    }


@pytest.mark.asyncio
async def test_disallow_does_not_call_transport() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="<html></html>")

    client = BankiruClient(transport=httpx.MockTransport(handler))
    client._robots = (RobotsGroup(("*",), (RobotsRule("disallow", "/"),)),)
    with pytest.raises(RobotsDenied):
        await client.get(LISTING, kind="listing")
    assert calls["n"] == 0
