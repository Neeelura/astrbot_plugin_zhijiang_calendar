from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
from typing import Iterable


SOURCE_URL = "https://asoul.love/#diary-drift"
FETCH_URL = "https://asoul.love/"
_SHANGHAI_TIME = timezone(timedelta(hours=8), name="Asia/Shanghai")
_MEMBER_NAMES = {
    "ava": "向晚",
    "bella": "贝拉",
    "carol": "珈乐",
    "diana": "嘉然",
    "eileen": "乃琳",
    "fiona": "心宜",
    "gladys": "思诺",
}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class ScheduleFetchError(RuntimeError):
    """Raised when the schedule page cannot be fetched or parsed."""


@dataclass(frozen=True, slots=True)
class ScheduleItem:
    time: str
    title: str
    streamers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "time": self.time,
            "title": self.title,
            "streamers": list(self.streamers),
        }

    @classmethod
    def from_dict(cls, value: object) -> ScheduleItem:
        if not isinstance(value, dict):
            raise ValueError("schedule item must be an object")
        item_time = value.get("time")
        title = value.get("title")
        streamers = value.get("streamers", [])
        if not isinstance(item_time, str) or not isinstance(title, str):
            raise ValueError("schedule item is missing time or title")
        if not isinstance(streamers, list) or not all(
            isinstance(name, str) for name in streamers
        ):
            raise ValueError("schedule item streamers must be a string list")
        return cls(item_time, title, tuple(streamers))


@dataclass(slots=True)
class _HtmlNode:
    tag: str
    attrs: dict[str, str]
    parent: _HtmlNode | None
    children: list[_HtmlNode]
    text_parts: list[str]

    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def text(self) -> str:
        parts = list(self.text_parts)
        for child in self.children:
            parts.append(child.text())
        return "".join(parts)

    def descendants(self) -> Iterable[_HtmlNode]:
        for child in self.children:
            yield child
            yield from child.descendants()


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("document", {}, None, [], [])
        self._stack = [self.root]

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        node = _HtmlNode(
            tag=tag,
            attrs={key: value or "" for key, value in attrs},
            parent=self._stack[-1],
            children=[],
            text_parts=[],
        )
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].text_parts.append(data)


def _decode_astro_value(value: object) -> object:
    """Decode the tagged values used by Astro's serialized island props."""
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], int)
    ):
        tag, payload = value
        if tag == 0:
            return _decode_astro_value(payload)
        if tag == 1 and isinstance(payload, list):
            return [_decode_astro_value(item) for item in payload]
        raise ScheduleFetchError(f"unsupported Astro value tag: {tag}")
    if isinstance(value, dict):
        return {key: _decode_astro_value(item) for key, item in value.items()}
    return value


def _extract_initial_events(root: _HtmlNode) -> list[dict[str, object]]:
    props_text = next(
        (
            node.attrs["props"]
            for node in root.descendants()
            if node.tag == "astro-island"
            and "initialEvents" in node.attrs.get("props", "")
        ),
        None,
    )
    if props_text is None:
        raise ScheduleFetchError("schedule page is missing initialEvents")

    try:
        props = json.loads(props_text)
        events = _decode_astro_value(props["initialEvents"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ScheduleFetchError("schedule page has invalid initialEvents") from exc
    if not isinstance(events, list) or not all(
        isinstance(event, dict) for event in events
    ):
        raise ScheduleFetchError("schedule page has invalid event data")
    return events


def _parse_start_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ScheduleFetchError("schedule event is missing startTime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduleFetchError(f"invalid event startTime: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_SHANGHAI_TIME)


def parse_schedule_html(html: str, target_date: date) -> list[ScheduleItem]:
    parser = _DocumentParser()
    parser.feed(html)
    events = _extract_initial_events(parser.root)
    items: list[ScheduleItem] = []
    for event in events:
        if event.get("status") != "published":
            continue
        start_time = _parse_start_time(event.get("startTime"))
        if start_time.date() != target_date:
            continue

        description = event.get("description")
        event_title = event.get("title")
        title = (
            description.strip()
            if isinstance(description, str) and description.strip()
            else event_title.strip()
            if isinstance(event_title, str) and event_title.strip()
            else "标题暂未公布"
        )
        performers = event.get("performers", [])
        if not isinstance(performers, list):
            performers = []
        if not performers and isinstance(event.get("host"), str):
            performers = [event["host"]]
        names = tuple(
            _MEMBER_NAMES.get(member, member)
            for member in performers
            if isinstance(member, str) and member
        )
        items.append(ScheduleItem(start_time.strftime("%H:%M"), title, names))

    return sorted(items, key=lambda item: item.time)


async def fetch_schedule(target_date: date) -> list[ScheduleItem]:
    try:
        import httpx

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
            headers={
                "User-Agent": (
                    "AstrBot-ZhijiangCalendar/1.0 "
                    "(+https://github.com/your-name/astrbot_plugin_zhijiang_calendar)"
                )
            },
        ) as client:
            response = await client.get(FETCH_URL)
            response.raise_for_status()
    except Exception as exc:
        raise ScheduleFetchError(f"failed to fetch schedule: {exc}") from exc

    response.encoding = "utf-8"
    return parse_schedule_html(response.text, target_date)


def format_schedule_message(
    target_date: date, items: Iterable[ScheduleItem]
) -> str:
    lines = [f"【枝江今日日程｜{target_date.month}月{target_date.day}日】"]
    for item in items:
        names = "、".join(item.streamers) if item.streamers else "暂未公布"
        lines.append(f"{item.time}｜{item.title}｜{names}")
    return "\n".join(lines)


def format_start_reminder(items: Iterable[ScheduleItem]) -> str:
    lines = ["【枝江开播提醒】"]
    for item in items:
        lines.append(f"{item.time}｜{item.title}")
        if item.streamers:
            lines.append(f"{'、'.join(item.streamers)}的直播时间到了")
        else:
            lines.append("该场直播时间到了")
    return "\n".join(lines)


def future_start_reminders(
    target_date: date, items: Iterable[ScheduleItem], now: datetime
) -> list[tuple[datetime, list[ScheduleItem]]]:
    if target_date != now.date():
        return []

    grouped: dict[datetime, list[ScheduleItem]] = {}
    for item in items:
        try:
            hour, minute = (int(part) for part in item.time.split(":"))
            run_at = datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                hour,
                minute,
                tzinfo=now.tzinfo,
            )
        except (TypeError, ValueError):
            continue
        if run_at > now:
            grouped.setdefault(run_at, []).append(item)
    return sorted(grouped.items(), key=lambda entry: entry[0])


def cooldown_remaining(
    now: datetime, last_fetch_at: datetime | None, cooldown: timedelta
) -> timedelta:
    if last_fetch_at is None:
        return timedelta(0)
    remaining = cooldown - (now - last_fetch_at)
    return max(remaining, timedelta(0))


def next_daily_run(now: datetime, hour: int, minute: int = 0) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
