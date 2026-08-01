"""لوحة تحكم الأدمن: /stats، /broadcast، /ban، /unban، /queue، /clear_queue،
/stock، /tokens، /refresh_tokens.

كل هذه الأوامر تعمل فقط لصاحب البوت (ADMIN_ID من متغيرات البيئة).
"""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import settings
from services.database import Database
from services.garena import GarenaClient, GarenaError
from services.like_engine import LikeEngine

router = Router(name="admin")

# حماية: جميع معالجات هذا الملف للأدمن فقط
router.message.filter(F.from_user.id == settings.admin_id)


@router.message(Command("stats"))
async def admin_stats(message: Message, db: Database, engine: LikeEngine) -> None:
    s = await db.get_stats()
    stock_by_region = await db.guest_stock_by_region()
    stock_lines = "\n".join(
        f"  • {region}: {count}" for region, count in stock_by_region.items()
    ) or "  (فارغ)"
    await message.answer(
        "📊 <b>إحصائيات البوت:</b>\n"
        f"👥 إجمالي المستخدمين: {s['total_users']}\n"
        f"📈 نشطون آخر 24 ساعة: {s['active_24h']}\n"
        f"📨 طلبات الإعجاب: {s['total_requests']}\n"
        f"❤️ إجمالي الإعجابات المرسلة: {s['total_likes']}\n"
        f"⛔ محظورون: {s['banned']}\n"
        f"🔄 قائمة الانتظار: {engine.queue_size}\n"
        f"⚙️ مهام نشطة الآن: {engine.active_count}\n\n"
        f"📦 <b>مخزون الحسابات:</b>\n{stock_lines}"
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


# ========================================================
# أوامر جديدة: مخزون الحسابات والتوكنات
# ========================================================


@router.message(Command("stock"))
async def admin_stock(message: Message, db: Database, command: CommandObject) -> None:
    """يعرض حالة مخزون الحسابات لكل منطقة."""
    region = command.args.strip().upper() if command.args else None

    if region:
        count = await db.guest_stock_count(region)
        await message.answer(f"📦 مخزون حسابات منطقة {region}: <b>{count}</b> حساب")
        return

    stock_by_region = await db.guest_stock_by_region()
    total = await db.guest_stock_count()
    if not stock_by_region:
        await message.answer("📦 المخزون فارغ حالياً.")
        return

    lines = [f"📦 <b>مخزون الحسابات ({total} إجمالي):</b>"]
    for region, count in stock_by_region.items():
        lines.append(f"  • {region}: {count} حساب")
    await message.answer("\n".join(lines))


@router.message(Command("tokens"))
async def admin_tokens(message: Message, db: Database, command: CommandObject) -> None:
    """يعرض بيانات الحسابات والتوكنات — للأدمن فقط.

    الاستخدام:
      /tokens          — أول 10 حسابات
      /tokens <N>      — أول N حسابات
      /tokens <region> — حسابات منطقة محددة
    """
    args = command.args.strip() if command.args else ""

    # تحديد المنطقة
    region = None
    limit = 10
    if args:
        upper = args.upper()
        if upper.isdigit():
            limit = min(int(upper), 50)
        elif len(upper) <= 4:
            region = upper
            limit = 50
        else:
            limit = min(int(args), 50)

    if region:
        accounts = await db.get_accounts_by_region(region)
        header = f"🔑 <b>حسابات منطقة {region}:</b>\n"
    else:
        accounts = await db.get_accounts_for_token_grant(limit)
        header = f"🔑 <b>أول {len(accounts)} حسابات:</b>\n"

    if not accounts:
        await message.answer("📦 لا توجد حسابات في المخزون.")
        return

        lines = [header]
        seen_uids = set()
        for i, acc in enumerate(accounts[:limit], 1):
            uid = acc.get("account_uid")
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            token_full = acc.get("access_token") or "❌ لا يوجد"
            open_id_display = (acc.get("open_id") or "—")[:30] + ("..." if len(str(acc.get("open_id") or "")) > 30 else "")
            lines.append(
                f"{i}. UID: <code>{uid}</code> | Region: {acc.get('region','—')} | Nick: {acc.get('nickname','—')}\n"
                f"   Password: <code>{acc.get('password','—')}</code> | PassHash: <code>{acc.get('password_hash','—')[:40]}...</code>\n"
                f"   Token (كامل): <code>{token_full}</code>\n"
                f"   OpenID: <code>{open_id_display}</code> | Created: {acc.get('created_at','—')}"
            )

    text = "\n".join(lines)
    # تقسيم الرسالة إذا كانت طويلة
    if len(text) > 4000:
        for chunk_start in range(0, len(text), 4000):
            chunk = text[chunk_start:chunk_start + 4000]
            await message.answer(chunk)
    else:
        await message.answer(text)


@router.message(Command("refresh_tokens"))
async def admin_refresh_tokens(
    message: Message, db: Database, client: GarenaClient, command: CommandObject
) -> None:
    """يحدّث التوكنات لكل الحسابات في المخزون — يحصل على access_token جديد لكل حساب.

    الاستخدام:
      /refresh_tokens          — أول 20 حساب
      /refresh_tokens <N>      — أول N حسابات
    """
    args = command.args.strip() if command.args else ""
    limit = 20
    if args and args.isdigit():
        limit = min(int(args), 100)

    accounts = await db.get_accounts_for_token_grant(limit)
    if not accounts:
        await message.answer("📦 لا توجد حسابات في المخزون.")
        return

    status_msg = await message.answer(
        f"🔄 جاري تحديث التوكنات لـ {len(accounts)} حساب..."
    )

    ok = 0
    fail = 0
    for acc in accounts:
        uid = acc["account_uid"]
        password_hash = acc["password_hash"]
        try:
            access_token, open_id = await client.token_grant(uid, password_hash)
            await db.update_account_token(uid, access_token, open_id)
            ok += 1
        except GarenaError as exc:
            fail += 1
            # حذف الحساب غير الصالح
            if "auth" in str(exc).lower():
                await db.delete_guest_account(uid)
                logger.info("حُذف حساب غير صالح أثناء تحديث التوكنات: %s", uid)
            else:
                logger.warning("فشل تحديث توكن %s: %s", uid, exc)
        except Exception as exc:  # noqa: BLE001
            fail += 1
            logger.warning("خطأ أثناء تحديث توكن %s: %s", uid, exc)
        # تأخير بسيط لتجنب rate limit
        await asyncio.sleep(1)

    await status_msg.edit_text(
        f"✅ <b>انتهى تحديث التوكنات:</b>\n"
        f"🟢 نجح: {ok}\n"
        f"🔴 فشل: {fail}\n"
        f"📦 إجمالي: {len(accounts)}"
    )


@router.message(Command("table"))
async def admin_table(message: Message, db: Database, command: CommandObject) -> None:
    """عرض بيانات الحسابات كجدول نظيف بدون تكرار مع التوكن الكامل."""
    args = command.args.strip() if command.args else ""
    region = args.upper() if args else None
    if region:
        accounts = await db.get_accounts_by_region(region)
        header = f"📋 جدول حسابات منطقة {region} ({len(accounts)} حساب):"
    else:
        accounts = await db.get_all_accounts()
        header = f"📋 جدول كل الحسابات ({len(accounts)} حساب):"
    if not accounts:
        await message.answer("📦 لا توجد حسابات.")
        return
    # تجنب التكرار
    seen = set()
    unique = []
    for acc in accounts:
        uid = acc.get("account_uid")
        if uid and uid not in seen:
            seen.add(uid)
            unique.append(acc)
    lines = [header, "```"]
    # عنوان الأعمدة
    lines.append(f"{'#':<3} {'UID':<12} {'Region':<6} {'Nick':<10} {'Created':<10}")
    lines.append("-" * 45)
    for idx, acc in enumerate(unique[:30], 1):
        uid = str(acc.get("account_uid") or "—")[:11]
        reg = str(acc.get("region") or "—")[:5]
        nick = str(acc.get("nickname") or "—")[:9]
        created = str(acc.get("created_at") or "—")
        tok = str(acc.get("access_token") or "—")
        open_id = str(acc.get("open_id") or "—")
        # عرض كل نتيجة في كتلة واضحة مع التوكن الكامل
        lines.append(f"{idx}. UID={uid} | Reg={reg} | Nick={nick} | Created={created}")
        lines.append(f"   Token_KAMEL={tok}")
        lines.append(f"   OpenID={open_id[:40]}... | PassHash={str(acc.get('password_hash') or '')[:30]}...")
        lines.append("")
    lines.append("```")
    lines.append(f"📌 إجمالي حسابات فريدة: {len(unique)} — جميع التوكنات كاملة أعلاه (بدون تكرار)")
    text = "\n".join(lines)
    # إذا كانت طويلة جداً، نرسل جزءاً أولاً ونذكر أنه كامل
    if len(text) > 4000:
        await message.answer(text[:4000] + "\n... (تم التقصير بسبب الطول — استخدم /export_accounts للتفصيل الكامل)")
    else:
        await message.answer(text)


@router.message(Command("export_accounts"))
async def admin_export_accounts(message: Message, db: Database, command: CommandObject) -> None:
    """يصدّر بيانات الحسابات كاملة — للأدمن فقط.

    الاستخدام:
      /export_accounts          — كل الحسابات
      /export_accounts <region> — حسابات منطقة محددة
    """
    args = command.args.strip() if command.args else ""
    region = args.upper() if args else None

    if region:
        accounts = await db.get_accounts_by_region(region)
        header = f"📋 <b>تصدير حسابات {region}:</b>\n\n"
    else:
        accounts = await db.get_all_accounts()
        header = f"📋 <b>تصدير كل الحسابات:</b>\n\n"

    if not accounts:
        await message.answer("📦 لا توجد حسابات.")
        return

    lines = [header]
    for i, acc in enumerate(accounts, 1):
        lines.append(
            f"{i}. UID: <code>{acc['account_uid']}</code>\n"
            f"   Password: <code>{acc.get('password', '—')}</code>\n"
            f"   PasswordHash: <code>{acc['password_hash']}</code>\n"
            f"   AccessToken: <code>{acc.get('access_token', '—')}</code>\n"
            f"   OpenID: <code>{acc.get('open_id', '—')}</code>\n"
            f"   Region: {acc['region']} | Nick: {acc.get('nickname', '—')}"
        )

    text = "\n\n".join(lines)
    if len(text) > 4000:
        for chunk_start in range(0, len(text), 4000):
            chunk = text[chunk_start:chunk_start + 4000]
            await message.answer(chunk)
    else:
        await message.answer(text)
