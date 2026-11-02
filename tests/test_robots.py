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
    # The fixture's longest prefix of USER_AGENT is bankiru-reviews, which does
    # not inherit the `*` rules. An agent that only matches `*` still is denied.
    other = "other-crawler/1.0"
    assert matching_disallow(GROUPS, filtered, other) is not None
    assert matching_disallow(GROUPS, comments, other) is not None
    assert matching_disallow(GROUPS, filtered, USER_AGENT) is None
    assert matching_disallow(GROUPS, comments, USER_AGENT) is None


def test_comparserbot_rule_does_not_apply() -> None:
    assert matching_disallow(GROUPS, "https://www.banki.ru/", USER_AGENT) is None


LIVE_SHAPED = "User-agent: *\nDisallow: *rate[]=*\nDisallow: *?source=*\n"


def test_longer_group_does_not_inherit_star_rules() -> None:
    rated = LISTING + "&rate[]=1&rate[]=2"
    encoded = LISTING + "&rate%5B%5D=1"
    assert matching_disallow(GROUPS, rated, USER_AGENT) is None
    assert matching_disallow(GROUPS, encoded, USER_AGENT) is None
    assert matching_disallow(GROUPS, "https://www.banki.ru/private", USER_AGENT) == "/private"


def test_star_group_denies_bracket_and_allows_single_rate() -> None:
    groups = parse_robots(LIVE_SHAPED)
    rated = LISTING + "&rate[]=1&rate[]=2"
    encoded = LISTING + "&rate%5B%5D=1"
    rate1 = LISTING + "&rate=1"
    rate2 = (
        "https://www.banki.ru/services/responses/list/product/creditcards/"
        "?page=2&type=all&rate=2"
    )
    assert matching_disallow(groups, rated, USER_AGENT) == "*rate[]=*"
    assert matching_disallow(groups, encoded, USER_AGENT) == "*rate[]=*"
    assert matching_disallow(groups, rate1, USER_AGENT) is None
    assert matching_disallow(groups, rate2, USER_AGENT) is None
    other = "other-crawler/1.0"
    assert matching_disallow(GROUPS, rated, other) == "*rate[]=*"
    assert matching_disallow(GROUPS, encoded, other) == "*rate[]=*"


def test_specific_agent_beats_star() -> None:
    assert matching_disallow(GROUPS, "https://www.banki.ru/private", USER_AGENT) == "/private"
