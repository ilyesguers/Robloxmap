"""معالجات المستخدمين — بنية أغسطس 2026.

التغييرات الجوهرية:
  • لا يسأل البوت عن السيرفر في تدفق UID — يُكتشف تلقائياً.
  • بحث بالاسم (FuzzySearchAccountByName) → اختيار الحساب من النتائج.
  • /donate — مساهمة بحساب ضيف مستوى 8+ (الطريقة الوحيدة الشغالة بعد OB51).
"""
from __future__ import annotations

import asyncio
import html

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import settings
from services.database import Database
from services.garena import GarenaClient
from services.like_engine import LikeEngine, LikeJob
from services.pool import parse_donation_lines, validate_and_store
from utils.constants import REGIONS
from utils.validators import is_valid_uid, normalize_uid

router = Router(name="user")


class LikeFlow(StatesGroup):
    waiting_uid = State()


class SearchFlow(StatesGroup):
    waiting_name = State()
    waiting_region = State()
    picking = State()


class DonateFlow(StatesGroup):
    waiting_lines = State()


# ---------------- لوحات المفاتيح ----------------
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❤️ إرسال لايكات (UID)", callback_data="start_like")],
            [InlineKeyboardButton(text="🔍 بحث بالاسم", callback_data="search_name")],
            [InlineKeyboardButton(text="🤝 مساهمة بحساب (donate)", callback_data="donate")],
            [InlineKeyboardButton(text="📊 إحصائياتي", callback_data="my_stats")],
            [InlineKeyboardButton(text="ℹ️ المساعدة", callback_data="help")],
        ]
    )


def region_keyboard(prefix: str = "sregion") -> InlineKeyboardMarkup:
    items = list(REGIONS.items())
    rows = [
        [
            InlineKeyboardButton(text=label, callback_data=f"{prefix}:{code}")
            for label, code in items[i : i + 2]
        ]
        for i in range(0, len(items), 2)
    ]
    rows.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pick_keyboard(players) -> InlineKeyboardMarkup:
    rows = []
    for p in players:
        nick = (p.nickname or "بدون اسم")[:20]
        lvl = f"· ⭐{p.level}" if p.level is not None else ""
        likes = f"· ❤️{p.likes}" if p.likes is not None else ""
        rows.append(
            [InlineKeyboardButton(
                text=f"{nick} {lvl} {likes}".strip(),
                callback_data=f"pick:{p.uid}",
            )]
        )
    rows.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------- الأوامر الأساسية ----------------
@router.message(CommandStart())
async def cmd_start(message: Message, db: Database) -> None:
    await db.register_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )
    await message.answer(
        "👋 مرحباً بك في بوت لايكات فري فاير (نسخة أغسطس 2026)\n\n"
        "⚙️ <b>كيف يعمل بعد تحديث OB51؟</b>\n"
        "• أرسل UID الحساب (أو ابحث بالاسم) — السيرفر يُكتشف تلقائياً\n"
        f"• الإعجابات تُرسل من <b>حسابات حقيقية مستوى {settings.min_donor_level}+</b> "
        "موثقة — الطريقة الوحيدة التي ما زالت تُحتسب في اللعبة\n"
        "• أرقام التقارير = ما زاد فعلاً في عداد اللعبة، لا مجرد «أُرسل»\n\n"
        "🤝 البوت يعمل بنظام المساهمة: كل حساب مستوى "
        f"{settings.min_donor_level}+ تضيفه عبر /donate يخدم الجميع.\n\n"
        "⚠️ الاستخدام على مسؤوليتك (مخالف لشروط Garena).\n"
        "👇 اختر من القائمة:",
        reply_markup=main_keyboard(),
    )


@router.message(Command("likes"))
async def cmd_likes(
    message: Message, state: FSMContext, db: Database, engine: LikeEngine
) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) == 2 and is_valid_uid(parts[1]):
        await _submit_like_request(
            message, state, db, engine, normalize_uid(parts[1])
        )
    else:
        await state.set_state(LikeFlow.waiting_uid)
        await message.answer("📩 أرسل رقم UID الخاص بالحساب المستهدف (أرقام فقط):")


@router.message(Command("donate"))
async def cmd_donate(message: Message, state: FSMContext) -> None:
    await state.set_state(DonateFlow.waiting_lines)
    await message.answer(_donate_instructions())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, engine: LikeEngine) -> None:
    await state.clear()
    if engine.cancel_for_user(message.from_user.id):
        await message.answer("🛑 تم إلغاء مهمتك الحالية.")
    else:
        await message.answer("ℹ️ لا توجد مهمة نشطة لك حالياً.")


