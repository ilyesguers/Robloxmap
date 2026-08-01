"""
عميل Garena الحقيقي — محدّث للتدفق الشغال الحالي (OB54 — يوليو 2026).

مصادر التحديث (تحقق مزدوج):
  • ISMAILdz13/FreeFireLikesBot — آخر تحديث 2026-07-30، وملاحظة في الكود
    «LikeProfile varint format — confirmed working 2026-07-29».
  • @spinzaf/freefire-api وخليفته ffapis (مايو 2026).

التدفق الحالي (OB54):
  1) تسجيل حساب ضيف (Guest Register):
     POST https://connect.garena.com/oauth/guest/register   (مع بدائل)
     (form) password=SHA256(كلمة سر) & client_type=2 & source=2 & app_id=100067
     التوقيع: HMAC-SHA256(client_secret, جسم الطلب) في  Authorization: Signature ...

  2) منح توكن (Token Grant):
     POST https://100067.connect.garena.com/oauth/guest/token/grant
     بدائل: v2 على ffmconnect.live.gop.garenanow.com/api/v2/oauth/guest/token:grant
     ثم v1 على ffmconnect (القديم)
     → access_token + open_id. (429 = حد معدل الطلبات → تراجع وانتظار)

  3) إنشاء الحساب داخل اللعبة (Major Register):
     POST https://loginbp.ggblueshark.com/MajorRegister
     Protobuf مشفّر AES-128-CBC. الحقل 15 = اللغة (وليس المنطقة!) + حقل 17 = 1

  4) تسجيل الدخول (Major Login) → JWT:
     يُحاول بالترتيب: ggpolarbear ← ME أولاً على common.ggbluefox ← ثم ggblueshark
     الجسم = Protobuf MajorLogin كامل (الحقول 3..100) مشفّر AES-128-CBC
     الرد: token=JWT (حقل 8)، url=serverUrl (حقل 10)، region (حقل 2)

  5) إرسال الإعجاب (الصيغة الجديدة المؤكدة 2026-07-29):
     POST {serverUrl}/LikeProfile
     الجسم = 0x08 + varint(uid الهدف) + 0x10 + varint(كود المنطقة)  ← varint وليس نصوصاً!
     أكواد المناطق: ME=7, IND=1, BR=2, SG=3, TH=4, PH=5, VN=6, RU=8, US=9, PK=10, BD=11, TW=12

  6) التحقق من عدد الإعجابات: POST {serverUrl}/GetPlayerPersonalShow

⚠️ ملاحظة أمان: القيم ثابتة في الكود حتى لا تحتاج متغيرات بيئة إضافية على
Railway (يكفي BOT_TOKEN و ADMIN_ID). إذا غيّرت Garena القيم، عدّلها من أعلى
هذا الملف فقط.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
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
# الثوابت — OB54 (يوليو 2026)
# ==========================================================================
AES_KEY = b"Yg&tc%DEuh6%Zc^8"          # مفتاح AES-128-CBC (لم يتغير)
AES_IV = b"6oyZDr22E3ychjM%"           # IV ثابت (لم يتغير)

CLIENT_ID = "100067"
CLIENT_SECRET = "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3"

# --- OAuth (تسجيل الضيوف + منح التوكن) -----------------------------------
# الرئيسي أولاً ثم البدائل. systemd-smoke-test يستبدل هذه القيم بسيرفر وهمي.
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

# MajorLogin: الترتيب الافتراضي (ME تبدأ بـ ggbluefox)
URL_MAJOR_LOGIN = "https://loginbp.ggpolarbear.com/MajorLogin"
MAJOR_LOGIN_FALLBACKS: List[str] = [
    "https://loginbp.common.ggbluefox.com/MajorLogin",
    "https://loginbp.ggblueshark.com/MajorLogin",
]
MAJOR_LOGIN_ME_FIRST = "https://loginbp.common.ggbluefox.com/MajorLogin"

UA_GARENA = "GarenaMSDK/4.0.19P8(ASUS_Z01QD ;Android 12;en;US;)"
UA_DALVIK = "Dalvik/2.1.0 (Linux; U; Android 11; ASUS_Z01QD Build/PI)"
RELEASE_VERSION = "OB54"
X_UNITY_VERSION = "2018.4.11f1"

# بيانات عميل MajorLogin (مطابقة لعميل OB54 الحالي)
CLIENT_VERSION = "1.126.2"
CLIENT_VERSION_CODE = "2024010012"

# --- سيرفرات اللعبة (clientbp) -------------------------------------------
# المرجع الشغال يرسل LikeProfile دائماً تقريباً إلى ggpolarbear؛ نفضّل serverUrl
# العائد من MajorLogin ثم ggpolarbear ثم خريطة المناطق.
DEFAULT_SERVER = "https://clientbp.ggpolarbear.com"
SERVER_BASE: Dict[str, str] = {
    "ME": "https://clientbp.common.ggbluefox.com",
    "IND": "https://client.ind.freefiremobile.com",
    "BR": "https://client.us.freefiremobile.com",
    "US": "https://client.us.freefiremobile.com",
    "NA": "https://client.us.freefiremobile.com",
    "SAC": "https://client.us.freefiremobile.com",
}

# أكواد المناطق الرقمية لصيغة LikeProfile الجديدة (varint — مؤكدة 2026-07-29)
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
DEFAULT_REGION_CODE = 7  # ME كافتراضي (الأكثر استخداماً)

# لغة الحقل 15 في MajorRegister حسب المنطقة
REGION_LANG: Dict[str, str] = {
    "ME": "ar", "IND": "hi", "ID": "id", "VN": "vi", "TH": "th",
    "BD": "bn", "PK": "ur", "TW": "zh", "RU": "ru", "CIS": "ru",
    "BR": "pt", "SAC": "es",
}

# ==========================================================================
# ⚠️ لا حسابات جاهزة مزروعة.
# النسخة السابقة كانت تحتوي حسابات "جاهزة" مُنشأة يدوياً — uid/password_hash
# غير موجودة فعلياً في Garena، لذا كان Token Grant يرد {'error': 'auth_error'}
# ويتكرر نفس الخطأ إلى ما لا نهاية. بقيت القائمة فارغة عمداً: المخزون في قاعدة
# البيانات يمتلئ تلقائياً بحسابات حقيقية (تُسجَّل بنجاح) فقط.
# ==========================================================================
SEED_GUEST_ACCOUNTS: Dict[str, List[Dict[str, str]]] = {}

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
    """فك Protobuf بسيط: كل حقل → قائمة قيمه (varint أو bytes). متسامح مع البيانات التالفة."""
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
                break  # wire type غير معروف — نتوقف
        except Exception:
            break
    return fields


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
    """جسم MajorLogin الكامل — مطابق لبروتو MajorLoginReq في OB54
    (أرقام الحقول مستخرجة من MajoRLoGinrEq_pb2 المحدّث)."""
    gs = _field_varint(6, 55) + _field_varint(8, 81)      # memory_available (GameSecurity)
    analytics = base64.b64decode("FwQVTgUPX1UaUllDDwcWCRBpWAUOUgsvA1snWlBaO1kFYg==")
    body = b"".join([
        _field_string(3, time.strftime("%Y-%m-%d %H:%M:%S")),   # event_time
        _field_string(4, "free fire"),                          # game_name
        _field_varint(5, 2),                                    # platform_id
        _field_string(7, CLIENT_VERSION),                       # client_version
        _field_string(8, "Android OS 11 / API-30 (RQ3A.210805.001)"),
        _field_string(9, "Handheld"),                           # system_hardware
        _field_string(10, "Verizon"),                           # telecom_operator
        _field_string(11, "WIFI"),                              # network_type
        _field_varint(12, 1080),                                # screen_width
        _field_varint(13, 2400),                                # screen_height
        _field_string(14, "440"),                               # screen_dpi
        _field_string(15, "ARMv8"),                             # processor_details
        _field_varint(16, 6144),                                # memory
        _field_string(17, "Adreno (TM) 650"),                   # gpu_renderer
        _field_string(18, "OpenGL ES 3.2 V@1.50"),              # gpu_version
        _field_string(19, "Google|34a7dcdf-a7d5-4cb6-8d7e-3b0e448a0c57"),
        _field_string(20, ""),                                  # client_ip
        _field_string(21, "en"),                                # language
        _field_string(22, open_id),                             # ★ open_id
        _field_string(23, "4"),                                 # open_id_type
        _field_string(24, "Handheld"),                          # device_type
        _field_bytes(25, gs),                                   # memory_available
        _field_string(29, access_token),                        # ★ access_token
        _field_varint(30, 2),                                   # platform_sdk_id
        _field_string(41, "Verizon"),                           # network_operator_a
        _field_string(42, "WIFI"),                              # network_type_a
        _field_string(57, "7428b253defc164018c604a1ebbfebdf"),  # client_using_version
        _field_varint(60, 128512),                              # external_storage_total
        _field_varint(61, random.randint(38000, 52000)),
        _field_varint(62, 110731),                              # internal_storage_total
        _field_varint(63, random.randint(18000, 32000)),
        _field_varint(64, random.randint(18000, 25000)),
        _field_varint(65, 26628),                               # game_disk_storage_total
        _field_varint(66, random.randint(25000, 60000)),
        _field_varint(67, 119234),                              # external_sdcard_total
        _field_varint(73, 3),                                   # login_by
        _field_string(74, "/data/app/~~random/base.apk"),       # library_path
        _field_string(77, "hash|base.apk"),                     # library_token
        _field_varint(79, 2),                                   # cpu_type
        _field_string(81, "64"),                                # cpu_architecture
        _field_string(83, CLIENT_VERSION_CODE),                 # client_version_code
        _field_string(86, "OpenGLES3"),                         # graphics_api
        _field_varint(87, 16383),                               # supported_astc_bitset
        _field_varint(88, 4),                                   # login_open_id_type
        _field_bytes(89, analytics),                            # analytics_detail
        _field_varint(92, random.randint(9000, 18000)),         # loading_time
        _field_string(99, "4"),                                 # origin_platform_type
        _field_string(100, "4"),                                # primary_platform_type
    ])
    return body


# ==========================================================================
# العميل
# ==========================================================================
class GarenaClient:
    """عميل واجهات Garena — إنشاء ضيوف، JWT، إعجابات، فحص البروفايل."""

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
    # 1) تسجيل حساب ضيف جديد كامل (register + token + major register)
    # ------------------------------------------------------------------
    async def register_guest(self, region: str, nickname: Optional[str] = None) -> GuestAccount:
        password = "".join(random.choices(string.ascii_uppercase + string.digits, k=14))
        password_hash = hashlib.sha256(password.encode()).hexdigest().upper()
        nick = nickname or f"Liker{random.randint(1000, 9999)}"

        # ---- 1.1 Guest Register (نجرب المضيف الأساسي ثم البدائل) ----
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
            except Exception as exc:  # شبكة/مهلة
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

        # ---- 1.3 Major Register (الحقل 15 = اللغة، والحقل 17 = 1 — OB54) ----
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
                except Exception as exc:  # شبكة/مهلة
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
                # auth_error أو غيره → لا فائدة من إعادة المحاولة على نفس المضيف
                logger.debug("Token Grant عبر %s فشل: %s", url, last_err)
                break
        raise GarenaError(f"Token Grant فشل: {last_err}")

    # ------------------------------------------------------------------
    # 3) تسجيل الدخول (Major Login) → JWT + serverUrl
    #    جسم Protobuf كامل (OB54) + مضيفات حسب المنطقة
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
        for url in self._major_login_urls(region):
            try:
                status, raw = await self._post_raw(url, enc, headers)
            except Exception as exc:  # شبكة/مهلة
                last_err = f"{url.split('/')[2]}: {type(exc).__name__}"
                logger.debug("MajorLogin عبر %s فشل: %s", url, exc)
                continue
            if status != 200:
                last_err = f"{url.split('/')[2]}: HTTP {status} {raw[:60]!r}"
                logger.debug("MajorLogin عبر %s فشل: %s", url, last_err)
                continue

            fields = _parse_protobuf(raw)
            jwt = _bytes_to_str(fields.get(8, [None])[0])
            if not jwt:
                # احتياط: مسح البايتات عن JWT مباشرة (تغيّر أرقام الحقول)
                jwt = _scan_jwt(raw)
            if not jwt:
                blacklist = _bytes_to_str(fields.get(6, [None])[0]) or _bytes_to_str(
                    fields.get(25, [None])[0]
                )
                last_err = f"{url.split('/')[2]}: رد بلا JWT ({len(raw)} بايت)"
                if blacklist:
                    last_err += f" [حظر: {blacklist[:80]}]"
                logger.debug("MajorLogin عبر %s: %s", url, last_err)
                continue

            server_url = _bytes_to_str(fields.get(10, [None])[0]) or ""
            account_id = next((v for v in fields.get(1, []) if isinstance(v, int)), None)
            lock_region = _bytes_to_str(fields.get(2, [None])[0]) or region
            if url != URL_MAJOR_LOGIN:
                logger.info("MajorLogin نجح عبر المضيف البديل: %s", url.split("/")[2])
            return LoginSession(
                jwt=jwt,
                server_url=server_url,
                account_id=account_id,
                lock_region=lock_region,
            )
        raise GarenaError(f"MajorLogin فشل على كل المضيفات: {last_err}")

    # ------------------------------------------------------------------
    # 4) إرسال الإعجاب — الصيغة الجديدة (varint — مؤكدة 2026-07-29)
    #    الجسم: 0x08 + varint(uid) + 0x10 + varint(كود المنطقة)
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
            except Exception as exc:  # شبكة/مهلة
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
    # 5) جلب معلومات اللاعب (أفضل جهد)
    # ------------------------------------------------------------------
    async def get_player_info(
        self, session: LoginSession, uid: str, region: str = "ME"
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
        for base in self._like_bases(session, region):
            url = f"{base.rstrip('/')}/GetPlayerPersonalShow"
            try:
                status, raw = await self._post_raw(url, enc, headers)
            except Exception:  # noqa: BLE001
                continue
            if status != 200:
                continue
            likes, nickname = self._extract_player_info(raw)
            return PlayerInfo(uid=str(uid), nickname=nickname, likes=likes)
        return None

    @staticmethod
    def _extract_player_info(raw: bytes) -> Tuple[Optional[int], Optional[str]]:
        """يبحث في الـ protobuf عن حقل liked (21) و nickname (3) مهما كان التداخل."""
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
            return None, None
        return liked, nickname


def _unique(seq: List[str]) -> List[str]:
    """إزالة التكرار مع الحفاظ على الترتيب وتجاهل الفراغات."""
    seen: Dict[str, None] = {}
    for item in seq:
        if item and item not in seen:
            seen[item] = None
    return list(seen)


def _scan_jwt(raw: bytes) -> str:
    """البحث الخام عن JWT داخل الرد (يبدأ بـ eyJhbGci)."""
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
