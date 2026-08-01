"""عميل Garena — محدّث بعد تشخيص أغسطس 2026.

═══════════════════════════════════════════════════════════════════
★ تشخيص أغسطس 2026 (لماذا لم تكن اللايكات تُحتسب):
  منذ تحديث OB51 (أبريل 2026) فرضت Garena «بوابة مستوى» على الإعجابات:
    • حساب بمستوى < 8   → لا يستطيع إرسال إعجاب أصلاً (يُتجاهل صامتاً).
    • حساب بمستوى 8-20  → يُحتسب منه ~20 إعجاباً يومياً فقط لكل هدف.
    • حساب بمستوى 22+   → العدد الكامل (~100/يوم).
  الحسابات الضيفية الجديدة = مستوى 1 → كل إعجاباتها تُرمى صامتاً رغم
  أن السيرفر يرد HTTP 200 (واجهة تقبل، والمنطق الداخلي يتجاهل).
  كما أن الضيف الجديد غير المكتمل (بدون لعب) لا يظهر في بحث اللعبة.
  ⇒ لذلك هذا العميل لم يعد يُنشئ ضيوفاً لإرسال الإعجابات؛ يُستخدم
    التسجيل الضيفي فقط لجلسات القراءة (كشف المنطقة/العداد/البحث).
═══════════════════════════════════════════════════════════════════

هياكل protobuf المؤكدة (من مستودع 0xMe/FreeFire-Api — تحديث أبريل 2026):
  • MajorLogin.response:
      accountId=1, lockRegion=2, token(JWT)=8, ttl=9, serverUrl=10,
      blacklist=12 → { banReason=1, expireDuration=2, banTime=3 }
  • GetPlayerPersonalShow.request:  accountId=1, callSignSrc=2(=7),
      needGalleryInfo=3, needBlacklist=4, needSparkInfo=5
  • GetPlayerPersonalShow.response: basicinfo=1 → AccountInfoBasic{
      accountid=1, nickname=3, region=5, level=6, exp=7, liked=21,
      lastloginat=24, isdeleted=22 }
  • FuzzySearchAccountByName.request:  keyword=1
    FuzzySearchAccountByName.response: infos=1 (repeated AccountInfoBasic)

ملاحظة: ردود سيرفرات clientbp protobuf غير مشفّرة (تُفسَّر مباشرة).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import string
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp
from Crypto.Cipher import AES

from config import settings

logger = logging.getLogger(__name__)

# ==========================================================================
# الثوابت — OB54 (يوليو/أغسطس 2026)
# ==========================================================================
AES_KEY = b"Yg&tc%DEuh6%Zc^8"          # مفتاح AES-128-CBC (لم يتغير)
AES_IV = b"6oyZDr22E3ychjM%"           # IV ثابت (لم يتغير)

CLIENT_ID = "100067"
CLIENT_SECRET = "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3"

# --- OAuth (تسجيل الضيوف + منح التوكن) -----------------------------------
URL_GUEST_REGISTER = "https://connect.garena.com/oauth/guest/register"
GUEST_REGISTER_FALLBACKS: List[str] = [
    "https://100067.connect.garena.com/oauth/guest/register",
    "https://ffmconnect.live.gop.garenanow.com/oauth/guest/register",
]
URL_TOKEN_GRANT = "https://100067.connect.garena.com/oauth/guest/token/grant"
TOKEN_GRANT_FALLBACKS: List[str] = [
    "https://ffmconnect.live.gop.garenanow.com/api/v2/oauth/guest/token:grant",
    "https://ffmconnect.live.gop.garenanow.com/oauth/guest/token/grant",
]

# --- داخل اللعبة ----------------------------------------------------------
URL_MAJOR_REGISTER = "https://loginbp.ggblueshark.com/MajorRegister"

URL_MAJOR_LOGIN = "https://loginbp.ggpolarbear.com/MajorLogin"
MAJOR_LOGIN_FALLBACKS: List[str] = [
    "https://loginbp.common.ggbluefox.com/MajorLogin",
    "https://loginbp.ggblueshark.com/MajorLogin",
]
MAJOR_LOGIN_ME_FIRST = "https://loginbp.common.ggbluefox.com/MajorLogin"

UA_GARENA = "GarenaMSDK/4.0.19P8(ASUS_Z01QD ;Android 12;en;US;)"
UA_DALVIK = "Dalvik/2.1.0 (Linux; U; Android 11; ASUS_Z01QD Build/PI)"
UA_UNITY = "UnityPlayer/2022.3.47f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)"
RELEASE_VERSION = os.getenv("RELEASE_VERSION", "OB54")
X_UNITY_VERSION = "2018.4.11f1"

CLIENT_VERSION = "1.126.2"
CLIENT_VERSION_CODE = "2024010012"

# --- سيرفرات اللعبة (clientbp) -------------------------------------------
DEFAULT_SERVER = "https://clientbp.ggpolarbear.com"
SERVER_BASE: Dict[str, str] = {
    "ME": "https://clientbp.common.ggbluefox.com",
    "IND": "https://client.ind.freefiremobile.com",
    "BR": "https://client.us.freefiremobile.com",
    "US": "https://client.us.freefiremobile.com",
    "NA": "https://client.us.freefiremobile.com",
    "SAC": "https://client.us.freefiremobile.com",
}

# أكواد المناطق الرقمية لصيغة LikeProfile (varint)
REGION_CODES: Dict[str, int] = {
    "IND": 1, "ID": 1, "INDONESIA": 1,
    "BR": 2, "BRA": 2,
    "SG": 3,
    "TH": 4,
    "PH": 5,
    "VN": 6,
    "ME": 7,
    "RU": 8, "CIS": 8,
    "US": 9, "NA": 9,
    "PK": 10,
    "BD": 11,
    "TW": 12,
}
DEFAULT_REGION_CODE = 7  # ME

# لغة الحقل 15 في MajorRegister حسب المنطقة
REGION_LANG: Dict[str, str] = {
    "ME": "ar", "IND": "hi", "ID": "id", "VN": "vi", "TH": "th",
    "BD": "bn", "PK": "ur", "TW": "zh", "RU": "ru", "CIS": "ru",
    "BR": "pt", "SAC": "es",
}

# كلمات مفتاحية تدل على بلوغ الحد اليومي في ردود السيرفر
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


class AccountBannedError(GarenaError):
    """الحساب محظور — MajorLogin رد بمعلومات حظر (الحقل 12)."""

    def __init__(self, reason: str = "", expire_duration: int = 0) -> None:
        self.reason = reason
        self.expire_duration = expire_duration
        super().__init__(f"الحساب محظور (banReason={reason}, مدة={expire_duration}s)")


class LowLevelError(GarenaError):
    """الحساب دون المستوى المطلوب لإرسال الإعجابات (بوابة OB51+)."""


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


@dataclass
class LoginSession:
    jwt: str
    server_url: str
    account_id: Optional[int] = None
    lock_region: str = ""
    access_token: str = ""
    open_id: str = ""


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
    level: Optional[int] = None
    region: Optional[str] = None


@dataclass
class AccountValidation:
    """نتيجة التحقق من حساب مساهَم به."""
    uid: str
    level: int
    region: str
    nickname: str
    access_token: str
    open_id: str

    @property
    def eligible(self) -> bool:
        return self.level >= settings.min_donor_level


# ==========================================================================
# أدوات Protobuf (ترميز يدوي — بدون مكتبات خارجية)
# ==========================================================================
def _varint_encode(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _read_varint(data: bytes, i: int) -> Tuple[int, int]:
    result, shift = 0, 0
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
    """فك Protobuf بسيط: كل حقل → قائمة قيمه. متسامح مع البيانات التالفة."""
    fields: Dict[int, List[Any]] = {}
    i = 0
    while i < len(data):
        try:
            tag, i = _read_varint(data, i)
            num, wire = tag >> 3, tag & 0x07
            if num == 0:
                break
            if wire == 0:  # varint
                val, i = _read_varint(data, i)
                fields.setdefault(num, []).append(val)
            elif wire == 2:  # length-delimited
                ln, i = _read_varint(data, i)
                fields.setdefault(num, []).append(data[i : i + ln])
                i += ln
            elif wire == 5:  # 32-bit
                fields.setdefault(num, []).append(data[i : i + 4])
                i += 4
            elif wire == 1:  # 64-bit
                fields.setdefault(num, []).append(data[i : i + 8])
                i += 8
            else:
                break
        except Exception:
            break
    return fields


def _parse_basic_info(blob: bytes) -> Dict[str, Any]:
    """يفك AccountInfoBasic: accountid=1, nickname=3, region=5, level=6, liked=21."""
    out: Dict[str, Any] = {}
    fields = _parse_protobuf(blob)
    acc = next((v for v in fields.get(1, []) if isinstance(v, int)), None)
    if acc is not None:
        out["accountid"] = acc
    nick = next((v for v in fields.get(3, []) if isinstance(v, bytes)), None)
    if nick:
        try:
            out["nickname"] = nick.decode("utf-8", errors="ignore")
        except Exception:
            pass
    region = next((v for v in fields.get(5, []) if isinstance(v, bytes)), None)
    if region:
        out["region"] = region.decode("utf-8", errors="ignore").upper()
    level = next((v for v in fields.get(6, []) if isinstance(v, int)), None)
    if level is not None:
        out["level"] = level
    liked = next((v for v in fields.get(21, []) if isinstance(v, int)), None)
    if liked is not None:
        out["liked"] = liked
    deleted = next((v for v in fields.get(22, []) if isinstance(v, int)), None)
    if deleted:
        out["isdeleted"] = True
    return out


# ==========================================================================
# التشفير
# ==========================================================================
def _aes_encrypt(data: bytes) -> bytes:
    pad_len = 16 - (len(data) % 16)
    data = data + bytes([pad_len]) * pad_len
    return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(data)


def _aes_decrypt(data: bytes) -> bytes:
    """فك التشفير (للاختبارات واستكشاف الأخطاء)."""
    raw = AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(data)
    if not raw:
        return b""
    pad_len = raw[-1]
    return raw[:-pad_len] if 1 <= pad_len <= 16 else raw


_XOR_KEY = bytes(
    [0, 0, 0, 2, 0, 1, 7, 0, 0, 0, 0, 0, 2, 0, 1, 7,
     0, 0, 0, 0, 0, 2, 0, 1, 7, 0, 0, 0, 0, 0, 2, 0]
)


def _xor_encrypt_openid(open_id: str) -> bytes:
    raw = open_id.encode("utf-8")
    return bytes(b ^ _XOR_KEY[i % len(_XOR_KEY)] ^ 48 for i, b in enumerate(raw))


def _deep_get(obj: Any, *paths: str) -> Any:
    """يبحث عن مفتاح في JSON — سواء كان في الجذر أو داخل حقل data."""
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


def _build_major_login_proto(open_id: str, access_token: str) -> bytes:
    """جسم MajorLogin الكامل — مطابق لطلب MajorLogin.request الحديث."""
    gs = _field_varint(6, 55) + _field_varint(8, 81)
    analytics = base64.b64decode("FwQVTgUPX1UaUllDDwcWCRBpWAUOUgsvA1snWlBaO1kFYg==")
    body = b"".join([
        _field_string(3, time.strftime("%Y-%m-%d %H:%M:%S")),   # event_time
        _field_string(4, "free fire"),                          # game_id
        _field_varint(5, 2),                                    # plat_id
        _field_string(7, CLIENT_VERSION),                       # client_version
        _field_string(8, "Android OS 11 / API-30 (RQ3A.210805.001)"),
        _field_string(9, "Handheld"),                           # system_hardware
        _field_string(10, "Verizon"),                           # telecom_oper
        _field_string(11, "WIFI"),                              # network
        _field_varint(12, 1080),
        _field_varint(13, 2400),
        _field_string(14, "440"),                               # dpi
        _field_string(15, "ARMv8"),                             # cpu_hardware
        _field_varint(16, 6144),                                # memory
        _field_string(17, "Adreno (TM) 650"),
        _field_string(18, "OpenGL ES 3.2 V@1.50"),
        _field_string(19, "Google|34a7dcdf-a7d5-4cb6-8d7e-3b0e448a0c57"),  # device_id
        _field_string(20, ""),                                  # client_ip
        _field_string(21, "en"),                                # language
        _field_string(22, open_id),                             # ★ open_id
        _field_string(23, "4"),                                 # open_id_type
        _field_string(24, "Handheld"),                          # device_type
        _field_bytes(25, gs),
        _field_string(29, access_token),                        # ★ login_token
        _field_varint(30, 2),                                   # platform_sdk_id
        _field_string(41, "Verizon"),
        _field_string(42, "WIFI"),
        _field_string(57, "7428b253defc164018c604a1ebbfebdf"),
        _field_varint(60, 128512),
        _field_varint(61, random.randint(38000, 52000)),
        _field_varint(62, 110731),
        _field_varint(63, random.randint(18000, 32000)),
        _field_varint(64, random.randint(18000, 25000)),
        _field_varint(65, 26628),
        _field_varint(66, random.randint(25000, 60000)),
        _field_varint(67, 119234),
        _field_varint(73, 3),                                   # login_by
        _field_string(74, "/data/app/~~random/base.apk"),
        _field_string(77, "hash|base.apk"),
        _field_varint(79, 2),                                   # cpu_type
        _field_string(81, "64"),
        _field_string(83, CLIENT_VERSION_CODE),
        _field_string(86, "OpenGLES3"),
        _field_varint(87, 16383),
        _field_varint(88, 4),
        _field_bytes(89, analytics),
        _field_varint(92, random.randint(9000, 18000)),
        _field_string(99, "4"),
        _field_string(100, "4"),
    ])
    return body


# ==========================================================================
# العميل
# ==========================================================================
class GarenaClient:
    """عميل واجهات Garena — ضيوف للقراءة، JWT، إعجابات، بحث، فحص حسابات."""

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._proxy_pool: List[str] = []
        self._proxy_idx: int = 0
        self._proxy_last_fetch: float = 0.0

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
        """POST برسالة form-urlencoded ويعيد {status, json, raw?}."""
        assert self._session is not None
        proxy = await self._next_proxy()
        async with self._session.post(
            url, data=params, headers=headers, proxy=proxy
        ) as resp:
            text = await resp.text()
            try:
                return {"status": resp.status, "json": json.loads(text)}
            except Exception:
                return {"status": resp.status, "json": {}, "raw": text}

    async def _post_raw(
        self, url: str, body: bytes, headers: Dict[str, str]
    ) -> Tuple[int, bytes]:
        """POST بجسم خام (octet-stream) ويعيد (الحالة، الرد)."""
        assert self._session is not None
        proxy = await self._next_proxy()
        async with self._session.post(
            url, data=body, headers=headers, proxy=proxy
        ) as resp:
            return resp.status, await resp.read()

    # ------------------------------------------------------------------
    # 1) تسجيل حساب ضيف جديد (لجلسات القراءة فقط — انظر ترويسة الملف)
    # ------------------------------------------------------------------
    async def register_guest(self, region: str, nickname: Optional[str] = None) -> GuestAccount:
        password = "".join(random.choices(string.ascii_uppercase + string.digits, k=14))
        password_hash = hashlib.sha256(password.encode()).hexdigest().upper()
        nick = nickname or f"Reader{random.randint(1000, 9999)}"

        # ---- 1.1 Guest Register ----
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
        uid: Optional[str] = None
        last_err = "بدون رد"
        for url in _unique([URL_GUEST_REGISTER, *GUEST_REGISTER_FALLBACKS]):
            try:
                resp = await self._post_form(url, params, headers)
            except Exception as exc:
                last_err = f"{url.split('/')[2]}: {type(exc).__name__}"
                logger.debug("Guest Register عبر %s فشل: %s", url, exc)
                continue
            data = resp.get("json", {}) or {}
            got = _deep_get(data, "uid", "data.uid")
            if got and resp.get("status") == 200:
                uid = str(got)
                if url != URL_GUEST_REGISTER:
                    logger.info("Guest Register نجح عبر المضيف البديل: %s", url)
                break
            last_err = str(resp.get("raw") or data)[:200] + f" (HTTP {resp.get('status')})"
            logger.debug("Guest Register عبر %s فشل: %s", url, last_err)
        if not uid:
            raise GarenaError(f"فشل تسجيل حساب ضيف ({region}): {last_err}")

        # ---- 1.2 Token Grant ----
        access_token, open_id = await self.token_grant(uid, password_hash)

        # ---- 1.3 Major Register ----
        lang = REGION_LANG.get(region.upper(), "en")
        body = (
            _field_string(1, nick)
            + _field_string(2, access_token)
            + _field_string(3, open_id)
            + _field_varint(5, 102000007)
            + _field_varint(6, 4)
            + _field_varint(7, 1)
            + _field_varint(13, 1)
            + _field_bytes(14, _xor_encrypt_openid(open_id))
            + _field_string(15, lang)
            + _field_varint(16, 1)
            + _field_varint(17, 1)
        )
        enc = _aes_encrypt(body)
        reg_headers = {
            "Authorization": f"Bearer {access_token}",
            "X-Unity-Version": X_UNITY_VERSION,
            "X-GA": "v1 1",
            "ReleaseVersion": RELEASE_VERSION,
            "Content-Type": "application/octet-stream",
            "User-Agent": UA_GARENA,
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
        }
        status, _resp_body = await self._post_raw(URL_MAJOR_REGISTER, enc, reg_headers)
        if status != 200:
            raise GarenaError(f"MajorRegister فشل (HTTP {status})")

        logger.info("تم إنشاء حساب ضيف قراءة %s (منطقة %s)", uid, region)
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
    # 2) منح التوكن (لحساب ضيف موجود) — مع بدائل v1/v2 وتراجع 429
    # ------------------------------------------------------------------
    async def token_grant(self, uid: str, password_hash: str) -> Tuple[str, str]:
        params = {
            "uid": str(uid),
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
            "Content-Type": "application/x-www-form-urlencoded",
        }
        last_err = "بدون رد"
        for url in _unique([URL_TOKEN_GRANT, *TOKEN_GRANT_FALLBACKS]):
            for attempt in range(3):  # تراجع عند 429
                try:
                    resp = await self._post_form(url, params, headers)
                except Exception as exc:
                    last_err = f"{url.split('/')[2]}: {type(exc).__name__}"
                    break
                data = resp.get("json", {}) or {}
                access_token = _deep_get(data, "access_token", "data.access_token")
                open_id = _deep_get(data, "open_id", "data.open_id")
                if access_token and resp.get("status") == 200:
                    return str(access_token), str(open_id)
                last_err = str(resp.get("raw") or data)[:200]
                if resp.get("status") == 429 and attempt < 2:
                    wait = 5 * (attempt + 1)
                    logger.warning("Token Grant 429 — انتظار %ds وإعادة المحاولة", wait)
                    await asyncio.sleep(wait)
                    continue
                logger.debug("Token Grant عبر %s فشل: %s", url, last_err)
                break
        raise GarenaError(f"Token Grant فشل: {last_err}")

    # ------------------------------------------------------------------
    # 3) تسجيل الدخول (Major Login) → JWT + serverUrl — مع كشف الحظر (حقل 12)
    # ------------------------------------------------------------------
    def _major_login_urls(self, region: str) -> List[str]:
        urls = [URL_MAJOR_LOGIN, *MAJOR_LOGIN_FALLBACKS]
        if region.upper() in ("ME", "TH"):
            urls.insert(0, MAJOR_LOGIN_ME_FIRST)
        return _unique(urls)

    async def major_login(
        self, access_token: str, open_id: str, region: str = "ME"
    ) -> LoginSession:
        body = _build_major_login_proto(open_id, access_token)
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
            "Authorization": f"Bearer {access_token}",
        }
        last_err = "بدون رد"
        ban_info: Optional[Dict[str, Any]] = None
        for url in self._major_login_urls(region):
            try:
                status, raw = await self._post_raw(url, enc, headers)
            except Exception as exc:
                last_err = f"{url.split('/')[2]}: {type(exc).__name__}"
                logger.debug("MajorLogin عبر %s فشل: %s", url, exc)
                continue
            if status != 200:
                last_err = f"{url.split('/')[2]}: HTTP {status} {raw[:60]!r}"
                logger.debug("MajorLogin عبر %s فشل: %s", url, last_err)
                continue

            fields = _parse_protobuf(raw)

            # ---- كشف الحظر: blacklist=12 → {banReason=1, expireDuration=2, banTime=3}
            ban_blob = next((v for v in fields.get(12, []) if isinstance(v, bytes)), None)
            if ban_blob:
                bf = _parse_protobuf(ban_blob)
                ban_info = {
                    "reason": str(next((v for v in bf.get(1, []) if isinstance(v, int)), 0)),
                    "expire": next((v for v in bf.get(2, []) if isinstance(v, int)), 0),
                }
                last_err = (
                    f"{url.split('/')[2]}: حساب محظور "
                    f"(reason={ban_info['reason']}, مدة={ban_info['expire']}s)"
                )
                logger.debug("MajorLogin عبر %s: %s", url, last_err)
                continue

            jwt = _bytes_to_str(next((v for v in fields.get(8, []) if isinstance(v, bytes)), None))
            if not jwt:
                jwt = _scan_jwt(raw)
            if not jwt:
                last_err = f"{url.split('/')[2]}: رد بلا JWT ({len(raw)} بايت)"
                logger.debug("MajorLogin عبر %s: %s", url, last_err)
                continue

            server_url = _bytes_to_str(
                next((v for v in fields.get(10, []) if isinstance(v, bytes)), None)
            )
            account_id = next((v for v in fields.get(1, []) if isinstance(v, int)), None)
            lock_region = _bytes_to_str(
                next((v for v in fields.get(2, []) if isinstance(v, bytes)), None)
            ) or region
            if url != URL_MAJOR_LOGIN:
                logger.info("MajorLogin نجح عبر المضيف البديل: %s", url.split("/")[2])
            return LoginSession(
                jwt=jwt,
                server_url=server_url,
                account_id=account_id,
                lock_region=lock_region,
                access_token=access_token,
                open_id=open_id,
            )
        if ban_info is not None:
            raise AccountBannedError(ban_info["reason"], int(ban_info["expire"]))
        raise GarenaError(f"MajorLogin فشل على كل المضيفات: {last_err}")

    # ------------------------------------------------------------------
    # 4) إرسال الإعجاب — varint(uid) + varint(كود المنطقة)
    # ------------------------------------------------------------------
    def _like_bases(self, session: LoginSession, region: str) -> List[str]:
        return _unique(
            [
                session.server_url,
                DEFAULT_SERVER,
                SERVER_BASE.get(region.upper(), DEFAULT_SERVER),
            ]
        )

    async def send_like(
        self, session: LoginSession, target_uid: str, region: str
    ) -> LikeResult:
        region_code = REGION_CODES.get(region.upper(), DEFAULT_REGION_CODE)
        body = _field_varint(1, int(target_uid)) + _field_varint(2, region_code)
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
        last_msg = ""
        for base in self._like_bases(session, region):
            url = f"{base.rstrip('/')}/LikeProfile"
            try:
                status, raw = await self._post_raw(url, enc, headers)
            except Exception as exc:
                last_msg = f"{type(exc).__name__} عبر {base.split('/')[2]}"
                continue
            if status == 200:
                return LikeResult(success=True)
            text = raw.decode("utf-8", errors="ignore").lower()
            if any(k in text for k in LIMIT_KEYWORDS) or status == 429:
                return LikeResult(
                    success=False,
                    limit_reached=True,
                    message=f"HTTP {status}: {text[:120]}",
                )
            last_msg = f"HTTP {status}: {text[:120]}"
            logger.debug("LikeProfile عبر %s: %s", base, last_msg)
        return LikeResult(success=False, message=last_msg or "فشل غير معروف")

    # ------------------------------------------------------------------
    # 5) جلب معلومات اللاعب: basicinfo{nickname, region, level, liked}
    # ------------------------------------------------------------------
    def _info_headers(self, jwt: str) -> Dict[str, str]:
        """ترويسات نقاط القراءة (مطابقة للمرجع الشغال أبريل 2026)."""
        return {
            "User-Agent": UA_UNITY,
            "Accept": "*/*",
            "Accept-Encoding": "deflate, gzip",
            "Authorization": f"Bearer {jwt}",
            "X-GA": "v1 1",
            "ReleaseVersion": RELEASE_VERSION,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Unity-Version": "2022.3.47f1",
        }

    async def get_player_info(
        self, session: LoginSession, uid: str, region: str = "ME",
        call_sign_src: int = 7,
    ) -> Optional[PlayerInfo]:
        body = _field_varint(1, int(uid)) + _field_varint(2, call_sign_src)
        enc = _aes_encrypt(body)
        headers = self._info_headers(session.jwt)
        for base in self._like_bases(session, region):
            url = f"{base.rstrip('/')}/GetPlayerPersonalShow"
            try:
                status, raw = await self._post_raw(url, enc, headers)
            except Exception:  # noqa: BLE001
                continue
            if status != 200 or not raw:
                continue
            info = self._extract_player_info(raw)
            if info is not None:
                info.uid = info.uid or str(uid)
                return info
        return None

    @staticmethod
    def _extract_player_info(raw: bytes) -> Optional[PlayerInfo]:
        """يفك استجابة GetPlayerPersonalShow بالهيكل الحقيقي:
        response.basicinfo(1) → AccountInfoBasic{1,3,5,6,21}.
        مع تراجع للمسح العام إذا تغيّر التداخل."""
        try:
            top = _parse_protobuf(raw)
            blob = next((v for v in top.get(1, []) if isinstance(v, bytes)), None)
            if blob:
                basic = _parse_basic_info(blob)
                if basic:
                    return PlayerInfo(
                        uid=str(basic["accountid"]) if basic.get("accountid") else None,
                        nickname=basic.get("nickname"),
                        likes=basic.get("liked"),
                        level=basic.get("level"),
                        region=basic.get("region"),
                    )
        except Exception:  # noqa: BLE001
            pass

        # ---- تراجع عام: دور على liked (21) و nickname (3) في أي تداخل ----
        liked: Optional[int] = None
        nickname: Optional[str] = None

        def walk(data: bytes, depth: int = 0) -> None:
            nonlocal liked, nickname
            if depth > 6:
                return
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
                        walk(val, depth + 1)

        try:
            walk(raw)
        except Exception:  # noqa: BLE001
            return None
        if liked is None and nickname is None:
            return None
        return PlayerInfo(likes=liked, nickname=nickname)

    # ------------------------------------------------------------------
    # 6) بحث بالاسم: FuzzySearchAccountByName → repeated AccountInfoBasic(1)
    # ------------------------------------------------------------------
    async def search_accounts(
        self, session: LoginSession, keyword: str, region: str = "ME", limit: int = 8
    ) -> List[PlayerInfo]:
        body = _field_string(1, keyword)
        enc = _aes_encrypt(body)
        headers = self._info_headers(session.jwt)
        results: List[PlayerInfo] = []
        for base in self._like_bases(session, region):
            url = f"{base.rstrip('/')}/FuzzySearchAccountByName"
            try:
                status, raw = await self._post_raw(url, enc, headers)
            except Exception:  # noqa: BLE001
                continue
            if status != 200 or not raw:
                continue
            try:
                top = _parse_protobuf(raw)
                for blob in top.get(1, []):
                    if not isinstance(blob, bytes):
                        continue
                    basic = _parse_basic_info(blob)
                    if not basic:
                        continue
                    results.append(
                        PlayerInfo(
                            uid=str(basic["accountid"]) if basic.get("accountid") else None,
                            nickname=basic.get("nickname"),
                            likes=basic.get("liked"),
                            level=basic.get("level"),
                            region=basic.get("region"),
                        )
                    )
            except Exception:  # noqa: BLE001
                continue
            if results:
                break
        # إزالة التكرار والنتائج بلا uid
        seen: set = set()
        uniq: List[PlayerInfo] = []
        for r in results:
            if not r.uid or r.uid in seen:
                continue
            seen.add(r.uid)
            uniq.append(r)
        return uniq[:limit]

    # ------------------------------------------------------------------
    # 7) التحقق من حساب مساهَم به: دخول حقيقي + قراءة المستوى/المنطقة/الاسم
    #    هذا هو «الفلتر» الذي يمنع تخزين حسابات ميّتة/منخفضة المستوى.
    # ------------------------------------------------------------------
    async def validate_account(
        self, uid: str, password_hash: str, region_hint: str = "ME"
    ) -> AccountValidation:
        """يتحقق بالكامل: Token Grant → MajorLogin (كشف حظر) → المستوى الحقيقي.

        يرفع:
          GarenaError        — بيانات دخول غير صالحة/فشل شبكة
          AccountBannedError — الحساب محظور (من الحقل 12 في MajorLogin)
        """
        access_token, open_id = await self.token_grant(uid, password_hash)
        session = await self.major_login(access_token, open_id, region_hint)

        # المستوى والمنطقة الحقيقيان من بروفايل الحساب نفسه (callSignSrc=9 مالك)
        info = await self.get_player_info(
            session, str(uid), session.lock_region or region_hint, call_sign_src=9
        )
        level = info.level if info and info.level is not None else 0
        region = (
            (info.region if info else None)
            or session.lock_region
            or region_hint
        ).upper()
        nickname = info.nickname if info else ""
        return AccountValidation(
            uid=str(uid),
            level=level,
            region=region,
            nickname=nickname or "",
            access_token=access_token,
            open_id=open_id,
        )


# ==========================================================================
# أدوات مساعدة
# ==========================================================================
def _unique(seq: List[str]) -> List[str]:
    seen: Dict[str, None] = {}
    for item in seq:
        if item and item not in seen:
            seen[item] = None
    return list(seen)


def _scan_jwt(raw: bytes) -> str:
    try:
        text = raw.decode("latin1")
    except Exception:  # noqa: BLE001
        return ""
    idx = text.find("eyJhbGci")
    if idx == -1:
        return ""
    token = []
    for ch in text[idx:]:
        if ch.isalnum() or ch in "-_.":
            token.append(ch)
        else:
            break
    return "".join(token) if len(token) > 20 else ""


def _bytes_to_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return ""


def password_to_hash(password: str) -> str:
    """يقبل كلمة سر نصية أو هاش SHA256 جاهزاً (64 محرف hex)."""
    password = password.strip()
    if len(password) == 64 and all(c in "0123456789abcdefABCDEF" for c in password):
        return password.upper()
    return hashlib.sha256(password.encode()).hexdigest().upper()
