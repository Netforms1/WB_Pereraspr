import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.db import get_storage
from app.wb import WBAuthError, WBError, WBNotReadyError, get_wb_client

logger = logging.getLogger(__name__)

MSK = timezone.utc  # APScheduler принимает строкой; см. build_scheduler


async def _notify(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(settings.tg_owner_id, text)
    except Exception:
        logger.exception("failed to notify owner")


async def refresh_warehouses_job(bot: Bot) -> None:
    storage = get_storage()
    wb = get_wb_client()
    if not await wb.load():
        logger.info("warehouses refresh skipped: no session")
        return
    try:
        items = await wb.fetch_warehouses()
    except WBAuthError as e:
        logger.warning("warehouses refresh: auth error %s", e)
        await _notify(bot, "WB сессия истекла. Выполните /login заново.")
        return
    except WBError as e:
        logger.warning("warehouses refresh failed: %s", e)
        return
    await storage.upsert_warehouses(items)
    logger.info("warehouses refreshed: %d", len(items))


async def _attack_loop(bot: Bot, deadline: float) -> None:
    storage = get_storage()
    wb = get_wb_client()
    if not await wb.load():
        await _notify(bot, "9:00 наступило, но я не залогинен в WB. /login пожалуйста.")
        return

    await storage.reset_pending_for_today()
    await _notify(bot, "🚀 Штурм перераспределения начался.")

    while asyncio.get_event_loop().time() < deadline:
        pending = await storage.list_requests(status="pending")
        if not pending:
            await _notify(bot, "✅ Все заявки выполнены или отменены.")
            return

        progressed = False
        for req in pending:
            try:
                await wb.submit_redistribution(
                    req.from_warehouse_id, req.to_warehouse_id, req.article, req.quantity
                )
            except WBNotReadyError as e:
                await storage.mark_attempt(req.id, str(e)[:200])
                continue
            except WBAuthError as e:
                await storage.mark_attempt(req.id, f"auth: {e}"[:200])
                await _notify(bot, "WB отверг сессию во время штурма. Нужен /login. Заявки ждут.")
                return
            except WBError as e:
                await storage.mark_failed(req.id, str(e)[:300])
                await _notify(bot, f"❌ Заявка #{req.id} провалена: {e}")
                progressed = True
                continue
            await storage.mark_done(req.id)
            await _notify(
                bot,
                f"✅ #{req.id} {req.article} ×{req.quantity}: "
                f"склад {req.from_warehouse_id} → {req.to_warehouse_id}",
            )
            progressed = True

        if not progressed:
            await asyncio.sleep(settings.attack_retry_seconds)

    remaining = await storage.list_requests(status="pending")
    if remaining:
        await _notify(
            bot,
            f"⏰ Окно штурма закрыто. Осталось pending: {len(remaining)}. "
            "Запустите вручную /run или дождитесь следующего дня.",
        )
    else:
        await _notify(bot, "✅ Окно штурма закрыто, всё разложено.")


async def run_attack_now(bot: Bot) -> None:
    deadline = asyncio.get_event_loop().time() + settings.attack_window_seconds
    await _attack_loop(bot, deadline)


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    scheduler.add_job(
        refresh_warehouses_job,
        IntervalTrigger(seconds=settings.warehouses_refresh_seconds),
        args=[bot],
        id="refresh_warehouses",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_attack_now,
        CronTrigger(hour=settings.daily_run_hour, minute=settings.daily_run_minute),
        args=[bot],
        id="daily_attack",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
