"""
عميل Garena الحقيقي — الطريقة الشغالة الحالية (OB53) + تحسينات:
- Hybrid guest pool: كاش جلسات للقراءة + تجمع مُسخَّن مسبقاً للإعجابات
- Register fallbacks: جرّب مناطق بديلة + أسماء مستعارة بديلة عند الفشل
- Diagnostics: عدادات تفصيلية لكل خطوة + سجل أخطاء أخير

التدفق الأصلي (مطابق تماماً لعميل المكتبة الشغالة @spinzaf/freefire-api — أبريل 2026):
  1) تسجيل حساب ضيف جديد
  2) منح توكن
  3) إنشاء الحساب داخل اللعبة
  4) تسجيل الدخول → JWT
  5) إرسال الإعجاب
  6) التحقق من عدد الإعجابات
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import hmac
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp
from Crypto.Cipher import AES

from config import settings

logger = logging.getLogger(__name__)

# ==========================================================================
# الثوابت الحقيقية — OB53
# ==========================================================================
AES_KEY = b"Yg&tc%DEuh6%Zc^8"
AES_IV = b"6oyZDr22E3ychjM%"

CLIENT_ID = "100067"
CLIENT_SECRET = "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3"

URL_GUEST_REGISTER = "https://ffmconnect.live.gop.garenanow.com/oauth/guest/register"
URL_TOKEN_GRANT = "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant"
URL_MAJOR_REGISTER = "https://loginbp.ggblueshark.com/MajorRegister"
URL_MAJOR_LOGIN = "https://loginbp.ggblueshark.com/MajorLogin"

UA_GARENA = "GarenaMSDK/4.0.19P9(A063 ;Android 13;en;IN;)"
UA_DALVIK = "Dalvik/2.1.0 (Linux; U; Android 13; A063 Build/TKQ1.221220.001)"
RELEASE_VERSION = "OB53"
X_UNITY_VERSION = "2018.4.11f1"

SERVER_BASE: Dict[str, str] = {
    "IND": "https://client.ind.freefiremobile.com",
    "BR": "https://client.us.freefiremobile.com",
    "US": "https://client.us.freefiremobile.com",
    "SAC": "https://client.us.freefiremobile.com",
    "NA": "https://client.us.freefiremobile.com",
}
DEFAULT_SERVER = "https://clientbp.ggblueshark.com"

GUEST_REGIONS: List[str] = ["IND", "SG", "BR", "US", "RU", "TH", "VN", "TW", "ME", "CIS", "BD"]
# ترتيب مفضل للـ fallback — يبدأ من ME ثم SG ثم البقية (الأكثر استقراراً أولاً)
GUEST_FALLBACK_ORDER: List[str] = ["ME", "SG", "IND", "BR", "US", "RU", "TH", "VN", "TW", "BD", "CIS"]

LIMIT_KEYWORDS = (
    "limit", "daily", "reach", "max", "too many", "exceed", "full",
    "quota", "cooldown", "already", "banned", "ban", "blocked", "denied",
    "forbidden", "frequently", "restrict",
)

# ==========================================================================
# أخطاء
# ==========================================================================
class GarenaError(Exception):
    """خطأ عام من سيرفر Garena."""


class DailyLimitError(GarenaError):
    """تم الوصول للحد اليومي للإعجابات."""


# ==========================================================================
# كيانات
# ==========================================================================
@dataclass
class GuestAccount:
    uid: str
    password: str
    password_hash: str
    region: str
    nickname: str
    access_token: str
    open_id: str
    created_at: float = field(default_factory=time.time)


@dataclass
class LoginSession:
    jwt: str
    server_url: str
    account_id: Optional[int] = None
    lock_region: str = ""
    created_at: float = field(default_factory=time.time)
    region: str = ""  # المنطقة الأصلية التي أنشئ منها


@dataclass
class LikeResult:
    success: bool
    limit_reached: bool = False
    message: str = ""


@dataclass
class PlayerInfo:
    uid: Optional[str] = None
    nickname: Optional[str] = None
    likes: Optional[int] = None


@dataclass
class Diagnostics:
    """حاوية عدادات تشخيصية — تُحدث في كل عملية."""
    started_at: float = field(default_factory=time.time)
    register_attempts: int = 0
    register_success: int = 0
    register_failed: int = 0
    register_per_region_attempt: Dict[str, int] = field(default_factory=lambda: collections.Counter())
    register_per_region_success: Dict[str, int] = field(default_factory=lambda: collections.Counter())
    register_per_region_failed: Dict[str, int] = field(default_factory=lambda: collections.Counter())
    token_attempts: int = 0
    token_success: int = 0
    token_failed: int = 0
    major_register_attempts: int = 0
    major_register_success: int = 0
    major_register_failed: int = 0
    login_attempts: int = 0
    login_success: int = 0
    login_failed: int = 0
    like_attempts: int = 0
    like_success: int = 0
    like_failed: int = 0
    like_limit_hits: int = 0
    read_attempts: int = 0
    read_success: int = 0
    read_failed: int = 0
    pool_hits: int = 0
    pool_miss: int = 0
    pool_size_current: int = 0
    last_errors: Deque[str] = field(default_factory=lambda: collections.deque(maxlen=30))

    def to_dict(self) -> Dict[str, Any]:
        uptime = int(time.time() - self.started_at)
        return {
            "uptime_sec": uptime,
            "register": {
                "attempts": self.register_attempts,
                "success": self.register_success,
                "failed": self.register_failed,
                "per_region_attempt": dict(self.register_per_region_attempt),
                "per_region_success": dict(self.register_per_region_success),
                "per_region_failed": dict(self.register_per_region_failed),
            },
            "token_grant": {"attempts": self.token_attempts, "success": self.token_success, "failed": self.token_failed},
            "major_register": {"attempts": self.major_register_attempts, "success": self.major_register_success, "failed": self.major_register_failed},
            "login": {"attempts": self.login_attempts, "success": self.login_success, "failed": self.login_failed},
            "like": {"attempts": self.like_attempts, "success": self.like_success, "failed": self.like_failed, "limit_hits": self.like_limit_hits},
            "read": {"attempts": self.read_attempts, "success": self.read_success, "failed": self.read_failed},
            "pool": {"hits": self.pool_hits, "miss": self.pool_miss, "current_size": self.pool_size_current},
            "last_errors": list(self.last_errors),
        }

    def record_error(self, msg: str) -> None:
        self.last_errors.append(f"{time.strftime('%H:%M:%S')} {msg[:300]}")


# ==========================================================================
# أدوات Protobuf
# ==========================================================================
def _varint_encode(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _read_varint(data: bytes, i: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, i
        shift += 7


def _field_varint(num: int, value: int) -> bytes:
    return _varint_encode((num << 3) | 0) + _varint_encode(value)


def _field_bytes(num: int, value: bytes) -> bytes:
    return _varint_encode((num << 3) | 2) + _varint_encode(len(value)) + value


def _field_string(num: int, value: str) -> bytes:
    return _field_bytes(num, value.encode("utf-8"))


def _parse_protobuf(data: bytes) -> Dict[int, List[Any]]:
    fields: Dict[int, List[Any]] = {}
    i = 0
    while i < len(data):
        tag, i = _read_varint(data, i)
        num, wire = tag >> 3, tag & 0x07
        if num == 0:
            break
        if wire == 0:
            val, i = _read_varint(data, i)
            fields.setdefault(num, []).append(val)
        elif wire == 2:
            ln, i = _read_varint(data, i)
            fields.setdefault(num, []).append(data[i : i + ln])
            i += ln
        elif wire == 5:
            fields.setdefault(num, []).append(data[i : i + 4])
            i += 4
        elif wire == 1:
            fields.setdefault(num, []).append(data[i : i + 8])
            i += 8
        else:
            break
    return fields


# ==========================================================================
# التشفير
# ==========================================================================
def _aes_encrypt(data: bytes) -> bytes:
    pad_len = 16 - (len(data) % 16)
    data = data + bytes([pad_len]) * pad_len
    return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(data)


_XOR_KEY = bytes(
    [0, 0, 0, 2, 0, 1, 7, 0, 0, 0, 0, 0, 2, 0, 1, 7,
     0, 0, 0, 0, 0, 2, 0, 1, 7, 0, 0, 0, 0, 0, 2, 0]
)


def _xor_encrypt_openid(open_id: str) -> bytes:
    raw = open_id.encode("utf-8")
    return bytes(b ^ _XOR_KEY[i % len(_XOR_KEY)] ^ 48 for i, b in enumerate(raw))


def _deep_get(obj: Any, *paths: str) -> Any:
    for path in paths:
        current = obj
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current is not None:
            return current
    return None


# ==========================================================================
# العميل - مع Pool + Fallback + Diagnostics
# ==========================================================================
class GarenaClient:
    """عميل واجهات Garena — مع تحسينات هجينة."""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._proxy_pool: List[str] = []
        self._proxy_idx: int = 0
        self._proxy_last_fetch: float = 0.0

        # Diagnostics
        self.diagnostics = Diagnostics()

        # Hybrid pools
        self._read_sessions: Dict[str, Tuple[LoginSession, float]] = {}  # region -> (session, expiry)
        self._like_pool: Dict[str, Deque[Tuple[GuestAccount, LoginSession]]] = {}  # region -> deque
        self._pool_lock = asyncio.Lock()
        self._read_ttl = 600  # 10 دقائق للقراءة
        self._like_pool_max = 5

    # ------------------------------------------------------------------
    # إدارة دورة الحياة
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=settings.request_timeout)
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _next_proxy(self) -> Optional[str]:
        now = time.time()
        if now - self._proxy_last_fetch > settings.proxy_refresh_seconds:
            await self._refresh_proxies()
            self._proxy_last_fetch = now
        pool = list(settings.proxies) + self._proxy_pool
        if not pool:
            return None
        proxy = pool[self._proxy_idx % len(pool)]
        self._proxy_idx += 1
        return proxy

    async def _refresh_proxies(self) -> None:
        if not settings.proxy_api_url or self._session is None:
            return
        try:
            async with self._session.get(settings.proxy_api_url) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
            if isinstance(data, dict):
                data = data.get("results", data.get("data", []))
            self._proxy_pool = [
                p if "://" in p else f"http://{p}"
                for p in data
                if isinstance(p, str) and p.strip()
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("فشل جلب البروكسيات الدوّارة: %s", exc)

    async def _post_form(
        self, url: str, params: Dict[str, str], headers: Dict[str, str]
    ) -> Dict[str, Any]:
        assert self._session is not None
        proxy = await self._next_proxy()
        async with self._session.post(
            url, data=params, headers=headers, proxy=proxy
        ) as resp:
            text = await resp.text()
            try:
                return {"status": resp.status, "json": json_loads(text)}
            except Exception:
                return {"status": resp.status, "json": {}, "raw": text}

    async def _post_raw(
        self, url: str, body: bytes, headers: Dict[str, str]
    ) -> Tuple[int, bytes]:
        assert self._session is not None
        proxy = await self._next_proxy()
        async with self._session.post(
            url, data=body, headers=headers, proxy=proxy
        ) as resp:
            return resp.status, await resp.read()

    # ------------------------------------------------------------------
    # Diagnostics helpers
    # ------------------------------------------------------------------
    def _diag_error(self, msg: str) -> None:
        self.diagnostics.record_error(msg)
        logger.debug("DIAG error: %s", msg)

    def get_diagnostics(self) -> Dict[str, Any]:
        # تحديث حجم التجمع الحالي
        total = sum(len(q) for q in self._like_pool.values())
        self.diagnostics.pool_size_current = total
        return self.diagnostics.to_dict()

    # ------------------------------------------------------------------
    # تسجيل ضيف - محاولة واحدة لمنطقة معينة
    # ------------------------------------------------------------------
    async def _try_register_single_region(
        self, region: str, nickname: Optional[str]
    ) -> GuestAccount:
        password = str(random.randint(10**9, 10**10 - 1))
        password_hash = hashlib.sha256(password.encode()).hexdigest().upper()
        nick = nickname or f"Liker{random.randint(1000, 99999)}"

        self.diagnostics.register_attempts += 1
        self.diagnostics.register_per_region_attempt[region] += 1

        params = {
            "password": password_hash,
            "client_type": "2",
            "source": "2",
            "app_id": CLIENT_ID,
        }
        signature = hmac.new(
            CLIENT_SECRET.encode(), urlencode(params).encode(), hashlib.sha256
        ).hexdigest()
        headers = {
            "User-Agent": UA_GARENA,
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Authorization": f"Signature {signature}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        resp = await self._post_form(URL_GUEST_REGISTER, params, headers)
        data = resp.get("json", {})
        uid = _deep_get(data, "uid", "data.uid")
        if not uid or resp.get("status", 0) != 200:
            err_txt = str(resp.get("raw") or data)[:250]
            self.diagnostics.register_failed += 1
            self.diagnostics.register_per_region_failed[region] += 1
            self._diag_error(f"GuestRegister fail region={region} status={resp.get('status')} err={err_txt}")
            raise GarenaError(
                f"فشل تسجيل حساب ضيف ({region}): {err_txt}"
            )
        uid = str(uid)

        # Token Grant
        self.diagnostics.token_attempts += 1
        try:
            access_token, open_id = await self.token_grant(uid, password_hash)
            self.diagnostics.token_success += 1
        except Exception as e:
            self.diagnostics.token_failed += 1
            self._diag_error(f"TokenGrant fail uid={uid} region={region}: {e}")
            raise

        # Major Register
        self.diagnostics.major_register_attempts += 1
        body = (
            _field_string(1, nick)
            + _field_string(2, access_token)
            + _field_string(3, open_id)
            + _field_varint(5, 102000007)
            + _field_varint(6, 4)
            + _field_varint(7, 1)
            + _field_varint(13, 1)
            + _field_bytes(14, _xor_encrypt_openid(open_id))
            + _field_string(15, region)
            + _field_varint(16, 1)
        )
        enc = _aes_encrypt(body)
        reg_headers = {
            "Authorization": f"Bearer {access_token}",
            "X-Unity-Version": X_UNITY_VERSION,
            "X-GA": "v1 1",
            "ReleaseVersion": RELEASE_VERSION,
            "Content-Type": "application/octet-stream",
            "User-Agent": UA_GARENA,
            "Host": "loginbp.ggblueshark.com",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
        }
        status, _resp_body = await self._post_raw(URL_MAJOR_REGISTER, enc, reg_headers)
        if status != 200:
            self.diagnostics.major_register_failed += 1
            self._diag_error(f"MajorRegister HTTP {status} region={region} uid={uid}")
            raise GarenaError(f"MajorRegister فشل (HTTP {status}) منطقة {region}")
        self.diagnostics.major_register_success += 1
        self.diagnostics.register_success += 1
        self.diagnostics.register_per_region_success[region] += 1

        logger.info("تم إنشاء حساب ضيف جديد %s (منطقة %s)", uid, region)
        return GuestAccount(
            uid=uid,
            password=password,
            password_hash=password_hash,
            region=region,
            nickname=nick,
            access_token=access_token,
            open_id=open_id,
        )

    # ------------------------------------------------------------------
    # 1) تسجيل حساب ضيف جديد كامل — مع fallback
    # ------------------------------------------------------------------
    async def register_guest(
        self, region: str, nickname: Optional[str] = None, fallback: bool = True
    ) -> GuestAccount:
        """
        يسجّل حساب ضيف جديد. إذا فشل للمنطقة المطلوبة و fallback=True،
        يجرّب مناطق أخرى من GUEST_FALLBACK_ORDER، مع 3 محاولات بأسماء مختلفة لكل منطقة.
        """
        region = region.upper()
        regions_to_try: List[str] = [region]
        if fallback:
            # أضف باقي المناطق حسب الترتيب المفضل بدون تكرار
            for r in GUEST_FALLBACK_ORDER:
                if r != region and r not in regions_to_try:
                    regions_to_try.append(r)
            # أضف أي مناطق أخرى مفقودة
            for r in GUEST_REGIONS:
                if r not in regions_to_try:
                    regions_to_try.append(r)

        last_exc: Optional[Exception] = None
        attempted: List[str] = []

        for reg in regions_to_try:
            attempted.append(reg)
            # جرّب 3 أسماء مختلفة لنفس المنطقة (لتجاوز تضارب الاسم)
            for nick_try in range(3):
                try_nick = None
                if nick_try == 0 and nickname:
                    try_nick = nickname
                else:
                    try_nick = f"Liker{random.randint(1000, 99999)}_{random.randint(10,99)}"

                try:
                    return await self._try_register_single_region(reg, try_nick)
                except GarenaError as e:
                    last_exc = e
                    err_str = str(e).lower()
                    # إذا الخطأ يوحي بمشكلة اسم، جرّب اسماً آخر لنفس المنطقة
                    if any(k in err_str for k in ["nickname", "name", "duplicate", "exist", "400"]):
                        logger.debug("Retry nickname for region %s attempt %s", reg, nick_try)
                        await asyncio.sleep(0.3)
                        continue
                    else:
                        # خطأ منطقة — انتقل للمنطقة التالية
                        logger.debug("Region %s failed, fallback to next: %s", reg, e)
                        break
                except Exception as e:  # noqa: BLE001
                    last_exc = e
                    logger.debug("Unexpected register failure region %s: %s", reg, e)
                    break

        # كل المحاولات فشلت
        aggregate = ", ".join(attempted)
        msg = f"فشل إنشاء حساب ضيف بعد تجربة المناطق [{aggregate}]: {last_exc}"
        self._diag_error(msg)
        raise GarenaError(msg)

    # ------------------------------------------------------------------
    # 2) منح التوكن
    # ------------------------------------------------------------------
    async def token_grant(self, uid: str, password_hash: str) -> Tuple[str, str]:
        params = {
            "uid": uid,
            "password": password_hash,
            "response_type": "token",
            "client_type": "2",
            "client_secret": CLIENT_SECRET,
            "client_id": CLIENT_ID,
        }
        headers = {
            "User-Agent": UA_GARENA,
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
        }
        resp = await self._post_form(URL_TOKEN_GRANT, params, headers)
        data = resp.get("json", {})
        access_token = _deep_get(data, "access_token", "data.access_token")
        open_id = _deep_get(data, "open_id", "data.open_id")
        if not access_token:
            raise GarenaError(
                f"Token Grant فشل: {str(resp.get('raw') or data)[:200]}"
            )
        return str(access_token), str(open_id)

    # ------------------------------------------------------------------
    # 3) تسجيل الدخول → JWT
    # ------------------------------------------------------------------
    async def major_login(self, access_token: str, open_id: str) -> LoginSession:
        self.diagnostics.login_attempts += 1
        body = (
            _field_string(22, open_id)
            + _field_string(29, access_token)
            + _field_string(99, "4")
        )
        enc = _aes_encrypt(body)
        headers = {
            "User-Agent": UA_DALVIK,
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Expect": "100-continue",
            "X-Unity-Version": X_UNITY_VERSION,
            "X-GA": "v1 1",
            "ReleaseVersion": RELEASE_VERSION,
            "Content-Type": "application/octet-stream",
            "Authorization": "Bearer",
        }
        status, raw = await self._post_raw(URL_MAJOR_LOGIN, enc, headers)
        if status != 200:
            self.diagnostics.login_failed += 1
            self._diag_error(f"MajorLogin HTTP {status}: {raw[:120]!r}")
            raise GarenaError(f"MajorLogin فشل (HTTP {status}): {raw[:100]!r}")

        fields = _parse_protobuf(raw)
        jwt = _bytes_to_str(fields.get(8, [None])[0])
        if not jwt:
            self.diagnostics.login_failed += 1
            self._diag_error("MajorLogin no JWT in response")
            raise GarenaError("MajorLogin لم يعد JWT — الاستجابة غير متوقعة")

        self.diagnostics.login_success += 1
        server_url = _bytes_to_str(fields.get(10, [None])[0]) or self._base_server()
        account_id = next((v for v in fields.get(1, []) if isinstance(v, int)), None)
        lock_region = _bytes_to_str(fields.get(2, [None])[0]) or ""
        return LoginSession(
            jwt=jwt, server_url=server_url, account_id=account_id, lock_region=lock_region
        )

    # ------------------------------------------------------------------
    # Hybrid Pool - Read cache
    # ------------------------------------------------------------------
    async def get_read_session(self, region: str) -> LoginSession:
        """يعيد جلسة صالحة للقراءة (كاش 10 دقائق — hybrid pool)."""
        self.diagnostics.read_attempts += 1
        now = time.time()
        async with self._pool_lock:
            if region in self._read_sessions:
                sess, expiry = self._read_sessions[region]
                if now < expiry:
                    self.diagnostics.pool_hits += 1
                    self.diagnostics.read_success += 1
                    return sess

        self.diagnostics.pool_miss += 1
        try:
            guest = await self.register_guest(region, fallback=True)
            sess = await self.major_login(guest.access_token, guest.open_id)
            sess.region = region
            async with self._pool_lock:
                self._read_sessions[region] = (sess, now + self._read_ttl)
            self.diagnostics.read_success += 1
            return sess
        except Exception as e:
            self.diagnostics.read_failed += 1
            self._diag_error(f"get_read_session fail region={region}: {e}")
            raise

    # ------------------------------------------------------------------
    # Hybrid Pool - Like sessions (pre-warmed)
    # ------------------------------------------------------------------
    async def get_like_session(self, region: str) -> Tuple[GuestAccount, LoginSession]:
        """Hybrid: أولاً جرّب تجمع مُسخّن، وإلا أنشئ حساباً جديداً."""
        async with self._pool_lock:
            q = self._like_pool.get(region)
            if q and len(q) > 0:
                self.diagnostics.pool_hits += 1
                guest, sess = q.popleft()
                # تحقق صلاحية JWT (بسيط: لم يمضِ أكثر من 25 دقيقة)
                if time.time() - sess.created_at < 1500:
                    return guest, sess
                # منتهي — استمر لإنشاء جديد
            self.diagnostics.pool_miss += 1

        # إنشاء جديد (مع fallback)
        guest = await self.register_guest(region, fallback=True)
        sess = await self.major_login(guest.access_token, guest.open_id)
        sess.region = guest.region  # قد تكون منطقة بديلة
        sess.created_at = time.time()
        return guest, sess

    async def prewarm_like_pool(self, region: str, count: int = 2) -> int:
        """يملأ التجمع مسبقاً بـ count جلسات — يُستدعى في الخلفية."""
        filled = 0
        async with self._pool_lock:
            q = self._like_pool.setdefault(region, collections.deque(maxlen=self._like_pool_max))
            need = max(0, count - len(q))
        for _ in range(need):
            try:
                guest = await self.register_guest(region, fallback=True)
                sess = await self.major_login(guest.access_token, guest.open_id)
                sess.region = guest.region
                async with self._pool_lock:
                    if len(q) < self._like_pool_max:
                        q.append((guest, sess))
                        filled += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("prewarm_like_pool fail region=%s: %s", region, e)
                break
        return filled

    def clear_pool(self, region: Optional[str] = None) -> int:
        """يمسح التجمع (للتشخيص أو بعد أخطاء)."""
        if region:
            q = self._like_pool.pop(region, None)
            self._read_sessions.pop(region, None)
            return len(q) if q else 0
        total = sum(len(q) for q in self._like_pool.values())
        self._like_pool.clear()
        self._read_sessions.clear()
        return total

    # ------------------------------------------------------------------
    # 4) إرسال الإعجاب
    # ------------------------------------------------------------------
    async def send_like(
        self, session: LoginSession, target_uid: str, region: str
    ) -> LikeResult:
        self.diagnostics.like_attempts += 1
        body = _field_string(1, str(target_uid)) + _field_string(2, region)
        enc = _aes_encrypt(body)
        headers = {
            "User-Agent": UA_DALVIK,
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/octet-stream",
            "Expect": "100-continue",
            "Authorization": f"Bearer {session.jwt}",
            "X-Unity-Version": X_UNITY_VERSION,
            "X-GA": "v1 1",
            "ReleaseVersion": RELEASE_VERSION,
        }
        url = f"{session.server_url.rstrip('/')}/LikeProfile"
        status, raw = await self._post_raw(url, enc, headers)

        if status == 200:
            self.diagnostics.like_success += 1
            return LikeResult(success=True)

        text = raw.decode("utf-8", errors="ignore").lower()
        if any(k in text for k in LIMIT_KEYWORDS) or status in (403, 429):
            self.diagnostics.like_limit_hits += 1
            return LikeResult(
                success=False,
                limit_reached=True,
                message=f"HTTP {status}: {text[:120]}",
            )
        self.diagnostics.like_failed += 1
        self._diag_error(f"Like fail HTTP {status} uid={target_uid} region={region}: {text[:120]}")
        return LikeResult(success=False, message=f"HTTP {status}: {text[:120]}")

    # ------------------------------------------------------------------
    # 5) جلب معلومات اللاعب
    # ------------------------------------------------------------------
    async def get_player_info(
        self, session: LoginSession, uid: str
    ) -> Optional[PlayerInfo]:
        body = (
            _field_varint(1, int(uid))
            + _field_varint(2, 7)
            + _field_varint(3, 1)
        )
        enc = _aes_encrypt(body)
        headers = {
            "User-Agent": UA_DALVIK,
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/octet-stream",
            "Authorization": f"Bearer {session.jwt}",
            "X-Unity-Version": X_UNITY_VERSION,
            "X-GA": "v1 1",
            "ReleaseVersion": RELEASE_VERSION,
        }
        url = f"{session.server_url.rstrip('/')}/GetPlayerPersonalShow"
        status, raw = await self._post_raw(url, enc, headers)
        if status != 200:
            return None
        likes, nickname = self._extract_player_info(raw)
        return PlayerInfo(uid=str(uid), nickname=nickname, likes=likes)

    @staticmethod
    def _extract_player_info(raw: bytes) -> Tuple[Optional[int], Optional[str]]:
        liked: Optional[int] = None
        nickname: Optional[str] = None

        def walk(data: bytes) -> None:
            nonlocal liked, nickname
            fields = _parse_protobuf(data)
            for num, values in fields.items():
                for val in values:
                    if isinstance(val, int):
                        if num == 21 and liked is None:
                            liked = val
                    elif isinstance(val, bytes):
                        if num == 3 and nickname is None:
                            try:
                                nickname = val.decode("utf-8")
                            except UnicodeDecodeError:
                                pass
                        walk(val)

        try:
            walk(raw)
        except Exception:  # noqa: BLE001
            return None, None
        return liked, nickname

    # ------------------------------------------------------------------
    def _base_server(self, region: Optional[str] = None) -> str:
        if region:
            return SERVER_BASE.get(region.upper(), DEFAULT_SERVER)
        return DEFAULT_SERVER


def _bytes_to_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return ""


def json_loads(text: str) -> Any:
    import json

    try:
        return json.loads(text)
    except Exception:
        return {}
