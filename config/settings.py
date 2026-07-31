"""
إعدادات البوت — كل القيم تُقرأ من متغيرات البيئة (Environment Variables)
حتى نتمكن من تغييرها على Railway أو أي سيرفر بدون تعديل الكود.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # ================= Telegram =================
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_id: int = _get_int("ADMIN_ID", 0)

    # ================= قاعدة البيانات =================
    db_path: str = os.getenv("DB_PATH", "data/bot.db")

    # ================= Garena Endpoints =================
    # ⚠️ كل الروابط التالية PLACEHOLDERS — يجب استبدالها بالروابط الفعلية
    # الحالية من عميل اللعبة (تتغير باستمرار وتختلف حسب المنطقة).
    guest_register_url: str = os.getenv(
        "GUEST_REGISTER_URL", "https://YOUR_HOST.garena.com/api/guest/register"
    )
    guest_login_url: str = os.getenv(
        "GUEST_LOGIN_URL", "https://YOUR_HOST.garena.com/api/guest/login"
    )
    like_url: str = os.getenv(
        "LIKE_URL", "https://YOUR_HOST.garena.com/api/like/send"
    )
    app_id: str = os.getenv("APP_ID", "100026")  # معرّف تطبيق FF عند Garena (placeholder)
    sign_secret: str = os.getenv("SIGN_SECRET", "")  # سر التوقيع (placeholder)
    api_lang: str = os.getenv("API_LANG", "en")

    # ================= منطق الإعجابات =================
    max_likes_per_session: int = _get_int("MAX_LIKES_PER_SESSION", 100)
    likes_per_guest: int = _get_int("LIKES_PER_GUEST", 1)
    min_delay_seconds: float = _get_float("MIN_DELAY_SECONDS", 2.0)
    max_delay_seconds: float = _get_float("MAX_DELAY_SECONDS", 6.0)
    request_timeout: int = _get_int("REQUEST_TIMEOUT", 20)
    max_retries: int = _get_int("MAX_RETRIES", 3)
    progress_every: int = _get_int("PROGRESS_EVERY", 10)

    # ================= بروكسيات =================
    # بروكسيات ثابتة مفصولة بفواصل: http://user:pass@host:port أو socks5://...
    proxies: List[str] = field(default_factory=lambda: [
        p.strip() for p in os.getenv("PROXIES", "").split(",") if p.strip()
    ])
    # رابط API يسحب قائمة بروكسيات دوّارة (Webshare / ProxyLite / ...)
    proxy_api_url: Optional[str] = os.getenv("PROXY_API_URL") or None
    proxy_refresh_seconds: int = _get_int("PROXY_REFRESH_SECONDS", 300)

    # ================= حدود الاستخدام =================
    rate_limit_hours: float = _get_float("RATE_LIMIT_HOURS", 1.0)

    # ================= أخرى =================
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    @property
    def rate_limit_seconds(self) -> float:
        return self.rate_limit_hours * 3600.0

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token) and self.admin_id != 0


settings = Settings()
