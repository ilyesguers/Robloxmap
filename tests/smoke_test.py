"""
Smoke Test — يتحقق من المنطق الكامل بدون الحاجة لـ Garena:
سيرفر وهمي محلي يحاكي واجهات التسجيل والإعجاب.

التشغيل:
    python tests/smoke_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys

# إعدادات تجريبية قبل استيراد config
os.environ["GUEST_REGISTER_URL"] = "http://127.0.0.1:8971/guest/register"
os.environ["GUEST_LOGIN_URL"] = "http://127.0.0.1:8971/guest/login"
os.environ["LIKE_URL"] = "http://127.0.0.1:8971/like/send"
os.environ["SIGN_SECRET"] = "testsecret"
os.environ["MIN_DELAY_SECONDS"] = "0"
os.environ["MAX_DELAY_SECONDS"] = "0"
os.environ["MAX_LIKES_PER_SESSION"] = "50"
os.environ["PROGRESS_EVERY"] = "2"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web  # noqa: E402

from services.api_client import FFAPIClient  # noqa: E402
from services.database import Database  # noqa: E402
from services.like_engine import LikeEngine, LikeJob  # noqa: E402

TARGET = "123456789"
MAX_LIKES = 7  # السيرفر الوهمي يقبل 7 إعجابات ثم يرد بالحد اليومي
state = {"n": 0}


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append(text)


async def handle_register(request: web.Request) -> web.Response:
    body = await request.json()
    assert body.get("device_id"), "device_id missing"
    assert request.headers.get("User-Agent"), "User-Agent missing"
    return web.json_response({"data": {"session_key": f"TOKEN-{body['device_id'][:8]}"}})


async def handle_login(request: web.Request) -> web.Response:
    body = await request.json()
    return web.json_response({"data": {"access_token": f"FINAL-{body['token']}"}})


async def handle_like(request: web.Request) -> web.Response:
    body = await request.json()
    assert body.get("token"), "token missing"
    assert body.get("target_uid") == TARGET, "wrong target"
    if state["n"] >= MAX_LIKES:
        return web.json_response({"code": 4001, "message": "daily like limit reached"})
    state["n"] += 1
    return web.json_response({"code": 0, "message": "OK"})


async def handle_like_slow(request: web.Request) -> web.Response:
    """استجابة بطيئة — لاختبار الإلغاء في منتصف المهمة."""
    await asyncio.sleep(0.05)
    return web.json_response({"code": 0})


async def main() -> None:
    # ---------- سيرفر وهمي 1: المنطق الأساسي ----------
    app = web.Application()
    app.router.add_post("/guest/register", handle_register)
    app.router.add_post("/guest/login", handle_login)
    app.router.add_post("/like/send", handle_like)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8971).start()

    client = FFAPIClient()
    await client.start()

    # --- الخطوات 3-4-5: ضيف + توكن + إعجاب ---
    session = await client.create_guest_session("DZ")
    assert session.token.startswith("FINAL-TOKEN-"), f"token extraction failed: {session.token}"
    print("✔ guest session + token extraction OK →", session.token[:20])

    for _ in range(3):
        r = await client.send_like(session, TARGET)
        assert r.success, f"expected success, got {r}"
    print("✔ like dispatch OK (3 likes sent)")

    # أرسل حتى يرد السيرفر بالحد اليومي (7 إعجابات ثم حد)
    limit_hit = False
    while True:
        r = await client.send_like(session, TARGET)
        if r.limit_reached:
            limit_hit = True
            break
        assert r.success, f"unexpected result: {r}"
    assert limit_hit, "daily limit was never detected"
    print("✔ daily limit detection OK →", r.message)

    # إعادة ضبط عداد السيرفر الوهمي لاختبار المحرك
    state["n"] = 0

    # ---------- سيرفر وهمي 1 + محرك: حلقة كاملة ----------
    db = Database(path="/tmp/engine_test.db")
    await db.init()
    bot = FakeBot()
    engine = LikeEngine(bot=bot, db=db, client=client)
    engine.start()

    job = LikeJob(user_id=42, target_uid=TARGET, region="DZ")
    engine.submit(job)
    await asyncio.sleep(2)

    info = await db.user_info(42)
    assert info and info["total_likes"] == MAX_LIKES, f"expected {MAX_LIKES}, got {info}"
    assert any("الحد اليومي" in m for m in bot.messages), "no daily-limit message"
    assert any("تم إرسال <b>7</b> إعجاب" in m for m in bot.messages), "no final count"
    print("✔ engine loop + limit break + notifications OK")

    # ---------- سيرفر وهمي 2 (بطيء): اختبار الإلغاء ----------
    app2 = web.Application()
    app2.router.add_post("/guest/register", handle_register)
    app2.router.add_post("/like/send", handle_like_slow)
    runner2 = web.AppRunner(app2)
    await runner2.setup()
    await web.TCPSite(runner2, "127.0.0.1", 8973).start()

    client2 = FFAPIClient(
        guest_register_url="http://127.0.0.1:8973/guest/register",
        like_url="http://127.0.0.1:8973/like/send",
    )
    await client2.start()
    engine2 = LikeEngine(bot=bot, db=db, client=client2)
    engine2.start()

    job2 = LikeJob(user_id=43, target_uid="987654321", region="EGY")
    engine2.submit(job2)
    await asyncio.sleep(0.3)
    assert engine2.cancel_for_user(43), "cancel_for_user should find active job"
    await asyncio.sleep(1)
    assert any("🛑 تم إلغاء المهمة" in m for m in bot.messages), "cancel message missing"
    print("✔ cancel flow OK")

    await engine2.stop()
    await client2.close()
    await runner2.cleanup()
    await engine.stop()
    await client.close()
    await runner.cleanup()

    print("\n✅ ALL SMOKE TESTS PASSED — likes sent:", info["total_likes"])


if __name__ == "__main__":
    asyncio.run(main())
