"""محرك الإعجابات: صف انتظار + عامل غير متزامن يرسل الإعجابات ويحدّث المستخدم."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Dict, Optional

from aiogram import Bot

from config import settings
from services.api_client import APIError, FFAPIClient, LikeResult
from services.database import Database

logger = logging.getLogger(__name__)


@dataclass
class LikeJob:
    user_id: int
    target_uid: str
    region: str
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


class LikeEngine:
    """قلب البوت — يستهلك المهام من الصف ويشغّلها واحدة تلو الأخرى."""

    def __init__(self, bot: Bot, db: Database, client: FFAPIClient) -> None:
        self.bot = bot
        self.db = db
        self.client = client
        self.queue: asyncio.Queue[LikeJob] = asyncio.Queue()
        self._active: Dict[int, LikeJob] = {}
        self._task: Optional[asyncio.Task] = None

    # ---------------- إدارة دورة الحياة ----------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="like-engine-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ---------------- واجهة عامة ----------------
    def submit(self, job: LikeJob) -> int:
        """إضافة مهمة للصف؛ يعيد عدد المهام المنتظرة (تقريباً موقعه)."""
        self.queue.put_nowait(job)
        return self.queue.qsize()

    def cancel_for_user(self, user_id: int) -> bool:
        job = self._active.get(user_id)
        if job is None:
            return False
        job.cancel_event.set()
        return True

    def clear_queue(self) -> int:
        n = self.queue.qsize()
        for _ in range(n):
            self.queue.get_nowait()
        return n

    @property
    def queue_size(self) -> int:
        return self.queue.qsize()

    @property
    def active_count(self) -> int:
        return len(self._active)

    # ---------------- حلقة العمل ----------------
    async def _run(self) -> None:
        while True:
            job = await self.queue.get()
            self._active[job.user_id] = job
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("خطأ غير متوقع أثناء معالجة مهمة")
                await self._safe_send(job.user_id, "❌ حدث خطأ غير متوقع أثناء المهمة.")
            finally:
                self._active.pop(job.user_id, None)
                self.queue.task_done()

    # ---------------- المعالجة ----------------
    async def _process(self, job: LikeJob) -> None:
        await self._safe_send(
            job.user_id,
            "🚀 بدأ إرسال الإعجابات إلى UID <code>{}</code> (السيرفر: {})\n"
            "⚠️ الحد الأقصى لهذه الجلسة: {} إعجاب.".format(
                job.target_uid, job.region, settings.max_likes_per_session
            ),
        )

        sent = 0
        failed = 0
        max_failures = max(3, settings.max_retries * 3)

        for _ in range(settings.max_likes_per_session):
            if job.cancelled:
                await self._safe_send(job.user_id, "🛑 تم إلغاء المهمة.")
                await self.db.add_likes(job.user_id, sent)
                return

            try:
                result = await self._send_one_like(job)
            except APIError as exc:
                failed += 1
                logger.warning("فشل إرسال إعجاب: %s", exc)
                if failed >= max_failures:
                    await self._safe_send(
                        job.user_id,
                        f"⚠️ توقفت المهمة بسبب أخطاء اتصال متكررة: {exc}",
                    )
                    await self.db.add_likes(job.user_id, sent)
                    return
                continue

            # Step 6: الوصول للحد اليومي → إيقاف فوري وإشعار المستخدم
            if result.limit_reached:
                await self._safe_send(
                    job.user_id,
                    "🎯 <b>تم الوصول إلى الحد اليومي للإعجابات!</b>\n"
                    "📨 رسالة السيرفر: {}\n"
                    "✅ تم إرسال <b>{}</b> إعجاب بنجاح.".format(
                        result.message or "daily limit reached", sent
                    ),
                )
                await self.db.add_likes(job.user_id, sent)
                return

            if result.success:
                sent += 1
                if sent % settings.progress_every == 0:
                    await self._safe_send(
                        job.user_id, f"✅ تم إرسال <b>{sent}</b> إعجاب حتى الآن..."
                    )
            else:
                failed += 1
                logger.debug("إعجاب فاشل: %s", result.message)

            # تأخير عشوائي بين الطلبات — يقلل خطر تفعيل أنظمة مكافحة البوت
            await asyncio.sleep(
                random.uniform(settings.min_delay_seconds, settings.max_delay_seconds)
            )

        await self._safe_send(
            job.user_id,
            "🏁 انتهت الجلسة — تم إرسال <b>{}</b> إعجاب إلى <code>{}</code>.\n"
            "💡 إن لم يكتمل الحد اليومي، أعد المحاولة بعد ساعة.".format(
                sent, job.target_uid
            ),
        )
        await self.db.add_likes(job.user_id, sent)

    async def _send_one_like(self, job: LikeJob) -> LikeResult:
        """حساب ضيف جديد + إعجاب واحد (أو عدة حسب LIKES_PER_GUEST)."""
        last = LikeResult(success=False, message="no attempts")
        for _ in range(max(1, settings.likes_per_guest)):
            session = await self.client.create_guest_session(job.region)
            last = await self.client.send_like(session, job.target_uid)
            if last.limit_reached or not last.success:
                return last
        return last

    async def _safe_send(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(user_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("تعذر إرسال رسالة للمستخدم %s: %s", user_id, exc)
