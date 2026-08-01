"""لوحة تحكم الأدمن: /stats، /broadcast، /ban، /unban، /queue، /clear_queue،
/pool، /addaccounts، /checkaccounts.

كل هذه الأوامر تعمل فقط لصاحب البوت (ADMIN_ID من متغيرات البيئة).
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import settings
from services.database import Database
from services.garena import AccountBannedError, GarenaError
from services.like_engine import LikeEngine
from services.pool import parse_donation_lines, validate_and_store

logger = logging.getLogger(__name__)

router = Router(name="admin")

# حماية: جميع معالجات هذا الملف للأدمن فقط
router.message.filter(F.from_user.id == settings.admin_id)


@router.message(Command("stats"))
async def admin_stats(message: Message, db: Database, engine: LikeEngine) -> None:
    s = await db.get_stats()
    counts = await db.pool_counts()
    ok = counts.get("ok", 0)
    banned = counts.get("banned", 0)
    invalid = counts.get("invalid", 0)
    low = counts.get("low_level", 0) + counts.get("legacy_low", 0)
    await message.answer(
        "📊 <b>إحصائيات البوت:</b>\n"
        f"👥 إجمالي المستخدمين: {s['total_users']}\n"
        f"📈 نشطون آخر 24 ساعة: {s['active_24h']}\n"
        f"📨 طلبات الإعجاب: {s['total_requests']}\n"
        f"❤️ أُرسل: {s['total_likes']} | محتسبة ✅: {s['counted_likes']}\n"
        f"⛔ محظورون (مستخدمون): {s['banned']}\n"
        f"🔄 قائمة الانتظار: {engine.queue_size} | ⚙️ نشطة: {engine.active_count}\n\n"
        "📦 <b>المخزون:</b>\n"
        f"  • صالح (ok): {ok}\n"
        f"  • محظور: {banned} | غير صالح: {invalid} | منخفض المستوى: {low}"
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
        await asyncio.sleep(0.05)

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


# ========================================================
# إدارة مخزون الحسابات (بنية أغسطس 2026)
# ========================================================


@router.message(Command("pool"))
async def admin_pool(message: Message, db: Database) -> None:
    """ملخص المخزون الصالح: المناطق، المستويات، حسابات 22+."""
    summary = await db.stock_summary()
    counts = await db.pool_counts()
    if not summary:
        await message.answer(
            "📦 المخزون فارغ — أضف حسابات عبر /addaccounts بالصيغة:\n"
            "<code>UID:كلمة_السر</code> (سطر لكل حساب)"
        )
        return
    lines = ["📦 <b>المخزون الصالح حسب السيرفر:</b>"]
    for r in summary:
        lines.append(
            f"  • <b>{r['region']}</b>: {r['total']} حساب "
            f"(⭐{r['avg_level']} وسطياً، أعلى ⭐{r['max_level']}، "
            f"{settings.full_like_level}+: <b>{r['high_level']}</b>)"
        )
    ok = counts.get("ok", 0)
    lines.append(
        f"\nالحالات: ok={ok} | banned={counts.get('banned', 0)} | "
        f"invalid={counts.get('invalid', 0)} | low={counts.get('low_level', 0)}"
    )
    lines.append(
        f"💡 حسابات {settings.full_like_level}+ هي الوحيدة القادرة على تجاوز "
        "سقف ~20 إعجاباً محتسباً يومياً لكل هدف."
    )
    await message.answer("\n".join(lines))


@router.message(Command("addaccounts"))
async def admin_add_accounts(
    message: Message, db: Database, engine: LikeEngine, command: CommandObject
) -> None:
    """إضافة حسابات دفعة واحدة — مثل /donate لكن بسقف أكبر (30 سطراً)."""
    if not command.args:
        await message.answer(
            "الاستخدام: /addaccounts ثم أسطر الحسابات في نفس الرسالة:\n"
            "<code>/addaccounts\nUID1:PASS1\nUID2:PASS2</code>\n"
            "(حتى 30 حساباً — يُقبل الهاش الجاهز أيضاً)"
        )
        return

    pairs, rejected = parse_donation_lines(command.args, max_lines=30)
    if not pairs:
        await message.answer(
            "❌ لم يُفهم أي سطر. الصيغة: <code>UID:كلمة_السر</code>\n"
            + ("\n".join(f"• {html.escape(r)}" for r in rejected[:5]) if rejected else "")
        )
        return

    status = await message.answer(
        f"🔐 جاري التحقق الحقيقي من <b>{len(pairs)}</b> حساب (دخول + مستوى + حظر)..."
    )
    results = []
    for i, (uid, password) in enumerate(pairs, 1):
        res = await validate_and_store(
            db, engine.client, uid, password, message.from_user.id
        )
        results.append(res)
        if i % 5 == 0 or i == len(pairs):
            try:
                await status.edit_text(
                    f"🔐 تم فحص {i}/{len(pairs)} — "
                    f"مقبول: {sum(1 for r in results if r.ok)}"
                )
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(1)

    lines = [r.line() for r in results]
    if rejected:
        lines += [f"⚠️ {html.escape(r)}" for r in rejected[:5]]
    accepted = sum(1 for r in results if r.ok)
    lines.append(f"\n📦 النتيجة: <b>{accepted}/{len(results)}</b> حساباً أُضيف للمخزون.")

    text = "\n".join(lines)
    for start in range(0, len(text), 4000):
        await message.answer(text[start : start + 4000])


@router.message(Command("checkaccounts"))
async def admin_check_accounts(
    message: Message, db: Database, engine: LikeEngine, command: CommandObject
) -> None:
    """إعادة تحقق دورية: دخول + مستوى + حظر لأقدم الحسابات غير المفحوصة.

    /checkaccounts [N] — افتراضياً 20 حساباً (الأقدم فحصاً أولاً).
    """
    limit = 20
    if command.args and command.args.strip().isdigit():
        limit = max(1, min(int(command.args.strip()), 100))

    stale_before = int(time.time()) - 6 * 3600  # لم تُفحص منذ 6 ساعات
    accounts = await db.accounts_to_revalidate(stale_before, limit)
    if not accounts:
        await message.answer("✅ كل الحسابات مفحوصة حديثاً — لا شيء لإعادة التحقق منه.")
        return

    status = await message.answer(f"🔄 إعادة فحص <b>{len(accounts)}</b> حساب...")
    ok = banned = invalid = errors = 0
    for acc in accounts:
        uid, phash, region = acc["account_uid"], acc["password_hash"], acc["region"]
        try:
            v = await engine.client.validate_account(uid, phash, region)
            if v.eligible:
                await db.upsert_validated_account(
                    uid, v.region, phash, "", v.nickname, v.level, "ok",
                    None, v.access_token, v.open_id,
                )
                ok += 1
            else:
                await db.upsert_validated_account(
                    uid, v.region, phash, "", v.nickname, v.level, "low_level",
                    None, note=f"مستوى {v.level}",
                )
                invalid += 1
        except AccountBannedError as exc:
            await db.set_account_status(uid, "banned", f"reason={exc.reason}")
            banned += 1
        except GarenaError as exc:
            msg = str(exc).lower()
            if "auth" in msg or "invalid" in msg:
                await db.set_account_status(uid, "invalid", str(exc)[:150])
                invalid += 1
            else:
                errors += 1  # شبكة/429 — يبقى ok ويُفحص لاحقاً
        except Exception:  # noqa: BLE001
            errors += 1
        await asyncio.sleep(1)

    await status.edit_text(
        "✅ <b>انتهت إعادة الفحص:</b>\n"
        f"🟢 صالح: {ok}\n"
        f"⛔ محظور: {banned}\n"
        f"🔴 غير صالح/منخفض: {invalid}\n"
        f"🌐 أخطاء شبكة (يبقى ok): {errors}\n\n"
        "/pool لعرض المخزون."
    )
