"""طبقة قاعدة البيانات (SQLite + aiosqlite) — المستخدمون، الحظر، الإحصائيات."""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, List, Optional, Tuple

import aiosqlite

from config import settings
from services.garena import SEED_GUEST_ACCOUNTS


class Database:
    def __init__(self, path: str = settings.db_path) -> None:
        self.path = path

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        async with self._conn() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id       INTEGER PRIMARY KEY,
                    username      TEXT,
                    first_name    TEXT,
                    joined_at     INTEGER NOT NULL,
                    last_used_at  INTEGER DEFAULT 0,
                    total_requests INTEGER DEFAULT 0,
                    total_likes   INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS banned_users (
                    user_id   INTEGER PRIMARY KEY,
                    reason    TEXT,
                    banned_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS guest_accounts (
                    account_uid   TEXT PRIMARY KEY,
                    region        TEXT NOT NULL,
                    password_hash TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS used_likes (
                    account_uid TEXT NOT NULL,
                    target_uid  TEXT NOT NULL,
                    region      TEXT NOT NULL,
                    used_at     INTEGER NOT NULL,
                    PRIMARY KEY (account_uid, target_uid)
                );
                CREATE INDEX IF NOT EXISTS idx_guest_accounts_region
                    ON guest_accounts(region);
                CREATE INDEX IF NOT EXISTS idx_used_likes_target
                    ON used_likes(target_uid);
                """
            )
            # حقن الحسابات الجاهزة عند أول تشغيل (فقط إذا كان المخزون فارغاً)
            cur = await db.execute("SELECT COUNT(*) AS c FROM guest_accounts")
            row = await cur.fetchone()
            if row["c"] == 0:
                for region, accounts in SEED_GUEST_ACCOUNTS.items():
                    for acc in accounts:
                        await db.execute(
                            "INSERT OR IGNORE INTO guest_accounts "
                            "(account_uid, region, password_hash) VALUES (?, ?, ?)",
                            (acc["uid"], region, acc["password_hash"]),
                        )
            await db.commit()

    # ---------------- المستخدمون ----------------
    async def register_user(
        self, user_id: int, username: Optional[str], first_name: Optional[str]
    ) -> None:
        async with self._conn() as db:
            await db.execute(
                """
                INSERT INTO users (user_id, username, first_name, joined_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username   = excluded.username,
                    first_name = excluded.first_name
                """,
                (user_id, username, first_name, int(time.time())),
            )
            await db.commit()

    async def user_info(self, user_id: int) -> Optional[Dict]:
        async with self._conn() as db:
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
        return dict(row) if row else None

    # ---------------- الحظر ----------------
    async def is_banned(self, user_id: int) -> bool:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
        return row is not None

    async def ban_user(self, user_id: int, reason: str = "") -> None:
        async with self._conn() as db:
            await db.execute(
                "INSERT OR REPLACE INTO banned_users (user_id, reason, banned_at) VALUES (?, ?, ?)",
                (user_id, reason, int(time.time())),
            )
            await db.commit()

    async def unban_user(self, user_id: int) -> bool:
        async with self._conn() as db:
            cur = await db.execute(
                "DELETE FROM banned_users WHERE user_id = ?", (user_id,)
            )
            await db.commit()
            return cur.rowcount > 0

    # ---------------- حدود الاستخدام (مرة كل ساعة) ----------------
    async def can_request(self, user_id: int) -> Tuple[bool, int]:
        """يعيد (مسموح?, الثواني المتبقية من الانتظار)."""
        now = int(time.time())
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT last_used_at FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
        if row is None:
            return True, 0
        elapsed = now - row["last_used_at"]
        if elapsed < settings.rate_limit_seconds:
            return False, int(settings.rate_limit_seconds - elapsed)
        return True, 0

    async def mark_request_started(self, user_id: int) -> None:
        """Upsert: يعمل حتى لو المستخدم لم يضغط /start بعد (سجله يُنشأ تلقائياً)."""
        async with self._conn() as db:
            await db.execute(
                """
                INSERT INTO users (user_id, last_used_at, total_requests, joined_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_used_at    = excluded.last_used_at,
                    total_requests  = users.total_requests + 1
                """,
                (int(time.time()), user_id, int(time.time())),
            )
            await db.commit()

    async def add_likes(self, user_id: int, count: int) -> None:
        async with self._conn() as db:
            await db.execute(
                """
                INSERT INTO users (user_id, total_likes, joined_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    total_likes = users.total_likes + excluded.total_likes
                """,
                (user_id, count, int(time.time())),
            )
            await db.commit()

    # ---------------- إحصائيات وبث ----------------
    async def get_stats(self) -> Dict:
        day_ago = int(time.time()) - 86400
        async with self._conn() as db:
            cur = await db.execute("SELECT COUNT(*) AS c FROM users")
            total_users = (await cur.fetchone())["c"]
            cur = await db.execute("SELECT COALESCE(SUM(total_likes),0) AS s FROM users")
            total_likes = (await cur.fetchone())["s"]
            cur = await db.execute("SELECT COALESCE(SUM(total_requests),0) AS s FROM users")
            total_requests = (await cur.fetchone())["s"]
            cur = await db.execute(
                "SELECT COUNT(*) AS c FROM users WHERE last_used_at >= ?", (day_ago,)
            )
            active_24h = (await cur.fetchone())["c"]
            cur = await db.execute("SELECT COUNT(*) AS c FROM banned_users")
            banned = (await cur.fetchone())["c"]
        return {
            "total_users": total_users,
            "total_likes": total_likes,
            "total_requests": total_requests,
            "active_24h": active_24h,
            "banned": banned,
        }

    async def all_user_ids(self) -> List[int]:
        async with self._conn() as db:
            cur = await db.execute("SELECT user_id FROM users")
            rows = await cur.fetchall()
        return [r["user_id"] for r in rows]

    # ---------------- مخزون حسابات الضيوف الجاهزة ----------------
    async def save_guest_account(self, account_uid: str, region: str, password_hash: str) -> None:
        """يحفظ حساب ضيف في المخزون (مستخدم جديد سُجّل بنجاح)."""
        async with self._conn() as db:
            await db.execute(
                "INSERT OR IGNORE INTO guest_accounts (account_uid, region, password_hash) "
                "VALUES (?, ?, ?)",
                (account_uid, region, password_hash),
            )
            await db.commit()

    async def get_available_guest(
        self, region: str, target_uid: str
    ) -> Optional[Tuple[str, str]]:
        """يعيد (uid, password_hash) لحساب جاهز غير مستخدم لهذا الهدف، أو None.

        كل حساب يصلح لإعجاب واحد فقط لنفس الهدف: يُستثنى أي حساب مسجَّل
        في used_likes لنفس (account_uid, target_uid).
        """
        async with self._conn() as db:
            cur = await db.execute(
                """
                SELECT g.account_uid, g.password_hash
                FROM guest_accounts g
                WHERE g.region = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM used_likes u
                      WHERE u.account_uid = g.account_uid AND u.target_uid = ?
                  )
                ORDER BY g.account_uid
                LIMIT 1
                """,
                (region, target_uid),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return row["account_uid"], row["password_hash"]

    async def mark_guest_used(self, account_uid: str, target_uid: str, region: str) -> None:
        """يعلّم حساباً بأنه استُخدم لإعجاب على هدف معيّن (إعجاب واحد لكل هدف)."""
        async with self._conn() as db:
            await db.execute(
                "INSERT OR IGNORE INTO used_likes (account_uid, target_uid, region, used_at) "
                "VALUES (?, ?, ?, ?)",
                (account_uid, target_uid, region, int(time.time())),
            )
            await db.commit()
