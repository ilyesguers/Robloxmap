"""وسطية التحكم بالوصول: تمنع المستخدمين المحظورين، وتسمح دائماً للأدمن."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from config import settings
from services.database import Database

logger = logging.getLogger(__name__)


class AccessControlMiddleware(BaseMiddleware):
    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None and user.id != settings.admin_id:
            try:
                if await self.db.is_banned(user.id):
                    logger.info("محاولة استخدام من مستخدم محظور: %s", user.id)
                    return None  # تجاهل صامت
            except Exception:  # noqa: BLE001
                logger.exception("خطأ أثناء فحص الحظر")
        return await handler(event, data)
