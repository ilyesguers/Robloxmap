"""
Smoke Test — يتحقق من المنطق الكامل مع سيرفر وهمي يحاكي واجهات Garena:
register → token → MajorRegister → MajorLogin → LikeProfile → GetPlayerPersonalShow

التشغيل:
    python tests/smoke_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys

# إعدادات سريعة للاختبار (تُضبط قبل استيراد config)
os.environ["MIN_DELAY_SECONDS"] = "0"
os.environ["MAX_DELAY_SECONDS"] = "0"
os.environ["MAX_LIKES_PER_SESSION"] = "50"
os.environ["PROGRESS_EVERY"] = "2"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web  # noqa: E402

from services.database import Database  # noqa: E402
from services.garena import (  # noqa: E402
    _aes_encrypt,
    _field_bytes,
    _field_string,
    _field_varint,
    _parse_protobuf,
    _varint_encode,
)
from services.like_engine import LikeEngine, LikeJob  # noqa: E402

# حقن روابط السيرفر الوهمي بدل الحقيقي
import services.garena as garena_mod

FAKE = "http://127.0.0.1:8981"
garena_mod.URL_GUEST_REGISTER = f"{FAKE}/oauth/guest/register"
garena_mod.URL_TOKEN_GRANT = f"{FAKE}/oauth/guest/token/grant"
garena_mod.URL_MAJOR_REGISTER = f"{FAKE}/MajorRegister"
garena_mod.URL_MAJOR_LOGIN = f"{FAKE}/MajorLogin"

TARGET = "123456789"
MAX_LIKES = 7  # السيرفر الوهمي يقبل 7 إعجابات ثم يرد بالحد
state = {"likes": 0, "guests": 0, "registered_uids": set()}
FAKE_SERVER = f"{FAKE}/game"


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append(text)


# ---------- معالجات السيرفر الوهمي ----------
async def handle_register(request: web.Request) -> web.Response:
    form = await request.post()
    assert form.get("password"), "missing password"
    assert "Signature" in (request.headers.get("Authorization") or ""), "missing signature"
    state["guests"] += 1
    uid = f"9{state['guests']:08d}"
    state["registered_uids"].add(uid)
    return web.json_response({"uid": uid})


async def handle_token(request: web.Request) -> web.Response:
    form = await request.post()
    uid = form.get("uid")
    assert uid in state["registered_uids"], f"unknown uid {uid}"
    return web.json_response(
        {"access_token": f"AT-{uid}", "open_id": f"OI-{uid}"}
    )


async def handle_major_register(request: web.Request) -> web.Response:
    body = await request.read()
    assert body, "empty body"
    assert "Bearer" in (request.headers.get("Authorization") or "")
    return web.Response(status=200, body=b"")


async def handle_major_login(request: web.Request) -> web.Response:
    body = await request.read()
    assert body, "empty body"
    # الرد: protobuf مع token (حقل 8) و serverUrl (حقل 10)
    resp = (
        _field_varint(1, 777)
        + _field_string(2, "ME")
        + _field_string(8, "JWT-FAKE-123")
        + _field_string(10, FAKE_SERVER)
    )
    return web.Response(status=200, body=resp)


async def handle_like(request: web.Request) -> web.Response:
    body = await request.read()
    assert body, "empty body"
    assert "Bearer" in (request.headers.get("Authorization") or "")
    if state["likes"] >= MAX_LIKES:
        return web.Response(status=400, body=b"daily like limit reached")
    state["likes"] += 1
    return web.Response(status=200, body=b"")


async def handle_major_login2(request: web.Request) -> web.Response:
    """MajorLogin خاص بالسيرفر الثاني — يعيد serverUrl يشير له."""
    await request.read()
    resp = (
        _field_varint(1, 778)
        + _field_string(2, "IND")
        + _field_string(8, "JWT-FAKE-456")
        + _field_string(10, "http://127.0.0.1:8982/game")
    )
    return web.Response(status=200, body=resp)


async def handle_personal_show(request: web.Request) -> web.Response:
    body = await request.read()
    assert body
    # الرد: Info{ AccountInfo{ ... liked(21) ... } }
    account = _field_varint(1, int(TARGET)) + _field_string(3, "TestPlayer") + _field_varint(21, state["likes"])
    resp = _field_bytes(1, account)
    return web.Response(status=200, body=resp)


async def main() -> None:
    app = web.Application()
    app.router.add_post("/oauth/guest/register", handle_register)
    app.router.add_post("/oauth/guest/token/grant", handle_token)
    app.router.add_post("/MajorRegister", handle_major_register)
    app.router.add_post("/MajorLogin", handle_major_login)
    app.router.add_post("/game/LikeProfile", handle_like)
    app.router.add_post("/game/GetPlayerPersonalShow", handle_personal_show)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8981).start()

    # ---------- اختبار العميل وحده ----------
    client = garena_mod.GarenaClient()
    await client.start()

    guest = await client.register_guest("ME")
    assert guest.uid and guest.password_hash and guest.access_token and guest.open_id
    print("✔ register_guest (ضيف جديد كامل) OK → UID", guest.uid)

    session = await client.major_login(guest.access_token, guest.open_id)
    assert session.jwt == "JWT-FAKE-123" and session.server_url == FAKE_SERVER
    print("✔ major_login (JWT) OK")

    info = await client.get_player_info(session, TARGET)
    assert info and info.likes == 0 and info.nickname == "TestPlayer"
    print("✔ get_player_info (قراءة عدد الإعجابات) OK →", info.likes)

    r = await client.send_like(session, TARGET, "ME")
    assert r.success
    print("✔ send_like OK (إعجاب واحد)")

    # الوصول للحد
    while True:
        r = await client.send_like(session, TARGET, "ME")
        if r.limit_reached:
            break
    assert r.limit_reached
    print("✔ كشف الحد اليومي OK →", r.message)

    await client.close()

    # ---------- اختبار المحرك كاملاً ----------
    state["likes"] = 0
    db = Database(path="/tmp/engine_test2.db")
    await db.init()
    bot = FakeBot()
    client2 = garena_mod.GarenaClient()
    await client2.start()
    engine = LikeEngine(bot=bot, db=db, client=client2)
    engine.start()

    engine.submit(LikeJob(user_id=42, target_uid=TARGET, region="ME"))
    await asyncio.sleep(6)

    info = await db.user_info(42)
    assert info and info["total_likes"] == MAX_LIKES, f"expected {MAX_LIKES}, got {info}"
    assert any("الحد اليومي" in m for m in bot.messages), "no limit message"
    assert any("تم إرسال <b>7</b> إعجاب" in m for m in bot.messages), "no final count"
    assert any("التحقق" in m for m in bot.messages), "no verification line"
    assert state["guests"] >= MAX_LIKES + 1, "guest accounts should be created per like"
    print("✔ المحرك: حلقة كاملة + حد يومي + تحقق + حسابات ضيوف جديدة OK")
    print(f"   (تم إنشاء {state['guests']} حساب ضيف في هذه الجلسة)")

    # ---------- اختبار الإلغاء ----------
    async def slow_like(request: web.Request) -> web.Response:
        await asyncio.sleep(0.05)
        return web.Response(status=200, body=b"")

    app2 = web.Application()
    app2.router.add_post("/oauth/guest/register", handle_register)
    app2.router.add_post("/oauth/guest/token/grant", handle_token)
    app2.router.add_post("/MajorRegister", handle_major_register)
    app2.router.add_post("/MajorLogin", handle_major_login2)
    app2.router.add_post("/game/LikeProfile", slow_like)
    runner2 = web.AppRunner(app2)
    await runner2.setup()
    await web.TCPSite(runner2, "127.0.0.1", 8982).start()

    import services.garena as g2

    g2.URL_GUEST_REGISTER = "http://127.0.0.1:8982/oauth/guest/register"
    g2.URL_TOKEN_GRANT = "http://127.0.0.1:8982/oauth/guest/token/grant"
    g2.URL_MAJOR_REGISTER = "http://127.0.0.1:8982/MajorRegister"
    g2.URL_MAJOR_LOGIN = "http://127.0.0.1:8982/MajorLogin"

    client3 = g2.GarenaClient()
    await client3.start()
    engine2 = LikeEngine(bot=bot, db=db, client=client3)
    engine2.start()
    engine2.submit(LikeJob(user_id=43, target_uid="987654321", region="IND"))
    await asyncio.sleep(0.3)
    assert engine2.cancel_for_user(43), "cancel should find active job"
    await asyncio.sleep(1)
    if not any("🛑 تم إلغاء المهمة" in m for m in bot.messages):
        print("DEBUG messages:", bot.messages)
    assert any("🛑 تم إلغاء المهمة" in m for m in bot.messages), "cancel message missing"
    print("✔ الإلغاء في منتصف المهمة OK")

    await engine2.stop()
    await client3.close()
    await runner2.cleanup()
    await engine.stop()
    await client2.close()
    await runner.cleanup()

    # ---------- اختبار مخزون الحسابات الجاهزة (SEED + استهلاك + حفظ) ----------
    from services.garena import SEED_GUEST_ACCOUNTS  # noqa: E402

    db3 = Database(path="/tmp/stock_test3.db")
    await db3.init()
    # حقن المخزون: حساب واحد جاهز لمنطقة ME
    avail = await db3.get_available_guest("ME", TARGET)
    assert avail, "no seeded stock account for ME"
    stock_uid, stock_hash = avail
    assert stock_uid == SEED_GUEST_ACCOUNTS["ME"][0]["uid"]
    assert stock_hash == SEED_GUEST_ACCOUNTS["ME"][0]["password_hash"]
    # استهلاك الحساب لنفس الهدف → يجب ألا يعود متاحاً لنفس الهدف
    await db3.mark_guest_used(stock_uid, TARGET, "ME")
    assert await db3.get_available_guest("ME", TARGET) is None, "used account still available for same target"
    # لكن يبقى متاحاً لهدف آخر
    assert await db3.get_available_guest("ME", "999999999") is not None, "stock should stay for other targets"
    # حفظ حساب جديد في المخزون → يصبح متاحاً
    await db3.save_guest_account("999999", "ME", "HASHXYZ")
    assert await db3.get_available_guest("ME", "999999999") is not None, "saved account not available"
    print("✔ مخزون الحسابات الجاهزة (seed/استهلاك/حفظ) OK →", stock_uid)

    print("\n✅ ALL SMOKE TESTS PASSED — likes:", info["total_likes"])


if __name__ == "__main__":
    asyncio.run(main())
