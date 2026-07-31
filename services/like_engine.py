"""محرك الإعجابات: صف انتظار + عامل غير متزامن.

لكل إعجاب:
  1) إنشاء حساب ضيف جديد كامل (تسجيل + توكن + إنشاء داخل اللعبة) ← حساب وهمي جديد
  2) تسجيل دخول → JWT
  3) إرسال الإعجاب للـ UID المستهدف
  4) عند الوصول للحد اليومي → إيقاف فوري + إشعار المستخدم
  والتحقق النهائي: قراءة عدد الإعجابات من بروفايل الهدف قبل/بعد.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Dict, Optional

from aiogram import Bot

from config import settings
from services.database import Database
from services.garena import DailyLimitError, GarenaClient, GarenaError

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

    def __init__(self, bot: Bot, db: Database, client: GarenaClient) -> None:
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
            "👤 سيتم إنشاء <b>حساب ضيف جديد</b> لكل إعجاب.\n"
            "🎯 الحد الأقصى لهذه الجلسة: {} إعجاب.".format(
                job.target_uid, job.region, settings.max_likes_per_session
            ),
        )

        # ---- قراءة عدد الإعجابات الحالي (للتحقق بعد الانتهاء) ----
        before = await self._read_likes(job)
        if before is not None:
            await self._safe_send(
                job.user_id, f"📊 عدد إعجابات الهدف حالياً: <b>{before}</b>"
            )

        sent = 0
        failed = 0
        max_failures = max(5, settings.max_retries * 3)
        last_error = ""

        for _ in range(settings.max_likes_per_session):
            if job.cancelled:
                await self._safe_send(job.user_id, "🛑 تم إلغاء المهمة.")
                await self.db.add_likes(job.user_id, sent)
                return

            try:
                result = await self._send_one_like(job)
            except (GarenaError, DailyLimitError) as exc:
                failed += 1
                last_error = str(exc)
                logger.warning("فشل إرسال إعجاب: %s", exc)
                if failed >= max_failures:
                    await self._safe_send(
                        job.user_id,
                        f"⚠️ توقفت المهمة بعد {failed} أخطاء متتالية: {last_error}",
                    )
                    await self.db.add_likes(job.user_id, sent)
                    return
                if not await self._backoff(job, random.uniform(2, 5)):
                    await self._safe_send(job.user_id, "🛑 تم إلغاء المهمة.")
                    await self.db.add_likes(job.user_id, sent)
                    return
                continue
            except Exception as exc:  # شبكة/مهلة
                failed += 1
                last_error = str(exc)
                logger.warning("خطأ شبكة أثناء الإعجاب: %s", exc)
                if failed >= max_failures:
                    await self._safe_send(
                        job.user_id,
                        f"⚠️ توقفت المهمة بسبب أخطاء اتصال: {last_error}",
                    )
                    await self.db.add_likes(job.user_id, sent)
                    return
                continue

            # ---- الوصول للحد اليومي → إيقاف فوري ----
            if result.limit_reached:
                await self._safe_send(
                    job.user_id,
                    "🎯 <b>تم الوصول إلى الحد اليومي للإعجابات!</b>\n"
                    "📨 رسالة السيرفر: {}\n"
                    "✅ تم إرسال <b>{}</b> إعجاب بنجاح.".format(
                        result.message or "daily limit reached", sent
                    ),
                )
                await self._finish(job, sent, before)
                return

            if result.success:
                sent += 1
                if sent % settings.progress_every == 0:
                    await self._safe_send(
                        job.user_id, f"✅ تم إرسال <b>{sent}</b> إعجاب حتى الآن..."
                    )
            else:
                failed += 1
                last_error = result.message
                if failed >= max_failures:
                    await self._safe_send(
                        job.user_id,
                        f"⚠️ توقفت المهمة بسبب أخطاء متكررة: {last_error}",
                    )
                    await self.db.add_likes(job.user_id, sent)
                    return

            # تأخير عشوائي بين الطلبات — يقلل خطر تفعيل أنظمة مكافحة البوت
            if not await self._backoff(
                job,
                random.uniform(settings.min_delay_seconds, settings.max_delay_seconds),
            ):
                await self._safe_send(job.user_id, "🛑 تم إلغاء المهمة.")
                await self.db.add_likes(job.user_id, sent)
                return

        await self._finish(job, sent, before)

    # ---------------- رسالة النهاية + التحقق النهائي ----------------
    async def _finish(self, job: LikeJob, sent: int, before: Optional[int]) -> None:
        after = await self._read_likes(job)
        final_lines = [
            "🏁 <b>انتهت الجلسة</b> — تم إرسال <b>{}</b> إعجاب إلى <code>{}</code>.".format(
                sent, job.target_uid
            ),
        ]
        if before is not None and after is not None:
            delta = max(0, after - before)
            final_lines.append(
                f"📈 التحقق: عدد الإعجابات <b>{before}</b> ← <b>{after}</b> (+{delta})"
            )
        final_lines.append(
            "💡 إن لم يكتمل الحد اليومي، يمكنك طلب جلسة أخرى بعد ساعة."
        )
        await self._safe_send(job.user_id, "\n".join(final_lines))
        await self.db.add_likes(job.user_id, sent)

    # ---------------- انتظار قابل للإلغاء ----------------
    async def _backoff(self, job: LikeJob, seconds: float) -> bool:
        """ينتظر المدة المطلوبة لكنه يستجيب فوراً لأمر الإلغاء.
        يعيد False إذا أُلغيت المهمة أثناء الانتظار."""
        waited = 0.0
        while waited < seconds:
            if job.cancelled:
                return False
            await asyncio.sleep(0.25)
            waited += 0.25
        return True

    # ---------------- إرسال إعجاب واحد بحساب ضيف ----------------
    async def _send_one_like(self, job: LikeJob):
        """يحاول تسجيل حساب ضيف جديد؛ عند الفشل (مثل error_not_found 1005 من
        سيرفر Railway) يلجأ إلى مخزون الحسابات الجاهزة (كل حساب = إعجاب واحد
        لنفس الهدف). إذا نجح التسجيل يُحفظ الحساب الجديد في المخزون."""
        try:
            guest = await self.client.register_guest(job.region)
        except GarenaError:
            return await self._like_from_stock(job)

        # التسجيل نجح → احفظ الحساب الجديد في المخزون لاستخدامه لاحقاً
        try:
            await self.db.save_guest_account(guest.uid, guest.region, guest.password_hash)
        except Exception as exc:  # noqa: BLE001 — الفشل هنا لا يوقف الإعجاب
            logger.debug("تعذر حفظ الحساب الجديد في المخزون: %s", exc)

        session = await self.client.major_login(guest.access_token, guest.open_id)
        result = await self.client.send_like(session, job.target_uid, job.region)
        if result.success:
            await self.db.mark_guest_used(guest.uid, job.target_uid, job.region)
        return result

    async def _like_from_stock(self, job: LikeJob):
        """يستخدم حساباً جاهزاً من المخزون لإرسال إعجاب واحد لنفس الهدف."""
        account = await self.db.get_available_guest(job.region, job.target_uid)
        if account is None:
            raise GarenaError(
                f"فشل تسجيل حساب ضيف ولا توجد حسابات جاهزة متبقية لمنطقة "
                f"{job.region} لهذا الهدف."
            )
        uid, password_hash = account
        access_token, open_id = await self.client.token_grant(uid, password_hash)
        session = await self.client.major_login(access_token, open_id)
        result = await self.client.send_like(session, job.target_uid, job.region)
        if result.success:
            await self.db.mark_guest_used(uid, job.target_uid, job.region)
        return result

    # ---------------- قراءة عدد الإعجابات (أفضل جهد) ----------------
    async def _read_likes(self, job: LikeJob) -> Optional[int]:
        try:
            guest = await self.client.register_guest(job.region)
            session = await self.client.major_login(guest.access_token, guest.open_id)
            info = await self.client.get_player_info(session, job.target_uid)
            return info.likes if info else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("تعذر قراءة عدد الإعجابات: %s", exc)
            return None

    # ---------------- إرسال آمن ----------------
    async def _safe_send(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(user_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("تعذر إرسال رسالة للمستخدم %s: %s", user_id, exc)
