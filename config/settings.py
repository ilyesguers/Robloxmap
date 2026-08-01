"""
إعدادات البوت — كل القيم لها افتراضيات جاهزة.

على Railway تحتاج فقط متغيرين إلزاميين:
    BOT_TOKEN   ← من @BotFather
    ADMIN_ID    ← معرّفك الرقمي من @userinfobot
    DATABASE_URL ← مرجع Postgres في مشروعك

كل ما عدا ذلك اختياري (له قيم افتراضية تعمل فوراً).
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
    # ============ Telegram (إلزامي فقط هذان) ============
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_id: int = _get_int("ADMIN_ID", 0)

    # ============ قاعدة البيانات ============
    database_url: str = os.getenv("DATABASE_URL", "")
    db_connect_retries: int = _get_int("DB_CONNECT_RETRIES", 5)
    db_connect_retry_delay: float = _get_float("DB_CONNECT_RETRY_DELAY", 3.0)

    # ============ منطق الإعجابات (اختياري) ============
    max_likes_per_session: int = _get_int("MAX_LIKES_PER_SESSION", 100)
    min_delay_seconds: float = _get_float("MIN_DELAY_SECONDS", 1.0)
    max_delay_seconds: float = _get_float("MAX_DELAY_SECONDS", 3.0)
    request_timeout: int = _get_int("REQUEST_TIMEOUT", 20)
    max_retries: int = _get_int("MAX_RETRIES", 3)
    progress_every: int = _get_int("PROGRESS_EVERY", 10)

    # ============ بوابة المستوى (OB51+ — إلزامية من Garena) ============
    # منذ تحديث OB51 (أبريل 2026) لا تُحتسب الإعجابات إلا من حسابات
    # بمستوى ≥ 8، والحسابات بمستوى 8-20 تُحتسب ~20 إعجاباً يومياً فقط
    # للهدف الواحد؛ العدد الكامل (~100) يتطلب مرسلين بمستوى 22+.
    min_donor_level: int = _get_int("MIN_DONOR_LEVEL", 8)
    full_like_level: int = _get_int("FULL_LIKE_LEVEL", 22)
    # كل حساب يستطيع الإعجاب بنفس الهدف مرة واحدة كل فترة (تصفير يومي تقريباً)
    like_cooldown_hours: float = _get_float("LIKE_COOLDOWN_HOURS", 20.0)
    # كم إعجاباً متتالياً بدون زيادة في العداد قبل إعلان التجاهل والتوقف المبكر
    stall_window: int = _get_int("STALL_WINDOW", 15)
    # كل كم إعجاب مرسل نعيد قراءة العداد للتحقق الحي
    verify_every: int = _get_int("VERIFY_EVERY", 5)

    # ============ جلسات القراءة (كشف المنطقة/العداد) ============
    reader_session_ttl: int = _get_int("READER_SESSION_TTL", 600)

    # ============ بروكسيات (اختياري لكنه مفيد) ============
    proxies: List[str] = field(default_factory=lambda: [
        p.strip() for p in os.getenv("PROXIES", "").split(",") if p.strip()
    ])
    proxy_api_url: Optional[str] = os.getenv("PROXY_API_URL") or None
    proxy_refresh_seconds: int = _get_int("PROXY_REFRESH_SECONDS", 300)

    # ============ حدود الاستخدام ============
    rate_limit_hours: float = _get_float("RATE_LIMIT_HOURS", 1.0)
    # حد أسطر المساهمة الواحدة للمستخدم (uid:password لكل سطر)
    donate_max_lines: int = _get_int("DONATE_MAX_LINES", 5)

    # ============ أخرى ============
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    @property
    def rate_limit_seconds(self) -> float:
        return self.rate_limit_hours * 3600.0

    @property
    def like_cooldown_seconds(self) -> float:
        return self.like_cooldown_hours * 3600.0

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token) and self.admin_id != 0 and bool(self.database_url)


settings = Settings()
