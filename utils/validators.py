"""التحقق من صحة المدخلات (UID والسيرفر)."""

import re

UID_RE = re.compile(r"^\d{6,12}$")


def is_valid_uid(value: str) -> bool:
    """UID فري فاير: أرقام فقط، طوله عادة 6-12 رقماً."""
    return bool(UID_RE.match(value.strip()))


def normalize_uid(value: str) -> str:
    return value.strip()
