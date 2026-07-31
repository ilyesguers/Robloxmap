"""لوحة تحكم الأدمن: /stats، /broadcast، /ban، /unban، /queue، /clear_queue، /diag.

كل هذه الأوامر تعمل فقط لصاحب البوت (ADMIN_ID من متغيرات البيئة).
"""

from __future__ import annotations

import asyncio
import json

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import settings
from services.database import Database
from services.like_engine import LikeEngine

router = Router(name="admin")

# حماية: جميع معالجات هذا الملف للأدمن فقط
router.message.filter(F.from_user.id == settings.admin_id)


@router.message(Command("stats"))
async def admin_stats(message: Message, db: Database, engine: LikeEngine) -> None:
    s = await db.get_stats()
    await message.answer(
        "📊 <b>إحصائيات البوت:</b>\n"
        f"👥 إجمالي المستخدمين: {s['total_users']}\n"
        f"📈 نشطون آخر 24 ساعة: {s['active_24h']}\n"
        f"📨 طلبات الإعجاب: {s['total_requests']}\n"
        f"❤️ إجمالي الإعجابات المرسلة: {s['total_likes']}\n"
        f"⛔ محظورون: {s['banned']}\n"
        f"🔄 قائمة الانتظار: {engine.queue_size}\n"
        f"⚙️ مهام نشطة الآن: {engine.active_count}"
    )


@router.message(Command("broadcast"))
async def admin_broadcast(message: Message, db: Database) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("الاستخدام: /broadcast <النص الذي تريد إرساله>")
        return

    text = parts[1]
    user_ids = await db.all_user_ids()
    if not user_ids:
        await message.answer("ℹ️ لا يوجد مستخدمون مسجلون بعد.")
        return

    status = await message.answer(f"📣 جاري الإرسال إلى {len(user_ids)} مستخدم...")
    ok = fail = 0
    for uid in user_ids:
        try:
            await message.bot.send_message(uid, text)
            ok += 1
        except Exception:  # noqa: BLE001
            fail += 1
        await asyncio.sleep(0.05)  # تجنب flood limit من تيليجرام

    await status.edit_text(f"✅ تم الإرسال إلى {ok} مستخدم، فشل {fail}.")


@router.message(Command("ban"))
async def admin_ban(message: Message, db: Database, command: CommandObject) -> None:
    if not command.args:
        await message.answer("الاستخدام: /ban <user_id> [السبب]")
        return
    parts = command.args.split(maxsplit=1)
    try:
        target = int(parts[0])
    except ValueError:
        await message.answer("❌ معرّف مستخدم غير صالح.")
        return
    if target == settings.admin_id:
        await message.answer("❌ لا يمكنك حظر نفسك!")
        return
    reason = parts[1] if len(parts) > 1 else ""
    await db.ban_user(target, reason)
    await message.answer(f"⛔ تم حظر {target}." + (f"\nالسبب: {reason}" if reason else ""))


@router.message(Command("unban"))
async def admin_unban(message: Message, db: Database, command: CommandObject) -> None:
    if not command.args:
        await message.answer("الاستخدام: /unban <user_id>")
        return
    try:
        target = int(command.args.strip())
    except ValueError:
        await message.answer("❌ معرّف مستخدم غير صالح.")
        return
    if await db.unban_user(target):
        await message.answer(f"✅ تم فك الحظر عن {target}.")
    else:
        await message.answer("ℹ️ هذا المستخدم ليس محظوراً.")


@router.message(Command("queue"))
async def admin_queue(message: Message, engine: LikeEngine) -> None:
    await message.answer(
        f"🔄 مهام في قائمة الانتظار: {engine.queue_size}\n"
        f"⚙️ مهام نشطة الآن: {engine.active_count}"
    )


@router.message(Command("clear_queue"))
async def admin_clear_queue(message: Message, engine: LikeEngine) -> None:
    cleared = engine.clear_queue()
    await message.answer(f"🗑️ تم مسح {cleared} مهمة من قائمة الانتظار.")


@router.message(Command("diag"))
@router.message(Command("diagnostics"))
async def admin_diag(message: Message, engine: LikeEngine) -> None:
    """عرض التشخيص المفصل — Hybrid pool + fallbacks + errors"""
    diag = engine.get_diagnostics()
    eng = diag.get("engine", {})
    gar = diag.get("garena", {})

    reg = gar.get("register", {})
    like = gar.get("like", {})
    pool = gar.get("pool", {})

    text = (
        "🔍 <b>تشخيص البوت — Hybrid Pool + Fallbacks</b>\n\n"
        f"🕒 Uptime: {gar.get('uptime_sec',0)}s\n"
        f"📦 Jobs كلّي: {eng.get('total_jobs',0)} | Likes مرسلة: {eng.get('total_likes_sent',0)} | فشل: {eng.get('total_failed',0)}\n"
        f"🎯 Limit hits: {eng.get('total_limit_hits',0)} | ملغاة: {eng.get('total_cancelled',0)}\n"
        f"⏱ متوسط/لايك: {eng.get('avg_time_per_like',0)}s\n"
        f"🌍 per region jobs: {eng.get('jobs_per_region',{})}\n"
        f"❤️ per region likes: {eng.get('likes_per_region',{})}\n\n"
        f"🧩 <b>Garena:</b>\n"
        f"• Register: {reg.get('attempts',0)} محاولة / نجاح {reg.get('success',0)} / فشل {reg.get('failed',0)}\n"
        f"  ↳ per region attempt: {reg.get('per_region_attempt',{})}\n"
        f"  ↳ per region success: {reg.get('per_region_success',{})}\n"
        f"• Token: {gar.get('token_grant',{}).get('attempts',0)} / نجاح {gar.get('token_grant',{}).get('success',0)}\n"
        f"• MajorRegister: {gar.get('major_register',{}).get('attempts',0)} / نجاح {gar.get('major_register',{}).get('success',0)}\n"
        f"• Login: {gar.get('login',{}).get('attempts',0)} / نجاح {gar.get('login',{}).get('success',0)} / فشل {gar.get('login',{}).get('failed',0)}\n"
        f"• Like: محاولات {like.get('attempts',0)} / نجاح {like.get('success',0)} / فشل {like.get('failed',0)} / limit {like.get('limit_hits',0)}\n"
        f"• Read: محاولات {gar.get('read',{}).get('attempts',0)} / نجاح {gar.get('read',{}).get('success',0)}\n\n"
        f"🏊 <b>Pool:</b> hits={pool.get('hits',0)} miss={pool.get('miss',0)} current={pool.get('current_size',0)} "
        f"| engine_pool={eng.get('pool_stats',{}).get('engine_pool',0)} read_cache={eng.get('pool_stats',{}).get('read_cache_regions',0)}\n"
    )

    # آخر أخطاء
    last_errs = gar.get("last_errors", [])[-7:]
    if last_errs:
        text += "\n⚠️ آخر أخطاء:\n" + "\n".join(f"• {e[:120]}" for e in last_errs)

    # قطع إلى 4000 حرف (حد تيليجرام)
    if len(text) > 4000:
        text = text[:4000]

    await message.answer(text)


@router.message(Command("clear_pool"))
async def admin_clear_pool(message: Message, engine: LikeEngine) -> None:
    cleared = engine.client.clear_pool()
    # أيضاً نظّف كاش المحرك
    engine._read_sessions_cache.clear()
    engine._like_sessions_pool.clear()
    await message.answer(f"🧹 تم مسح التجمع الهجين: {cleared} جلسة")
