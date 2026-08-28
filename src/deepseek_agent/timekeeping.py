"""集中提供中国北京时间及跨时区时间戳排序能力。"""

from datetime import datetime, timedelta, timezone


CHINA_TIMEZONE_NAME = "Asia/Shanghai"
CHINA_TIMEZONE = timezone(timedelta(hours=8), name=CHINA_TIMEZONE_NAME)


def now_china() -> datetime:
    """返回带有 UTC+8 偏移信息的当前北京时间。"""
    return datetime.now(CHINA_TIMEZONE)


def iso_now_china() -> str:
    """返回包含 ``+08:00`` 偏移的当前北京时间字符串。"""
    return now_china().isoformat()


def timestamp_sort_key(value: str) -> tuple[int, float, str]:
    """把新旧 ISO 时间统一转换为可比较键，无效旧值稳定排在最前。"""
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return (0, 0.0, value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TIMEZONE)
    return (1, parsed.timestamp(), value)


__all__ = [
    "CHINA_TIMEZONE",
    "CHINA_TIMEZONE_NAME",
    "iso_now_china",
    "now_china",
    "timestamp_sort_key",
]
