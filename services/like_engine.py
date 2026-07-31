"""محرك الإعجابات: صف انتظار + عامل غير متزامن
- Hybrid Guest Pool: تجمع هجين (جلسات قراءة مُعاد استخدامها + تجمع مُسخّن للإعجابات)
- Register Fallbacks: يُدار من GarenaClient (مناطق بديلة + أسماء بديلة)
- Diagnostics: عدادات وتشخيص لكل عملية
"""

from __future__ import annotations

import asyncio
import collections
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple

from aiogram import Bot

from config import settings
from services.database import Database
from services.garena import DailyLimitError, GarenaClient, GarenaError, GuestAccount, LoginSession

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


@dataclass
class EngineDiagnostics:
    total_jobs: int = 0
    total_likes_sent: int = 0
    total_failed: int = 0
    total_limit_hits: int = 0
    total_cancelled: int = 0
    jobs_per_region: Dict[str, int] = field(default_factory=lambda: collections.Counter())
    likes_per_region: Dict[str, int] = field(default_factory=lambda: collections.Counter())
    avg_time_per_like: float = 0.0
    last_job_at: float = 0.0
    pool_stats: Dict[str, int] = field(default_factory=dict)
    _timings: Deque[float] = field(default_factory=lambda: collections.deque(maxlen=50))

    def record_like(self, region: str, elapsed: float):
        self.total_likes_sent += 1
        self.likes_per_region[region] += 1
        self._timings.append(elapsed)
        if self._timings:
            self.avg_time_per_like = sum(self._timings) / len(self._timings)

    def to_dict(self):
        return {
            "total_jobs": self.total_jobs,
            "total_likes_sent": self.total_likes_sent,
            "total_failed": self.total_failed,
            "total_limit_hits": self.total_limit_hits,
            "total_cancelled": self.total_cancelled,
            "jobs_per_region": dict(self.jobs_per_region),
            "likes_per_region": dict(self.likes_per_region),
            "avg_time_per_like": round(self.avg_time_per_like, 2),
            "last_job_at": self.last_job_at,
            "pool_stats": self.pool_stats,
        }


