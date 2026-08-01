"""Live Check — فحص حي حقيقي على سيرفرات Garena (شغّله من Railway Shell).

ينشئ حساب ضيف «قراءة» ويسجل دخوله، ثم — إن مرّرت UID — يقرأ معلوماته
الحقيقية (الاسم/المستوى/اللايكات/المنطقة). لا يُرسل أي إعجاب.

التشغيل:
    python tests/live_check.py            # فحص اتصال فقط
    python tests/live_check.py <UID>      # + قراءة معلومات حساب
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from services.garena import GarenaClient  # noqa: E402


async def main() -> None:
    target = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    client = GarenaClient()
    await client.start()
    try:
        print("1) إنشاء حساب ضيف قراءة...")
        guest = await client.register_guest("ME", nickname="BotCheck1")
        print(f"   ✔ UID: {guest.uid} | OpenID: {guest.open_id[:14]}...")

        print("2) تسجيل الدخول (MajorLogin → JWT)...")
        session = await client.major_login(guest.access_token, guest.open_id, "ME")
        print(f"   ✔ JWT: {session.jwt[:20]}...")
        print(f"   ✔ ServerURL: {session.server_url} | LockRegion: {session.lock_region}")

        if target:
            print(f"3) قراءة معلومات الحساب {target} ...")
            info = await client.get_player_info(session, target, "ME")
            if info and (info.nickname or info.likes is not None):
                print(f"   ✔ الاسم: {info.nickname}")
                print(f"   ✔ المستوى: {info.level} | اللايكات: {info.likes} | المنطقة: {info.region}")
            else:
                print("   ⚠️ لم يُعثر عليه في سيرفر ME — جرّب منطقة أخرى في البوت (كشف تلقائي).")

        print("\n✅ LIVE CHECK PASSED — الاتصال بـ Garena يعمل من هذه البيئة.")
        print("تذكير: الإعجابات تُحتسب فقط من حسابات مستوى ≥ 8 (بوابة OB51+).")
    except Exception as exc:
        print(f"\n❌ LIVE CHECK FAILED: {exc}")
        print("الأسباب المحتملة:")
        print("  • IP السيرفر محظور/مشبوه (شائع مع IP مراكز البيانات → فعّل PROXIES)")
        print("  • Garena غيّرت الروابط/الثوابت (حدّث أعلى services/garena.py)")
        sys.exit(1)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
