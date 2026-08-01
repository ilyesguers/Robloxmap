"""محرك الإعجابات — بنية أغسطس 2026 (بعد بوابة المستوى OB51+).

★ الفرق الجوهري عن النسخة السابقة:
  النسخة القديمة كانت تنشئ حساباً ضيفاً (مستوى 1) لكل إعجاب — ومنذ OB51
  (أبريل 2026) تتجاهل Garena إعجابات الحسابات دون المستوى 8 صامتاً، لذا
  كانت اللايكات «تُرسل» ولا «تُحتسب» أبداً في اللعبة.

  الآن:
    • الإعجابات تُرسل فقط من «مخزون حسابات حقيقية مساهَم بها» موثَّقة
      (token grant + login حقيقي + مستوى مقروء من البروفايل ≥ MIN_DONOR_LEVEL).
    • منطقة الهدف تُكتشف تلقائياً (لا يختارها المستخدم بعد الآن).
    • التحقق الحي أثناء الإرسال: نقرأ العداد كل عدة إعجابات، ونتوقف مبكراً
      إذا تجاهلت Garena الدفعة (مثلاً: تجاوز سقف ~20 إعجاباً من مرسلين 8-20).
    • الرقم النهائي المعروض = ما زاد فعلاً في عداد اللعبة، لا ما أُرسل.

  الحسابات الضيفية الجديدة تُستخدم فقط كـ«جلسات قارئة» (كشف منطقة/عداد/بحث).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from aiogram import Bot

from config import settings
from services.database import Database
from services.garena import (
    AccountBannedError,
    DailyLimitError,
    GarenaClient,
    GarenaError,
    LoginSession,
    PlayerInfo,
)

logger = logging.getLogger(__name__)

# أولوية تجربة المناطق عند كشف منطقة الهدف (الأكثر شيوعاً أولاً)
REGION_PROBE_ORDER: List[str] = [
    "ME", "IND", "BR", "SG", "BD", "US", "RU", "TH", "VN", "TW", "CIS",
]


@dataclass
class LikeJob:
    user_id: int
    target_uid: str
    region: str = ""           # فارغ = كشف تلقائي
    target_name: str = ""      # للعرض فقط (عند اختيار الهدف من البحث)
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
        # جلسات قارئة مؤقتة لكل منطقة: region → (انتهاء الصلاحية, الجلسة)
        self._readers: Dict[str, Tuple[float, LoginSession]] = {}
        self._reader_lock = asyncio.Lock()

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

    # ---------------- جلسات القراءة (كشف المنطقة/العداد/البحث) ----------------
    async def reader_session(self, region: str) -> LoginSession:
        """جلسة قراءة لمنطقة معيّنة: أولاً من مخزون الحسابات الموثقة (أي
        مستوى — القراءة لا تتطلب مستوى)، وإلا حساب ضيف جديد للقراءة فقط."""
        region = region.upper()
        now = time.time()
        cached = self._readers.get(region)
        if cached and cached[0] > now:
            return cached[1]

        async with self._reader_lock:
            cached = self._readers.get(region)
            if cached and cached[0] > now:
                return cached[1]

            session: Optional[LoginSession] = None
            # 1) حساب مساهَم به من نفس المنطقة (لا يُستهلك — مجرد دخول/قراءة)
            try:
                pool_acc = await self._any_pool_account(region)
                if pool_acc is not None:
                    uid, password_hash = pool_acc
                    access_token, open_id = await self.client.token_grant(uid, password_hash)
                    session = await self.client.major_login(access_token, open_id, region)
            except Exception as exc:  # noqa: BLE001
                logger.debug("قارئة من المخزون (%s) فشلت: %s", region, exc)
                session = None

            # 2) ضيف جديد للقراءة فقط
            if session is None:
                guest = await self.client.register_guest(region)
                session = await self.client.major_login(
                    guest.access_token, guest.open_id, region
                )

            self._readers[region] = (now + settings.reader_session_ttl, session)
            return session

    async def _any_pool_account(self, region: str) -> Optional[Tuple[str, str]]:
        row = await self.db.get_any_account_for_read(region)
        return row

    async def detect_region(self, target_uid: str) -> Tuple[str, Optional[PlayerInfo]]:
        """يكتشف منطقة الهدف تلقائياً بالبحث في سيرفرات المناطق بالترتيب.
        يعيد (المنطقة, معلومات اللاعب) أو يرفع GarenaError."""
        last_err = "بدون رد"
        for region in REGION_PROBE_ORDER:
            try:
                session = await self.reader_session(region)
            except Exception as exc:  # noqa: BLE001
                last_err = f"{region}: {exc}"
                logger.debug("تعذرت جلسة قراءة %s أثناء كشف المنطقة: %s", region, exc)
                continue
            try:
                info = await self.client.get_player_info(session, target_uid, region)
            except Exception as exc:  # noqa: BLE001
                info = None
                logger.debug("قراءة اللاعب %s على %s فشلت: %s", target_uid, region, exc)
            if info and (info.nickname or info.likes is not None):
                return region, info
        raise GarenaError(
            f"تعذّر العثور على الحساب {target_uid} في أي سيرفر ({last_err}). "
            "تأكد من صحة الـ UID وأن الحساب نشط."
        )

    async def read_target_info(
        self, region: str, target_uid: str
    ) -> Optional[PlayerInfo]:
        try:
            session = await self.reader_session(region)
            return await self.client.get_player_info(session, target_uid, region)
        except Exception as exc:  # noqa: BLE001
            logger.debug("تعذّرت قراءة معلومات الهدف: %s", exc)
            return None

    async def search_players(
        self, region: str, keyword: str, limit: int = 8
    ) -> List[PlayerInfo]:
        session = await self.reader_session(region)
        return await self.client.search_accounts(session, keyword, region, limit)

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
        label = f"<code>{job.target_uid}</code>"
        if job.target_name:
            label = f"<b>{job.target_name}</b> (<code>{job.target_uid}</code>)"

        # ---- المرحلة 1: كشف المنطقة تلقائياً + قراءة العداد الحالي ----
        await self._safe_send(job.user_id, f"🔎 جاري تحديد سيرفر الحساب {label} ...")
        before: Optional[int] = None
        target_info: Optional[PlayerInfo] = None
        if job.region:
            region = job.region
            target_info = await self.read_target_info(region, job.target_uid)
            if target_info is None:
                await self._safe_send(
                    job.user_id,
                    "❌ تعذّر العثور على هذا الحساب في السيرفر المحدد.\n"
                    "جرّب بدون تحديد سيرفر وسأكتشفه تلقائياً.",
                )
                return
        else:
            try:
                region, target_info = await self.detect_region(job.target_uid)
            except GarenaError as exc:
                await self._safe_send(job.user_id, f"❌ {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                await self._safe_send(job.user_id, f"❌ فشل كشف المنطقة: {exc}")
                return

        job.region = region
        name = (target_info.nickname if target_info else None) or job.target_name or "بدون اسم"
        before = target_info.likes if target_info else None
        await self._safe_send(
            job.user_id,
            "✅ تم العثور على الحساب:\n"
            f"👤 الاسم: <b>{name}</b>\n"
            f"🌍 السيرفر: <b>{region}</b>"
            + (f"\n⭐ المستوى: {target_info.level}" if target_info and target_info.level is not None else "")
            + (f"\n❤️ اللايكات الحالية: <b>{before}</b>" if before is not None else ""),
        )

        # ---- المرحلة 2: فحص المخزون المتاح لهذا الهدف ----
        available = await self.db.count_available(region, job.target_uid)
        if available <= 0:
            lvl = settings.min_donor_level
            await self._safe_send(
                job.user_id,
                "😞 <b>المخزون فارغ حالياً لهذا السيرفر.</b>\n\n"
                f"منذ تحديث OB51 لا تُحتسب اللايكات إلا من حسابات حقيقية بمستوى <b>{lvl}+</b> —\n"
                "الحسابات الضيفية الجديدة (مستوى 1) تتجاهلها Garena تماماً، ولهذا\n"
                "توقّفت الطريقة القديمة عن العمل في كل البوتات.\n\n"
                "🤝 <b>الحل:</b> ساهم بحساب ضيف بمستوى "
                f"{lvl}+ عبر /donate (اللعب ~3 مباريات كافٍ للمستوى 8).\n"
                "كل مساهمة تخدمك وتخدم كل مستخدمي البوت."
            )
            return

        goal = min(available, settings.max_likes_per_session)
        await self._safe_send(
            job.user_id,
            f"🚀 بدأ إرسال الإعجابات إلى {label}\n"
            f"📦 حسابات متاحة (مستوى {settings.min_donor_level}+): <b>{available}</b>\n"
            f"🎯 هدف الجلسة: <b>{goal}</b> إعجاب\n"
            "📡 سأتحقق من العداد أثناء الإرسال وأعرض الرقم <b>المحتسب فعلياً</b>."
        )

        # ---- المرحلة 3: حلقة الإرسال مع تحقق حي ----
        sent = 0                      # أُرسل (HTTP 200)
        failed = 0                    # أخطاء متتالية
        counted_at_last_check = before if before is not None else None
        stall = 0                     # إعجابات مرسلة منذ آخر زيادة في العداد
        max_failures = max(5, settings.max_retries * 3)
        stop_reason = ""

        for _ in range(goal):
            if job.cancelled:
                stop_reason = "cancelled"
                break

            try:
                result = await self._send_one_like(job)
            except AccountBannedError as exc:
                logger.info("حساب محظور أثناء الإعجاب: %s", exc)
                failed += 0  # حُذف من المخزون — لا يُحسب خطأ متتالياً
                continue
            except (GarenaError, DailyLimitError) as exc:
                failed += 1
                logger.warning("فشل إرسال إعجاب: %s", exc)
                if failed >= max_failures:
                    stop_reason = f"errors:{exc}"
                    break
                if not await self._backoff(job, random.uniform(2, 5)):
                    stop_reason = "cancelled"
                    break
                continue
            except Exception as exc:  # noqa: BLE001 — شبكة/مهلة
                failed += 1
                logger.warning("خطأ شبكة أثناء الإعجاب: %s", exc)
                if failed >= max_failures:
                    stop_reason = f"errors:{exc}"
                    break
                continue

            if result is None:
                # نفد مخزون الحسابات المتاحة لهذا الهدف
                stop_reason = "stock_empty"
                break

            failed = 0
            if result.limit_reached:
                stop_reason = f"limit:{result.message or 'daily limit'}"
                break

            if result.success:
                sent += 1
                stall += 1
                if sent % settings.progress_every == 0:
                    await self._safe_send(
                        job.user_id, f"📨 أُرسل <b>{sent}</b> إعجاب حتى الآن..."
                    )
            else:
                failed += 1
                if failed >= max_failures:
                    stop_reason = f"errors:{result.message}"
                    break
                continue

            # ---- تحقق حي من العداد كل عدة إعجابات ----
            if sent % settings.verify_every == 0:
                current = await self._read_likes(job)
                if current is not None and counted_at_last_check is not None:
                    if current > counted_at_last_check:
                        counted_at_last_check = current
                        stall = 0
                    elif stall >= settings.stall_window:
                        stop_reason = "stalled"
                        break
                elif current is not None:
                    counted_at_last_check = current
                    stall = 0

            if not await self._backoff(
                job,
                random.uniform(settings.min_delay_seconds, settings.max_delay_seconds),
            ):
                stop_reason = "cancelled"
                break

        # ---- المرحلة 4: القراءة النهائية + التقرير الصادق ----
        await asyncio.sleep(2)  # مهلة بسيطة لتثبيت العداد
        after = await self._read_likes(job)
        await self._finish_report(job, name, sent, before, after, stop_reason)

    # ---------------- تقرير النهاية ----------------
    async def _finish_report(
        self,
        job: LikeJob,
        name: str,
        sent: int,
        before: Optional[int],
        after: Optional[int],
        stop_reason: str,
    ) -> None:
        counted: Optional[int] = None
        if before is not None and after is not None:
            counted = max(0, after - before)

        lines: List[str] = []
        if stop_reason == "cancelled":
            lines.append("🛑 <b>أُلغيت المهمة.</b>")
        else:
            lines.append("🏁 <b>انتهت الجلسة</b>")

        lines.append(f"👤 الحساب: <b>{name}</b> (<code>{job.target_uid}</code>) — {job.region}")
        lines.append(f"📨 أُرسل: <b>{sent}</b> إعجاب من حسابات حقيقية موثقة.")

        if before is not None and after is not None:
            lines.append(
                f"📈 العداد في اللعبة: <b>{before}</b> ← <b>{after}</b> "
                f"(+{counted} محتسب ✅)"
            )
            if sent > 0 and counted == 0:
                lines.append(
                    "⚠️ <b>تنبيه:</b> لم تُحتسب الإعجابات رغم نجاح الإرسال — "
                    "غالباً تجاوز الهدف سقف الاستقبال اليومي من حسابات "
                    "مستوى 8-20 (~20/يوم). جرّب غداً بعد التصفير اليومي."
                )
            elif counted is not None and 0 < counted < sent:
                lines.append(
                    "ℹ️ جزء من الإعجابات لم يُحتسب (سقف Garena الداخلي للاستقبال اليومي) — "
                    "هذا طبيعي؛ العدد الكامل يتطلب مرسلين بمستوى 22+."
                )
        else:
            lines.append("ℹ️ تعذّرت قراءة العداد النهائي (السيرفر بطيء) — تحقق داخل اللعبة.")

        if stop_reason.startswith("limit:"):
            lines.append(f"🎯 توقف: بلوغ حد المرسلين اليومي ({stop_reason[6:][:80]}).")
        elif stop_reason == "stalled":
            lines.append(
                "🎯 توقف مبكر: العداد ثبت رغم استمرار الإرسال — وفرنا عليك بقية المحاولات."
            )
        elif stop_reason == "stock_empty":
            lines.append(
                "📦 استُهلك المخزون المتاح لهذا الهدف اليوم — كل حساب يعجب مرة كل 20 ساعة."
            )
        elif stop_reason.startswith("errors:"):
            lines.append(f"⚠️ توقف بسبب أخطاء متكررة: {stop_reason[7:][:120]}")

        lines.append("💡 ساهم بحساب مستوى 8+ عبر /donate لزيادة قدرة البوت.")
        await self._safe_send(job.user_id, "\n".join(lines))
        await self.db.add_likes(job.user_id, sent, counted or 0)

    # ---------------- انتظار قابل للإلغاء ----------------
    async def _backoff(self, job: LikeJob, seconds: float) -> bool:
        waited = 0.0
        while waited < seconds:
            if job.cancelled:
                return False
            await asyncio.sleep(0.25)
            waited += 0.25
        return True

    # ---------------- إرسال إعجاب واحد من المخزون الموثق ----------------
    async def _send_one_like(self, job: LikeJob) -> Optional[object]:
        """يلتقط أفضل حساب متاح (مستوى ≥ الحد، غير مستخدم للهدف خلال 20س)
        ويرسل منه إعجاباً واحداً.

        يعيد:
          LikeResult — نتيجة الإرسال
          None       — لا يوجد حساب متاح (نفد المخزون للهدف)
        يرفع:
          AccountBannedError — بعد وسم الحساب banned في المخزون (يُتخطى)
          GarenaError        — فشل دخول/شبكة يستحق إعادة المحاولة بخطأ متتالي
        """
        account = await self.db.get_available_guest(job.region, job.target_uid)
        if account is None:
            return None
        uid, password_hash = account

        # دخول حقيقي بالحساب المساهَم به
        try:
            access_token, open_id = await self.client.token_grant(uid, password_hash)
        except GarenaError as exc:
            msg = str(exc).lower()
            if "auth" in msg or "invalid" in msg or "password" in msg:
                # بيانات غير صالحة (ربما غيّر المستخدم كلمة السر) → إقصاء
                await self._mark_invalid(uid, f"token_grant: {exc}")
                raise AccountBannedError(f"حساب غير صالح أُقصي: {uid}") from exc
            raise

        try:
            session = await self.client.major_login(access_token, open_id, job.region)
        except AccountBannedError as exc:
            await self._mark_invalid(uid, f"banned: {exc.reason}", status="banned")
            raise

        try:
            await self.db.update_account_token(uid, access_token, open_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("تعذر تحديث التوكن للحساب %s: %s", uid, exc)

        result = await self.client.send_like(session, job.target_uid, job.region)
        if result.success:
            await self.db.mark_guest_used(uid, job.target_uid, job.region)
        return result

    async def _mark_invalid(self, uid: str, note: str, status: str = "invalid") -> None:
        try:
            await self.db.set_account_status(uid, status, note[:200])
            logger.info("وُسم الحساب %s كـ %s (%s)", uid, status, note)
        except Exception as exc:  # noqa: BLE001
            logger.debug("تعذر تحديث حالة الحساب %s: %s", uid, exc)

    # ---------------- قراءة عدد الإعجابات ----------------
    async def _read_likes(self, job: LikeJob) -> Optional[int]:
        info = await self.read_target_info(job.region, job.target_uid)
        return info.likes if info else None

    # ---------------- إرسال آمن ----------------
    async def _safe_send(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(user_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("تعذر إرسال رسالة للمستخدم %s: %s", user_id, exc)