class LikeEngine:
    """قلب البوت — يستهلك المهام من الصف ويشغّلها."""

    def __init__(self, bot: Bot, db: Database, client: GarenaClient) -> None:
        self.bot = bot
        self.db = db
        self.client = client
        self.queue: asyncio.Queue[LikeJob] = asyncio.Queue()
        self._active: Dict[int, LikeJob] = {}
        self._task: Optional[asyncio.Task] = None

        # Hybrid pools (level 2 — داخل المحرك أيضاً)
        self._read_sessions_cache: Dict[str, Tuple[LoginSession, float]] = {}
        self._like_sessions_pool: Dict[str, Deque[Tuple[GuestAccount, LoginSession]]] = {}
        self._pool_lock = asyncio.Lock()
        self._engine_diag = EngineDiagnostics()
        self._garena_diag_last: Dict = {}

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
        self._engine_diag.total_jobs += 1
        self._engine_diag.jobs_per_region[job.region] += 1
        # خلفية: سخّن التجمع لهذه المنطقة
        asyncio.create_task(self._ensure_pool_warm(job.region))
        return self.queue.qsize()

    def cancel_for_user(self, user_id: int) -> bool:
        job = self._active.get(user_id)
        if job is None:
            return False
        job.cancel_event.set()
        self._engine_diag.total_cancelled += 1
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

    def get_diagnostics(self) -> Dict:
        """يجمع تشخيصات المحرك + تشخيصات عميل Garena"""
        garena_diag = self.client.get_diagnostics()
        self._garena_diag_last = garena_diag
        # تحديث pool_stats من المحرك
        total_pool = sum(len(q) for q in self._like_sessions_pool.values()) + garena_diag.get("pool", {}).get("current_size", 0)
        self._engine_diag.pool_stats = {
            "engine_pool": sum(len(q) for q in self._like_sessions_pool.values()),
            "garena_pool": garena_diag.get("pool", {}).get("current_size", 0),
            "total": total_pool,
            "read_cache_regions": len(self._read_sessions_cache),
        }
        return {
            "engine": self._engine_diag.to_dict(),
            "garena": garena_diag,
        }

    # ---------------- Hybrid Pool Helpers ----------------
    async def _ensure_pool_warm(self, region: str, desired: int = 2) -> None:
        """يضمن وجود جلسات مسخنة — يُستدعى عند استلام مهمة جديدة."""
        try:
            # أولاً استخدم تجمع GarenaClient الداخلي
            await self.client.prewarm_like_pool(region, count=desired)
            # ثم المحرك نفسه
            async with self._pool_lock:
                q = self._like_sessions_pool.setdefault(region, collections.deque(maxlen=5))
                need = max(0, desired - len(q))
            for _ in range(need):
                guest = await self.client.register_guest(region, fallback=True)
                sess = await self.client.major_login(guest.access_token, guest.open_id)
                sess.region = guest.region
                async with self._pool_lock:
                    if len(q) < 5:
                        q.append((guest, sess))
        except Exception as e:
            logger.debug("Failed to warm pool for %s: %s", region, e)

    async def _get_like_session_hybrid(self, region: str) -> Tuple[GuestAccount, LoginSession]:
        """Hybrid guest pool: جرّب محلي → جرّب عميل → أنشئ جديد (fallback)."""
        start = time.time()
        # 1) جرّب pool المحرك
        async with self._pool_lock:
            q = self._like_sessions_pool.get(region)
            if q and len(q) > 0:
                guest, sess = q.popleft()
                # خلفية: أعد الملء
                asyncio.create_task(self._ensure_pool_warm(region))
                logger.debug("Pool HIT (engine) for %s — remaining %s", region, len(q))
                return guest, sess

        # 2) جرّب pool الخاص بـ GarenaClient (يتضمن fallback + TTL)
        try:
            guest, sess = await self.client.get_like_session(region)
            elapsed = time.time() - start
            logger.debug("Pool used (garena) for %s in %.2fs", region, elapsed)
            # خلفية: أعد الملء
            asyncio.create_task(self._ensure_pool_warm(region))
            return guest, sess
        except Exception as e:
            logger.warning("Hybrid pool miss for %s: %s — creating fresh", region, e)

        # 3) إنشاء جديد كملاذ أخير (مع fallback داخل register_guest)
        guest = await self.client.register_guest(region, fallback=True)
        sess = await self.client.major_login(guest.access_token, guest.open_id)
        sess.region = guest.region
        return guest, sess

    async def _get_read_session_hybrid(self, region: str) -> Optional[LoginSession]:
        """لقراءة العداد — يعيد استخدام الكاش الهجين لتوفير إنشاء ضيوف زائد."""
        # جرّب كاش المحرك أولاً
        now = time.time()
        if region in self._read_sessions_cache:
            sess, exp = self._read_sessions_cache[region]
            if now < exp:
                return sess

        # ثانياً كاش GarenaClient
        try:
            sess = await self.client.get_read_session(region)
            self._read_sessions_cache[region] = (sess, now + 600)
            return sess
        except Exception:
            return None

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
        self._engine_diag.last_job_at = time.time()
        await self._safe_send(
            job.user_id,
            "🚀 بدأ إرسال الإعجابات إلى UID <code>{}</code> (السيرفر: {})\\n"
            "👤 سيتم إنشاء <b>حساب ضيف جديد</b> لكل إعجاب (مع تجمع هجين لتسريع العملية).\\n"
            "🔄 عند فشل المنطقة سيجرب مناطق بديلة تلقائياً (fallback).\\n"
            "🎯 الحد الأقصى لهذه الجلسة: {} إعجاب.".format(
                job.target_uid, job.region, settings.max_likes_per_session
            ),
        )

        before = await self._read_likes(job)
        if before is not None:
            await self._safe_send(
                job.user_id, f"📊 عدد إعجابات الهدف حالياً: <b>{before}</b>"
            )

        sent = 0
        failed = 0
        max_failures = max(5, settings.max_retries * 3)
        last_error = ""
        consecutive_fallback_success = 0

        # تسخين مسبق للتجمع
        asyncio.create_task(self._ensure_pool_warm(job.region, desired=3))

        for _ in range(settings.max_likes_per_session):
            if job.cancelled:
                await self._safe_send(job.user_id, "🛑 تم إلغاء المهمة.")
                await self.db.add_likes(job.user_id, sent)
                return

            like_start = time.time()
            try:
                result = await self._send_one_like(job)
            except (GarenaError, DailyLimitError) as exc:
                failed += 1
                self._engine_diag.total_failed += 1
                last_error = str(exc)
                logger.warning("فشل إرسال إعجاب (Garena): %s — region=%s fallback_used?", exc, job.region)
                # إذا فشل التسجيل حتى بعد fallback — هذا خطأ شبكة/ Garena غيّرت المفاتيح
                if failed >= max_failures:
                    await self._safe_send(
                        job.user_id,
                        f"⚠️ توقفت المهمة بعد {failed} أخطاء متتالية (مع fallback): {last_error}\\n"
                        f"📊 تشخيص: {self._format_diag_brief()}",
                    )
                    await self.db.add_likes(job.user_id, sent)
                    return
                if not await self._backoff(job, random.uniform(2, 5)):
                    await self._safe_send(job.user_id, "🛑 تم إلغاء المهمة.")
                    await self.db.add_likes(job.user_id, sent)
                    return
                continue
            except Exception as exc:
                failed += 1
                self._engine_diag.total_failed += 1
                last_error = str(exc)
                logger.warning("خطأ شبكة أثناء الإعجاب: %s", exc)
                if failed >= max_failures:
                    await self._safe_send(
                        job.user_id,
                        f"⚠️ توقفت المهمة بسبب أخطاء اتصال: {last_error}\\n"
                        f"📊 تشخيص سريع: {self._format_diag_brief()}",
                    )
                    await self.db.add_likes(job.user_id, sent)
                    return
                continue

            # نجاح — احسب الوقت
            elapsed = time.time() - like_start
            # تمييز إذا استخدم fallback (guest.region != requested region)
            # result لا يحتوي المنطقة، لكن يمكن معرفته من السجل؛ نبسط هنا

            if result.limit_reached:
                self._engine_diag.total_limit_hits += 1
                await self._safe_send(
                    job.user_id,
                    "🎯 <b>تم الوصول إلى الحد اليومي للإعجابات!</b>\\n"
                    "📨 رسالة السيرفر: {}\\n"
                    "✅ تم إرسال <b>{}</b> إعجاب بنجاح.\\n"
                    "📊 التشخيص: {}".format(
                        result.message or "daily limit reached", sent, self._format_diag_brief()
                    ),
                )
                await self._finish(job, sent, before)
                return

            if result.success:
                sent += 1
                self._engine_diag.record_like(job.region, elapsed)
                if sent % settings.progress_every == 0:
                    await self._safe_send(
                        job.user_id, f"✅ تم إرسال <b>{sent}</b> إعجاب حتى الآن... (avg {self._engine_diag.avg_time_per_like:.1f}s)"
                    )
                # إعادة ضبط الفشل المتتالي بعد نجاح
                failed = 0
            else:
                failed += 1
                self._engine_diag.total_failed += 1
                last_error = result.message
                if failed >= max_failures:
                    await self._safe_send(
                        job.user_id,
                        f"⚠️ توقفت المهمة بسبب أخطاء متكررة: {last_error}\\n"
                        f"📊 {self._format_diag_brief()}",
                    )
                    await self.db.add_likes(job.user_id, sent)
                    return

            if not await self._backoff(
                job,
                random.uniform(settings.min_delay_seconds, settings.max_delay_seconds),
            ):
                await self._safe_send(job.user_id, "🛑 تم إلغاء المهمة.")
                await self.db.add_likes(job.user_id, sent)
                return

        await self._finish(job, sent, before)

    def _format_diag_brief(self) -> str:
        g = self.client.get_diagnostics()
        reg_attempts = g.get("register", {}).get("per_region_attempt", {})
        pool = g.get("pool", {})
        return f"تسجيل: {g.get('register', {}).get('attempts',0)} محاولة ({reg_attempts}) | تجمع hits={pool.get('hits',0)} miss={pool.get('miss',0)}"

    # ---------------- رسالة النهاية + التحقق ----------------
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
        # إضافة ملخص تشخيصي مختصر
        diag = self.get_diagnostics()
        eng = diag["engine"]
        gar = diag["garena"]
        final_lines.append(
            f"📊 تشخيص: {eng['total_likes_sent']} كلّي / متوسط {eng['avg_time_per_like']}s / تجمع {eng['pool_stats'].get('total',0)} / فشل تسجيل {gar['register']['failed']}"
        )
        final_lines.append(
            "💡 إن لم يكتمل الحد اليومي، يمكنك طلب جلسة أخرى بعد ساعة."
        )
        await self._safe_send(job.user_id, "\\n".join(final_lines))
        await self.db.add_likes(job.user_id, sent)

    # ---------------- انتظار قابل للإلغاء ----------------
    async def _backoff(self, job: LikeJob, seconds: float) -> bool:
        waited = 0.0
        while waited < seconds:
            if job.cancelled:
                return False
            await asyncio.sleep(0.25)
            waited += 0.25
        return True

    # ---------------- إرسال إعجاب واحد — hybrid pool + fallback ----------------
    async def _send_one_like(self, job: LikeJob):
        guest, session = await self._get_like_session_hybrid(job.region)
        # ملاحظة: guest.region قد يكون مختلفاً عن job.region إذا استُخدم fallback — نرسل الإعجاب بالمنطقة الأصلية المطلوبة
        # لكن إذا فشل، نجرّب بمنطقة الضيف كاحتياط
        try:
            result = await self.client.send_like(session, job.target_uid, job.region)
            if not result.success and not result.limit_reached:
                # محاولة ثانية بمنطقة الضيف (fallback region) إذا مختلفة
                if guest.region != job.region:
                    logger.info("Retry like with guest region %s instead of %s", guest.region, job.region)
                    result = await self.client.send_like(session, job.target_uid, guest.region)
            return result
        except Exception:
            # تنظيف التجمع المعطوب
            async with self._pool_lock:
                self._like_sessions_pool.get(job.region, collections.deque()).clear()
            raise

    # ---------------- قراءة عدد الإعجابات — hybrid cached ----------------
    async def _read_likes(self, job: LikeJob) -> Optional[int]:
        try:
            # أولاً جرّب جلسة كاش هجينة
            session = await self._get_read_session_hybrid(job.region)
            if session is None:
                # fallback: إنشاء مباشر
                guest = await self.client.register_guest(job.region, fallback=True)
                session = await self.client.major_login(guest.access_token, guest.open_id)

            info = await self.client.get_player_info(session, job.target_uid)
            return info.likes if info else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("تعذر قراءة عدد الإعجابات (مع تجمع): %s", exc)
            # محاولة أخيرة بدون كاش
            try:
                guest = await self.client.register_guest(job.region, fallback=True)
                session = await self.client.major_login(guest.access_token, guest.open_id)
                info = await self.client.get_player_info(session, job.target_uid)
                return info.likes if info else None
            except Exception:
                return None

    # ---------------- إرسال آمن ----------------
    async def _safe_send(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(user_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("تعذر إرسال رسالة للمستخدم %s: %s", user_id, exc)
