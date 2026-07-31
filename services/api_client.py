"""
عميل Garena API — قلب المشروع.
منطق إنشاء حساب ضيف (Guest Session) واستخراج التوكن وإرسال الإعجاب.

⚠️ تنبيه تقني مهم:
كل الروابط والخوارزميات هنا PLACEHOLDERS. واجهات Garena الداخلية:
  1. ليست موثقة رسمياً.
  2. تتغير باستمرار مع تحديثات اللعبة.
  3. تتطلب توقيع (signature) وخوارزمية anti-bot مطابقة تماماً
     لما يرسله عميل اللعبة الحالي (عادة MD5 لترتيب محدد من المعاملات + سر).
يجب استبدال القيم من التقاط حقيقي لحركة عميل اللعبة (MITM / Frida / تحليل APK)
قبل أن يعمل البوت فعلياً — الكود هنا جاهز للربط بمجرد توفير تلك القيم.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp

from config import settings

logger = logging.getLogger(__name__)


class APIError(Exception):
    """فشل في الاتصال أو استجابة غير متوقعة من السيرفر."""


@dataclass
class GuestSession:
    token: str
    device_id: str
    region: str
    raw: Dict[str, Any]


@dataclass
class LikeResult:
    success: bool
    limit_reached: bool = False
    message: str = ""


# ---------------------------------------------------------------------------
# محاكاة الأجهزة: قائمة User-Agent واقعية (خذ قيماً أحدث من أي عميل حالي)
# ---------------------------------------------------------------------------
IPHONE_13_UAS = [
    "FreeFire/1.0 (iPhone; iOS 15.7; Scale/3.00) Mobile/15E148",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6 Mobile/15E148 Safari/604.1",
]

ANDROID_UAS = [
    "Dalvik/2.1.0 (Linux; U; Android 11; SM-G991B Build/RP1A.200720.012)",
    "Dalvik/2.1.0 (Linux; U; Android 12; M2012K11AG Build/SQ3A.220705.004)",
    "Dalvik/2.1.0 (Linux; U; Android 10; V2029 Build/QP1A.190711.020)",
]


def _compute_sign(params: Dict[str, Any], secret: str) -> str:
    """
    ⚠️ PLACEHOLDER لتوقيع الطلبات.
    Garena يوقّع الطلبات عادةً بصيغة md5(ترتيب معيّن من المعاملات + secret).
    هذه دالة عامة — عدّل الخوارزمية لتطابق عميل اللعبة الحالي بالضبط.
    """
    if not secret:
        return ""
    keys = sorted(params.keys())
    raw = "".join(f"{k}={params[k]}" for k in keys) + secret
    return hashlib.md5(raw.encode()).hexdigest()


def _build_headers(device_id: str, region: str) -> Dict[str, str]:
    """رؤوس HTTP تحاكي جهازاً حقيقياً (iPhone 13 / Android) لتفادي الفحوص الأساسية."""
    ua = random.choice(IPHONE_13_UAS + ANDROID_UAS)
    is_ios = "iOS" in ua or "iPhone" in ua
    return {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": f"{settings.api_lang};q=0.9,en;q=0.8",
        "Content-Type": "application/json; charset=utf-8",
        "X-Request-Id": str(uuid.uuid4()),
        "X-Device-Id": device_id,
        "X-Client-Version": settings.app_id,
        "X-Platform": "ios" if is_ios else "android",
        "X-Unity-Version": "2019.4.40f1",
        "Connection": "keep-alive",
    }


def _deep_find(obj: Any, keys: List[str]) -> Optional[Any]:
    """بحث عميق في JSON متداخل عن أول مفتاح موجود (مقاوم لتغيّر شكل الاستجابة)."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                return obj[k]
        for v in obj.values():
            found = _deep_find(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find(v, keys)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------
# مدوّر البروكسيات: ثابتة من الإعدادات أو API دوّار (Webshare/ProxyLite/...)
# ---------------------------------------------------------------------------
class ProxyRotator:
    def __init__(self) -> None:
        self._static: List[str] = list(settings.proxies)
        self._dynamic: List[str] = []
        self._last_fetch: float = 0.0
        self._idx: int = 0

    async def _fetch_dynamic(self) -> List[str]:
        if not settings.proxy_api_url:
            return []
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(settings.proxy_api_url) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
            # يدعم: ["http://host:port", ...] أو {"results": [...]} أو {"data": [...]}
            if isinstance(data, dict):
                data = data.get("results", data.get("data", []))
            return [
                p if "://" in p else f"http://{p}"
                for p in data
                if isinstance(p, str) and p.strip()
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("فشل جلب قائمة البروكسيات من الـ API: %s", exc)
            return []

    async def next(self) -> Optional[str]:
        now = time.time()
        if now - self._last_fetch > settings.proxy_refresh_seconds:
            self._dynamic = await self._fetch_dynamic()
            self._last_fetch = now
        pool = self._static + self._dynamic
        if not pool:
            return None
        proxy = pool[self._idx % len(pool)]
        self._idx += 1
        return proxy


# ---------------------------------------------------------------------------
# العميل الأساسي
# ---------------------------------------------------------------------------
class FFAPIClient:
    def __init__(
        self,
        guest_register_url: Optional[str] = None,
        guest_login_url: Optional[str] = None,
        like_url: Optional[str] = None,
        sign_secret: Optional[str] = None,
    ) -> None:
        # روابط قابلة للتجاوز (مفيد للاختبار ببيئة محلية)
        self.guest_register_url = guest_register_url or settings.guest_register_url
        self.guest_login_url = guest_login_url or settings.guest_login_url
        self.like_url = like_url or settings.like_url
        self.sign_secret = sign_secret if sign_secret is not None else settings.sign_secret
        self._session: Optional[aiohttp.ClientSession] = None
        self.proxies = ProxyRotator()

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=settings.request_timeout)
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _post(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
        proxy: Optional[str],
        retries: int = 0,
    ) -> Dict[str, Any]:
        """POST مع إعادة محاولة تلقائية عند فشل الشبكة/البروكسي."""
        assert self._session is not None
        last_err: Optional[Exception] = None
        for attempt in range(1 + retries):
            try:
                async with self._session.post(
                    url,
                    data=json.dumps(payload),
                    headers=headers,
                    proxy=proxy,
                ) as resp:
                    text = await resp.text()
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return {"_raw": text, "status_code": resp.status}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_err = exc
                logger.warning(
                    "محاولة %d/%d فشلت (بروكسي %s): %s",
                    attempt + 1, retries + 1, proxy, exc,
                )
                await asyncio.sleep(1.5 * (attempt + 1))
        raise APIError(f"فشل الطلب بعد {1 + retries} محاولات: {last_err}")

    # ------------------------------------------------------------------
    # Step 3 & 4: إنشاء حساب ضيف واستخراج التوكن
    # ------------------------------------------------------------------
    async def create_guest_session(self, region: str) -> GuestSession:
        device_id = str(uuid.uuid4()).replace("-", "")[:32]
        headers = _build_headers(device_id, region)

        payload: Dict[str, Any] = {
            "device_id": device_id,
            "region": region,
            "app_id": settings.app_id,
            "lang": settings.api_lang,
            "os_version": "15.7" if headers["X-Platform"] == "ios" else "12",
            "device_model": "iPhone13,2" if headers["X-Platform"] == "ios" else "SM-G991B",
        }
        payload["sign"] = _compute_sign(payload, self.sign_secret)

        proxy = await self.proxies.next()
        data = await self._post(
            self.guest_register_url, payload, headers, proxy, settings.max_retries
        )

        token = _deep_find(
            data,
            ["access_token", "session_key", "session_token", "token", "auth_token", "session_id"],
        )
        if not token:
            raise APIError(
                f"لم يُعثر على توكن في استجابة التسجيل: {json.dumps(data)[:300]}"
            )

        # بعض الإصدارات تتطلب خطوة تأكيد/تسجيل دخول إضافية بعد التسجيل
        if self.guest_login_url and "YOUR_HOST" not in self.guest_login_url:
            confirm_payload = {
                "device_id": device_id,
                "region": region,
                "token": token,
                "app_id": settings.app_id,
            }
            confirm_payload["sign"] = _compute_sign(confirm_payload, self.sign_secret)
            confirm_data = await self._post(
                self.guest_login_url, confirm_payload, headers, proxy, 1
            )
            new_token = _deep_find(
                confirm_data,
                ["access_token", "session_key", "session_token", "token", "auth_token", "session_id"],
            )
            if new_token:
                token = new_token

        logger.info("تم إنشاء حساب ضيف جديد (منطقة %s)", region)
        return GuestSession(token=str(token), device_id=device_id, region=region, raw=data)

    # ------------------------------------------------------------------
    # Step 5: إرسال الإعجاب للـ UID المستهدف
    # ------------------------------------------------------------------
    async def send_like(self, session: GuestSession, target_uid: str) -> LikeResult:
        payload: Dict[str, Any] = {
            "target_uid": target_uid,
            "region": session.region,
            "device_id": session.device_id,
            "token": session.token,
            "app_id": settings.app_id,
        }
        payload["sign"] = _compute_sign(payload, self.sign_secret)

        headers = _build_headers(session.device_id, session.region)
        proxy = await self.proxies.next()
        data = await self._post(
            self.like_url, payload, headers, proxy, settings.max_retries
        )

        code = _deep_find(
            data, ["code", "status", "retcode", "result_code", "status_code", "error_code"]
        )
        message = str(
            _deep_find(data, ["message", "msg", "error", "desc", "reason"]) or ""
        )

        # ---------- فحص النجاح ----------
        success_codes = (0, 200, "0", "200", "OK", "SUCCESS", "ok", "success", True)
        if code in success_codes:
            return LikeResult(success=True)

        # ---------- Step 6: فحص حد الإعجابات اليومي ----------
        limit_keywords = (
            "limit", "daily", "reach", "max", "too many", "exceed", "full",
            "quota", "cooldown", "already liked", "already_liked",
        )
        if message and any(k in message.lower() for k in limit_keywords):
            logger.info("الحد اليومي مكتمل: %s", message)
            return LikeResult(success=False, limit_reached=True, message=message)

        return LikeResult(success=False, message=message or str(code))
