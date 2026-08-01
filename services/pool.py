"""وحدة المخزون — التحقق من الحسابات المساهَم بها وتخزينها.

المساهمة: المستخدم يرسل «uid:password» (كلمة السر نصاً أو هاش SHA256).
التحقق حقيقي 100%:
  1) Token Grant      → بيانات الدخول صحيحة؟
  2) MajorLogin        → الحساب غير محظور؟ (كشف الحقل 12)
  3) GetPlayerPersonalShow (للحساب نفسه) → المستوى/المنطقة/الاسم الحقيقي
لا يُقبل إلا مستوى ≥ MIN_DONOR_LEVEL (بوابة OB51+).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Tuple

from config import settings
from services.database import Database
from services.garena import (
    AccountBannedError,
    GarenaClient,
    GarenaError,
    password_to_hash,
)

logger = logging.getLogger(__name__)

_LINE_RE = re.compile(r"^\s*(\d{6,12})\s*[:;\s]\s*(\S+)\s*$")


@dataclass
class DonationResult:
    uid: str
    ok: bool
    level: int = 0
    region: str = ""
    nickname: str = ""
    reason: str = ""

    def line(self) -> str:
        if self.ok:
            return (
                f"✅ <code>{self.uid}</code> — «{self.nickname or '؟'}» "
                f"مستوى <b>{self.level}</b> ({self.region}) — أُضيف للمخزون"
            )
        return f"❌ <code>{self.uid}</code> — {self.reason}"


def parse_donation_lines(text: str, max_lines: int) -> Tuple[List[Tuple[str, str]], List[str]]:
    """يفك أسطر «uid:password» / «uid password».
    يعيد (قائمة (uid, password), قائمة أسطر مرفوضة الصيغة)."""
    pairs: List[Tuple[str, str]] = []
    rejected: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if len(pairs) >= max_lines:
            rejected.append(f"{line[:30]}… (تجاوز الحد {max_lines})")
            continue
        m = _LINE_RE.match(line)
        if not m:
            rejected.append(f"{line[:40]} (صيغة غير مفهومة)")
            continue
        pairs.append((m.group(1), m.group(2)))
    return pairs, rejected


async def validate_and_store(
    db: Database,
    client: GarenaClient,
    uid: str,
    password: str,
    contributed_by: int,
    region_hint: str = "ME",
) -> DonationResult:
    """يتحقق من الحساب حقيقياً ويخزنه مع مستواه وحالته."""
    password_hash = password_to_hash(password)
    try:
        v = await client.validate_account(uid, password_hash, region_hint)
    except AccountBannedError as exc:
        await db.upsert_validated_account(
            uid, region_hint, password_hash, password, "", 0, "banned",
            contributed_by, note=str(exc)[:200],
        )
        await db.add_contribution(contributed_by, uid, 0, region_hint, False, "حساب محظور")
        return DonationResult(uid, False, reason=f"الحساب محظور من Garena ({exc.reason})")
    except GarenaError as exc:
        await db.add_contribution(contributed_by, uid, 0, region_hint, False, str(exc)[:120])
        reason = str(exc)
        if "auth_error" in reason or "Token Grant" in reason:
            reason = "بيانات الدخول غير صحيحة (UID أو كلمة السر)"
        return DonationResult(uid, False, reason=reason[:120])
    except Exception as exc:  # noqa: BLE001 — شبكة/مهلة
        await db.add_contribution(contributed_by, uid, 0, region_hint, False, str(exc)[:120])
        return DonationResult(uid, False, reason=f"خطأ اتصال: {type(exc).__name__}")

    if not v.eligible:
        await db.upsert_validated_account(
            v.uid, v.region, password_hash, password, v.nickname, v.level,
            "low_level", contributed_by, v.access_token, v.open_id,
            note=f"مستوى {v.level} < {settings.min_donor_level}",
        )
        await db.add_contribution(
            contributed_by, v.uid, v.level, v.region, False,
            f"مستوى منخفض ({v.level})",
        )
        needed = settings.min_donor_level - v.level
        return DonationResult(
            uid, False, level=v.level, region=v.region, nickname=v.nickname,
            reason=(
                f"مستواه <b>{v.level}</b> فقط — المطلوب <b>{settings.min_donor_level}+</b> "
                f"(العب ~{needed} مستويات إضافية ثم أعد المساهمة)"
            ),
        )

    await db.upsert_validated_account(
        v.uid, v.region, password_hash, password, v.nickname, v.level,
        "ok", contributed_by, v.access_token, v.open_id,
    )
    await db.add_contribution(contributed_by, v.uid, v.level, v.region, True)
    return DonationResult(
        uid, True, level=v.level, region=v.region, nickname=v.nickname
    )
