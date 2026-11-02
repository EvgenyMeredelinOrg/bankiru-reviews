import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class RobotsRule:
    directive: Literal["allow", "disallow"]
    path: str


@dataclass(frozen=True)
class RobotsGroup:
    agents: tuple[str, ...]
    rules: tuple[RobotsRule, ...]


def parse_robots(text: str) -> tuple[RobotsGroup, ...]:
    groups: list[RobotsGroup] = []
    agents: list[str] = []
    rules: list[RobotsRule] = []

    def flush() -> None:
        nonlocal agents, rules
        if agents or rules:
            groups.append(RobotsGroup(tuple(agents), tuple(rules)))
        agents = []
        rules = []

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if rules:
                flush()
            agents.append(value)
        elif key == "allow" and agents:
            rules.append(RobotsRule("allow", value))
        elif key == "disallow" and agents:
            rules.append(RobotsRule("disallow", value))
    flush()
    return tuple(groups)


def _matches(pattern: str, target: str) -> bool:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "^" + ".*".join(re.escape(part) for part in body.split("*"))
    if anchored:
        regex += "$"
    return re.match(regex, target) is not None


def _best_rule(rules: tuple[RobotsRule, ...], target: str) -> RobotsRule | None:
    best: RobotsRule | None = None
    for rule in rules:
        if not _matches(rule.path, target):
            continue
        if best is None or len(rule.path) > len(best.path):
            best = rule
        elif len(rule.path) == len(best.path) and rule.directive == "allow":
            best = rule
    return best


def matching_disallow(
    groups: tuple[RobotsGroup, ...],
    url: str,
    user_agent: str,
) -> str | None:
    split = urlsplit(url)
    target = unquote(split.path + (f"?{split.query}" if split.query else ""))
    chosen: RobotsGroup | None = None
    chosen_rank: int | None = None
    ua = user_agent.lower()
    for group in groups:
        rank: int | None = None
        for name in group.agents:
            token = name.lower()
            if token == "*":
                this = 0
            elif ua.startswith(token):
                this = len(token)
            else:
                continue
            if rank is None or this > rank:
                rank = this
        if rank is None:
            continue
        if chosen_rank is None or rank > chosen_rank:
            chosen = group
            chosen_rank = rank
    best: RobotsRule | None = None
    for rule in (chosen.rules if chosen is not None else ()):
        if not _matches(rule.path, target):
            continue
        if best is None or len(rule.path) > len(best.path):
            best = rule
        elif len(rule.path) == len(best.path) and rule.directive == "allow":
            best = rule
    if best is None or best.directive == "allow" or best.path == "":
        return None
    return best.path
