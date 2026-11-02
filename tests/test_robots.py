from pathlib import Path

from bankiru.parser.robots import matching_disallow, parse_robots
from bankiru.parser.settings import USER_AGENT

BODY = (Path(__file__).parent / "fixtures" / "robots-star.txt").read_text()
GROUPS = parse_robots(BODY)
LISTING = (
    "https://www.banki.ru/services/responses/list/product/creditcards/"
    "?page=1&type=all"
)
DETAIL = "https://www.banki.ru/services/responses/bank/response/13519173/"


def test_listing_and_detail_allowed() -> None:
    assert matching_disallow(GROUPS, LISTING, USER_AGENT) is None
    assert matching_disallow(GROUPS, DETAIL, USER_AGENT) is None


def test_filter_query_and_comments_denied() -> None:
    filtered = "https://www.banki.ru/services/responses/list/?page=1&f[a]=1"
    comments = "https://www.banki.ru/services/responses/bank/response/13519173/comments/"
    assert matching_disallow(GROUPS, filtered, USER_AGENT) is not None
    assert matching_disallow(GROUPS, comments, USER_AGENT) is not None


def test_comparserbot_rule_does_not_apply() -> None:
    assert matching_disallow(GROUPS, "https://www.banki.ru/", USER_AGENT) is None


def test_rate_query_is_denied_and_plain_listing_is_allowed() -> None:
    rated = LISTING + "&rate[]=1&rate[]=2"
    assert matching_disallow(GROUPS, rated, USER_AGENT) == "*rate[]=*"
    assert matching_disallow(GROUPS, LISTING, USER_AGENT) is None


def test_specific_agent_beats_star() -> None:
    assert matching_disallow(GROUPS, "https://www.banki.ru/private", USER_AGENT) == "/private"
