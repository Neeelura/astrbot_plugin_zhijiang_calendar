from datetime import date, datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_zhijiang_calendar.schedule_service import (
    ScheduleFetchError,
    ScheduleItem,
    cooldown_remaining,
    format_schedule_message,
    format_start_reminder,
    future_start_reminders,
    next_daily_run,
    parse_schedule_html,
)


def _tag(value: object) -> list[object]:
    if isinstance(value, list):
        return [1, [_tag(item) for item in value]]
    if isinstance(value, dict):
        return [0, {key: _tag(item) for key, item in value.items()}]
    return [0, value]


def _page(events: list[dict[str, object]] | None = None) -> str:
    props = {
        "weeksToGenerate": [0, 1],
        "initialEvents": _tag(events or []),
    }
    return (
        '<html><body><astro-island component-export="FilterableCalendar" '
        f'props="{escape(json.dumps(props, ensure_ascii=False), quote=True)}">'
        "</astro-island></body></html>"
    )


def _event(
    start_time: str,
    title: str,
    *performers: str,
    description: str = "",
    status: str = "published",
) -> dict[str, object]:
    return {
        "title": title,
        "description": description,
        "startTime": start_time,
        "host": performers[0] if performers else None,
        "performers": list(performers),
        "status": status,
    }


class ParseScheduleTests(unittest.TestCase):
    def test_parses_and_sorts_single_and_multi_streamer_events(self) -> None:
        html = _page(
            [
                _event(
                    "2026-07-22T12:00:00.000Z",
                    "嘉然&乃琳直播",
                    "diana",
                    "eileen",
                    description="小恶魔VS坏女人",
                ),
                _event(
                    "2026-07-22T09:00:00.000Z",
                    "心宜直播",
                    "fiona",
                    description="摸鱼聊天室",
                ),
            ]
        )

        self.assertEqual(
            parse_schedule_html(html, date(2026, 7, 22)),
            [
                ScheduleItem("17:00", "摸鱼聊天室", ("心宜",)),
                ScheduleItem("20:00", "小恶魔VS坏女人", ("嘉然", "乃琳")),
            ],
        )

    def test_returns_empty_list_for_day_without_events(self) -> None:
        self.assertEqual(parse_schedule_html(_page(), date(2026, 7, 22)), [])

    def test_rejects_page_without_calendar_data(self) -> None:
        with self.assertRaises(ScheduleFetchError):
            parse_schedule_html("<html></html>", date(2026, 7, 22))

    def test_ignores_cancelled_events(self) -> None:
        html = _page(
            [
                _event(
                    "2026-07-22T09:00:00.000Z",
                    "取消的直播",
                    "fiona",
                    status="cancelled",
                )
            ]
        )
        self.assertEqual(parse_schedule_html(html, date(2026, 7, 22)), [])


class FormattingAndSchedulingTests(unittest.TestCase):
    def test_formats_requested_message_shape(self) -> None:
        message = format_schedule_message(
            date(2026, 7, 22),
            [ScheduleItem("20:00", "小恶魔VS坏女人", ("嘉然", "乃琳"))],
        )
        self.assertEqual(
            message,
            "【枝江今日日程｜7月22日】\n20:00｜小恶魔VS坏女人｜嘉然、乃琳",
        )

    def test_next_daily_run_uses_next_day_after_target_time(self) -> None:
        shanghai_time = timezone(timedelta(hours=8), name="Asia/Shanghai")
        now = datetime(2026, 7, 22, 9, 1, tzinfo=shanghai_time)
        self.assertEqual(
            next_daily_run(now, 9),
            datetime(2026, 7, 23, 9, 0, tzinfo=shanghai_time),
        )

    def test_formats_single_start_reminder(self) -> None:
        message = format_start_reminder(
            [ScheduleItem("20:00", "小恶魔VS坏女人", ("嘉然", "乃琳"))]
        )
        self.assertEqual(
            message,
            "【枝江开播提醒】\n20:00｜小恶魔VS坏女人\n嘉然、乃琳的直播时间到了",
        )

    def test_combines_start_reminders_at_the_same_time(self) -> None:
        message = format_start_reminder(
            [
                ScheduleItem("20:00", "直播 A", ("嘉然",)),
                ScheduleItem("20:00", "直播 B", ("乃琳",)),
            ]
        )
        self.assertEqual(
            message,
            (
                "【枝江开播提醒】\n"
                "20:00｜直播 A\n嘉然的直播时间到了\n"
                "20:00｜直播 B\n乃琳的直播时间到了"
            ),
        )

    def test_only_schedules_future_reminders_and_groups_equal_times(self) -> None:
        shanghai_time = timezone(timedelta(hours=8), name="Asia/Shanghai")
        now = datetime(2026, 7, 22, 20, 0, tzinfo=shanghai_time)
        first = ScheduleItem("21:00", "直播 A", ("嘉然",))
        second = ScheduleItem("21:00", "直播 B", ("乃琳",))
        reminders = future_start_reminders(
            date(2026, 7, 22),
            [
                ScheduleItem("19:00", "已经错过", ("向晚",)),
                ScheduleItem("20:00", "当前时间", ("贝拉",)),
                first,
                second,
            ],
            now,
        )
        self.assertEqual(
            reminders,
            [(datetime(2026, 7, 22, 21, 0, tzinfo=shanghai_time), [first, second])],
        )

    def test_cooldown_remaining_is_zero_without_previous_fetch(self) -> None:
        now = datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            cooldown_remaining(now, None, timedelta(minutes=30)), timedelta(0)
        )

    def test_cooldown_remaining_never_becomes_negative(self) -> None:
        now = datetime(2026, 7, 22, 9, 31, tzinfo=timezone.utc)
        last_fetch = datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            cooldown_remaining(now, last_fetch, timedelta(minutes=30)),
            timedelta(0),
        )

    def test_cooldown_remaining_reports_active_window(self) -> None:
        now = datetime(2026, 7, 22, 9, 10, tzinfo=timezone.utc)
        last_fetch = datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            cooldown_remaining(now, last_fetch, timedelta(minutes=30)),
            timedelta(minutes=20),
        )


if __name__ == "__main__":
    unittest.main()
