"""
نقطة دخول بوت تيليجرام — زيادة لايكات فري فاير
التشغيل: python main.py
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from handlers import admin, user
from middlewares.access_control import AccessControlMiddleware
from services.database import Database
from services.garena import GarenaClient
from services.like_engine import LikeEngine
from utils.logger import setup_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging(settings.log_level)

    if not settings.is_configured:
        missing = []
        if not settings.bot_token:
            missing.append("BOT_TOKEN")
        if settings.admin_id == 0:
            missing.append("ADMIN_ID")
        if not settings.database_url:
            missing.append("DATABASE_URL")
        raise SystemExit(
            f"⚠️ متغيرات بيئة ناقصة: {', '.join(missing)}\n"
            "أضفها في متغيرات البيئة على Railway."
        )

    # ---------- قاعدة البيانات ----------
    db = Database()
    await db.init()

    # ---------- البوت ----------
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # ---------- الخدمات المشتركة (تُحقن في المعالجات) ----------
    api_client = GarenaClient()
    await api_client.start()
    engine = LikeEngine(bot=bot, db=db, client=api_client)

    dp.workflow_data.update(db=db, engine=engine)

    # ---------- الوسطيات والراوترات ----------
    dp.update.middleware(AccessControlMiddleware(db))
    dp.include_router(user.router)
    dp.include_router(admin.router)

    # ---------- بدء محرك الإعجابات ----------
    engine.start()

    logger.info("🚀 البوت يعمل الآن... (polling)")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await engine.stop()
        await api_client.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("تم إيقاف البوت.")
