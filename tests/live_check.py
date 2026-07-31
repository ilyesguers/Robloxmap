"""
Live Check — فحص حي حقيقي على سيرفرات Garena.

ينشئ حساب ضيف واحد حقيقي ويسجل الدخول به (بدون إرسال أي إعجاب لأي لاعب).
الهدف: التأكد أن الروابط والمفاتيح الحالية تعمل من بيئتك.

التشغيل:
    python tests/live_check.py
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from services.garena import GarenaClient  # noqa: E402


async def main() -> None:
    client = GarenaClient()
    await client.start()
    try:
        print("1) إنشاء حساب ضيف حقيقي...")
        guest = await client.register_guest("ME", nickname="BotCheck1")
        print(f"   ✔ UID: {guest.uid}")
        print(f"   ✔ Nickname: {guest.nickname} | Region: {guest.region}")
        print(f"   ✔ Access Token: {guest.access_token[:16]}...")
        print(f"   ✔ OpenID: {guest.open_id[:16]}...")

        print("2) تسجيل الدخول (MajorLogin → JWT)...")
        session = await client.major_login(guest.access_token, guest.open_id, "ME")
        print(f"   ✔ JWT: {session.jwt[:20]}...")
        print(f"   ✔ Server URL: {session.server_url}")
        print(f"   ✔ Account ID: {session.account_id}")

        print("\n✅ LIVE CHECK PASSED — الاتصال بـ Garena يعمل من هذه البيئة.")
        print("ملاحظة: لم يتم إرسال أي إعجاب (فحص الاتصال فقط).")
    except Exception as exc:
        print(f"\n❌ LIVE CHECK FAILED: {exc}")
        print("الأسباب المحتملة:")
        print("  • IP السيرفر الحالي محظور/مشبوه (يحدث مع IP مراكز البيانات)")
        print("  • Garena غيّرت الروابط أو المفاتيح في تحديث أحدث")
        sys.exit(1)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
