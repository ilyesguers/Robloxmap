"""
عميل Garena الحقيقي — الطريقة الشغالة الحالية (OB53).

التدفق (مطابق تماماً لعميل المكتبة الشغالة @spinzaf/freefire-api — أبريل 2026):

  1) تسجيل حساب ضيف جديد (Guest Register):
     POST https://ffmconnect.live.gop.garenanow.com/oauth/guest/register
     (form) password=SHA256(كلمة سر) & client_type=2 & source=2 & app_id=100067
     التوقيع: HMAC-SHA256(client_secret, جسم الطلب) في header Authorization: Signature ...

  2) منح توكن (Token Grant):
     POST .../oauth/guest/token/grant  →  access_token + open_id

  3) إنشاء الحساب داخل اللعبة (Major Register):
     POST https://loginbp.ggblueshark.com/MajorRegister
     جسم = Protobuf مشفّر AES-128-CBC (مفتاح وIV ثابتان معروفان)

  4) تسجيل الدخول (Major Login) → JWT:
     POST https://loginbp.ggblueshark.com/MajorLogin
     جسم = Protobuf (openid=22, logintoken=29, platform=99) مشفّر AES-128-CBC
     الرد = Protobuf → token(JWT) + serverUrl

  5) إرسال الإعجاب:
     POST {serverUrl}/LikeProfile
     جسم = Protobuf (الهدف uid كـ string في الحقل 1، المنطقة في الحقل 2) مشفّر AES

  6) التحقق من عدد الإعجابات:
     POST {serverUrl}/GetPlayerPersonalShow → حقل liked = 21

⚠️ ملاحظة أمان: كل المفاتيح والروابط هنا ثابتة في الكود حتى لا تحتاج
أي متغيرات بيئة إضافية على Railway (يكفي BOT_TOKEN و ADMIN_ID).
إذا غيّرت Garena هذه القيم في تحديث مستقبلي، عدّلها من أعلى هذا الملف فقط.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp
from Crypto.Cipher import AES

from config import settings

logger = logging.getLogger(__name__)

# ==========================================================================
# الثوابت الحقيقية — OB53 (مطابقة للعميل الشغال الحالي)
# ==========================================================================
AES_KEY = b"Yg&tc%DEuh6%Zc^8"          # مفتاح AES-128-CBC
AES_IV = b"6oyZDr22E3ychjM%"           # IV ثابت

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

# سيرفرات اللعبة حسب المنطقة (تُستخدم كاحتياط إذا لم يرسل MajorLogin serverUrl)
SERVER_BASE: Dict[str, str] = {
    "IND": "https://client.ind.freefiremobile.com",
    "BR": "https://client.us.freefiremobile.com",
    "US": "https://client.us.freefiremobile.com",
    "SAC": "https://client.us.freefiremobile.com",
    "NA": "https://client.us.freefiremobile.com",
}
DEFAULT_SERVER = "https://clientbp.ggblueshark.com"

# المناطق التي تدعم تسجيل الحسابات الضيفية (حسب أحدث عميل شغال)
GUEST_REGIONS: List[str] = ["IND", "SG", "BR", "US", "RU", "TH", "VN", "TW", "ME", "CIS", "BD"]

# ==========================================================================
# مخزون الحسابات الجاهزة — تُستخدم عندما يرفض Garena تسجيل ضيف جديد من سيرفر
# Railway (خطأ error_not_found 1005). كل منطقة لها حساب جاهز (uid + password_hash).
# كل حساب يصلح لإعجاب واحد فقط لنفس الهدف (بعد الاستخدام يُعلَّم كمستخدم).
# ==========================================================================
SEED_GUEST_ACCOUNTS: Dict[str, List[Dict[str, str]]] = {
    "IND": [{"uid": "4104125669", "password_hash": "E5655A0D14EF812A908726152BDD38021BEF528801AA42B16CFA4ED67141C4CA"}],
    "SG": [{"uid": "3158350464", "password_hash": "70EA041FCF79190E3D0A8F3CA95CAAE1F39782696CE9D85C2CCD525E28D223FC"}],
    "RU": [{"uid": "3301239795", "password_hash": "DD40EE772FCBD61409BB15033E3DE1B1C54EDA83B75DF0CDD24C34C7C8798475"}],
    "ID": [{"uid": "3301269321", "password_hash": "D11732AC9BBED0DED65D0FED7728CA8DFF408E174202ECF1939E328EA3E94356"}],
    "TW": [{"uid": "3301329477", "password_hash": "359FB179CD92C9C1A2A917293666B96972EF8A5FC43B5D9D61A2434DD3D7D0BC"}],
    "US": [{"uid": "3301387397", "password_hash": "BAC03CCF677F8772473A09870B6228ADFBC1F503BF59C8D05746DE451AD67128"}],
    "VN": [{"uid": "3301447047", "password_hash": "044714F5B9284F3661FB09E4E9833327488B45255EC9E0CCD953050E3DEF1F54"}],
    "TH": [{"uid": "3301470613", "password_hash": "39EFD9979BD6E9CCF6CBFF09F224C4B663E88B7093657CB3D4A6F3615DDE057A"}],
    "ME": [{"uid": "3301535568", "password_hash": "BEC9F99733AC7B1FB139DB3803F90A7E78757B0BE395E0A6FE3A520AF77E0517"}],
    "PK": [{"uid": "3301828218", "password_hash": "3A0E972E57E9EDC39DC4830E3D486DBFB5DA7C52A4E8B0B8F3F9DC4450899571"}],
    "CIS": [{"uid": "3309128798", "password_hash": "412F68B618A8FAEDCCE289121AC4695C0046D2E45DB07EE512B4B3516DDA8B0F"}],
    "BR": [{"uid": "3158668455", "password_hash": "44296D19343151B25DE68286BDC565904A0DA5A5CC5E96B7A7ADBE7C11E07933"}],
}

# كلمات مفتاحية تدل على وصول حد الإعجابات اليومي في ردود السيرفر
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
# أدوات Protobuf (ترميز يدوي — بسيط وسريع بدون مكتبات خارجية)
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
    """فك Protobuf بسيط: كل حقل → قائمة قيمه (varint أو bytes)."""
    fields: Dict[int, List[Any]] = {}
    i = 0
    while i < len(data):
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
        """POST برسالة form-urlencoded ويعيد JSON."""
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
        password = str(random.randint(10**9, 10**10 - 1))
        password_hash = hashlib.sha256(password.encode()).hexdigest().upper()
        nick = nickname or f"Liker{random.randint(1000, 9999)}"

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
        resp = await self._post_form(URL_GUEST_REGISTER, params, headers)
        data = resp.get("json", {})
        uid = _deep_get(data, "uid", "data.uid")
        if not uid or resp.get("status", 0) != 200:
            raise GarenaError(
                f"فشل تسجيل حساب ضيف ({region}): {str(resp.get('raw') or data)[:200]}"
            )
        uid = str(uid)

        # ---- 1.2 Token Grant ----
        access_token, open_id = await self.token_grant(uid, password_hash)

        # ---- 1.3 Major Register (إنشاء الحساب كاملاً: nickname + region) ----
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
    # 2) منح التوكن (لحساب ضيف موجود)
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
    # 3) تسجيل الدخول (Major Login) → JWT + serverUrl
    # ------------------------------------------------------------------
    async def major_login(self, access_token: str, open_id: str) -> LoginSession:
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
            raise GarenaError(f"MajorLogin فشل (HTTP {status}): {raw[:100]!r}")

        fields = _parse_protobuf(raw)
        jwt = _bytes_to_str(fields.get(8, [None])[0])
        if not jwt:
            raise GarenaError("MajorLogin لم يعد JWT — الاستجابة غير متوقعة")

        server_url = _bytes_to_str(fields.get(10, [None])[0]) or self._base_server()
        account_id = next((v for v in fields.get(1, []) if isinstance(v, int)), None)
        lock_region = _bytes_to_str(fields.get(2, [None])[0]) or ""
        return LoginSession(
            jwt=jwt, server_url=server_url, account_id=account_id, lock_region=lock_region
        )

    # ------------------------------------------------------------------
    # 4) إرسال الإعجاب
    # ------------------------------------------------------------------
    async def send_like(
        self, session: LoginSession, target_uid: str, region: str
    ) -> LikeResult:
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
            return LikeResult(success=True)

        text = raw.decode("utf-8", errors="ignore").lower()
        if any(k in text for k in LIMIT_KEYWORDS) or status in (403, 429):
            return LikeResult(
                success=False,
                limit_reached=True,
                message=f"HTTP {status}: {text[:120]}",
            )
        return LikeResult(success=False, message=f"HTTP {status}: {text[:120]}")

    # ------------------------------------------------------------------
    # 5) جلب معلومات اللاعب (للتحقق من أن عدد الإعجابات زاد فعلاً)
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
        """يبحث في الـ protobuf عن حقل liked (21) و nickname (3) مهما كان التداخل."""
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
    # مساعدات
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
