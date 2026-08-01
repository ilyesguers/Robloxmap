"""طبقة قاعدة البيانات (PostgreSQL + asyncpg) — المستخدمون، الحظر، الإحصائيات، مخزون الحسابات.

الربط عبر متغير البيئة DATABASE_URL فقط (رابط PostgreSQL على Railway).
مثال: postgresql://postgres:password@hostname:port/railway

عند الإقلاع: يُتحقق من شكل الرابط مبكراً، وتُعاد محاولة الاتصال مع تراجع
(أخطاء الشبكة/DNS المؤقتة شائعة أثناء إقلاع Postgres على Railway)، وعند
الفشل النهائي تُطبع رسالة تشخيص واضحة بدل تتبّع مربك.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import asyncpg

from config import settings
from services.garena import SEED_GUEST_ACCOUNTS

logger = logging.getLogger(__name__)

# أسماء مضيفات لا يمكن أن تعمل داخل حاوية Railway إطلاقاً
# (القيم التجريبية في .env.example وعناوين الاسترجاع المحلية).
_LOCAL_OR_PLACEHOLDER_HOSTS = {
    "localhost", "127.0.0.1", "::1", "0.0.0.0",
    "host", "hostname", "example.com", "postgres.example.com",
}


def _mask_dsn(dsn: str) -> str:
    """يعيد الرابط مع إخفاء كلمة المرور (لعرضه آمناً في السجلات)."""
    try:
        parts = urlsplit(dsn)
        host = parts.hostname or "<؟>"
        netloc = host
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        if parts.username:
            netloc = f"{parts.username}:***@{netloc}"
        return f"{parts.scheme}://{netloc}{parts.path or ''}"
    except Exception:
        return "<رابط غير قابل للقراءة>"


def _describe_error(exc: BaseException) -> str:
    """وصف عربي مبسّط لخطأ الاتصال."""
    if isinstance(exc, socket.gaierror):
        return f"تعذّر حلّ اسم المضيف عبر DNS — {exc}"
    if isinstance(exc, ConnectionRefusedError):
        return f"الاتصال مرفوض (لا يوجد Postgres يستمع على هذا العنوان/المنفذ) — {exc}"
    if isinstance(exc, asyncio.TimeoutError):
        return "انتهت مهلة الاتصال بقاعدة البيانات"
    return f"{type(exc).__name__}: {exc}"


class Database:
    def __init__(self, dsn: str = "") -> None:
        self._dsn = dsn or settings.database_url
        self._pool: Optional[asyncpg.Pool] = None

    async def init(self) -> None:
        if not self._dsn:
            raise SystemExit(
                "⚠️ DATABASE_URL غير مضبوط.\n"
                "أضف رابط PostgreSQL في متغيرات البيئة على Railway."
            )
        # دعم صيغ postgres:// و postgresql://
        dsn = self._dsn
        if dsn.startswith("postgres://"):
            dsn = "postgresql://" + dsn[len("postgres://"):]

        self._validate_dsn(dsn)
        await self._connect_with_retry(dsn)

        await self._create_tables()
        # حقن الحسابات الجاهزة (فقط إذا كان المخزون فارغاً)
        await self._seed_accounts()
        logger.info("✅ تم الاتصال بقاعدة بيانات PostgreSQL")

    # ---------------- التحقق والاتصال ----------------
    def _validate_dsn(self, dsn: str) -> None:
        """يرفض القيم الخاطئة البدهية مبكراً برسالة واضحة بدل انهيار DNS مربك."""
        if "${{" in dsn or "}}" in dsn:
            raise SystemExit(
                "⚠️ DATABASE_URL يحتوي مرجع Railway غير مُوسَّع حرفياً: "
                f"{dsn[:60]}\n"
                "لا تنسخ ${{Postgres.DATABASE_URL}} كنص عادي — في تبويب Variables\n"
                "اضغط «Add Variable Reference» واختر Postgres ← DATABASE_URL\n"
                "حتى تستبدلها Railway بالقيمة الفعلية تلقائياً."
            )
        parts = urlsplit(dsn)
        if not parts.scheme.startswith("postgresql") or not parts.hostname:
            raise SystemExit(
                f"⚠️ DATABASE_URL ليس رابط PostgreSQL صالحاً: {_mask_dsn(dsn)}\n"
                "الصيغة المطلوبة: postgresql://user:password@host:port/database"
            )
        host = parts.hostname.lower()
        if host in _LOCAL_OR_PLACEHOLDER_HOSTS:
            raise SystemExit(
                f"⚠️ DATABASE_URL يحتوي مضيفاً لا يعمل على Railway: '{parts.hostname}'\n"
                "يبدو أنك نسخت القيمة التجريبية من .env.example حرفياً.\n"
                "الحل: أنشئ Postgres في مشروعك (New ← Database ← Add PostgreSQL)\n"
                "ثم اضبط المتغير كمرجع: DATABASE_URL = ${{Postgres.DATABASE_URL}}"
            )

    async def _connect_with_retry(self, dsn: str) -> None:
        """ينشئ التجمع مع إعادة محاولة للأخطاء الشبكية (إقلاع Postgres قد يتأخر)."""
        retries = max(1, settings.db_connect_retries)
        delay = max(0.5, settings.db_connect_retry_delay)
        host = urlsplit(dsn).hostname or "؟"
        last_error: Optional[BaseException] = None

        for attempt in range(1, retries + 1):
            try:
                self._pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
                return
            except (OSError, asyncio.TimeoutError) as exc:
                # يشمل socket.gaierror (DNS) وConnectionRefused وانتهاء المهلة
                # وأخطاء اتصال asyncpg (ترث OSError)
                last_error = exc
                if attempt < retries:
                    logger.warning(
                        "⚠️ محاولة الاتصال بقاعدة البيانات %d/%d فشلت (%s) — "
                        "إعادة المحاولة بعد %.1f ثانية...",
                        attempt, retries, _describe_error(exc), delay,
                    )
                    await asyncio.sleep(delay)
            except asyncpg.PostgresError as exc:
                # أخطاء المصادقة/الصلاحيات — إعادة المحاولة لن تفيد
                raise SystemExit(
                    "⚠️ قاعدة البيانات رفضت الاتصال (تحقق من المستخدم/كلمة المرور/اسم القاعدة):\n"
                    f"الرابط: {_mask_dsn(dsn)}\n"
                    f"الخطأ: {exc}"
                ) from exc

        raise SystemExit(
            "⚠️ تعذّر الاتصال بقاعدة بيانات PostgreSQL بعد عدة محاولات.\n"
            f"الرابط (كلمة المرور مخفية): {_mask_dsn(dsn)}\n"
            f"الخطأ الأخير: {_describe_error(last_error)}\n\n"
            f"المعنى: لا يمكن الوصول إلى المضيف '{host}' من داخل حاوية البوت.\n"
            "الحل على Railway:\n"
            "  1) تأكد أن مشروعك يحتوي Postgres: New ← Database ← Add PostgreSQL.\n"
            "  2) في خدمة البوت ← Variables اضبط:\n"
            "       DATABASE_URL = ${{Postgres.DATABASE_URL}}\n"
            "     عبر «Add Variable Reference» (وليس نسخاً نصياً).\n"
            "  3) إن كانت القاعدة في مشروع آخر: استخدم الرابط العام من تبويب\n"
            "     Connect ← Public Networking (المضيف بالشكل *.proxy.rlwy.net مع منفذه).\n"
            "  4) Redeploy بعد التصحيح."
        )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    def _conn(self):
        """يعيد سياق الاتصال من التجمع."""
        return self._pool.acquire()

    async def _execute(self, query: str, *args) -> str:
        async with self._pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def _fetch(self, query: str, *args) -> List[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def _fetchrow(self, query: str, *args) -> Optional[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def _fetchval(self, query: str, *args) -> Any:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def _create_tables(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id       BIGINT PRIMARY KEY,
                    username      TEXT,
                    first_name    TEXT,
                    joined_at     INTEGER NOT NULL,
                    last_used_at  INTEGER DEFAULT 0,
                    total_requests INTEGER DEFAULT 0,
                    total_likes   INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS banned_users (
                    user_id   BIGINT PRIMARY KEY,
                    reason    TEXT,
                    banned_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS guest_accounts (
                    account_uid   TEXT PRIMARY KEY,
                    region        TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    password      TEXT NOT NULL DEFAULT '',
                    nickname      TEXT DEFAULT '',
                    access_token  TEXT DEFAULT '',
                    open_id       TEXT DEFAULT '',
                    created_at    INTEGER NOT NULL DEFAULT 0,
                    last_used_at  INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS used_likes (
                    account_uid TEXT NOT NULL,
                    target_uid  TEXT NOT NULL,
                    region      TEXT NOT NULL,
                    used_at     INTEGER NOT NULL,
                    PRIMARY KEY (account_uid, target_uid)
                );
                """
            )
            # إنشاء الفهارس (IF NOT EXISTS)
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_guest_accounts_region
                    ON guest_accounts(region);
                CREATE INDEX IF NOT EXISTS idx_used_likes_target
                    ON used_likes(target_uid);
                """
            )

    async def _seed_accounts(self) -> None:
        count = await self._fetchval("SELECT COUNT(*) AS c FROM guest_accounts")
        if count == 0 and SEED_GUEST_ACCOUNTS:
            for region, accounts in SEED_GUEST_ACCOUNTS.items():
                for acc in accounts:
                    await self._execute(
                        "INSERT INTO guest_accounts (account_uid, region, password_hash, password, created_at) "
                        "VALUES ($1, $2, $3, '', $4) ON CONFLICT DO NOTHING",
                        acc["uid"], region, acc["password_hash"], int(time.time()),
                    )

    # ---------------- المستخدمون ----------------
    async def register_user(
        self, user_id: int, username: Optional[str], first_name: Optional[str]
    ) -> None:
        await self._execute(
            """
            INSERT INTO users (user_id, username, first_name, joined_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name
            """,
            user_id, username, first_name, int(time.time()),
        )

    async def user_info(self, user_id: int) -> Optional[Dict]:
        row = await self._fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
        return dict(row) if row else None

    # ---------------- الحظر ----------------
    async def is_banned(self, user_id: int) -> bool:
        row = await self._fetchrow(
            "SELECT 1 FROM banned_users WHERE user_id = $1", user_id
        )
        return row is not None

    async def ban_user(self, user_id: int, reason: str = "") -> None:
        await self._execute(
            "INSERT INTO banned_users (user_id, reason, banned_at) VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id) DO UPDATE SET reason = excluded.reason, banned_at = excluded.banned_at",
            user_id, reason, int(time.time()),
        )

    async def unban_user(self, user_id: int) -> bool:
        result = await self._execute(
            "DELETE FROM banned_users WHERE user_id = $1", user_id
        )
        return "DELETE 1" in result

    # ---------------- حدود الاستخدام (مرة كل ساعة) ----------------
    async def can_request(self, user_id: int) -> Tuple[bool, int]:
        """يعيد (مسموح?, الثواني المتبقية من الانتظار)."""
        now = int(time.time())
        row = await self._fetchrow(
            "SELECT last_used_at FROM users WHERE user_id = $1", user_id
        )
        if row is None:
            return True, 0
        elapsed = now - row["last_used_at"]
        if elapsed < settings.rate_limit_seconds:
            return False, int(settings.rate_limit_seconds - elapsed)
        return True, 0

    async def mark_request_started(self, user_id: int) -> None:
        now = int(time.time())
        await self._execute(
            """
            INSERT INTO users (user_id, last_used_at, total_requests, joined_at)
            VALUES ($1, $2, 1, $3)
            ON CONFLICT(user_id) DO UPDATE SET
                last_used_at    = excluded.last_used_at,
                total_requests  = users.total_requests + 1
            """,
            user_id, now, now,
        )

    async def add_likes(self, user_id: int, count: int) -> None:
        await self._execute(
            """
            INSERT INTO users (user_id, total_likes, joined_at)
            VALUES ($1, $2, $3)
            ON CONFLICT(user_id) DO UPDATE SET
                total_likes = users.total_likes + excluded.total_likes
            """,
            user_id, count, int(time.time()),
        )

    # ---------------- إحصائيات وبث ----------------
    async def get_stats(self) -> Dict:
        day_ago = int(time.time()) - 86400
        total_users = await self._fetchval("SELECT COUNT(*) FROM users")
        total_likes = await self._fetchval("SELECT COALESCE(SUM(total_likes),0) FROM users")
        total_requests = await self._fetchval("SELECT COALESCE(SUM(total_requests),0) FROM users")
        active_24h = await self._fetchval(
            "SELECT COUNT(*) FROM users WHERE last_used_at >= $1", day_ago
        )
        banned = await self._fetchval("SELECT COUNT(*) FROM banned_users")
        return {
            "total_users": total_users,
            "total_likes": total_likes,
            "total_requests": total_requests,
            "active_24h": active_24h,
            "banned": banned,
        }

    async def all_user_ids(self) -> List[int]:
        rows = await self._fetch("SELECT user_id FROM users")
        return [r["user_id"] for r in rows]

    # ---------------- مخزون حسابات الضيوف الجاهزة ----------------
    async def save_guest_account(
        self,
        account_uid: str,
        region: str,
        password_hash: str,
        password: str = "",
        nickname: str = "",
        access_token: str = "",
        open_id: str = "",
    ) -> None:
        """يحفظ حساب ضيف في المخزون (مستخدم جديد سُجّل بنجاح)."""
        await self._execute(
            """
            INSERT INTO guest_accounts
                (account_uid, region, password_hash, password, nickname, access_token, open_id, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (account_uid) DO UPDATE SET
                password_hash = excluded.password_hash,
                password      = excluded.password,
                nickname      = excluded.nickname,
                access_token  = excluded.access_token,
                open_id       = excluded.open_id
            """,
            account_uid, region, password_hash, password, nickname, access_token, open_id, int(time.time()),
        )

    async def update_account_token(
        self, account_uid: str, access_token: str, open_id: str
    ) -> None:
        """يحدّث التوكن ل حساب موجود."""
        await self._execute(
            "UPDATE guest_accounts SET access_token = $1, open_id = $2 WHERE account_uid = $3",
            access_token, open_id, account_uid,
        )

    async def get_available_guest(
        self, region: str, target_uid: str
    ) -> Optional[Tuple[str, str, str]]:
        """يعيد (uid, password_hash, password) لحساب جاهز غير مستخدم لهذا الهدف، أو None.

        كل حساب يصلح لإعجاب واحد فقط لنفس الهدف: يُستثنى أي حساب مسجَّل
        في used_likes لنفس (account_uid, target_uid).
        """
        row = await self._fetchrow(
            """
            SELECT g.account_uid, g.password_hash, g.password
            FROM guest_accounts g
            WHERE g.region = $1
              AND NOT EXISTS (
                  SELECT 1 FROM used_likes u
                  WHERE u.account_uid = g.account_uid AND u.target_uid = $2
              )
            ORDER BY g.created_at DESC
            LIMIT 1
            """,
            region, target_uid,
        )
        if not row:
            return None
        return row["account_uid"], row["password_hash"], row["password"]

    async def mark_guest_used(self, account_uid: str, target_uid: str, region: str) -> None:
        """يعلّم حساباً بأنه استُخدم لإعجاب على هدف معيّن (إعجاب واحد لكل هدف)."""
        await self._execute(
            "INSERT INTO used_likes (account_uid, target_uid, region, used_at) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
            account_uid, target_uid, region, int(time.time()),
        )

    async def delete_guest_account(self, account_uid: str) -> None:
        """يحذف حساباً جاهزاً من المخزون (مثلاً بعد رد auth_error من Garena)."""
        await self._execute(
            "DELETE FROM guest_accounts WHERE account_uid = $1", account_uid
        )

    async def guest_stock_count(self, region: Optional[str] = None) -> int:
        """عدد الحسابات الجاهزة المتبقية (كلها أو لمنطقة محددة)."""
        if region:
            return await self._fetchval(
                "SELECT COUNT(*) FROM guest_accounts WHERE region = $1", region
            )
        return await self._fetchval("SELECT COUNT(*) FROM guest_accounts")

    async def guest_stock_by_region(self) -> Dict[str, int]:
        """عدد الحسابات الجاهزة لكل منطقة."""
        rows = await self._fetch(
            "SELECT region, COUNT(*) AS cnt FROM guest_accounts GROUP BY region ORDER BY region"
        )
        return {r["region"]: r["cnt"] for r in rows}

    # ---------------- استخراج التوكنات (للأدمن فقط) ----------------
    async def get_all_accounts(self) -> List[Dict]:
        """يعيد كل الحسابات مع بياناتها الكاملة (للأدمن)."""
        rows = await self._fetch(
            "SELECT account_uid, region, password, password_hash, nickname, "
            "access_token, open_id, created_at, last_used_at "
            "FROM guest_accounts ORDER BY created_at DESC"
        )
        return [dict(r) for r in rows]

    async def get_accounts_by_region(self, region: str) -> List[Dict]:
        """يعيد حسابات منطقة محددة."""
        rows = await self._fetch(
            "SELECT account_uid, region, password, password_hash, nickname, "
            "access_token, open_id, created_at, last_used_at "
            "FROM guest_accounts WHERE region = $1 ORDER BY created_at DESC",
            region,
        )
        return [dict(r) for r in rows]

    async def get_accounts_for_token_grant(self, limit: int = 50) -> List[Dict]:
        """يعيد حسابات تحتاج لتحديث التوكن (للأدمن)."""
        rows = await self._fetch(
            "SELECT account_uid, region, password, password_hash, nickname, "
            "access_token, open_id, created_at, last_used_at "
            "FROM guest_accounts ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]
