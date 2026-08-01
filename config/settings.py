"""
إعدادات البوت — كل القيم لها افتراضيات جاهزة.

على Railway تحتاج فقط متغيرين إلزاميين:
    BOT_TOKEN   ← من @BotFather
    ADMIN_ID    ← معرّفك الرقمي من @userinfobot

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
    # ============ Telegram (إلزامي فقط هذين) ============
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_id: int = _get_int("ADMIN_ID", 0)

    # ============ قاعدة البيانات ============
    database_url: str = os.getenv("DATABASE_URL", "")
    # مهلة الاتصال عند الإقلاع (Postgres على Railway قد يحتاج ثواني ليستيقظ)
    db_connect_retries: int = _get_int("DB_CONNECT_RETRIES", 5)
    db_connect_retry_delay: float = _get_float("DB_CONNECT_RETRY_DELAY", 3.0)

    # ============ منطق الإعجابات (اختياري) ============
    max_likes_per_session: int = _get_int("MAX_LIKES_PER_SESSION", 100)
    min_delay_seconds: float = _get_float("MIN_DELAY_SECONDS", 1.0)
    max_delay_seconds: float = _get_float("MAX_DELAY_SECONDS", 3.0)
    request_timeout: int = _get_int("REQUEST_TIMEOUT", 20)
    max_retries: int = _get_int("MAX_RETRIES", 3)
    progress_every: int = _get_int("PROGRESS_EVERY", 10)

    # ============ بروكسيات (اختياري لكنه مفيد) ============
    proxies: List[str] = field(default_factory=lambda: [
        p.strip() for p in os.getenv("PROXIES", "").split(",") if p.strip()
    ])
    proxy_api_url: Optional[str] = os.getenv("PROXY_API_URL") or None
    proxy_refresh_seconds: int = _get_int("PROXY_REFRESH_SECONDS", 300)

    # ============ حدود الاستخدام ============
    rate_limit_hours: float = _get_float("RATE_LIMIT_HOURS", 1.0)

    # ============ أخرى ============
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    @property
    def rate_limit_seconds(self) -> float:
        return self.rate_limit_hours * 3600.0

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token) and self.admin_id != 0 and bool(self.database_url)


settings = Settings()
