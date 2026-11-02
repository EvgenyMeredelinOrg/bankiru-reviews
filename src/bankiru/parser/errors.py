class ListingParseError(Exception):
    pass


class ChallengePageError(Exception):
    pass


class EmptyMarkupError(Exception):
    pass


class RobotsDenied(Exception):
    def __init__(self, rule: str, path: str) -> None:
        self.rule = rule
        self.path = path
        super().__init__(f"{rule} {path}")


class ListingFetchError(Exception):
    pass


class CrawlDeadlineError(Exception):
    pass


class RatingFilterMissed(Exception):
    pass
