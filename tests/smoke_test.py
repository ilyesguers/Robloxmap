"""Smoke Test — بنية أغسطس 2026 (بوابة المستوى).

سيرفر وهمي يحاكي سلوك Garena الحقيقي بعد OB51:
  • LikeProfile يرد HTTP 200 دائماً، لكن العداد لا يزيد إلا إذا كان
    مستوى المرسل ≥ 8 — «السكوت القاتل» الذي كان يوهم البوت القديم بالنجاح.
  • GetPlayerPersonalShow بالهيكل الحقيقي: basicinfo(1){uid1,nick3,region5,level6,liked21}
  • MajorLogin: JWT(8) + serverUrl(10) + lockRegion(2) + blacklist(12) للمحظورين
  • GetPlayerPersonalShow يرفض (400) إذا اختلفت منطقة المرسل عن منطقة الهدف
    — هكذا يُختبر كشف المنطقة التلقائي.

يغطي الاختبار:
  1) وحدات العميل (تسجيل/دخول/معلومات/بحث/تحقق حسابات)
  2) إثبات السبب الجذري: لايك من مستوى 1 «ينجح» ولا يُحتسب
  3) المحرك: إعجاب من مخزون موثق + محتسب فعلياً + كشف منطقة تلقائي
  4) مخزون فارغ → رسالة صادقة بدل أوهام
  5) /donate: قبول مستوى 8+ ورفض محظور/منخفض المستوى
  6) الإلغاء أثناء المهمة + إقصاء الحسابات المحظورة تلقائياً

التشغيل:
    python tests/smoke_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

# إعدادات سريعة للاختبار (تُضبط قبل استيراد config)
os.environ["MIN_DELAY_SECONDS"] = "0"
os.environ["MAX_DELAY_SECONDS"] = "0"
os.environ["MAX_LIKES_PER_SESSION"] = "50"
os.environ["PROGRESS_EVERY"] = "2"
os.environ["VERIFY_EVERY"] = "2"
os.environ["STALL_WINDOW"] = "30"
os.environ["MAX_RETRIES"] = "1"
os.environ["READER_SESSION_TTL"] = "60"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web  # noqa: E402

import services.garena as garena_mod  # noqa: E402
from config import settings  # noqa: E402
from services.garena import (  # noqa: E402
    AccountBannedError,
    GarenaError,
    _aes_decrypt,
    _field_bytes,
    _field_string,
    _field_varint,
    _parse_protobuf,
    password_to_hash,
)
from services.like_engine import LikeEngine, LikeJob  # noqa: E402
from services.pool import parse_donation_lines, validate_and_store  # noqa: E402


def _patch_urls(base: str) -> None:
    garena_mod.URL_GUEST_REGISTER = f"{base}/oauth/guest/register"
    garena_mod.GUEST_REGISTER_FALLBACKS.clear()
    garena_mod.URL_TOKEN_GRANT = f"{base}/oauth/guest/token/grant"
    garena_mod.TOKEN_GRANT_FALLBACKS.clear()
    garena_mod.URL_MAJOR_REGISTER = f"{base}/MajorRegister"
    garena_mod.URL_MAJOR_LOGIN = f"{base}/MajorLogin"
    garena_mod.MAJOR_LOGIN_FALLBACKS.clear()
    garena_mod.MAJOR_LOGIN_ME_FIRST = f"{base}/MajorLogin"
    garena_mod.DEFAULT_SERVER = f"{base}/game"
    garena_mod.SERVER_BASE.clear()


FAKE = "http://127.0.0.1:8981"
FAKE_SERVER = f"{FAKE}/game"

TARGET = "123456789"          # هدف على سيرفر ME
TARGET_IND = "555123456"      # هدف على سيرفر IND

# ---------------- حالة السيرفر الوهمي ----------------
# حسابات «مساهَم بها»: uid → بياناتها الحقيقية كما تراها Garena
ACCOUNTS = {
    "211111111": {"pw": password_to_hash("PassA1"), "level": 10, "region": "ME", "nick": "DonorA", "banned": False},
    "222222222": {"pw": password_to_hash("PassB2"), "level": 25, "region": "ME", "nick": "DonorB", "banned": False},
    "233333333": {"pw": password_to_hash("PassC3"), "level": 30, "region": "ME", "nick": "DonorC", "banned": False},
    "300000001": {"pw": password_to_hash("PassD4"), "level": 25, "region": "IND", "nick": "DonorIND", "banned": False},
    "244444444": {"pw": password_to_hash("PassE5"), "level": 3, "region": "ME", "nick": "LowLvl", "banned": False},
    "255555555": {"pw": password_to_hash("PassF6"), "level": 40, "region": "ME", "nick": "Banned1", "banned": True},
}
# حسابات إضافية لاختبار الإلغاء أثناء مهمة طويلة
for _i in range(20):
    _uid = f"26{_i:07d}"
    ACCOUNTS[_uid] = {
        "pw": password_to_hash(f"Bulk{_i}"), "level": 20,
        "region": "ME", "nick": f"Bulk{_i}", "banned": False,
    }
TARGETS = {
    TARGET: {"region": "ME", "nick": "TestPlayer", "level": 52, "likes": 0},
    TARGET_IND: {"region": "IND", "nick": "PlayerIND", "level": 33, "likes": 0},
}
GUESTS: dict = {}             # uid → {"pw":..., "region": "ME", "nick": ...}
JWT_MAP: dict = {}            # jwt → uid
state = {"guests": 0, "likes_sent": 0}


def _account_region(uid: str) -> str:
    if uid in ACCOUNTS:
        return ACCOUNTS[uid]["region"]
    if uid in GUESTS:
        return GUESTS[uid]["region"]
    return "ME"


def _basicinfo_blob(uid: str, nick: str, region: str, level: int, liked: int) -> bytes:
    return (
        _field_varint(1, int(uid))
        + _field_string(3, nick)
        + _field_string(5, region)
        + _field_varint(6, level)
        + _field_varint(21, liked)
    )


# ---------------- معالجات السيرفر الوهمي ----------------
async def handle_register(request: web.Request) -> web.Response:
    form = await request.post()
    assert form.get("password"), "missing password"
    assert "Signature" in (request.headers.get("Authorization") or ""), "missing signature"
    state["guests"] += 1
    uid = f"9{state['guests']:08d}"
    GUESTS[uid] = {"pw": str(form["password"]).upper(), "region": "ME", "nick": f"Reader{state['guests']}"}
    return web.json_response({"uid": uid})


async def handle_token(request: web.Request) -> web.Response:
    form = await request.post()
    uid = str(form.get("uid"))
    pw = str(form.get("password", "")).upper()
    assert form.get("client_id") == "100067"
    known = (uid in ACCOUNTS and ACCOUNTS[uid]["pw"] == pw) or (uid in GUESTS and GUESTS[uid]["pw"] == pw)
    if not known:
        return web.json_response({"error": "auth_error"})
    return web.json_response({"access_token": f"AT-{uid}", "open_id": f"OI-{uid}"})


async def handle_major_register(request: web.Request) -> web.Response:
    body = await request.read()
    fields = _parse_protobuf(_aes_decrypt(body))
    assert 15 in fields and 17 in fields
    return web.Response(status=200, body=b"")


async def handle_major_login(request: web.Request) -> web.Response:
    body = await request.read()
    fields = _parse_protobuf(_aes_decrypt(body))
    open_id = (fields.get(22) or [b""])[0]
    uid = open_id.decode().replace("OI-", "") if isinstance(open_id, bytes) else ""
    # حساب محظور → blacklist=12 {banReason=1, expireDuration=2}
    if uid in ACCOUNTS and ACCOUNTS[uid]["banned"]:
        ban = _field_varint(1, 1014) + _field_varint(2, 3600)
        return web.Response(status=200, body=_field_bytes(12, ban))
    if uid not in ACCOUNTS and uid not in GUESTS:
        return web.Response(status=400, body=b"unknown")
    jwt = f"JWT-{uid}-{state['guests']}"
    JWT_MAP[jwt] = uid
    resp = (
        _field_varint(1, int(uid))
        + _field_string(2, _account_region(uid))           # lockRegion
        + _field_string(8, jwt)                            # token
        + _field_string(10, FAKE_SERVER)                   # serverUrl
    )
    return web.Response(status=200, body=resp)


def _sender_uid(request: web.Request) -> str:
    auth = request.headers.get("Authorization") or ""
    jwt = auth.replace("Bearer", "").strip()
    return JWT_MAP.get(jwt, "")


async def handle_like(request: web.Request) -> web.Response:
    """★ محاكاة بوابة المستوى: 200 دائماً، والعداد يزيد فقط لمستوى ≥ 8."""
    if state.get("slow"):
        await asyncio.sleep(0.05)
    body = await request.read()
    fields = _parse_protobuf(_aes_decrypt(body))
    target = str(fields.get(1, [0])[0])
    sender = _sender_uid(request)
    sender_level = ACCOUNTS.get(sender, {}).get("level", 1)  # ضيوف = مستوى 1
    state["likes_sent"] += 1
    if target in TARGETS and sender_level >= settings.min_donor_level:
        TARGETS[target]["likes"] += 1
    return web.Response(status=200, body=b"")  # نجاح صامت مهما كان


async def handle_personal_show(request: web.Request) -> web.Response:
    body = await request.read()
    fields = _parse_protobuf(_aes_decrypt(body))
    uid = str(fields.get(1, [0])[0])
    sender_region = _account_region(_sender_uid(request))

    if uid in TARGETS:
        t = TARGETS[uid]
        if t["region"] != sender_region:
            return web.Response(status=400, body=b"wrong region")  # كشف المنطقة يعتمد على هذا
        blob = _basicinfo_blob(uid, t["nick"], t["region"], t["level"], t["likes"])
        return web.Response(status=200, body=_field_bytes(1, blob))

    src = ACCOUNTS.get(uid) or GUESTS.get(uid)
    if not src:
        return web.Response(status=400, body=b"not found")
    level = src.get("level", 1)
    blob = _basicinfo_blob(uid, src.get("nick", ""), _account_region(uid), level, 0)
    return web.Response(status=200, body=_field_bytes(1, blob))


async def handle_search(request: web.Request) -> web.Response:
    body = await request.read()
    fields = _parse_protobuf(_aes_decrypt(body))
    keyword = (fields.get(1) or [b""])[0]
    keyword = keyword.decode(errors="ignore") if isinstance(keyword, bytes) else ""
    out = b""
    t = TARGETS[TARGET]
    if t["nick"].lower().startswith(keyword.lower()[:4]) or keyword:
        out += _field_bytes(1, _basicinfo_blob(TARGET, t["nick"], t["region"], t["level"], t["likes"]))
        out += _field_bytes(1, _basicinfo_blob("987654321", "TestHero", "ME", 41, 120))
    return web.Response(status=200, body=out)


# ---------------- قاعدة بيانات وهمية (في الذاكرة) ----------------
class FakeDB:
    """تطبيق مصغّر لواجهة Database التي يستخدمها المحرك وpool."""

    def __init__(self) -> None:
        self.accounts: dict = {}
        self.used: dict = {}   # (account_uid, target_uid) → ts
        self.likes: dict = {}  # user_id → [sent, counted]
        self.contribs: list = []

    async def get_any_account_for_read(self, region: str):
        for uid, a in self.accounts.items():
            if a.get("status") == "ok" and a.get("region") == region:
                return uid, a["password_hash"]
        return None

    async def count_available(self, region: str, target_uid: str) -> int:
        n = 0
        cooldown_since = time.time() - settings.like_cooldown_seconds
        for uid, a in self.accounts.items():
            if a.get("status") != "ok" or a.get("region") != region:
                continue
            if a.get("level", 0) < settings.min_donor_level:
                continue
            if self.used.get((uid, target_uid), 0) > cooldown_since:
                continue
            n += 1
        return n

    async def get_available_guest(self, region: str, target_uid: str):
        cooldown_since = time.time() - settings.like_cooldown_seconds
        best = None
        for uid, a in self.accounts.items():
            if a.get("status") != "ok" or a.get("region") != region:
                continue
            if a.get("level", 0) < settings.min_donor_level:
                continue
            if self.used.get((uid, target_uid), 0) > cooldown_since:
                continue
            if best is None or a["level"] > self.accounts[best]["level"]:
                best = uid
        if best is None:
            return None
        return best, self.accounts[best]["password_hash"]

    async def mark_guest_used(self, account_uid: str, target_uid: str, region: str) -> None:
        self.used[(account_uid, target_uid)] = time.time()

    async def set_account_status(self, account_uid: str, status: str, note: str = "") -> None:
        if account_uid in self.accounts:
            self.accounts[account_uid]["status"] = status
            self.accounts[account_uid]["note"] = note

    async def update_account_token(self, account_uid: str, access_token: str, open_id: str) -> None:
        if account_uid in self.accounts:
            self.accounts[account_uid]["access_token"] = access_token

    async def upsert_validated_account(self, account_uid, region, password_hash, password,
                                       nickname, level, status, contributed_by=None,
                                       access_token="", open_id="", note="") -> None:
        self.accounts[account_uid] = {
            "region": region, "password_hash": password_hash, "nickname": nickname,
            "level": level, "status": status, "contributed_by": contributed_by,
            "access_token": access_token, "note": note,
        }

    async def add_contribution(self, user_id, account_uid, level, region, accepted, note="") -> None:
        self.contribs.append((account_uid, accepted))

    async def add_likes(self, user_id: int, count: int, counted: int = 0) -> None:
        cur = self.likes.setdefault(user_id, [0, 0])
        cur[0] += count
        cur[1] += counted

    async def user_info(self, user_id: int):
        sent, counted = self.likes.get(user_id, [0, 0])
        return {"total_requests": 1, "total_likes": sent, "counted_likes": counted,
                "contributions": sum(1 for _, ok in self.contribs if ok)}


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append(text)


# ---------------- الاختبارات ----------------
async def main() -> None:
    _patch_urls(FAKE)

    app = web.Application()
    app.router.add_post("/oauth/guest/register", handle_register)
    app.router.add_post("/oauth/guest/token/grant", handle_token)
    app.router.add_post("/MajorRegister", handle_major_register)
    app.router.add_post("/MajorLogin", handle_major_login)
    app.router.add_post("/game/LikeProfile", handle_like)
    app.router.add_post("/game/GetPlayerPersonalShow", handle_personal_show)
    app.router.add_post("/game/FuzzySearchAccountByName", handle_search)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8981).start()

    # ============ 1) وحدات العميل ============
    client = garena_mod.GarenaClient()
    await client.start()

    guest = await client.register_guest("ME")
    assert guest.uid and guest.access_token and guest.open_id
    print("✔ register_guest (جلسة قراءة) OK → UID", guest.uid)

    session = await client.major_login(guest.access_token, guest.open_id, "ME")
    assert session.jwt and session.server_url == FAKE_SERVER and session.lock_region == "ME"
    print("✔ major_login (JWT + serverUrl + lockRegion) OK")

    info = await client.get_player_info(session, TARGET, "ME")
    assert info and info.nickname == "TestPlayer" and info.likes == 0
    assert info.level == 52 and info.region == "ME", f"structured parse broken: {info}"
    print("✔ get_player_info بالهيكل الحقيقي (nickname/level/region/liked) OK")

    found = await client.search_accounts(session, "Test", "ME")
    assert len(found) == 2 and found[0].uid == TARGET and found[0].nickname == "TestPlayer"
    assert found[0].level == 52
    print("✔ search_accounts (FuzzySearchAccountByName) OK →", [f"{p.nickname}({p.uid})" for p in found])

    # ---- تحقق حساب مساهَم به: مستوى+منطقة+اسم حقيقيون ----
    v = await client.validate_account("211111111", ACCOUNTS["211111111"]["pw"], "ME")
    assert v.eligible and v.level == 10 and v.nickname == "DonorA"
    print("✔ validate_account (مستوى حقيقي ≥ 8) OK →", v.level, v.nickname)

    v_low = await client.validate_account("244444444", ACCOUNTS["244444444"]["pw"], "ME")
    assert v_low.level == 3 and not v_low.eligible
    print("✔ validate_account يرصد المستوى المنخفض (3 < 8) OK")

    try:
        await client.validate_account("255555555", ACCOUNTS["255555555"]["pw"], "ME")
        raise AssertionError("expected AccountBannedError")
    except AccountBannedError as exc:
        assert "3600" in str(exc)
    print("✔ كشف الحظر من MajorLogin (الحقل 12) OK")

    try:
        await client.validate_account("211111111", password_to_hash("WrongPass"), "ME")
        raise AssertionError("expected auth failure")
    except GarenaError as exc:
        assert "auth_error" in str(exc)
    print("✔ رفض كلمة سر خاطئة (auth_error) OK")

    # ============ 2) إثبات السبب الجذري (بوابة المستوى) ============
    before = TARGETS[TARGET]["likes"]
    r = await client.send_like(session, TARGET, "ME")  # ضيف مستوى 1
    assert r.success, "الواجهة تقبل الإرسال (HTTP 200) كما في الواقع"
    assert TARGETS[TARGET]["likes"] == before, "★ يجب ألا يُحتسب — هذا هو المرض القديم"
    print("✔ إثبات OB51: لايك مستوى 1 «ينجح» عند الواجهة لكنه لا يُحتسب —")
    print("  هذا بالضبط ما كان يراه المستخدم: «أُرسل 100» ولا شيء في اللعبة.")

    # ============ 3) المحرك: مخزون موثق + احتساب حقيقي + كشف منطقة ============
    db = FakeDB()
    for uid in ("222222222", "233333333"):  # مستوى 25 و 30
        a = ACCOUNTS[uid]
        await db.upsert_validated_account(uid, a["region"], a["pw"], "", a["nick"], a["level"], "ok", 777)
    bot = FakeBot()
    engine = LikeEngine(bot=bot, db=db, client=client)
    engine.start()

    bot.messages.clear()
    TARGETS[TARGET]["likes"] = 100
    engine.submit(LikeJob(user_id=42, target_uid=TARGET))  # بدون منطقة — كشف تلقائي
    for _ in range(60):
        if any("انتهت الجلسة" in m for m in bot.messages):
            break
        await asyncio.sleep(0.25)

    assert TARGETS[TARGET]["likes"] == 102, f"expected 102 counted, got {TARGETS[TARGET]['likes']}"
    info42 = await db.user_info(42)
    assert info42["total_likes"] == 2 and info42["counted_likes"] == 2, info42
    assert any("السيرفر: <b>ME</b>" in m for m in bot.messages), "no auto-detect message"
    assert any("+2 محتسب" in m for m in bot.messages), f"no counted report: {bot.messages[-1]}"
    assert any("الاسم: <b>TestPlayer</b>" in m for m in bot.messages)
    print("✔ المحرك: كشف منطقة تلقائي + إعجابان من المخزون + محتسبة فعلياً (+2) OK")

    # كشف منطقة هدف على IND (القارئ من مخزون IND وليس ضيفاً)
    a = ACCOUNTS["300000001"]
    await db.upsert_validated_account("300000001", a["region"], a["pw"], "", a["nick"], a["level"], "ok", 777)
    bot.messages.clear()
    engine.submit(LikeJob(user_id=43, target_uid=TARGET_IND))
    for _ in range(60):
        if any("انتهت الجلسة" in m for m in bot.messages):
            break
        await asyncio.sleep(0.25)
    assert TARGETS[TARGET_IND]["likes"] == 1, f"IND target: {TARGETS[TARGET_IND]['likes']}"
    assert any("السيرفر: <b>IND</b>" in m for m in bot.messages), "IND auto-detect missing"
    print("✔ كشف منطقة IND تلقائياً (ME أولاً ثم IND) + إعجاب محتسب OK")

    # ============ 4) مخزون فارغ → رسالة صادقة ============
    bot.messages.clear()
    engine.submit(LikeJob(user_id=44, target_uid=TARGET))  # استُهلكت حسابات ME اليوم
    for _ in range(60):
        if any("المخزون فارغ" in m for m in bot.messages):
            break
        await asyncio.sleep(0.25)
    assert any("المخزون فارغ" in m for m in bot.messages), "no empty-stock message"
    assert any("/donate" in m for m in bot.messages)
    print("✔ المخزون الفارغ → رسالة صادقة تشرح بوابة المستوى و/donate OK")

    # ============ 5) /donate: قبول/رفض حقيقي ============
    res_ok = await validate_and_store(db, client, "211111111", "PassA1", 777)
    assert res_ok.ok and res_ok.level == 10
    res_low = await validate_and_store(db, client, "244444444", "PassE5", 777)
    assert not res_low.ok and "مستواه" in res_low.reason and res_low.level == 3
    res_ban = await validate_and_store(db, client, "255555555", "PassF6", 777)
    assert not res_ban.ok and "محظور" in res_ban.reason
    assert db.accounts["244444444"]["status"] == "low_level"
    assert db.accounts["255555555"]["status"] == "banned"
    assert db.accounts["211111111"]["status"] == "ok"
    pairs, rejected = parse_donation_lines("123456:pw1\nBADLINE\n234567890 : pw2", 5)
    assert len(pairs) == 2 and pairs[0] == ("123456", "pw1") and len(rejected) == 1
    print("✔ donate: قبول 10+ ورفض منخفض/محظور + تحليل الأسطر OK")

    # ============ 6) الإلغاء أثناء المهمة ============
    state["slow"] = True
    # أعد الحسابات للإتاحة (تصفية سجل الاستخدام) + أضف الـ 20 حساباً الإضافية
    db.used.clear()
    for uid in (u for u in ACCOUNTS if u.startswith("26")):
        a = ACCOUNTS[uid]
        await db.upsert_validated_account(uid, a["region"], a["pw"], "", a["nick"], a["level"], "ok", 777)
    bot.messages.clear()
    engine.submit(LikeJob(user_id=45, target_uid=TARGET))
    await asyncio.sleep(0.3)
    assert engine.cancel_for_user(45), "cancel should find active job"
    for _ in range(20):
        if any("🛑" in m for m in bot.messages):
            break
        await asyncio.sleep(0.2)
    assert any("🛑" in m for m in bot.messages), f"cancel message missing: {bot.messages}"
    print("✔ الإلغاء في منتصف المهمة OK")

    await engine.stop()
    await client.close()
    await runner.cleanup()

    print("\n✅ ALL SMOKE TESTS PASSED (بنية أغسطس 2026 — بوابة المستوى)")


if __name__ == "__main__":
    asyncio.run(main())
