"""طبقة قاعدة البيانات (PostgreSQL + asyncpg).

الجديد (أغسطس 2026 — بوابة المستوى):
  • guest_accounts أصبحت «مخزون حسابات حقيقية مساهَم بها» مع:
      level          — المستوى الحقيقي المقروء من بروفايل الحساب
      status         — ok / banned / invalid / low_level / pending
      contributed_by — المستخدم الذي ساهم بالحساب
      last_validated_at — آخر تحقق ناجح من Garena
  • الإتاحة للإعجاب: status='ok' AND level>=min AND لم يُستخدم لنفس الهدف
    خلال like_cooldown_hours (التصفير اليومي عند Garena).
  • جدول contributions لتتبع مساهمات المستخدمين.

الربط عبر متغير البيئة DATABASE_URL فقط.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import asyncpg

from config import settings

logger = logging.getLogger(__name__)

_LOCAL_OR_PLACEHOLDER_HOSTS = {
    "localhost", "127.0.0.1", "::1", "0.0.0.0",
    "host", "hostname", "example.com", "postgres.example.com",
}


def _mask_dsn(dsn: str) -> str:
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
        dsn = self._dsn
        if dsn.startswith("postgres://"):
            dsn = "postgresql://" + dsn[len("postgres://"):]

        self._validate_dsn(dsn)
        await self._connect_with_retry(dsn)
        await self._create_tables()
        logger.info("✅ تم الاتصال بقاعدة بيانات PostgreSQL")

    # ---------------- التحقق والاتصال ----------------
    def _validate_dsn(self, dsn: str) -> None:
        if "${{" in dsn or "}}" in dsn:
            raise SystemExit(
                "⚠️ DATABASE_URL يحتوي مرجع Railway غير مُوسَّع حرفياً: "
                f"{dsn[:60]}\n"
                "لا تنسخ ${{Postgres.DATABASE_URL}} كنص عادي — في تبويب Variables\n"
                "اضغط «Add Variable Reference» واختر Postgres ← DATABASE_URL."
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
                "الحل: أنشئ Postgres في مشروعك (New ← Database ← Add PostgreSQL)\n"
                "ثم اضبط المتغير كمرجع: DATABASE_URL = ${{Postgres.DATABASE_URL}}"
            )

    async def _connect_with_retry(self, dsn: str) -> None:
        retries = max(1, settings.db_connect_retries)
        delay = max(0.5, settings.db_connect_retry_delay)
        host = urlsplit(dsn).hostname or "؟"
        last_error: Optional[BaseException] = None

        for attempt in range(1, retries + 1):
            try:
                self._pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
                return
            except (OSError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < retries:
                    logger.warning(
                        "⚠️ محاولة الاتصال بقاعدة البيانات %d/%d فشلت (%s) — إعادة بعد %.1fs...",
                        attempt, retries, _describe_error(exc), delay,
                    )
                    await asyncio.sleep(delay)
            except asyncpg.PostgresError as exc:
                raise SystemExit(
                    "⚠️ قاعدة البيانات رفضت الاتصال (تحقق من المستخدم/كلمة المرور/اسم القاعدة):\n"
                    f"الرابط: {_mask_dsn(dsn)}\nالخطأ: {exc}"
                ) from exc

        raise SystemExit(
            "⚠️ تعذّر الاتصال بقاعدة بيانات PostgreSQL بعد عدة محاولات.\n"
            f"الرابط (كلمة المرور مخفية): {_mask_dsn(dsn)}\n"
            f"الخطأ الأخير: {_describe_error(last_error)}\n"
            f"المعنى: لا يمكن الوصول إلى المضيف '{host}' من داخل حاوية البوت.\n"
            "الحل على Railway:\n"
            "  1) أنشئ Postgres: New ← Database ← Add PostgreSQL.\n"
            "  2) Variables: DATABASE_URL = ${{Postgres.DATABASE_URL}} (مرجع لا نص).\n"
            "  3) قاعدة في مشروع آخر؟ استخدم الرابط العام من Connect ← Public Networking.\n"
            "  4) Redeploy بعد التصحيح."
        )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

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

    # ---------------- الجداول (مع ترحيل الأعمدة الجديدة) ----------------
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

                CREATE TABLE IF NOT EXISTS contributions (
                    id          BIGSERIAL PRIMARY KEY,
                    user_id     BIGINT NOT NULL,
                    account_uid TEXT NOT NULL,
                    level       INTEGER DEFAULT 0,
                    region      TEXT DEFAULT '',
                    accepted    BOOLEAN NOT NULL DEFAULT FALSE,
                    note        TEXT DEFAULT '',
                    created_at  INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            # ---- ترحيل: أعمدة بوابة المستوى (أغسطس 2026) ----
            await conn.execute(
                """
                ALTER TABLE guest_accounts ADD COLUMN IF NOT EXISTS level INTEGER DEFAULT 0;
                ALTER TABLE guest_accounts ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
                ALTER TABLE guest_accounts ADD COLUMN IF NOT EXISTS contributed_by BIGINT;
                ALTER TABLE guest_accounts ADD COLUMN IF NOT EXISTS last_validated_at INTEGER DEFAULT 0;
                ALTER TABLE guest_accounts ADD COLUMN IF NOT EXISTS note TEXT DEFAULT '';
                ALTER TABLE users ADD COLUMN IF NOT EXISTS contributions INTEGER DEFAULT 0;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS counted_likes INTEGER DEFAULT 0;
                """
            )
            # حسابات قديمة من عصر «التسجيل لكل إعجاب» مستواها 1 — لا تصلح
            # لإرسال الإعجابات بعد بوابة OB51 → أرشفتها مرة واحدة.
            await conn.execute(
                """
                UPDATE guest_accounts
                SET status = 'legacy_low'
                WHERE status = 'pending' AND level = 0 AND contributed_by IS NULL
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_guest_accounts_pool
                    ON guest_accounts(status, region, level);
                CREATE INDEX IF NOT EXISTS idx_used_likes_target
                    ON used_likes(target_uid, used_at);
                CREATE INDEX IF NOT EXISTS idx_contributions_user
                    ON contributions(user_id);
                """
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

    # ---------------- حدود الاستخدام ----------------
    async def can_request(self, user_id: int) -> Tuple[bool, int]:
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

    async def add_likes(self, user_id: int, count: int, counted: int = 0) -> None:
        await self._execute(
            """
            INSERT INTO users (user_id, total_likes, counted_likes, joined_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT(user_id) DO UPDATE SET
                total_likes   = users.total_likes + excluded.total_likes,
                counted_likes = users.counted_likes + excluded.counted_likes
            """,
            user_id, count, counted, int(time.time()),
        )

    # ---------------- إحصائيات وبث ----------------
    async def get_stats(self) -> Dict:
        day_ago = int(time.time()) - 86400
        total_users = await self._fetchval("SELECT COUNT(*) FROM users")
        total_likes = await self._fetchval("SELECT COALESCE(SUM(total_likes),0) FROM users")
        counted_likes = await self._fetchval("SELECT COALESCE(SUM(counted_likes),0) FROM users")
        total_requests = await self._fetchval("SELECT COALESCE(SUM(total_requests),0) FROM users")
        active_24h = await self._fetchval(
            "SELECT COUNT(*) FROM users WHERE last_used_at >= $1", day_ago
        )
        banned = await self._fetchval("SELECT COUNT(*) FROM banned_users")
        return {
            "total_users": total_users,
            "total_likes": total_likes,
            "counted_likes": counted_likes,
            "total_requests": total_requests,
            "active_24h": active_24h,
            "banned": banned,
        }

    async def all_user_ids(self) -> List[int]:
        rows = await self._fetch("SELECT user_id FROM users")
        return [r["user_id"] for r in rows]

    # ---------------- مخزون الحسابات المساهَم بها ----------------
    async def upsert_validated_account(
        self,
        account_uid: str,
        region: str,
        password_hash: str,
        password: str,
        nickname: str,
        level: int,
        status: str,
        contributed_by: Optional[int] = None,
        access_token: str = "",
        open_id: str = "",
        note: str = "",
    ) -> None:
        """يحفظ/يحدّث حساباً بعد التحقق الحقيقي من Garena (مستوى+حالة)."""
        now = int(time.time())
        await self._execute(
            """
            INSERT INTO guest_accounts
                (account_uid, region, password_hash, password, nickname,
                 access_token, open_id, created_at, level, status,
                 contributed_by, last_validated_at, note)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (account_uid) DO UPDATE SET
                region            = excluded.region,
                password_hash     = excluded.password_hash,
                password          = excluded.password,
                nickname          = excluded.nickname,
                access_token      = excluded.access_token,
                open_id           = excluded.open_id,
                level             = excluded.level,
                status            = excluded.status,
                last_validated_at = excluded.last_validated_at,
                note              = excluded.note
            """,
            account_uid, region, password_hash, password, nickname,
            access_token, open_id, now, level, status,
            contributed_by, now, note,
        )

    async def set_account_status(self, account_uid: str, status: str, note: str = "") -> None:
        await self._execute(
            "UPDATE guest_accounts SET status = $1, note = $2 WHERE account_uid = $3",
            status, note, account_uid,
        )

    async def get_available_guest(
        self, region: str, target_uid: str, min_level: Optional[int] = None
    ) -> Optional[Tuple[str, str]]:
        """أفضل حساب متاح للإعجاب: أعلى مستوى وأقل استخداماً، غير مستخدم
        لنفس الهدف خلال فترة التبريد (like_cooldown_hours)."""
        min_level = settings.min_donor_level if min_level is None else min_level
        cooldown_since = int(time.time() - settings.like_cooldown_seconds)
        row = await self._fetchrow(
            """
            SELECT g.account_uid, g.password_hash
            FROM guest_accounts g
            WHERE g.region = $1
              AND g.status = 'ok'
              AND g.level >= $2
              AND NOT EXISTS (
                  SELECT 1 FROM used_likes u
                  WHERE u.account_uid = g.account_uid
                    AND u.target_uid  = $3
                    AND u.used_at     > $4
              )
            ORDER BY g.level DESC, g.last_used_at ASC, g.created_at DESC
            LIMIT 1
            """,
            region, min_level, target_uid, cooldown_since,
        )
        if not row:
            return None
        return row["account_uid"], row["password_hash"]

    async def count_available(self, region: str, target_uid: str) -> int:
        """كم إعجاباً يمكن إرساله فعلياً الآن لهذا الهدف."""
        cooldown_since = int(time.time() - settings.like_cooldown_seconds)
        return await self._fetchval(
            """
            SELECT COUNT(*) FROM guest_accounts g
            WHERE g.region = $1 AND g.status = 'ok' AND g.level >= $2
              AND NOT EXISTS (
                  SELECT 1 FROM used_likes u
                  WHERE u.account_uid = g.account_uid
                    AND u.target_uid = $3 AND u.used_at > $4
              )
            """,
            region, settings.min_donor_level, target_uid, cooldown_since,
        )

    async def mark_guest_used(self, account_uid: str, target_uid: str, region: str) -> None:
        now = int(time.time())
        await self._execute(
            "INSERT INTO used_likes (account_uid, target_uid, region, used_at) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (account_uid, target_uid) DO UPDATE SET used_at = excluded.used_at",
            account_uid, target_uid, region, now,
        )
        await self._execute(
            "UPDATE guest_accounts SET last_used_at = $1 WHERE account_uid = $2",
            now, account_uid,
        )

    async def delete_guest_account(self, account_uid: str) -> None:
        await self._execute(
            "DELETE FROM guest_accounts WHERE account_uid = $1", account_uid
        )

    async def get_any_account_for_read(
        self, region: str
    ) -> Optional[Tuple[str, str]]:
        """أي حساب صالح (ok) من منطقة — لجلسات القراءة فقط (كشف/عداد/بحث)."""
        row = await self._fetchrow(
            """
            SELECT account_uid, password_hash FROM guest_accounts
            WHERE region = $1 AND status = 'ok'
            ORDER BY last_used_at ASC, created_at DESC
            LIMIT 1
            """,
            region,
        )
        if not row:
            return None
        return row["account_uid"], row["password_hash"]

    async def update_account_token(
        self, account_uid: str, access_token: str, open_id: str
    ) -> None:
        """يحدّث التوكن المخزون بعد دخول ناجح (للمراقبة/التشخيص)."""
        await self._execute(
            "UPDATE guest_accounts SET access_token = $1, open_id = $2, "
            "last_validated_at = $3 WHERE account_uid = $4",
            access_token, open_id, int(time.time()), account_uid,
        )

    async def get_account(self, account_uid: str) -> Optional[Dict]:
        row = await self._fetchrow(
            "SELECT * FROM guest_accounts WHERE account_uid = $1", account_uid
        )
        return dict(row) if row else None

    async def guest_stock_count(self, region: Optional[str] = None, ok_only: bool = True) -> int:
        cond = "status = 'ok'" if ok_only else "TRUE"
        if region:
            return await self._fetchval(
                f"SELECT COUNT(*) FROM guest_accounts WHERE region = $1 AND {cond}",
                region,
            )
        return await self._fetchval(f"SELECT COUNT(*) FROM guest_accounts WHERE {cond}")

    async def stock_summary(self) -> List[Dict]:
        """ملخص المخزون الصالح لكل منطقة مع تقسيم المستويات."""
        rows = await self._fetch(
            """
            SELECT region,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE level >= $1) AS high_level,
                   COALESCE(MAX(level), 0) AS max_level,
                   COALESCE(AVG(level), 0)::int AS avg_level
            FROM guest_accounts
            WHERE status = 'ok' AND level >= $2
            GROUP BY region ORDER BY total DESC
            """,
            settings.full_like_level, settings.min_donor_level,
        )
        return [dict(r) for r in rows]

    async def pool_counts(self) -> Dict[str, int]:
        """عدّادات عامة للمخزون حسب الحالة."""
        rows = await self._fetch(
            "SELECT status, COUNT(*) AS c FROM guest_accounts GROUP BY status"
        )
        return {r["status"]: r["c"] for r in rows}

    async def accounts_to_revalidate(self, older_than: int, limit: int = 50) -> List[Dict]:
        """حسابات ok لم تُفحص منذ مدة (لإعادة التحقق الدوري)."""
        rows = await self._fetch(
            """
            SELECT account_uid, region, password_hash FROM guest_accounts
            WHERE status = 'ok' AND last_validated_at < $1
            ORDER BY last_validated_at ASC LIMIT $2
            """,
            older_than, limit,
        )
        return [dict(r) for r in rows]

    # ---------------- المساهمات ----------------
    async def add_contribution(
        self, user_id: int, account_uid: str, level: int, region: str,
        accepted: bool, note: str = "",
    ) -> None:
        await self._execute(
            "INSERT INTO contributions (user_id, account_uid, level, region, accepted, note, created_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            user_id, account_uid, level, region, accepted, note, int(time.time()),
        )
        if accepted:
            await self._execute(
                "UPDATE users SET contributions = COALESCE(contributions,0) + 1 WHERE user_id = $1",
                user_id,
            )
