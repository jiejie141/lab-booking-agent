"""时间工具：全链路统一使用同一时区的「本地时间」。

之所以不用 UTC 存储再转换，是因为这是一个面向国内实验室的预约系统：
用户说「明天下午两点」指的就是墙上的钟，任何一次 UTC 往返都可能被
时区或夏令时规则改掉 8 小时。约定死一个时区，比到处做转换更不容易错。
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .config import get_settings

WEEKDAY_HOURS = (
    "weekday_open",
    "weekday_close",
)


def tz() -> dt.tzinfo:
    """取配置里声明的时区。

    Windows 上 zoneinfo 没有系统时区库，缺 tzdata 包会抛 ZoneInfoNotFoundError。
    与其让整个服务起不来，不如退到一个固定偏移 —— 对国内场景这甚至是**等价**的：
    Asia/Shanghai 不使用夏令时，固定 +8 与它逐秒一致。
    真跨时区部署时应当装 tzdata，而不是依赖这个兜底。
    """
    name = get_settings().timezone
    try:
        return ZoneInfo(name)
    except Exception:  # 含 ZoneInfoNotFoundError，以及 tzdata 缺失/损坏
        if name == "Asia/Shanghai":
            return dt.timezone(dt.timedelta(hours=8), name="UTC+8")
        raise


def now_local() -> dt.datetime:
    """当前本地时间（无 tzinfo，便于直接入库）。"""
    return dt.datetime.now(tz()).replace(tzinfo=None)


def today_local() -> dt.date:
    return now_local().date()


def parse_date(text: str) -> dt.date:
    """接受 2026-09-24 / 2026/9/24 两种写法。"""
    cleaned = text.strip().replace("/", "-")
    return dt.date.fromisoformat(cleaned)


def parse_time(text: str) -> dt.time:
    """接受 14:00 / 14:00:00 / 1400。"""
    raw = text.strip()
    if ":" not in raw and len(raw) == 4 and raw.isdigit():
        raw = f"{raw[:2]}:{raw[2:]}"
    parts = raw.split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    return dt.time(hour=hour, minute=minute)


def fmt_time(value: dt.time) -> str:
    return value.strftime("%H:%M")


def fmt_slot(date_: dt.date, start: dt.time, end: dt.time) -> str:
    return f"{date_.isoformat()} {fmt_time(start)}-{fmt_time(end)}"


def weekday_key(date_: dt.date) -> str:
    """周六周日走 weekend 开放时间，其余走工作日。"""
    return "weekend" if date_.weekday() >= 5 else "weekday"


def combine(date_: dt.date, value: dt.time) -> dt.datetime:
    return dt.datetime.combine(date_, value)


def overlaps(a_start: dt.time, a_end: dt.time, b_start: dt.time, b_end: dt.time) -> bool:
    """开区间重叠判定：新的开始早于旧的结束，且新的结束晚于旧的开始。"""
    return a_start < b_end and a_end > b_start


def minutes_between(start: dt.time, end: dt.time) -> int:
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
