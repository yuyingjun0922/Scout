from datetime import datetime, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc


def now_utc() -> str:
    """当前UTC时间，ISO 8601格式"""
    return datetime.now(UTC).isoformat()


def kst_to_utc(kst_str: str) -> str:
    """KST字符串转UTC ISO 8601"""
    dt = datetime.fromisoformat(kst_str).replace(tzinfo=KST)
    return dt.astimezone(UTC).isoformat()


def utc_to_kst(utc_str: str) -> str:
    """UTC字符串转KST显示格式"""
    dt = datetime.fromisoformat(utc_str)
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
