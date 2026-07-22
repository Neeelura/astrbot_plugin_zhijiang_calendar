from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star

from .schedule_service import (
    ScheduleFetchError,
    ScheduleItem,
    cooldown_remaining,
    fetch_schedule,
    format_schedule_message,
    next_daily_run,
)


TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
CACHE_KEY = "today_schedule"
GROUP_ORIGINS_KEY = "group_origins"
LAST_FETCH_KEY = "last_fetch_at"
NO_SCHEDULE_MESSAGE = "今日暂无直播安排。"


class ZhijiangCalendarPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.group_whitelist = {
            str(group_id).strip()
            for group_id in config.get("group_whitelist", [])
            if str(group_id).strip()
        }
        self.cooldown = timedelta(
            minutes=max(int(config.get("refresh_cooldown_minutes", 30)), 0)
        )
        self._cache: dict[str, object] | None = None
        self._group_origins: dict[str, str] = {}
        self._last_fetch_at: datetime | None = None
        self._fetch_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    async def initialize(self) -> None:
        self._cache = self._coerce_cache(await self.get_kv_data(CACHE_KEY, None))
        self._group_origins = self._coerce_group_origins(
            await self.get_kv_data(GROUP_ORIGINS_KEY, {})
        )
        self._last_fetch_at = self._parse_datetime(
            await self.get_kv_data(LAST_FETCH_KEY, None)
        )

        await self._ensure_today_cache()
        self._start_task(self._daily_loop(0, self._scheduled_fetch), "fetch")
        self._start_task(self._daily_loop(9, self._scheduled_push), "push")
        logger.info(
            "枝江日程插件已启动，白名单群数量: %d", len(self.group_whitelist)
        )

    def _start_task(self, coroutine: Awaitable[None], name: str) -> None:
        task = asyncio.create_task(coroutine, name=f"zhijiang-calendar-{name}")
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("枝江日程后台任务异常")

    async def _daily_loop(
        self, hour: int, callback: Callable[[], Awaitable[None]]
    ) -> None:
        while True:
            now = datetime.now(TIMEZONE)
            run_at = next_daily_run(now, hour)
            await asyncio.sleep((run_at - now).total_seconds())
            try:
                await callback()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("枝江日程定时任务执行失败")

    async def _scheduled_fetch(self) -> None:
        try:
            await self._refresh(
                datetime.now(TIMEZONE).date(), only_if_missing=True
            )
        except ScheduleFetchError:
            logger.exception("零点抓取枝江日程失败")

    async def _scheduled_push(self) -> None:
        today = datetime.now(TIMEZONE).date()
        items = self._cached_items(today)
        if not items:
            return

        message = format_schedule_message(today, items)
        for group_id in sorted(self.group_whitelist):
            if not await self._send_to_group(group_id, message):
                logger.error("无法向白名单群 %s 推送枝江日程", group_id)

    async def _ensure_today_cache(self) -> None:
        today = datetime.now(TIMEZONE).date()
        if self._cache_date() == today:
            return
        try:
            await self._refresh(today)
        except ScheduleFetchError:
            logger.exception("启动时补抓枝江日程失败")

    async def _refresh(
        self, target_date: date, *, only_if_missing: bool = False
    ) -> list[ScheduleItem]:
        async with self._fetch_lock:
            if only_if_missing and self._cache_date() == target_date:
                return self._cached_items(target_date)
            return await self._fetch_and_store(target_date)

    async def _refresh_today_with_cooldown(
        self,
    ) -> tuple[date, list[ScheduleItem], timedelta]:
        async with self._fetch_lock:
            now = datetime.now(TIMEZONE)
            target_date = now.date()
            remaining = cooldown_remaining(now, self._last_fetch_at, self.cooldown)
            if remaining > timedelta(0):
                return target_date, self._cached_items(target_date), remaining
            items = await self._fetch_and_store(target_date)
            return target_date, items, timedelta(0)

    async def _fetch_and_store(self, target_date: date) -> list[ScheduleItem]:
        """Fetch and persist one day while the caller holds ``_fetch_lock``."""
        items = await fetch_schedule(target_date)
        fetched_at = datetime.now(TIMEZONE)
        self._cache = {
            "date": target_date.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "items": [item.to_dict() for item in items],
        }
        self._last_fetch_at = fetched_at
        await self.put_kv_data(CACHE_KEY, self._cache)
        await self.put_kv_data(LAST_FETCH_KEY, fetched_at.isoformat())
        logger.info("已更新 %s 枝江日程，共 %d 场", target_date, len(items))
        return items

    async def _get_or_fetch_today(self) -> list[ScheduleItem]:
        today = datetime.now(TIMEZONE).date()
        if self._cache_date() == today:
            return self._cached_items(today)
        return await self._refresh(today, only_if_missing=True)

    def _cache_date(self) -> date | None:
        if not self._cache or not isinstance(self._cache.get("date"), str):
            return None
        try:
            return date.fromisoformat(self._cache["date"])
        except ValueError:
            return None

    def _cached_items(self, target_date: date) -> list[ScheduleItem]:
        if self._cache_date() != target_date or not self._cache:
            return []
        raw_items = self._cache.get("items", [])
        if not isinstance(raw_items, list):
            return []
        items: list[ScheduleItem] = []
        for value in raw_items:
            try:
                items.append(ScheduleItem.from_dict(value))
            except ValueError:
                logger.warning("忽略损坏的日程缓存项: %r", value)
        return items

    @staticmethod
    def _coerce_cache(value: object) -> dict[str, object] | None:
        return value if isinstance(value, dict) else None

    @staticmethod
    def _coerce_group_origins(value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            str(group_id): origin
            for group_id, origin in value.items()
            if isinstance(origin, str) and origin
        }

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TIMEZONE)
        return parsed.astimezone(TIMEZONE)

    def _is_whitelisted_group(self, event: AstrMessageEvent) -> bool:
        group_id = event.get_group_id()
        return bool(group_id and str(group_id) in self.group_whitelist)

    @staticmethod
    def _mentions_bot(event: AstrMessageEvent) -> bool:
        self_id = str(event.get_self_id())
        return any(
            isinstance(segment, Comp.At)
            and str(getattr(segment, "qq", "")) == self_id
            for segment in event.get_messages()
        )

    async def _remember_group_origin(self, event: AstrMessageEvent) -> None:
        if not self._is_whitelisted_group(event):
            return
        group_id = str(event.get_group_id())
        origin = event.unified_msg_origin
        if self._group_origins.get(group_id) == origin:
            return
        self._group_origins[group_id] = origin
        await self.put_kv_data(GROUP_ORIGINS_KEY, self._group_origins)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def remember_group(self, event: AstrMessageEvent) -> None:
        """记录白名单群会话，以供定时主动推送。"""
        await self._remember_group_origin(event)

    @filter.regex(r"^\s*今日日程\s*$")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def today_schedule(self, event: AstrMessageEvent):
        """被提及时立即返回缓存中的今日日程。"""
        if not self._is_whitelisted_group(event) or not self._mentions_bot(event):
            return
        await self._remember_group_origin(event)

        try:
            items = await self._get_or_fetch_today()
        except ScheduleFetchError:
            logger.exception("按需抓取枝江日程失败")
            yield event.plain_result("日程获取失败，请稍后再试。")
            return

        if not items:
            yield event.plain_result(NO_SCHEDULE_MESSAGE)
            return
        yield event.plain_result(
            format_schedule_message(datetime.now(TIMEZONE).date(), items)
        )

    @filter.regex(r"^\s*刷新日程\s*$")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def refresh_schedule(self, event: AstrMessageEvent):
        """被提及时刷新并返回今日日程，受全局冷却限制。"""
        if not self._is_whitelisted_group(event) or not self._mentions_bot(event):
            return
        await self._remember_group_origin(event)

        try:
            target_date, items, remaining = await self._refresh_today_with_cooldown()
        except ScheduleFetchError:
            logger.exception("手动刷新枝江日程失败")
            yield event.plain_result("日程刷新失败，请稍后再试。")
            return

        if remaining > timedelta(0):
            minutes = max(1, int((remaining.total_seconds() + 59) // 60))
            message = f"日程刷新冷却中，请约 {minutes} 分钟后再试。"
            if items:
                message += "\n\n" + format_schedule_message(target_date, items)
            yield event.plain_result(message)
            return

        if not items:
            yield event.plain_result(NO_SCHEDULE_MESSAGE)
            return
        yield event.plain_result(format_schedule_message(target_date, items))

    async def _send_to_group(self, group_id: str, message: str) -> bool:
        origin = self._group_origins.get(group_id)
        if origin:
            try:
                await self.context.send_message(
                    origin, MessageChain().message(message)
                )
                return True
            except Exception:
                logger.exception("通过统一会话向群 %s 推送失败，将尝试 OneBot", group_id)

        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
                AiocqhttpAdapter,
            )

            for platform in self.context.platform_manager.get_insts():
                if isinstance(platform, AiocqhttpAdapter):
                    await platform.get_client().api.call_action(
                        "send_group_msg", group_id=int(group_id), message=message
                    )
                    return True
        except Exception:
            logger.exception("通过 OneBot 向群 %s 推送失败", group_id)
        return False

    async def terminate(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            with suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("枝江日程插件已停止")