# ---------------- إرسال طلب إعجاب (موحّد) ----------------
async def _submit_like_request(
    event,
    state: FSMContext,
    db: Database,
    engine: LikeEngine,
    uid: str,
    region: str = "",
    target_name: str = "",
) -> None:
    """يوحّد تقديم الطلب من رسالة أو من زر (CallbackQuery)."""
    if isinstance(event, CallbackQuery):
        user_id = event.from_user.id
        answer = event.message.answer
    else:
        user_id = event.from_user.id
        answer = event.answer

    await state.clear()

    allowed, wait = await db.can_request(user_id)
    if not allowed:
        minutes = int(wait // 60) + 1
        await answer(
            f"⏳ وصلت للحد المسموح. انتظر {minutes} دقيقة قبل طلب جديد."
        )
        return

    await db.mark_request_started(user_id)
    job = LikeJob(
        user_id=user_id, target_uid=uid,
        region=region, target_name=target_name,
    )
    position = engine.submit(job)
    await answer(
        f"✅ <b>تم استلام طلبك!</b>\n"
        f"🎯 الحساب: <code>{uid}</code>\n"
        f"📌 موقعك في قائمة الانتظار: ~{position}\n\n"
        "🔎 سأكتشف السيرفر تلقائياً وأرسل التحديثات هنا. /cancel للإلغاء."
    )


@router.message(LikeFlow.waiting_uid)
async def on_uid(
    message: Message, state: FSMContext, db: Database, engine: LikeEngine
) -> None:
    if not is_valid_uid(message.text or ""):
        await message.answer(
            "❌ UID غير صالح! يجب أن يكون أرقاماً فقط (من 6 إلى 12 رقماً).\n"
            "أرسل UID صحيح أو /cancel:"
        )
        return
    await _submit_like_request(
        message, state, db, engine, normalize_uid(message.text)
    )


# ---------------- البحث بالاسم ----------------
@router.callback_query(F.data == "search_name")
async def on_search_name(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SearchFlow.waiting_name)
    await callback.message.answer(
        "🔍 أرسل <b>اسم اللاعب</b> كما يظهر في اللعبة (أو جزءاً منه):"
    )


@router.message(SearchFlow.waiting_name)
async def on_search_name_text(message: Message, state: FSMContext) -> None:
    keyword = (message.text or "").strip()
    if len(keyword) < 2:
        await message.answer("❌ الاسم قصير جداً — أرسل حرفين على الأقل:")
        return
    await state.update_data(keyword=keyword)
    await state.set_state(SearchFlow.waiting_region)
    await message.answer(
        f"🔎 سأبحث عن «{html.escape(keyword)}» — اختر سيرفر البحث\n"
        "(الاسم يُبحث عنه داخل سيرفر واحد في كل مرة):",
        reply_markup=region_keyboard(),
    )


@router.callback_query(SearchFlow.waiting_region, F.data.startswith("sregion:"))
async def on_search_region(
    callback: CallbackQuery, state: FSMContext, engine: LikeEngine
) -> None:
    region = callback.data.split(":", 1)[1]
    data = await state.get_data()
    keyword = data.get("keyword", "")
    await state.set_state(SearchFlow.picking)
    await callback.answer("🔎 جاري البحث...")
    status = await callback.message.answer(
        f"🔎 أبحث عن «{html.escape(keyword)}» في سيرفر {region} ..."
    )
    try:
        players = await engine.search_players(region, keyword, limit=8)
    except Exception as exc:  # noqa: BLE001
        await state.clear()
        await status.edit_text(f"❌ فشل البحث: {str(exc)[:150]}")
        return
    if not players:
        await state.clear()
        await status.edit_text(
            f"😞 لا نتائج عن «{html.escape(keyword)}» في {region}.\n"
            "جرّب سيرفراً آخر أو تأكد من كتابة الاسم."
        )
        return
    await state.update_data(search_region=region)
    await status.edit_text(
        f"🎯 وجدت <b>{len(players)}</b> نتيجة في {region} — اختر الحساب:",
        reply_markup=pick_keyboard(players),
    )


@router.callback_query(SearchFlow.picking, F.data.startswith("pick:"))
async def on_pick_player(
    callback: CallbackQuery, state: FSMContext, db: Database, engine: LikeEngine
) -> None:
    uid = callback.data.split(":", 1)[1]
    data = await state.get_data()
    region = data.get("search_region", "")
    await callback.answer("✅")
    await _submit_like_request(callback, state, db, engine, uid, region=region)


# ---------------- المساهمة بحساب ----------------
def _donate_instructions() -> str:
    lvl = settings.min_donor_level
    return (
        "🤝 <b>ساهم بحساب ضيف لتشغيل البوت</b>\n\n"
        "لماذا؟ منذ تحديث OB51 تتجاهل Garena لايكات الحسابات تحت المستوى "
        f"<b>{lvl}</b> — لذا البوت يعمل فقط بحسابات حقيقية مساهَم بها.\n\n"
        "<b>الخطوات (من هاتفك):</b>\n"
        "1️⃣ افتح فري فاير ← أنشئ/استخدم حساب <b>ضيف</b> (Guest)\n"
        f"2️⃣ العب حتى تصل للمستوى {lvl}+ (~3 مباريات كلاش سكواد عادةً)\n"
        "3️⃣ أرسل هنا بالصيغة:\n"
        "<code>UID:كلمة_السر</code>\n"
        "مثال: <code>1234567890:MyPass123</code>\n\n"
        "• يمكن إرسال حتى 5 حسابات (سطر لكل حساب)\n"
        "• يُقبل هاش SHA256 الجاهز أيضاً (أداة استخراج الضيوف)\n"
        "• سأتحقق من كل حساب <b>حقيقياً</b> (دخول + مستوى + حظر) قبل قبوله\n\n"
        "أرسل الحسابات الآن أو /cancel للإلغاء."
    )


@router.callback_query(F.data == "donate")
async def on_donate_button(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(DonateFlow.waiting_lines)
    await callback.message.answer(_donate_instructions())


@router.message(DonateFlow.waiting_lines)
async def on_donate_lines(
    message: Message, state: FSMContext, db: Database, engine: LikeEngine
) -> None:
    await state.clear()
    pairs, rejected = parse_donation_lines(
        message.text or "", settings.donate_max_lines
    )
    if not pairs:
        await message.answer(
            "❌ لم أفهم أي سطر. الصيغة المطلوبة: <code>UID:كلمة_السر</code>\n"
            + ("\n".join(f"• {html.escape(r)}" for r in rejected[:5]) if rejected else "")
            + "\n\nأعد المحاولة عبر /donate"
        )
        return

    status = await message.answer(
        f"🔐 جاري التحقق الحقيقي من <b>{len(pairs)}</b> حساب (دخول + مستوى)..."
    )
    client: GarenaClient = engine.client
    results = []
    for uid, password in pairs:
        res = await validate_and_store(
            db, client, uid, password, message.from_user.id
        )
        results.append(res)
        await asyncio.sleep(1)  # لطف بالواجهات

    lines = [r.line() for r in results]
    if rejected:
        lines += [f"⚠️ {html.escape(r)}" for r in rejected[:3]]
    accepted = sum(1 for r in results if r.ok)
    if accepted:
        lines.append(
            f"\n🎉 شكراً! أُضيف <b>{accepted}</b> حساب للمخزون المشترك ❤️"
        )
    else:
        lines.append(
            "\nℹ️ لم يُقبل أي حساب هذه المرة. تحقق من البيانات والمستوى وأعد المحاولة."
        )
    await status.edit_text("\n".join(lines))


# ---------------- أزرار القائمة الرئيسية ----------------
@router.callback_query(F.data == "start_like")
async def on_start_like(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(LikeFlow.waiting_uid)
    await callback.message.answer("📩 أرسل رقم UID الخاص بالحساب المستهدف (أرقام فقط):")


@router.callback_query(F.data == "my_stats")
async def on_my_stats(callback: CallbackQuery, db: Database) -> None:
    await callback.answer()
    info = await db.user_info(callback.from_user.id)
    if not info:
        await callback.message.answer("ℹ️ لا توجد بيانات بعد — ابدأ أول طلب.")
        return
    total_stock = await db.guest_stock_count()
    summary = await db.stock_summary()
    stock_lines = ", ".join(f"{r['region']}:{r['total']}" for r in summary[:8]) or "فارغ"
    await callback.message.answer(
        "📊 <b>إحصائياتك:</b>\n"
        f"• عدد الطلبات: {info['total_requests']}\n"
        f"• إعجابات أُرسلت لطلباتك: {info['total_likes']}\n"
        f"• منها محتسبة في اللعبة ✅: {info.get('counted_likes', 0)}\n"
        f"• حسابات ساهمت بها 🤝: {info.get('contributions', 0)}\n\n"
        f"📦 المخزون المشترك الصالح: {total_stock} حساب\n"
        f"({stock_lines})"
    )


@router.callback_query(F.data == "help")
async def on_help(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        "ℹ️ <b>طريقة الاستخدام:</b>\n"
        "1️⃣ «❤️ إرسال لايكات» ← أرسل UID (السيرفر تلقائي)\n"
        "2️⃣ أو «🔍 بحث بالاسم» ← اختر الحساب من النتائج\n"
        "3️⃣ تابع التحديثات — الرقم النهائي هو <b>المحتسب فعلياً</b> في اللعبة\n\n"
        "<b>الأوامر:</b>\n"
        "/likes &lt;UID&gt; — بدء سريع\n"
        "/donate — مساهمة بحساب مستوى 8+ (يشغّل البوت)\n"
        "/cancel — إلغاء المهمة الحالية\n\n"
        "<b>لماذا المساهمات؟</b> منذ OB51 تتجاهل Garena لايكات الحسابات "
        "تحت المستوى 8 — لا حسابات = لا لايكات، عند كل البوتات."
    )


@router.callback_query(F.data == "cancel")
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer("❌ تم الإلغاء.")
