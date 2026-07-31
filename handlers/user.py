"""معالجات المستخدمين: /start، /likes، /cancel، أزرار القائمة، تحديثات الحالة."""

from __future__ import annotations

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

from services.database import Database
from services.like_engine import LikeEngine, LikeJob
from utils.constants import REGIONS
from utils.validators import is_valid_uid, normalize_uid

router = Router(name="user")


class LikeFlow(StatesGroup):
    waiting_uid = State()
    waiting_region = State()


# ---------------- لوحات المفاتيح ----------------
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❤️ إرسال لايكات", callback_data="start_like")],
            [InlineKeyboardButton(text="📊 إحصائياتي", callback_data="my_stats")],
            [InlineKeyboardButton(text="ℹ️ المساعدة", callback_data="help")],
        ]
    )


def region_keyboard() -> InlineKeyboardMarkup:
    items = list(REGIONS.items())
    rows = [
        [
            InlineKeyboardButton(text=label, callback_data=f"region:{code}")
            for label, code in items[i : i + 2]
        ]
        for i in range(0, len(items), 2)
    ]
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
        "👋 مرحباً بك في بوت زيادة لايكات فري فاير!\n\n"
        "⚠️ <b>تنبيهات قبل الاستخدام:</b>\n"
        "• الاستخدام على مسؤوليتك الخاصة\n"
        "• يوجد حد يومي للإعجابات (حسب السيرفر)\n"
        "• طلب واحد كل ساعة لكل مستخدم\n\n"
        "👇 اختر من القائمة:",
        reply_markup=main_keyboard(),
    )


@router.message(Command("likes"))
async def cmd_likes(message: Message, state: FSMContext) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) == 2 and is_valid_uid(parts[1]):
        await state.update_data(uid=normalize_uid(parts[1]))
        await state.set_state(LikeFlow.waiting_region)
        await message.answer("🌍 اختر السيرفر (المنطقة):", reply_markup=region_keyboard())
    else:
        await state.set_state(LikeFlow.waiting_uid)
        await message.answer("📩 أرسل رقم UID الخاص بالحساب المستهدف (أرقام فقط):")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, engine: LikeEngine) -> None:
    await state.clear()
    if engine.cancel_for_user(message.from_user.id):
        await message.answer("🛑 تم إلغاء مهمتك الحالية.")
    else:
        await message.answer("ℹ️ لا توجد مهمة نشطة لك حالياً.")


# ---------------- تدفق FSM: UID ← السيرفر ----------------
@router.message(LikeFlow.waiting_uid)
async def on_uid(message: Message, state: FSMContext) -> None:
    if not is_valid_uid(message.text or ""):
        await message.answer(
            "❌ UID غير صالح! يجب أن يكون أرقاماً فقط (من 6 إلى 12 رقماً).\n"
            "أرسل UID صحيح:"
        )
        return
    await state.update_data(uid=normalize_uid(message.text))
    await state.set_state(LikeFlow.waiting_region)
    await message.answer("🌍 اختر السيرفر (المنطقة):", reply_markup=region_keyboard())


@router.callback_query(LikeFlow.waiting_region, F.data.startswith("region:"))
async def on_region(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    engine: LikeEngine,
) -> None:
    region = callback.data.split(":", 1)[1]
    data = await state.get_data()
    uid = data.get("uid")
    await state.clear()

    if not uid:
        await callback.answer("انتهت الجلسة، ابدأ من جديد.", show_alert=True)
        return

    user_id = callback.from_user.id

    # ------- حد الاستخدام: مرة كل ساعة -------
    allowed, wait = await db.can_request(user_id)
    if not allowed:
        minutes = int(wait // 60)
        await callback.answer("تم.", show_alert=False)
        await callback.message.answer(
            f"⏳ وصلت للحد المسموح. انتظر {minutes} دقيقة قبل طلب جديد "
            "(مرة واحدة كل ساعة)."
        )
        return

    await db.mark_request_started(user_id)

    job = LikeJob(user_id=user_id, target_uid=uid, region=region)
    position = engine.submit(job)

    await callback.answer("✅ تم استلام الطلب!")
    await callback.message.answer(
        f"✅ <b>تم استلام طلبك!</b>\n"
        f"🎯 UID: <code>{uid}</code>\n"
        f"🌍 السيرفر: {region}\n"
        f"📌 موقعه في قائمة الانتظار: ~{position}\n\n"
        "سأرسل لك تحديثات لحظة بلحظة. استخدم /cancel للإلغاء."
    )


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
    await callback.message.answer(
        "📊 <b>إحصائياتك:</b>\n"
        f"• عدد الطلبات: {info['total_requests']}\n"
        f"• إجمالي الإعجابات المرسلة: {info['total_likes']}"
    )


@router.callback_query(F.data == "help")
async def on_help(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        "ℹ️ <b>طريقة الاستخدام:</b>\n"
        "1️⃣ اضغط «❤️ إرسال لايكات»\n"
        "2️⃣ أرسل UID الحساب الهدف\n"
        "3️⃣ اختر السيرفر\n"
        "4️⃣ انتظر التحديثات\n\n"
        "<b>الأوامر:</b>\n"
        "/likes &lt;UID&gt; — بدء سريع\n"
        "/cancel — إلغاء المهمة الحالية"
    )


@router.callback_query(F.data == "cancel")
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer("❌ تم الإلغاء.")
